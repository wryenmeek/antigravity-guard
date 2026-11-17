"""End-to-end integration tests for PreToolUse (--no-verify, skill/issue gates), PostToolUse, and Stop hook."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import antigravity_guard.cli as cli
from tests.conftest import GitWorktreeFixture, _clean_git_env


def test_pre_tool_blocks_no_verify_and_injects_validated_by_footers(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PreToolUse denies `--no-verify` / `-n`, blocks un-receipted commits, then injects validated-by footers once fresh."""
    repo = git_worktree_env.repo_dir
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "pdata"))
    cid = "12345678-1234-4234-8234-1234567890ab"

    # 1. --no-verify and -n blocked immediately
    for bad_cmd in ("git commit --no-verify -m 'fix: x'", "git commit -n -m 'fix: x'"):
        out = io.StringIO()
        payload = {
            "conversationId": cid,
            "cwd": str(repo),
            "toolCall": {"name": "run_command", "args": {"CommandLine": bad_cmd}},
        }
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
            cli.handle_pre_tool_hook()
        res = json.loads(out.getvalue())
        assert res["decision"] == "deny"
        assert "no-verify" in res["reason"].lower()

    # 2. Opt repo in to required-skills.json for *.py changes
    req = repo / ".github" / "required-skills.json"
    req.parent.mkdir(parents=True)
    req.write_text(
        json.dumps({"rules": [{"pattern": "*.py", "required_skills": ["ce-code-review"], "enforcement": "gate"}]}),
        encoding="utf-8",
    )
    skill_md = repo / ".github" / "skills" / "ce-code-review" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: ce-code-review\n---\n", encoding="utf-8")
    py_file = repo / "module.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True, capture_output=True, env=_clean_git_env())

    # Denied before reading SKILL.md
    blocked_out = io.StringIO()
    commit_payload = {
        "conversationId": cid,
        "cwd": str(repo),
        "toolCall": {"name": "run_command", "args": {"CommandLine": 'git commit -m "feat: add module"'}},
    }
    with patch("sys.stdin", io.StringIO(json.dumps(commit_payload))), patch("sys.stdout", blocked_out):
        cli.handle_pre_tool_hook()
    assert json.loads(blocked_out.getvalue())["decision"] == "deny"

    # Simulate PostToolUse read_file on SKILL.md
    post_out = io.StringIO()
    read_payload = {
        "conversationId": cid,
        "cwd": str(repo),
        "toolCall": {"name": "read_file", "args": {"Path": str(skill_md)}},
    }
    with patch("sys.stdin", io.StringIO(json.dumps(read_payload))), patch("sys.stdout", post_out):
        cli.handle_post_tool_hook()

    # Now commit is allowed and gets BOTH Antigravity-Session-ID AND validated-by footer injected!
    allow_out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(commit_payload))), patch("sys.stdout", allow_out):
        cli.handle_pre_tool_hook()
    allowed = json.loads(allow_out.getvalue())
    assert allowed["decision"] == "allow", allowed
    overwritten = allowed["overwrite"]["CommandLine"]
    assert f"Antigravity-Session-ID: {cid}" in overwritten
    assert "validated-by: ce-code-review at " in overwritten


def test_stop_hook_enforces_complete_issue_audits_once(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
) -> None:
    """Stop hook returns decision=continue once when session commits have incomplete audits, then allows continuationActive."""
    repo = git_worktree_env.repo_dir
    cid = "99999999-8888-4777-8666-555555555555"

    # Make a production commit tagged with this session ID while issue audit ledger is still SEEDED_OPEN
    ledger = repo / ".infigraph" / "reviews" / "issue_7_audit.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"issue_number": 7, "audit_phase": "SEEDED_OPEN", "findings": [], "acceptance_criteria": []}),
        encoding="utf-8",
    )
    (repo / "src_mod.py").write_text("val = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "src_mod.py", ".infigraph/reviews/issue_7_audit.json"],
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "feat: partial work", "-m", f"Antigravity-Session-ID: {cid}"],
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )

    env = dict(_clean_git_env(), HOME=str(tmp_path), PLUGIN_DATA=str(tmp_path / "pdata"))
    proc1 = subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", "hook-stop"],
        input=json.dumps({"conversationId": cid, "cwd": str(repo), "continuationActive": False}),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )
    assert proc1.returncode == 0
    res1 = json.loads(proc1.stdout)
    assert res1.get("decision") == "continue"

    # Second call with continuationActive=True must exit cleanly with {}
    proc2 = subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", "hook-stop"],
        input=json.dumps({"conversationId": cid, "cwd": str(repo), "continuationActive": True}),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )
    assert proc2.returncode == 0
    assert json.loads(proc2.stdout) == {}
