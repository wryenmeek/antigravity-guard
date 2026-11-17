"""Antigravity PreToolUse, PostToolUse, and Stop lifecycle harness hook handlers."""

from __future__ import annotations

import importlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from antigravity_guard.git_worktree import resolve_git_context, resolve_trusted_cwd
from antigravity_guard.issue_audit import handle_stop_hook, verify_issue_audits
from antigravity_guard.skill_receipts import (
    _resolve_repo_root,
    capture_post_tool_skill_receipt,
    collect_pending_footers,
    verify_skills,
)

_COMMIT_CMD_RE = re.compile(r"(?:^|[;&|]\s*)\bgit(?:\s+-[^\s]+)*\s+commit\b")
_NO_VERIFY_RE = re.compile(r"(?:^|\s)(?:--no-verify|-n)(?:\s|$)")
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


def _append_trailers_to_file(msg_file: Path, trailers: list[str]) -> tuple[bool, str]:
    """Appends session and validated-by trailers to an in-repo commit message file."""
    try:
        existing = msg_file.read_text(encoding="utf-8") if msg_file.exists() else ""
        msg_file.write_text(f"{existing}\n\n" + "\n".join(trailers) + "\n", encoding="utf-8")
        return True, ""
    except OSError as exc:
        return False, f"Failed updating commit message file {msg_file}: {exc}"


def _check_commit_gates(
    cmd_line: str,
    conv_id: str,
    trusted_cwd: str | None,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Blocks `--no-verify` and validates opt-in skill receipts & issue audit gates on commit."""
    if _NO_VERIFY_RE.search(cmd_line):
        return {
            "decision": "deny",
            "reason": "[ANTIGRAVITY COMMIT GUARD BLOCKED]\nBypassing git hooks via `--no-verify` / `-n` is prohibited.",
        }
    if not trusted_cwd:
        return None

    ok_skills, skill_msg = verify_skills(trusted_cwd, staged_only=True, session_id=conv_id or None)
    if not ok_skills:
        return {"decision": "deny", "reason": f"[ANTIGRAVITY SKILL GATE BLOCKED]\n{skill_msg}"}

    transcript = payload.get("transcriptPath")
    ok_issue, issue_msg = verify_issue_audits(
        trusted_cwd,
        mode="commit",
        commit_msg=cmd_line,
        transcript_path=str(transcript) if transcript else None,
        verify_transcripts=True,
    )
    if not ok_issue:
        return {"decision": "deny", "reason": f"[ANTIGRAVITY ISSUE GATE BLOCKED]\n{issue_msg}"}
    return None


def _collect_commit_trailers(cmd_line: str, conv_id: str, trusted_cwd: str | None) -> list[str]:
    """Assembles session ID and fresh validated-by footers not already present in cmd_line."""
    trailers: list[str] = []
    if conv_id and "Antigravity-Session-ID" not in cmd_line:
        trailers.append(f"Antigravity-Session-ID: {conv_id}")
    if trusted_cwd:
        repo_root = _resolve_repo_root(trusted_cwd)
        for footer in collect_pending_footers(repo_root, session_id=conv_id or None):
            if footer not in cmd_line:
                trailers.append(footer)
    return trailers


def _rewrite_dash_f_commit(
    cmd_line: str,
    raw_path: str,
    trusted_cwd: str | None,
    trailers: list[str],
) -> dict[str, Any]:
    """Handles `-F <msg-file>` commit rewriting inside `repo_root` with traversal checks."""
    base_dir = Path(trusted_cwd) if trusted_cwd else Path.cwd()
    msg_file = _resolve_commit_msg_file(raw_path, base_dir)
    if msg_file is None:
        return {
            "decision": "deny",
            "reason": (
                f"[ANTIGRAVITY SECURITY GUARD BLOCKED]\n"
                f"Blocked `-F {raw_path}` commit message path traversal outside repository root `{base_dir}`."
            ),
        }
    ok, err = _append_trailers_to_file(msg_file, trailers)
    if not ok:
        return {"decision": "deny", "reason": f"[ANTIGRAVITY COMMIT GUARD BLOCKED]\n{err}"}
    return {"decision": "allow", "overwrite": {"CommandLine": cmd_line}}


def _handle_commit_hook(
    cmd_line: str,
    conv_id: str,
    raw_cwd: str | None,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Enforces commit pre-gates, `-F` path-traversal safety, and trailer/footer injection."""
    trusted_cwd = resolve_trusted_cwd(raw_cwd) if raw_cwd else None
    gate_denial = _check_commit_gates(cmd_line, conv_id, trusted_cwd, payload)
    if gate_denial is not None:
        return gate_denial

    trailers = _collect_commit_trailers(cmd_line, conv_id, trusted_cwd)
    if not trailers:
        return None

    f_match = _DASH_F_RE.search(cmd_line)
    if f_match:
        raw_path = next(g for g in f_match.groups() if g is not None)
        return _rewrite_dash_f_commit(cmd_line, raw_path, trusted_cwd, trailers)

    mutated = cmd_line
    for trailer in trailers:
        quoted = f'"{trailer}"' if trailer.startswith("Antigravity-Session-ID:") else shlex.quote(trailer)
        mutated += f" -m {quoted}"
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


def _check_merge_issue_audits(repo_root: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Blocks merge if local repository has `.infigraph/reviews/issue_*_audit.json` ledgers still open."""
    if not repo_root:
        return None
    reviews_dir = Path(repo_root) / ".infigraph" / "reviews"
    if not reviews_dir.is_dir() or not list(reviews_dir.glob("issue_*_audit.json")):
        return None
    transcript = payload.get("transcriptPath")
    passed, msg = verify_issue_audits(
        repo_root,
        mode="strict",
        transcript_path=str(transcript) if transcript else None,
        verify_transcripts=True,
    )
    if passed:
        return None
    return {"decision": "deny", "reason": f"[ANTIGRAVITY ISSUE MERGE GUARD BLOCKED]\n{msg}"}


def _handle_merge_hook(cmd_line: str, raw_cwd: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Enforces worktree safety, issue audit completion, and PR telemetry before merging."""
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

    audit_denial = _check_merge_issue_audits(repo_root, payload)
    if audit_denial is not None:
        return audit_denial

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
    raw_args = tool_call.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    cmd_line = str(args.get("CommandLine", ""))
    raw_cwd = _extract_payload_cwd(payload, args)
    conv_id = str(payload.get("conversationId", ""))

    checks = (
        (_COMMIT_CMD_RE, lambda: _handle_commit_hook(cmd_line, conv_id, raw_cwd, payload)),
        (_PUSH_CMD_RE, lambda: _handle_push_hook(cmd_line, raw_cwd)),
        (_MERGE_CMD_RE, lambda: _handle_merge_hook(cmd_line, raw_cwd, payload)),
    )
    for pattern, handler in checks:
        if pattern.search(cmd_line):
            decision = handler()
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
    """Antigravity PostToolUse hook capturing skill receipts and auto-pruning merged worktrees."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.stdout.write(json.dumps({}))
        return
    if not isinstance(payload, dict):
        sys.stdout.write(json.dumps({}))
        return

    tool_call = payload.get("toolCall") or {}
    raw_args = tool_call.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    raw_cwd = _extract_payload_cwd(payload, args)
    cmd_line = str(args.get("CommandLine", ""))

    if tool_call.get("name") == "run_command" and _MERGE_CMD_RE.search(cmd_line):
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

    trusted = resolve_trusted_cwd(raw_cwd) if raw_cwd else None
    if trusted:
        capture_post_tool_skill_receipt(payload, _resolve_repo_root(trusted))

    sys.stdout.write(json.dumps({}))


def handle_stop_hook_cli() -> None:
    """Antigravity Stop stdin/stdout JSON hook entrypoint."""
    sys.stdout.write(json.dumps(handle_stop_hook()))
