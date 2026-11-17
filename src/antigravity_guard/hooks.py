"""Antigravity PreToolUse and PostToolUse lifecycle harness hook handlers."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from antigravity_guard.git_worktree import resolve_git_context, resolve_trusted_cwd

_COMMIT_CMD_RE = re.compile(r"(?:^|[;&|]\s*)\bgit(?:\s+-[^\s]+)*\s+commit\b")
_PUSH_CMD_RE = re.compile(r"(?:^|[;&|]\s*)\bgit(?:\s+-[^\s]+)*\s+push\b")
_MERGE_CMD_RE = re.compile(r"(?:^|[;&|]\s*)\b(gh\s+pr\s+merge|git(?:\s+-[^\s]+)*\s+merge)\b")
_DASH_F_RE = re.compile(r"(?:^|\s)-F\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))")
_GH_MERGE_PR_RE = re.compile(r"(?:^|[;&|]\s*)\bgh\s+pr\s+merge\s+(\d+)")
_REPO_FLAG_RE = re.compile(r"(?:-R|--repo)\s+([^\s]+)")


def _cli_mod() -> Any:
    return importlib.import_module("antigravity_guard.cli")


def _extract_payload_cwd(payload: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Resolves CWD from Antigravity's top-level payload fields or toolCall args."""
    cwd = payload.get("cwd")
    if not cwd and isinstance(payload.get("workspacePaths"), list) and payload["workspacePaths"]:
        cwd = payload["workspacePaths"][0]
    if not cwd and isinstance(args, dict):
        cwd = args.get("Cwd")
    return str(cwd) if isinstance(cwd, str) else None


def is_direct_push_to_main(cmd_line: str, current_branch: str = "") -> bool:
    """Detects whether a git push command targets protected `main` or `master` directly."""
    if not _PUSH_CMD_RE.search(cmd_line):
        return False
    if re.search(r"(?:^|\s)(?:--tags|refs/tags/[^\s]+)(?:\s|$)", cmd_line):
        return False
    if "--delete" in cmd_line or " -d " in cmd_line:
        return bool(re.search(r"\b(main|master)\b", cmd_line))
    if re.search(r"(?::|\s)(?:origin\s+)?(?:[^\s:]+:)?(main|master)(?:\s|$)", cmd_line):
        return True
    tokens = cmd_line.strip().split()
    if "push" in tokens:
        rem_args = [t for t in tokens[tokens.index("push") + 1 :] if not t.startswith("-")]
        if (not rem_args or rem_args == ["origin"]) and current_branch in {"main", "master"}:
            return True
    return False


def _resolve_commit_msg_file(raw_path: str, base_dir: Path) -> Path | None:
    """Resolves `-F <file>` strictly inside `base_dir`, blocking path traversal escapes."""
    candidate = Path(raw_path.strip())
    resolved_base = base_dir.resolve()
    target = (resolved_base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not target.is_relative_to(resolved_base):
        return None
    return target


def _append_trailer_to_file(msg_file: Path, conv_id: str) -> tuple[bool, str]:
    """Appends Antigravity-Session-ID trailer to an approved in-repo commit message file."""
    try:
        with msg_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\nAntigravity-Session-ID: {conv_id}\n")
        return True, ""
    except OSError as exc:
        return False, f"Failed updating commit message file {msg_file}: {exc}"


def _handle_commit_hook(cmd_line: str, conv_id: str, raw_cwd: str | None) -> dict[str, Any] | None:
    """Handles trailer injection and `-F` path traversal validation for `git commit`."""
    if not conv_id or "Antigravity-Session-ID" in cmd_line:
        return None
    f_match = _DASH_F_RE.search(cmd_line)
    if f_match:
        raw_path = next(g for g in f_match.groups() if g is not None)
        trusted = resolve_trusted_cwd(raw_cwd) if raw_cwd else None
        base_dir = Path(trusted) if trusted else Path.cwd()
        msg_file = _resolve_commit_msg_file(raw_path, base_dir)
        if msg_file is None:
            return {
                "decision": "deny",
                "reason": (
                    f"[ANTIGRAVITY SECURITY GUARD BLOCKED]\n"
                    f"Blocked `-F {raw_path}` commit message path traversal outside repository root `{base_dir}`."
                ),
            }
        ok, err = _append_trailer_to_file(msg_file, conv_id)
        if not ok:
            return {"decision": "deny", "reason": f"[ANTIGRAVITY COMMIT GUARD BLOCKED]\n{err}"}
        return {"decision": "allow", "overwrite": {"CommandLine": cmd_line}}

    mutated = f'{cmd_line} -m "Antigravity-Session-ID: {conv_id}"'
    return {"decision": "allow", "overwrite": {"CommandLine": mutated}}


def _handle_push_hook(cmd_line: str, raw_cwd: str | None) -> dict[str, Any] | None:
    """Blocks direct `git push` commands targeting protected `main` or `master` branches."""
    _, _, current_branch = resolve_git_context(raw_cwd)
    if not is_direct_push_to_main(cmd_line, current_branch):
        return None
    return {
        "decision": "deny",
        "reason": (
            "[ANTIGRAVITY DIRECT PUSH GUARD BLOCKED]\n"
            "Direct push to protected branch (main/master) is strictly prohibited.\n"
            "All code changes must be pushed to a feature branch, submitted as a pull request, "
            "and validated through the pre-merge telemetry receipts gate.\n\n"
            "Action Required: Create and push a feature branch "
            "(e.g., 'git checkout -b feat/your-feature && git push -u origin feat/your-feature'), "
            "then submit a pull request with 'gh pr create'."
        ),
    }


def _discover_repo_slug_for_merge(cmd_line: str, raw_cwd: str | None, repo_root: str | None) -> str | None:
    """Resolves `owner/repo` from `--repo`/`-R` flag, payload CWD, or resolved repo_root."""
    flag_match = _REPO_FLAG_RE.search(cmd_line)
    if flag_match:
        return flag_match.group(1).strip("\"' ,")
    cli = _cli_mod()
    for candidate in (raw_cwd, repo_root):
        trusted = resolve_trusted_cwd(candidate)
        if trusted:
            owner, name = cli.get_repo_owner_and_name(cwd=trusted, timeout=5)
            if owner and name:
                return f"{owner}/{name}"
    return None


def _handle_merge_hook(cmd_line: str, raw_cwd: str | None) -> dict[str, Any] | None:
    """Enforces worktree safety and pre-merge PR telemetry verification on merge commands."""
    repo_root, is_worktree, _ = resolve_git_context(raw_cwd)
    if is_worktree:
        return {
            "decision": "deny",
            "reason": (
                f"[ANTIGRAVITY WORKTREE GUARD BLOCKED]\n"
                f"Active working directory is inside a git worktree: {raw_cwd}\n"
                f"Executing merges from inside a mounted worktree causes git deletion locks and checkout conflicts.\n\n"
                f"Action Required: Switch working directory to primary repository root ({repo_root}) "
                f"and detach/remove the worktree before executing merge."
            ),
        }

    pr_match = _GH_MERGE_PR_RE.search(cmd_line)
    if not pr_match:
        return None
    pr_num = int(pr_match.group(1))
    repo_slug = _discover_repo_slug_for_merge(cmd_line, raw_cwd, repo_root)
    passed, msg, _, _ = _cli_mod().verify_pr_telemetry(pr_num, repo=repo_slug)
    if passed:
        return None
    return {
        "decision": "deny",
        "reason": (
            f"[ANTIGRAVITY MERGE GUARD BLOCKED]\n{msg}\n\n"
            f"Action Required: Execute 'antigravity-guard post --pr {pr_num}' "
            f"(or 'antigravity-telemetry post --pr {pr_num}') before merging."
        ),
    }


def _evaluate_pre_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Routes PreToolUse stdin JSON payload through commit, push, and merge guards."""
    tool_call = payload.get("toolCall") or {}
    if tool_call.get("name") != "run_command":
        return {"decision": "allow"}
    args = tool_call.get("args") or {}
    cmd_line = str(args.get("CommandLine", "")) if isinstance(args, dict) else ""
    raw_cwd = _extract_payload_cwd(payload, args if isinstance(args, dict) else {})
    conv_id = str(payload.get("conversationId", ""))

    if _COMMIT_CMD_RE.search(cmd_line):
        decision = _handle_commit_hook(cmd_line, conv_id, raw_cwd)
        if decision is not None:
            return decision
    if _PUSH_CMD_RE.search(cmd_line):
        decision = _handle_push_hook(cmd_line, raw_cwd)
        if decision is not None:
            return decision
    if _MERGE_CMD_RE.search(cmd_line):
        decision = _handle_merge_hook(cmd_line, raw_cwd)
        if decision is not None:
            return decision
    return {"decision": "allow"}


def handle_pre_tool_hook() -> None:
    """Antigravity PreToolUse stdin/stdout JSON hook entrypoint."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.stdout.write(json.dumps({"decision": "allow"}))
        return
    result = _evaluate_pre_tool_payload(payload if isinstance(payload, dict) else {})
    sys.stdout.write(json.dumps(result))


def handle_post_tool_hook() -> None:
    """Antigravity PostToolUse hook automatically pruning merged worktrees and branches."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.stdout.write(json.dumps({}))
        return

    tool_call = payload.get("toolCall") or {}
    args = tool_call.get("args") or {}
    cmd_line = str(args.get("CommandLine", "")) if isinstance(args, dict) else ""
    if tool_call.get("name") == "run_command" and _MERGE_CMD_RE.search(cmd_line):
        raw_cwd = _extract_payload_cwd(payload, args if isinstance(args, dict) else {})
        try:
            prune_res = _cli_mod().prune_merged_branches_and_worktrees(raw_cwd, quiet=True)
            branches = prune_res.get("pruned_branches", [])
            worktrees = prune_res.get("pruned_worktrees", [])
            if branches or worktrees:
                sys.stderr.write(
                    f"[Antigravity Prune] Automatically pruned {len(branches)} merged branch(es) and {len(worktrees)} worktree(s).\n"
                )
        except Exception as exc:
            sys.stderr.write(f"[Antigravity Prune] Note: {exc}\n")

    sys.stdout.write(json.dumps({}))
