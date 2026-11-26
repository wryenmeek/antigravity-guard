"""Antigravity PreToolUse, PostToolUse, and Stop lifecycle harness hook handlers."""

from __future__ import annotations

import contextlib
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

_NO_VERIFY_RE = re.compile(r"(?:^|\s)(?:--no-verify|-n)(?:\s|$)")
_DASH_F_RE = re.compile(r"(?:^|\s)-F\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))")
_REPO_FLAG_RE = re.compile(r"(?:-R|--repo)\s+([^\s]+)")
_SHELL_OPS = frozenset({";", "&", "&&", "|", "||"})
_GIT_ARG_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"})
_MERGE_ABORT_FLAGS = frozenset({"--abort", "--continue", "--quit"})
_ADD_WILDCARD_ARGS = frozenset({"-A", "--all", "-u", "--update", ".", ".."})


def _cli_mod() -> Any:
    return importlib.import_module("antigravity_guard.cli")


def _split_shell_segments(cmd_line: str) -> list[list[str]]:
    """Splits a shell command string into unquoted command-segment token lists."""
    try:
        lexer = shlex.shlex(cmd_line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [seg.split() for seg in re.split(r"\s*(?:&&|\|\||[;&|])\s*", cmd_line) if seg.strip()]

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_OPS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _git_subcommand(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Returns `(subcommand, remaining_args)` if `tokens` is a `git` invocation."""
    if not tokens or Path(tokens[0]).name != "git":
        return None
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        idx += 2 if tokens[idx] in _GIT_ARG_FLAGS else 1
    if idx >= len(tokens):
        return None
    return tokens[idx], tokens[idx + 1 :]


def _is_push_segment_to_main(tokens: list[str], current_branch: str) -> bool:
    """Checks a single command segment to see if it pushes directly to main/master."""
    sub = _git_subcommand(tokens)
    if not sub or sub[0] != "push":
        return False
    args = sub[1]
    if any(t == "--tags" or t.startswith("refs/tags/") for t in args):
        return False
    if "--delete" in args or "-d" in args:
        return any(t in {"main", "master"} or t.endswith(":main") or t.endswith(":master") for t in args)
    pos_args = [t for t in args if not t.startswith("-")]
    if any(t in {"main", "master"} or t.endswith(":main") or t.endswith(":master") for t in pos_args):
        return True
    return (not pos_args or pos_args == ["origin"]) and current_branch in {"main", "master"}


def is_direct_push_to_main(cmd_line: str, current_branch: str = "") -> bool:
    """Detects whether any `git push` segment targets protected `main` or `master` directly."""
    return any(_is_push_segment_to_main(seg, current_branch) for seg in _split_shell_segments(cmd_line))


def _extract_payload_cwd(payload: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Resolves CWD from Antigravity's top-level payload fields or toolCall args."""
    cwd = payload.get("cwd")
    if not cwd and isinstance(payload.get("workspacePaths"), list) and payload["workspacePaths"]:
        cwd = payload["workspacePaths"][0]
    if not cwd and isinstance(args, dict):
        cwd = args.get("Cwd")
    return str(cwd) if isinstance(cwd, str) else None


def _find_commit_segment(segments: list[list[str]]) -> list[str] | None:
    """Returns args of the first `git commit` command segment, if present."""
    for seg in segments:
        sub = _git_subcommand(seg)
        if sub and sub[0] == "commit":
            return sub[1]
    return None


def _has_push_segment(segments: list[list[str]]) -> bool:
    """Returns True if any command segment is a `git push` invocation."""
    return any((_git_subcommand(seg) or ("", []))[0] == "push" for seg in segments)


def _find_merge_segment(segments: list[list[str]]) -> list[str] | None:
    """Returns the first `gh pr merge` or non-abort `git merge` segment."""
    for seg in segments:
        if len(seg) >= 3 and Path(seg[0]).name == "gh" and seg[1:3] == ["pr", "merge"]:
            return seg
        sub = _git_subcommand(seg)
        if sub and sub[0] == "merge" and not any(f in _MERGE_ABORT_FLAGS for f in sub[1]):
            return seg
    return None


def _extract_dash_f_path(cmd_line: str, commit_args: list[str] | None = None) -> str | None:
    """Extracts `-F <path>` or `--file <path>` from `git commit` args or raw command line."""
    args = commit_args or []
    for idx, tok in enumerate(args):
        if tok in {"-F", "--file"} and idx + 1 < len(args):
            return args[idx + 1]
        if tok.startswith("--file="):
            return tok.split("=", 1)[1]
    match = _DASH_F_RE.search(cmd_line)
    return next(g for g in match.groups() if g is not None) if match else None


def _extract_chained_git_add_paths(segments: list[list[str]], repo_root: Path) -> list[str]:
    """Extracts explicit `git add <paths>` targets preceding `git commit` in a chained command."""
    collected: list[str] = []
    for seg in segments:
        sub = _git_subcommand(seg)
        if not sub:
            continue
        if sub[0] == "commit":
            break
        if sub[0] != "add":
            continue
        if any(arg in _ADD_WILDCARD_ARGS for arg in sub[1]):
            return []
        for arg in sub[1]:
            if arg.startswith("-"):
                continue
            p = Path(arg)
            rel = p if not p.is_absolute() else p.resolve().relative_to(repo_root.resolve())
            collected.append(rel.as_posix().lstrip("./"))
    return collected


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


def _resolve_commit_msg_text(
    cmd_line: str,
    commit_args: list[str] | None,
    trusted_cwd: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Reads `-F <msg-file>` commit message text safely inside `trusted_cwd` if `-F` is present."""
    raw_path = _extract_dash_f_path(cmd_line, commit_args)
    if not raw_path:
        return cmd_line, None
    base_dir = Path(trusted_cwd) if trusted_cwd else Path.cwd()
    msg_file = _resolve_commit_msg_file(raw_path, base_dir)
    if msg_file is None:
        return None, {
            "decision": "deny",
            "reason": (
                f"[ANTIGRAVITY SECURITY GUARD BLOCKED]\n"
                f"Blocked `-F {raw_path}` commit message path traversal outside repository root `{base_dir}`."
            ),
        }
    if msg_file.is_file():
        with contextlib.suppress(OSError):
            return msg_file.read_text(encoding="utf-8", errors="ignore"), None
    return cmd_line, None


def _check_commit_gates(
    cmd_line: str,
    conv_id: str,
    trusted_cwd: str | None,
    payload: dict[str, Any],
    segments: list[list[str]] | None = None,
    commit_args: list[str] | None = None,
) -> dict[str, Any] | None:
    """Blocks `--no-verify` and validates opt-in skill receipts & issue audit gates on commit."""
    if _NO_VERIFY_RE.search(cmd_line):
        return {
            "decision": "deny",
            "reason": "[ANTIGRAVITY COMMIT GUARD BLOCKED]\nBypassing git hooks via `--no-verify` / `-n` is prohibited.",
        }
    commit_msg_text, path_denial = _resolve_commit_msg_text(cmd_line, commit_args, trusted_cwd)
    if path_denial is not None:
        return path_denial
    if not trusted_cwd:
        return None

    ok_skills, skill_msg = verify_skills(trusted_cwd, staged_only=True, session_id=conv_id or None)
    if not ok_skills:
        return {"decision": "deny", "reason": f"[ANTIGRAVITY SKILL GATE BLOCKED]\n{skill_msg}"}

    repo_root = _resolve_repo_root(trusted_cwd)
    extra_staged = _extract_chained_git_add_paths(segments or [], repo_root)
    transcript = payload.get("transcriptPath")
    ok_issue, issue_msg = verify_issue_audits(
        trusted_cwd,
        mode="commit",
        commit_msg=commit_msg_text or cmd_line,
        transcript_path=str(transcript) if transcript else None,
        verify_transcripts=True,
        extra_staged_paths=extra_staged,
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
    segments: list[list[str]] | None = None,
    commit_args: list[str] | None = None,
) -> dict[str, Any] | None:
    """Enforces commit pre-gates, `-F` path-traversal safety, and trailer/footer injection."""
    trusted_cwd = resolve_trusted_cwd(raw_cwd) if raw_cwd else None
    gate_denial = _check_commit_gates(cmd_line, conv_id, trusted_cwd, payload, segments, commit_args)
    if gate_denial is not None:
        return gate_denial

    trailers = _collect_commit_trailers(cmd_line, conv_id, trusted_cwd)
    if not trailers:
        return None

    raw_path = _extract_dash_f_path(cmd_line, commit_args)
    if raw_path:
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


def _extract_gh_merge_pr_num(merge_seg: list[str]) -> int | None:
    """Extracts PR number from a `gh pr merge <N>` command segment."""
    if len(merge_seg) >= 4 and Path(merge_seg[0]).name == "gh" and merge_seg[1:3] == ["pr", "merge"] and merge_seg[3].isdigit():
        return int(merge_seg[3])
    return None


def _handle_merge_hook(
    cmd_line: str,
    raw_cwd: str | None,
    payload: dict[str, Any],
    merge_seg: list[str] | None = None,
) -> dict[str, Any] | None:
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

    pr_num = _extract_gh_merge_pr_num(merge_seg or [])
    if pr_num is None:
        return None
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

    segments = _split_shell_segments(cmd_line)
    commit_args = _find_commit_segment(segments)
    if commit_args is not None:
        decision = _handle_commit_hook(cmd_line, conv_id, raw_cwd, payload, segments, commit_args)
        if decision is not None:
            return decision
    if _has_push_segment(segments):
        decision = _handle_push_hook(cmd_line, raw_cwd)
        if decision is not None:
            return decision
    merge_seg = _find_merge_segment(segments)
    if merge_seg is not None:
        decision = _handle_merge_hook(cmd_line, raw_cwd, payload, merge_seg)
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

    if tool_call.get("name") == "run_command" and _find_merge_segment(_split_shell_segments(cmd_line)) is not None:
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
