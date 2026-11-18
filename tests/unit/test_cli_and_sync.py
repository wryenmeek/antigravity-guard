"""Unit tests for CLI dispatch, plugin bundle validation/sync, GitHub fallbacks, and error paths."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import antigravity_guard.cli as cli
from antigravity_guard import __version__, git_worktree, github, plugin_sync, telemetry


def test_cli_main_dispatch_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise all CLI subcommands through main() with deterministic exit codes."""
    monkeypatch.setattr("sys.argv", ["antigravity-guard", "version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0

    with patch.object(
        cli, "prune_merged_branches_and_worktrees", return_value={"pruned_branches": [], "pruned_worktrees": []}
    ) as m_prune:
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "prune", "--cwd", "/repo", "--quiet"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        m_prune.assert_called_once_with("/repo", quiet=True)

    with patch.object(cli, "generate_and_post_telemetry", return_value=True):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "post", "--pr", "12"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0

    with (
        patch.object(
            cli,
            "verify_pr_telemetry",
            return_value=(True, "OK", {"total_billed_tokens": 5000, "cache_hit_pct": 92.5}, "abcdef123456"),
        ),
        patch.object(cli, "post_commit_status", return_value=(False, "status warning")) as m_status,
        patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "99"}),
    ):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "verify", "--pr", "12", "--post-status"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        m_status.assert_called_once()

    with patch.object(cli, "verify_pr_telemetry", return_value=(False, "FAIL", None, "abcdef12")):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "verify", "--pr", "12", "--ignore-unresolved-comments"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1

    with patch.object(cli, "handle_pre_tool_hook") as m_pre:
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "hook-pre-tool"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        m_pre.assert_called_once()

    with patch.object(cli, "handle_post_tool_hook") as m_post:
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "hook-post-tool"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        m_post.assert_called_once()

    with patch.object(cli, "validate_plugin_bundle", return_value=(False, ["bad plugin"])):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "validate-plugin", "--strict"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1

    with patch.object(cli, "sync_plugin_local", return_value={"installed_dir": str(tmp_path), "version": "2.0.0"}):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "sync-plugin"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0

    with (
        patch.object(
            cli,
            "verify_pr_telemetry",
            return_value=(True, "Telemetry OK", None, "abcdef12"),
        ),
        patch.object(cli, "verify_skills", return_value=(True, "Skills OK")) as m_sk,
        patch.object(cli, "verify_issue_audits", return_value=(False, "Issue audit incomplete")) as m_is,
    ):
        monkeypatch.setattr(
            "sys.argv",
            ["antigravity-guard", "verify", "--pr", "15", "--verify-issue-audits", "--verify-skill-receipts"],
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        m_sk.assert_called_once()
        m_is.assert_called_once()

    with patch.object(cli, "verify_skills", return_value=(True, "OK")):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "verify-skills", "--cwd", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0

    with patch.object(cli, "verify_issue_audits", return_value=(False, "FAIL")):
        monkeypatch.setattr(
            "sys.argv", ["antigravity-guard", "verify-issue", "--cwd", str(tmp_path), "--mode", "strict"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1

    with patch("sys.stdin", io.StringIO("{bad")), patch("sys.stdout", io.StringIO()):
        monkeypatch.setattr("sys.argv", ["antigravity-guard", "hook-stop"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0


def test_plugin_validate_and_sync_local_removes_legacy_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_plugin_local installs plugin bundle, writes installed_version.json, and deletes legacy plugin."""
    repo_root = tmp_path / "repo"
    plugin_dir = repo_root / "plugin"
    (plugin_dir / "assets").mkdir(parents=True)
    (plugin_dir / "skills" / "antigravity-guard").mkdir(parents=True)
    (plugin_dir / "rules").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text(f'[project]\nversion = "{__version__}"\n', encoding="utf-8")
    (plugin_dir / "assets" / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
    (plugin_dir / "hooks.json").write_text("{}", encoding="utf-8")
    (plugin_dir / "skills" / "antigravity-guard" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (plugin_dir / "rules" / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "antigravity-guard",
                "displayName": "Antigravity Guard",
                "version": __version__,
                "logo": "assets/logo.svg",
                "suggestedPrompts": ["verify"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(plugin_sync.shutil, "which", lambda _name: None)
    orig_exists = Path.is_file

    def fake_is_file(self: Path) -> bool:
        if str(self) == "/Users/wryen/.local/bin/agy":
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)

    ok, errors = plugin_sync.validate_plugin_bundle(repo_root=repo_root, strict=True)
    assert ok and not errors

    plugins_root = tmp_path / "home" / ".gemini" / "config" / "plugins"
    legacy_dir = plugins_root / "antigravity-telemetry-guard"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "old.txt").write_text("legacy", encoding="utf-8")
    dest_dir = plugins_root / "antigravity-guard"
    dest_dir.mkdir(parents=True)

    res = plugin_sync.sync_plugin_local(repo_root=repo_root, target_config_dir=dest_dir, clean_legacy=True)
    assert res["removed_legacy"] is True
    assert not legacy_dir.exists()
    assert json.loads((dest_dir / "installed_version.json").read_text(encoding="utf-8")) == {"version": __version__}

    # Negative validation branches
    (plugin_dir / "hooks.json").write_text("{bad json", encoding="utf-8")
    (plugin_dir / "assets" / "logo.svg").unlink()
    (plugin_dir / "rules" / "AGENTS.md").unlink()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "wrong", "version": "1.0.0"}), encoding="utf-8")
    ok_bad, errs_bad = plugin_sync.validate_plugin_bundle(repo_root=repo_root, strict=True)
    assert not ok_bad
    assert len(errs_bad) >= 5

    ok_missing, errs_missing = plugin_sync.validate_plugin_bundle(repo_root=tmp_path / "missing", strict=True)
    assert not ok_missing and "Missing plugin manifest" in errs_missing[0]

    with pytest.raises(RuntimeError):
        plugin_sync.sync_plugin_local(repo_root=repo_root, target_config_dir=dest_dir)


def test_github_fallbacks_and_error_branches() -> None:
    """Exercise GraphQL first-page fallback to gh pr view, timeouts, and status error reporting."""
    with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}), patch("subprocess.run") as m_run:
        m_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="no git remote"),
            MagicMock(returncode=0, stdout=json.dumps({"owner": {"login": "acme"}, "name": "app"})),
        ]
        owner, name = github.get_repo_owner_and_name(repo=None, cwd=str(Path.cwd()))
        assert (owner, name) == ("acme", "app")

    with (
        patch.object(github, "get_repo_owner_and_name", return_value=("acme", "app")),
        patch("subprocess.run") as m_run,
    ):
        m_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="graphql disabled"),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"commits": [{"oid": "123", "messageHeadline": "hi", "messageBody": ""}], "headRefOid": "123"}
                ),
            ),
        ]
        commits, head = github.get_all_pr_commits_and_head(5)
        assert len(commits) == 1 and head == "123"

    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 10)),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        github.get_pr_comments(5, timeout=10)

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="denied")):
        with pytest.raises(RuntimeError, match="Failed to fetch PR #5 comments"):
            github.get_pr_comments(5)
        ok, msg = github.post_commit_status("abcdef1234", "success", "desc")
        assert not ok and "Failed to post commit status" in msg


def test_telemetry_varints_and_error_branches(tmp_path: Path) -> None:
    """Exercise repeated protobuf fields, fixed-width wire types, and PR verification error paths."""
    # Repeated field 2 (len=1 'a', len=1 'b'), wire type 1 (8 bytes), wire type 5 (4 bytes), unknown wire type 6
    blob = b"\x12\x01a\x12\x01b" + b"\x09" + (b"\x00" * 8) + b"\x15" + (b"\x00" * 4) + b"\x1e"
    parsed = telemetry.parse_varints(blob)
    assert parsed[2] == [b"a", b"b"]

    with patch.object(cli, "get_all_pr_commits_and_head", side_effect=RuntimeError("boom")):
        ok, msg, _, _ = cli.verify_pr_telemetry(1)
        assert not ok and "Could not inspect PR commits" in msg

    with (
        patch.object(cli, "get_all_pr_commits_and_head", return_value=([], "sha1")),
        patch.object(cli, "get_unresolved_review_threads", side_effect=RuntimeError("threads err")),
    ):
        ok, msg, _, _ = cli.verify_pr_telemetry(1)
        assert not ok and "Could not inspect PR review threads" in msg

    with (
        patch.object(cli, "get_all_pr_commits_and_head", return_value=([], "sha1")),
        patch.object(cli, "get_pr_comments", side_effect=RuntimeError("comments err")),
    ):
        ok, msg, _, _ = cli.verify_pr_telemetry(1, check_comments=False)
        assert not ok and "Could not inspect PR comments" in msg

    with (
        patch.object(cli, "get_pr_commits", return_value=[{"oid": "1", "messageHeadline": "x", "messageBody": ""}]),
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="fail")),
    ):
        assert cli.generate_and_post_telemetry(1) is False

    # Missing session DB fails cleanly
    with (
        patch.object(
            cli,
            "get_pr_commits",
            return_value=[
                {
                    "oid": "1",
                    "messageHeadline": "x",
                    "messageBody": "Antigravity-Session-ID: 12345678-1234-4234-8234-1234567890ab",
                }
            ],
        ),
        patch.object(cli, "get_session_telemetry", return_value=None),
    ):
        assert cli.generate_and_post_telemetry(1) is False


def test_hooks_and_worktree_edge_cases(tmp_path: Path) -> None:
    """Exercise invalid JSON hooks, non-run_command passthrough, and sanitizer error."""
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO("not json")), patch("sys.stdout", out):
        cli.handle_pre_tool_hook()
    assert json.loads(out.getvalue()) == {"decision": "allow"}

    out2 = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"toolCall": {"name": "read_file", "args": {}}}))),
        patch("sys.stdout", out2),
    ):
        cli.handle_pre_tool_hook()
    assert json.loads(out2.getvalue()) == {"decision": "allow"}

    out3 = io.StringIO()
    with patch("sys.stdin", io.StringIO("not json")), patch("sys.stdout", out3):
        cli.handle_post_tool_hook()
    assert json.loads(out3.getvalue()) == {}

    with pytest.raises(ValueError, match="Invalid working directory"):
        git_worktree.prune_merged_branches_and_worktrees("-flag")

    # _ensure_symlink helper
    target = tmp_path / "bin" / "exe"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "agy_bin" / "exe"
    plugin_sync._ensure_symlink(target, link)
    assert link.is_symlink()
    plugin_sync._ensure_symlink(target, link)
    assert link.resolve() == target.resolve()
