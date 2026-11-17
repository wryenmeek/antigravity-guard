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
from antigravity_guard.hooks import handle_post_tool_hook, handle_pre_tool_hook, is_direct_push_to_main
from antigravity_guard.plugin_sync import sync_plugin_local, validate_plugin_bundle
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
    "is_direct_push_to_main",
    "main",
    "parse_varints",
    "post_commit_status",
    "prune_merged_branches_and_worktrees",
    "resolve_trusted_cwd",
    "sync_plugin_local",
    "validate_plugin_bundle",
    "verify_pr_telemetry",
]


def _add_pr_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_post = subparsers.add_parser("post", help="Calculate and post PR token telemetry report with receipt")
    p_post.add_argument("--pr", type=int, required=True, help="GitHub Pull Request number")
    p_post.add_argument("--repo", type=str, default=None, help="Optional owner/repo")
    p_post.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Subprocess timeout in seconds")

    p_ver = subparsers.add_parser("verify", help="Verify PR telemetry receipt coverage against PR commits")
    p_ver.add_argument("--pr", type=int, required=True, help="GitHub Pull Request number")
    p_ver.add_argument("--repo", type=str, default=None, help="Optional owner/repo")
    p_ver.add_argument("--post-status", action="store_true", help="Post commit status check to PR head commit")
    p_ver.add_argument(
        "--ignore-unresolved-comments",
        action="store_true",
        help="Skip checking for unresolved PR review comments",
    )
    p_ver.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Subprocess timeout in seconds")


def _add_maintenance_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_prune = subparsers.add_parser("prune", help="Prune merged branches and attached git worktrees")
    p_prune.add_argument("--cwd", type=str, default=None, help="Target repository directory")
    p_prune.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    subparsers.add_parser("hook-pre-tool", help="Antigravity PreToolUse hook handler")
    subparsers.add_parser("hook-post-tool", help="Antigravity PostToolUse hook handler")

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
    _add_maintenance_commands(subparsers)
    return parser


def _run_verify(args: argparse.Namespace) -> int:
    passed, msg, receipt_data, head_sha = verify_pr_telemetry(
        args.pr,
        args.repo,
        timeout=args.timeout,
        check_comments=not args.ignore_unresolved_comments,
    )
    if args.post_status and head_sha:
        state = "success" if passed else "failure"
        desc = (
            f"Verified: {receipt_data.get('total_billed_tokens', 0):,} billed ({receipt_data.get('cache_hit_pct', 0)}% cache)"
            if passed and receipt_data
            else (
                "Verified Antigravity session telemetry & clean reviews"
                if passed
                else "Unresolved review comments or missing telemetry"
            )
        )
        repo_env, run_env = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
        target_url = f"https://github.com/{repo_env}/actions/runs/{run_env}" if repo_env and run_env else None
        ok, status_msg = post_commit_status(
            head_sha, state, desc, repo=args.repo, target_url=target_url, timeout=args.timeout
        )
        if not ok:
            sys.stderr.write(f"Warning: {status_msg}\n")

    stream = sys.stdout if passed else sys.stderr
    stream.write(msg + "\n")
    return 0 if passed else 1


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
        "hook-pre-tool": lambda: (handle_pre_tool_hook(), 0)[1],
        "hook-post-tool": lambda: (handle_post_tool_hook(), 0)[1],
        "validate-plugin": lambda: _run_validate_plugin(args),
        "sync-plugin": lambda: _run_sync_plugin(args),
    }
    sys.exit(handlers[args.command]())


if __name__ == "__main__":
    main()
