"""Gate 3 unit tests for PostToolUse skill receipt capture and verify-skills enforcement."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import antigravity_guard.cli as cli
from tests.conftest import GitWorktreeFixture, _clean_git_env


def _run_cli(
    args: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = _clean_git_env()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )


def test_post_tool_captures_skill_reads_invoke_script_and_subagent(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostToolUse records SKILL.md reads, canonical invoke-skill.sh runs, and invoke_subagent calls."""
    repo = git_worktree_env.repo_dir
    skill_md = repo / ".github" / "skills" / "ce-code-review" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: ce-code-review\n---\n# Review\n", encoding="utf-8")
    root_skill = repo / "SKILL.md"
    root_skill.write_text("---\nname: mcdm-graph\n---\n# Graph\n", encoding="utf-8")
    invoke_sh = repo / "scripts" / "invoke-skill.sh"
    invoke_sh.parent.mkdir(parents=True)
    invoke_sh.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")

    plugin_data = tmp_path / "plugin_data"
    monkeypatch.setenv("PLUGIN_DATA", str(plugin_data))
    cid = "12345678-1234-4234-8234-1234567890ab"

    events = [
        {"conversationId": cid, "cwd": str(repo), "toolCall": {"name": "read_file", "args": {"Path": str(skill_md)}}},
        {
            "conversationId": cid,
            "cwd": str(repo),
            "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(root_skill)}},
        },
        {
            "conversationId": cid,
            "cwd": str(repo),
            "toolCall": {"name": "run_command", "args": {"CommandLine": "bash scripts/invoke-skill.sh quality-gate"}},
        },
        {
            "conversationId": cid,
            "cwd": str(repo),
            "toolCall": {"name": "invoke_subagent", "args": {"TypeName": "research", "Role": "Issue Auditor"}},
        },
        # Must ignore non-canonical script and non-boundary command
        {
            "conversationId": cid,
            "cwd": str(repo),
            "toolCall": {"name": "run_command", "args": {"CommandLine": "bash /tmp/fake/invoke-skill.sh forged-skill"}},
        },
    ]
    for ev in events:
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(ev))), patch("sys.stdout", out):
            cli.handle_post_tool_hook()

    repo_jsonl = repo / ".github" / "hooks" / "state" / "skill-receipts.jsonl"
    plugin_jsonl = plugin_data / "skill-receipts.jsonl"
    footers_file = repo / ".git" / "SKILL_FOOTERS"

    assert repo_jsonl.is_file()
    assert plugin_jsonl.is_file()
    assert footers_file.is_file()

    records = [json.loads(line) for line in repo_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    recorded_skills = [r["skill"] for r in records]
    assert "ce-code-review" in recorded_skills
    assert "mcdm-graph" in recorded_skills
    assert "quality-gate" in recorded_skills
    assert any(r["tool"] == "invoke_subagent" for r in records)
    assert "forged-skill" not in recorded_skills

    footers_text = footers_file.read_text(encoding="utf-8")
    assert "validated-by: ce-code-review at " in footers_text
    assert f"(sessionId {cid}, " in footers_text


def test_verify_skills_opt_in_freshness_and_plugin_data_fallback(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
) -> None:
    """verify-skills skips unopted repos, enforces freshness against staged files, and supports $PLUGIN_DATA fallback."""
    repo = git_worktree_env.repo_dir

    # Rule 1: Repo without .github/required-skills.json passes as not opted in
    unopted = _run_cli(["verify-skills", "--cwd", str(repo)])
    assert unopted.returncode == 0
    assert "not opted in" in unopted.stdout.lower()

    # Configure required-skills.json for *.py changes
    req_file = repo / ".github" / "required-skills.json"
    req_file.parent.mkdir(parents=True)
    req_file.write_text(
        json.dumps(
            {
                "rules": [
                    {"pattern": "*.py", "required_skills": ["ce-code-review"], "enforcement": "gate"},
                ]
            }
        ),
        encoding="utf-8",
    )

    py_file = repo / "app.py"
    py_file.write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, capture_output=True, env=_clean_git_env())

    # Fails without receipt
    missing_res = _run_cli(["verify-skills", "--cwd", str(repo), "--staged-only"])
    assert missing_res.returncode == 1
    assert "ce-code-review" in (missing_res.stdout + missing_res.stderr)

    # Record stale receipt dated before py_file mtime -> fails
    plugin_data = tmp_path / "pdata"
    plugin_data.mkdir()
    stale_epoch = os.stat(py_file).st_mtime - 60.0
    cid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    stale_rec = {
        "skill": "ce-code-review",
        "tool": "read_file",
        "epoch": stale_epoch,
        "timestamp": "2026-01-01T00:00:00Z",
        "sessionId": cid,
        "repoRoot": str(repo.resolve()),
        "artifactSha256": hashlib.sha256(b"old").hexdigest(),
    }
    (plugin_data / "skill-receipts.jsonl").write_text(json.dumps(stale_rec) + "\n", encoding="utf-8")
    stale_res = _run_cli(
        ["verify-skills", "--cwd", str(repo), "--staged-only", "--session-id", cid],
        env_extra={"PLUGIN_DATA": str(plugin_data)},
    )
    assert stale_res.returncode == 1

    # Append fresh receipt in $PLUGIN_DATA (with repo state empty) -> fallback succeeds!
    time.sleep(0.02)
    fresh_rec = dict(stale_rec, epoch=time.time() + 1.0, timestamp="2026-11-17T06:00:00Z")
    (plugin_data / "skill-receipts.jsonl").write_text(json.dumps(fresh_rec) + "\n", encoding="utf-8")
    ok_res = _run_cli(
        ["verify-skills", "--cwd", str(repo), "--staged-only", "--session-id", cid],
        env_extra={"PLUGIN_DATA": str(plugin_data)},
    )
    assert ok_res.returncode == 0, ok_res.stdout + ok_res.stderr
