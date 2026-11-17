"""Real git worktree & filesystem integration tests for hook guards and pruning."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import antigravity_guard.cli as guard_cli
from tests.conftest import GitWorktreeFixture, _clean_git_env


def test_real_worktree_blocks_merge_and_prunes_cleanly(git_worktree_env: GitWorktreeFixture) -> None:
    """Real git worktree triggers merge block inside .worktrees/ and cleans up on prune."""
    # 1. PreToolUse inside real linked worktree denies gh pr merge
    payload = {
        "conversationId": "12345678-1234-4234-8234-1234567890ab",
        "cwd": str(git_worktree_env.worktree_dir),
        "toolCall": {"name": "run_command", "args": {"CommandLine": "gh pr merge 10 --merge"}},
    }
    stdout_buf = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", stdout_buf):
        guard_cli.handle_pre_tool_hook()

    res = json.loads(stdout_buf.getvalue())
    assert res["decision"] == "deny"
    assert "WORKTREE GUARD BLOCKED" in res["reason"]

    # 2. Pruning from primary repo removes real linked worktree and merged branch
    prune_res = guard_cli.prune_merged_branches_and_worktrees(str(git_worktree_env.repo_dir), quiet=True)
    assert git_worktree_env.branch_name in prune_res["pruned_branches"]
    assert not git_worktree_env.worktree_dir.exists()
    branches = subprocess.run(
        ["git", "-C", str(git_worktree_env.repo_dir), "branch", "--list", git_worktree_env.branch_name],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    assert branches.stdout.strip() == ""


def test_real_dash_f_commit_msg_resolves_relative_to_payload_cwd(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative -F path resolves inside repo_dir (never process CWD) and blocks ../ traversal."""
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    msg_file = git_worktree_env.repo_dir / "COMMIT_EDITMSG"
    msg_file.write_text("feat: real git commit\n", encoding="utf-8")

    cid = "12345678-1234-4234-8234-1234567890ab"
    payload = {
        "conversationId": cid,
        "cwd": str(git_worktree_env.repo_dir),
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -F COMMIT_EDITMSG"}},
    }
    stdout_buf = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", stdout_buf):
        guard_cli.handle_pre_tool_hook()

    out = json.loads(stdout_buf.getvalue())
    assert out["decision"] == "allow"
    assert f"Antigravity-Session-ID: {cid}" in msg_file.read_text(encoding="utf-8")
    assert not (other_cwd / "COMMIT_EDITMSG").exists()

    # Traverse outside repo_dir via relative ../outside.txt
    outside_msg = git_worktree_env.repo_dir.parent / "outside.txt"
    outside_msg.write_text("escape attempt\n", encoding="utf-8")
    bad_payload = {
        "conversationId": cid,
        "cwd": str(git_worktree_env.repo_dir),
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -F ../outside.txt"}},
    }
    bad_out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(bad_payload))), patch("sys.stdout", bad_out):
        guard_cli.handle_pre_tool_hook()

    denied = json.loads(bad_out.getvalue())
    assert denied["decision"] == "deny"
    assert outside_msg.read_text(encoding="utf-8") == "escape attempt\n"
