"""Unified CLI entrypoint and public symbol namespace for `antigravity-guard`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from antigravity_guard import __version__
from antigravity_guard.git_worktree import prune_merged_branches_and_worktrees, resolve_trusted_cwd
from antigravity_guard.github import (
    get_all_pr_commits_and_head,
    get_pr_comments,
    get_pr_commits,
    get_repo_owner_and_name,
    get_unresolved_review_threads,
    post_commit_status,
)
from antigravity_guard.hooks import (
    handle_post_tool_hook,
    handle_pre_tool_hook,
    handle_stop_hook_cli,
    is_direct_push_to_main,
)
from antigravity_guard.issue_audit import handle_stop_hook, verify_issue_audits
from antigravity_guard.issue_lint import lint_issue
from antigravity_guard.plugin_sync import sync_plugin_local, validate_plugin_bundle
from antigravity_guard.skill_audit import audit_skills, format_audit_report, init_required_skills
from antigravity_guard.skill_receipts import verify_skills
from antigravity_guard.telemetry import (
    DEFAULT_TIMEOUT_SEC,
    RECEIPT_END,
    RECEIPT_START,
    calc_usd_cost,
    extract_session_ids_from_text,
    generate_and_post_telemetry,
    get_session_telemetry,
    parse_varints,
    verify_pr_telemetry,
)

__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "RECEIPT_END",
    "RECEIPT_START",
    "audit_skills",
    "calc_usd_cost",
    "extract_session_ids_from_text",
    "generate_and_post_telemetry",
    "get_all_pr_commits_and_head",
    "get_pr_comments",
    "get_pr_commits",
    "get_repo_owner_and_name",
    "get_session_telemetry",
    "get_unresolved_review_threads",
    "handle_post_tool_hook",
    "handle_pre_tool_hook",
    "handle_stop_hook",
    "handle_stop_hook_cli",
    "init_required_skills",
    "is_direct_push_to_main",
    "lint_issue",
    "main",
    "parse_varints",
    "post_commit_status",
    "prune_merged_branches_and_worktrees",
    "resolve_trusted_cwd",
    "sync_plugin_local",
    "validate_plugin_bundle",
    "verify_issue_audits",
    "verify_pr_telemetry",
    "verify_skills",
]


def _add_pr_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_post = subparsers.add_parser("post", help="Calculate and post PR token telemetry report with receipt")
    p_post.add_argument("--pr", type=int, required=True, help="GitHub Pull Request number")
    p_post.add_argument("--repo", type=str, default=None, help="Optional owner/repo")
    p_post.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Subprocess timeout in seconds")

    p_ver = subparsers.add_parser("verify", help="Verify PR telemetry receipt coverage and optional CI audit gates")
    p_ver.add_argument("--pr", type=int, required=True, help="GitHub Pull Request number")
    p_ver.add_argument("--repo", type=str, default=None, help="Optional owner/repo")
    p_ver.add_argument("--post-status", action="store_true", help="Post commit status check to PR head commit")
    p_ver.add_argument(
        "--ignore-unresolved-comments",
        action="store_true",
        help="Skip checking for unresolved PR review comments",
    )
    p_ver.add_argument(
        "--verify-issue-audits",
        action="store_true",
        help="Validate .infigraph/reviews/issue_<N>_audit.json completeness",
    )
    p_ver.add_argument(
        "--verify-skill-receipts",
        action="store_true",
        help="Validate .github/required-skills.json skill receipts",
    )
    p_ver.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Subprocess timeout in seconds")


def _add_gate_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_skills = subparsers.add_parser("verify-skills", help="Verify skill receipts against .github/required-skills.json")
    p_skills.add_argument("--cwd", type=Path, default=None, help="Target repository directory")
    p_skills.add_argument("--staged-only", action="store_true", help="Inspect only git-staged files")
    p_skills.add_argument("--session-id", type=str, default=None, help="Filter fallback receipts by session ID")

    p_issue = subparsers.add_parser("verify-issue", help="Verify adversarial issue audit ledgers")
    p_issue.add_argument("--cwd", type=Path, default=None, help="Target repository directory")
    p_issue.add_argument("--issue", type=int, default=None, help="Specific issue number to verify")
    p_issue.add_argument("--mode", choices=("commit", "strict"), default="commit", help="Enforcement mode")
    p_issue.add_argument("--commit-msg", type=str, default="", help="Commit message or Closes #N text")
    p_issue.add_argument("--transcript-path", type=Path, default=None, help="Parent Antigravity transcriptPath JSONL")
    p_issue.add_argument(
        "--no-verify-transcripts", action="store_true", help="Skip local ~/.gemini transcript checks (CI)"
    )

    p_lint = subparsers.add_parser("lint-issue", help="Validate issue readiness against Pre-ready-for-agent gate")
    p_lint.add_argument("--cwd", type=Path, default=None, help="Target repository directory")
    p_lint.add_argument("--issue", type=int, default=None, help="GitHub issue number")
    p_lint.add_argument("--file", type=Path, default=None, help="Local issue markdown file")
    p_lint.add_argument("--repo", type=str, default=None, help="Optional owner/repo")

    p_audit = subparsers.add_parser(
        "audit-skills", help="Audit skill adoption and promotion readiness over git history"
    )
    p_audit.add_argument(
        "--cwd", "--repo-root", dest="cwd", type=Path, default=None, help="Target repository directory"
    )
    p_audit.add_argument("--limit", type=int, default=30, help="Number of recent commits to inspect")
    p_audit.add_argument("--check-promotion", action="store_true", help="Fail if any active gate degraded")
    p_audit.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")

    p_init = subparsers.add_parser("init-skills", help="Create starter .github/required-skills.json configuration")
    p_init.add_argument("--cwd", type=Path, default=None, help="Target repository directory")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .github/required-skills.json")


def _add_maintenance_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_prune = subparsers.add_parser("prune", help="Prune merged branches and attached git worktrees")
    p_prune.add_argument("--cwd", type=str, default=None, help="Target repository directory")
    p_prune.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    subparsers.add_parser("hook-pre-tool", help="Antigravity PreToolUse hook handler")
    subparsers.add_parser("hook-post-tool", help="Antigravity PostToolUse hook handler")
    subparsers.add_parser("hook-stop", help="Antigravity Stop hook handler")

    p_val = subparsers.add_parser("validate-plugin", help="Validate plugin manifest and assets")
    p_val.add_argument("--repo-root", type=Path, default=None, help="Repository root containing plugin/")
    p_val.add_argument("--strict", action="store_true", help="Enforce strict Marketplace/agy checks")

    p_sync = subparsers.add_parser("sync-plugin", help="Sync plugin/ locally and remove legacy plugin copy")
    p_sync.add_argument("--repo-root", type=Path, default=None, help="Repository root containing plugin/")
    p_sync.add_argument("--clean-legacy", action="store_true", default=True, help="Remove legacy plugin dir")

    subparsers.add_parser("version", help="Show version information")


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser."""
    parser = argparse.ArgumentParser(description="Antigravity Guard CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_pr_commands(subparsers)
    _add_gate_commands(subparsers)
    _add_maintenance_commands(subparsers)
    return parser


def _post_pr_status(
    args: argparse.Namespace, passed: bool, receipt_data: dict[str, object] | None, head_sha: str | None
) -> None:
    if not (args.post_status and head_sha):
        return
    state = "success" if passed else "failure"
    desc = (
        f"Verified: {receipt_data.get('total_billed_tokens', 0):,} billed ({receipt_data.get('cache_hit_pct', 0)}% cache)"
        if passed and receipt_data
        else (
            "Verified Antigravity session telemetry & guards"
            if passed
            else "Unresolved review comments, audit failure, or missing telemetry"
        )
    )
    repo_env, run_env = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
    target_url = f"https://github.com/{repo_env}/actions/runs/{run_env}" if repo_env and run_env else None
    ok, status_msg = post_commit_status(
        head_sha, state, desc, repo=args.repo, target_url=target_url, timeout=args.timeout
    )
    if not ok:
        sys.stderr.write(f"Warning: {status_msg}\n")


def _run_verify(args: argparse.Namespace) -> int:
    passed, msg, receipt_data, head_sha = verify_pr_telemetry(
        args.pr,
        args.repo,
        timeout=args.timeout,
        check_comments=not args.ignore_unresolved_comments,
    )
    extra_msgs: list[str] = []
    if args.verify_skill_receipts:
        ok_sk, sk_msg = verify_skills(Path.cwd(), staged_only=False)
        extra_msgs.append(sk_msg)
        passed = passed and ok_sk
    if args.verify_issue_audits:
        ok_iss, iss_msg = verify_issue_audits(Path.cwd(), mode="strict", verify_transcripts=False)
        extra_msgs.append(iss_msg)
        passed = passed and ok_iss

    _post_pr_status(args, passed, receipt_data, head_sha)
    full_msg = "\n".join([msg, *extra_msgs])
    stream = sys.stdout if passed else sys.stderr
    stream.write(full_msg + "\n")
    return 0 if passed else 1


def _run_verify_skills(args: argparse.Namespace) -> int:
    ok, msg = verify_skills(args.cwd, staged_only=args.staged_only, session_id=args.session_id)
    (sys.stdout if ok else sys.stderr).write(msg + "\n")
    return 0 if ok else 1


def _run_verify_issue(args: argparse.Namespace) -> int:
    ok, msg = verify_issue_audits(
        args.cwd,
        issue_num=args.issue,
        mode=args.mode,
        commit_msg=args.commit_msg,
        transcript_path=args.transcript_path,
        verify_transcripts=not args.no_verify_transcripts,
    )
    (sys.stdout if ok else sys.stderr).write(msg + "\n")
    return 0 if ok else 1


def _run_lint_issue(args: argparse.Namespace) -> int:
    ok, msg = lint_issue(args.cwd, issue_num=args.issue, file_path=args.file, repo=args.repo)
    (sys.stdout if ok else sys.stderr).write(msg + "\n")
    return 0 if ok else 1


def _run_audit_skills(args: argparse.Namespace) -> int:
    import json

    ok, report = audit_skills(args.cwd, limit=args.limit, check_promotion=args.check_promotion)
    if args.as_json:
        print(json.dumps(report, indent=2))
    elif not report.get("opted_in", True):
        print("Repository is not opted in (no .github/required-skills.json).")
    elif report.get("errors"):
        for err in report["errors"]:
            sys.stderr.write(f"ERROR: {err}\n")
    else:
        print(format_audit_report(report))
    return 0 if ok else 1


def _run_init_skills(args: argparse.Namespace) -> int:
    ok, msg = init_required_skills(args.cwd, force=args.force)
    (sys.stdout if ok else sys.stderr).write(msg + "\n")
    return 0 if ok else 1


def _run_validate_plugin(args: argparse.Namespace) -> int:
    ok, errors = validate_plugin_bundle(repo_root=args.repo_root, strict=args.strict)
    if ok:
        print(f"antigravity-guard plugin v{__version__} validated cleanly.")
        return 0
    for err in errors:
        sys.stderr.write(f"ERROR: {err}\n")
    return 1


def _run_sync_plugin(args: argparse.Namespace) -> int:
    res = sync_plugin_local(repo_root=args.repo_root, clean_legacy=args.clean_legacy)
    print(f"Synced antigravity-guard v{res['version']} -> {res['installed_dir']}")
    return 0


def main() -> None:
    """Primary CLI entrypoint."""
    args = build_parser().parse_args()
    handlers: dict[str, Callable[[], int]] = {
        "version": lambda: (print(f"antigravity-guard v{__version__}"), 0)[1],
        "prune": lambda: (prune_merged_branches_and_worktrees(args.cwd, quiet=args.quiet), 0)[1],
        "post": lambda: 0 if generate_and_post_telemetry(args.pr, args.repo, timeout=args.timeout) else 1,
        "verify": lambda: _run_verify(args),
        "verify-skills": lambda: _run_verify_skills(args),
        "verify-issue": lambda: _run_verify_issue(args),
        "lint-issue": lambda: _run_lint_issue(args),
        "audit-skills": lambda: _run_audit_skills(args),
        "init-skills": lambda: _run_init_skills(args),
        "hook-pre-tool": lambda: (handle_pre_tool_hook(), 0)[1],
        "hook-post-tool": lambda: (handle_post_tool_hook(), 0)[1],
        "hook-stop": lambda: (handle_stop_hook_cli(), 0)[1],
        "validate-plugin": lambda: _run_validate_plugin(args),
        "sync-plugin": lambda: _run_sync_plugin(args),
    }
    sys.exit(handlers[args.command]())


if __name__ == "__main__":
    main()
