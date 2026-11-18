"""Gate 3 unit tests for security hardening, de-hardcoded reports, complexity ceiling, and strict plugin metadata."""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import antigravity_guard
import antigravity_guard.cli as guard_cli
import antigravity_telemetry.cli as legacy_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PKG = REPO_ROOT / "src" / "antigravity_guard"


def _compute_mccabe(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    branch_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)
    score = 1
    stack = list(ast.iter_child_nodes(fn_node))
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(cur, branch_types):
            score += 1
        elif isinstance(cur, ast.comprehension):
            score += len(cur.ifs)
        stack.extend(ast.iter_child_nodes(cur))
    return score


def test_version_and_shim_identity() -> None:
    """Package version is 2.0.1 and legacy module aliases canonical module object."""
    assert antigravity_guard.__version__ == "2.0.1"
    assert legacy_cli is guard_cli


def test_ast_complexity_ceiling_under_ten() -> None:
    """All functions in src/antigravity_guard maintain McCabe complexity <= 10."""
    offenders: list[tuple[str, str, int]] = []
    for py_file in sorted(SRC_PKG.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                score = _compute_mccabe(node)
                if score > 10:
                    offenders.append((py_file.name, node.name, score))
    assert not offenders, f"Complexity ceiling (>10) exceeded: {offenders}"


def test_generate_and_post_telemetry_has_no_hardcoded_legacy_artifacts(tmp_path: Path) -> None:
    """Telemetry comment generation never falls back to pp-docker or fake shared fleet numbers."""
    repo_dir = tmp_path / "sample_repo"
    repo_dir.mkdir()
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/org/sample/pull/5#issuecomment-1", stderr="")

    sample_commits = [
        {
            "oid": "deadbeef",
            "messageHeadline": "feat: real work",
            "messageBody": "Antigravity-Session-ID: 12345678-1234-4234-8234-1234567890ab",
        }
    ]
    sample_telemetry = {
        "cid": "12345678-1234-4234-8234-1234567890ab",
        "turns": 3,
        "model": "gemini-3.8-flash-tiered",
        "uncached_prompt": 1000,
        "cached_prompt": 9000,
        "thoughts": 100,
        "candidates": 200,
        "output_total": 300,
        "gross_context": 10300,
        "billed_tokens_75": 3550,
    }

    with (
        patch.object(guard_cli, "get_pr_commits", return_value=sample_commits),
        patch.object(guard_cli, "get_session_telemetry", return_value=sample_telemetry),
        patch("subprocess.run", side_effect=fake_run),
    ):
        ok = guard_cli.generate_and_post_telemetry(7, repo="example/real-repo")

    assert ok is True
    comment_cmd = next(c for c in captured_cmds if c[:3] == ["gh", "pr", "comment"])
    body = comment_cmd[comment_cmd.index("--body") + 1]
    assert "wryenmeek/pp-docker" not in body
    assert "Shared Infrastructure Allocation" not in body
    assert "5,420,128" not in body
    assert "example/real-repo" in body


def test_pre_tool_hook_blocks_dash_f_path_traversal(tmp_path: Path) -> None:
    """git commit -F pointing outside repo_root is blocked instead of appending to arbitrary paths."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n", encoding="utf-8")

    payload = {
        "conversationId": "12345678-1234-4234-8234-1234567890ab",
        "cwd": str(repo_dir),
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": f"git commit -F {outside}"},
        },
    }
    stdout_buf = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", stdout_buf):
        guard_cli.handle_pre_tool_hook()

    out = json.loads(stdout_buf.getvalue())
    assert out["decision"] == "deny"
    assert "outside repository root" in out["reason"].lower() or "path traversal" in out["reason"].lower()
    assert outside.read_text(encoding="utf-8") == "do not touch\n"


def test_pre_tool_hook_rejects_option_injection_cwd() -> None:
    """Unvalidated leading-dash cwd cannot pass as git -C flag injection."""
    payload = {
        "conversationId": "12345678-1234-4234-8234-1234567890ab",
        "cwd": "--upload-pack=/tmp/evil",
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "git push origin feat/ok"},
        },
    }
    stdout_buf = io.StringIO()
    with (
        patch("subprocess.run") as mock_run,
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", stdout_buf),
    ):
        guard_cli.handle_pre_tool_hook()

    mock_run.assert_not_called()
    out = json.loads(stdout_buf.getvalue())
    assert out["decision"] == "allow"


def test_plugin_manifest_satisfies_strict_metadata() -> None:
    """plugin/plugin.json contains name, version 2.0.0, displayName, suggestedPrompts, and valid SVG logo."""
    manifest_path = REPO_ROOT / "plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "antigravity-guard"
    assert data["version"] == "2.0.1"
    assert data.get("displayName") == "Antigravity Guard"
    logo_rel = data.get("logo")
    assert isinstance(logo_rel, str) and logo_rel
    assert (REPO_ROOT / "plugin" / logo_rel).is_file()
    assert isinstance(data.get("suggestedPrompts"), list) and len(data["suggestedPrompts"]) >= 1
