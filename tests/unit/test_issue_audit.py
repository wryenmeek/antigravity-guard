"""Gate 3 unit tests for issue audit ledgers, UUIDv4 subagent transcript checks, and 3-rule bootstrap."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import GitWorktreeFixture, _clean_git_env

SUBAGENT_UUID = "11111111-2222-4333-8444-555555555555"
PARENT_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(
    args: list[str], cwd: Path | None = None, home_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = _clean_git_env()
    if home_dir is not None:
        env["HOME"] = str(home_dir)
    return subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )


def _setup_fake_transcripts(fake_home: Path, *, include_invoke_step: bool = True, create_sub_log: bool = True) -> Path:
    parent_log = (
        fake_home
        / ".gemini"
        / "antigravity"
        / "brain"
        / PARENT_UUID
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    parent_log.parent.mkdir(parents=True, exist_ok=True)
    parent_entry = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {
                "name": "invoke_subagent",
                "args": {"TypeName": "research", "Role": "Issue Auditor"},
                "result": {"conversationId": SUBAGENT_UUID if include_invoke_step else "other-id"},
            }
        ],
    }
    parent_log.write_text(json.dumps(parent_entry) + "\n", encoding="utf-8")

    if create_sub_log:
        sub_log = (
            fake_home
            / ".gemini"
            / "antigravity"
            / "brain"
            / SUBAGENT_UUID
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        sub_log.parent.mkdir(parents=True, exist_ok=True)
        sub_log.write_text(json.dumps({"step_index": 0, "content": "Audited issue #9"}) + "\n", encoding="utf-8")
    return parent_log


def _make_valid_ledger(mod_path: Path, test_path: Path) -> dict[str, object]:
    return {
        "issue_number": 9,
        "audit_phase": "VERIFIED_COMPLETE",
        "subagent_conversation_id": SUBAGENT_UUID,
        "findings": [
            {
                "id": "F-1",
                "adjudication": "VERIFIED",
                "evidence": "scripts/engine.py:3 implemented real_query()",
                "status": "REMEDIATED",
                "pre_fix_sha256": "0" * 64,
                "post_fix_sha256": _sha256_file(mod_path),
                "regression_test": "tests/test_engine.py::test_real_query",
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "description": "Real query symbol implemented",
                "status": "IMPLEMENTED",
                "artifact": "scripts/engine.py",
                "artifact_sha256": _sha256_file(mod_path),
                "symbols": ["real_query"],
            }
        ],
    }


def test_verify_issue_enforces_uuidv4_transcript_adjudication_and_ac(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
) -> None:
    """verify-issue rejects non-UUIDv4, missing parent invoke step, partial AC, stale SHA, and missing AST symbol."""
    repo = git_worktree_env.repo_dir
    fake_home = tmp_path / "home"
    parent_log = _setup_fake_transcripts(fake_home)

    mod_path = repo / "scripts" / "engine.py"
    mod_path.parent.mkdir(parents=True)
    mod_path.write_text("def real_query() -> bool:\n    return True\n", encoding="utf-8")
    test_path = repo / "tests" / "test_engine.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_real_query() -> None:\n    assert True\n", encoding="utf-8")

    ledger_path = repo / ".infigraph" / "reviews" / "issue_9_audit.json"
    ledger_path.parent.mkdir(parents=True)

    # 1. Valid complete ledger passes
    valid = _make_valid_ledger(mod_path, test_path)
    ledger_path.write_text(json.dumps(valid, indent=2), encoding="utf-8")
    ok_res = _run_cli(
        ["verify-issue", "--cwd", str(repo), "--issue", "9", "--mode", "strict", "--transcript-path", str(parent_log)],
        home_dir=fake_home,
    )
    assert ok_res.returncode == 0, ok_res.stdout + ok_res.stderr

    # 2. Bad UUIDv1 (version nibble 1 instead of 4) fails
    bad_uuid = dict(valid, subagent_conversation_id="11111111-2222-1333-8444-555555555555")
    ledger_path.write_text(json.dumps(bad_uuid), encoding="utf-8")
    assert (
        _run_cli(
            ["verify-issue", "--cwd", str(repo), "--issue", "9", "--mode", "strict"], home_dir=fake_home
        ).returncode
        == 1
    )

    # 3. Missing invoke_subagent link in parent transcript fails
    bad_parent = _setup_fake_transcripts(
        tmp_path / "home_missing_invoke", include_invoke_step=False, create_sub_log=True
    )
    ledger_path.write_text(json.dumps(valid), encoding="utf-8")
    assert (
        _run_cli(
            [
                "verify-issue",
                "--cwd",
                str(repo),
                "--issue",
                "9",
                "--mode",
                "strict",
                "--transcript-path",
                str(bad_parent),
            ],
            home_dir=tmp_path / "home_missing_invoke",
        ).returncode
        == 1
    )

    # 4. Partial / Mocked acceptance criterion fails
    partial_ac = json.loads(json.dumps(valid))
    partial_ac["acceptance_criteria"][0]["status"] = "MOCKED"
    ledger_path.write_text(json.dumps(partial_ac), encoding="utf-8")
    assert (
        _run_cli(
            ["verify-issue", "--cwd", str(repo), "--issue", "9", "--mode", "strict"], home_dir=fake_home
        ).returncode
        == 1
    )

    # 5. Missing AST symbol fails
    missing_sym = json.loads(json.dumps(valid))
    missing_sym["acceptance_criteria"][0]["symbols"] = ["nonexistent_fn"]
    ledger_path.write_text(json.dumps(missing_sym), encoding="utf-8")
    assert (
        _run_cli(
            ["verify-issue", "--cwd", str(repo), "--issue", "9", "--mode", "strict"], home_dir=fake_home
        ).returncode
        == 1
    )

    # 6. Stale SHA-256 fails
    stale_sha = json.loads(json.dumps(valid))
    stale_sha["acceptance_criteria"][0]["artifact_sha256"] = "f" * 64
    ledger_path.write_text(json.dumps(stale_sha), encoding="utf-8")
    assert (
        _run_cli(
            ["verify-issue", "--cwd", str(repo), "--issue", "9", "--mode", "strict"], home_dir=fake_home
        ).returncode
        == 1
    )


def test_verify_issue_bootstrap_rules_prevent_deadlocks(git_worktree_env: GitWorktreeFixture, tmp_path: Path) -> None:
    """3-rule activation allows seeded/test-only commits without deadlock while blocking premature closure."""
    repo = git_worktree_env.repo_dir
    ledger_path = repo / ".infigraph" / "reviews" / "issue_9_audit.json"
    ledger_path.parent.mkdir(parents=True)
    seeded = {
        "issue_number": 9,
        "audit_phase": "SEEDED_OPEN",
        "subagent_conversation_id": "",
        "findings": [{"id": "F-1", "adjudication": "VERIFIED", "evidence": "mocked", "status": "OPEN"}],
        "acceptance_criteria": [{"id": "AC-1", "description": "Real DB", "status": "MOCKED"}],
    }
    ledger_path.write_text(json.dumps(seeded), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".infigraph/reviews/issue_9_audit.json"],
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )

    # Rule 3: Initial SEEDED_OPEN ledger commit passes in commit mode when not closing #9
    seed_res = _run_cli(
        ["verify-issue", "--cwd", str(repo), "--mode", "commit", "--commit-msg", "chore(audit): seed issue 9"]
    )
    assert seed_res.returncode == 0, seed_res.stdout + seed_res.stderr

    # Rule 2: Gate 3 test-only commit passes in commit mode
    test_file = repo / "tests" / "test_red.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_red() -> None:\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "reset"], check=True, capture_output=True, env=_clean_git_env())
    subprocess.run(
        ["git", "-C", str(repo), "add", "tests/test_red.py"], check=True, capture_output=True, env=_clean_git_env()
    )
    red_res = _run_cli(["verify-issue", "--cwd", str(repo), "--mode", "commit", "--commit-msg", "test: gate 3 red"])
    assert red_res.returncode == 0, red_res.stdout + red_res.stderr

    # Production commit trying to close #9 against SEEDED_OPEN ledger MUST FAIL
    prod_file = repo / "app.py"
    prod_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, capture_output=True, env=_clean_git_env())
    close_res = _run_cli(
        ["verify-issue", "--cwd", str(repo), "--mode", "commit", "--commit-msg", "fix: resolve engine, closes #9"]
    )
    assert close_res.returncode == 1


def test_in_process_issue_audit_findings_ast_and_stop_hook(
    git_worktree_env: GitWorktreeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers in-process issue_audit branches: finding remediation, AST Class.method, CI mode, and Stop hook."""
    from typing import Any

    from antigravity_guard import issue_audit

    repo = git_worktree_env.repo_dir
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    parent_log = _setup_fake_transcripts(fake_home)

    mod_path = repo / "scripts" / "engine.py"
    mod_path.parent.mkdir(parents=True, exist_ok=True)
    mod_path.write_text("class Engine:\n    def run(self) -> int:\n        return 1\n", encoding="utf-8")
    js_path = repo / "scripts" / "helper.js"
    js_path.write_text("function jsHelper() { return 1; }\n", encoding="utf-8")
    test_path = repo / "tests" / "test_engine.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_real_query() -> None:\n    assert True\n", encoding="utf-8")

    ledger_path = repo / ".infigraph" / "reviews" / "issue_10_audit.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    valid: dict[str, Any] = {
        "issue_number": 10,
        "audit_phase": "VERIFIED_COMPLETE",
        "subagent_conversation_id": SUBAGENT_UUID,
        "findings": [
            {"id": "F-OK", "adjudication": "REJECTED", "evidence": "not reproduced"},
            {
                "id": "F-FIX",
                "adjudication": "VERIFIED",
                "evidence": "fixed",
                "status": "REMEDIATED",
                "pre_fix_sha256": "0" * 64,
                "post_fix_sha256": _sha256_file(mod_path),
                "regression_test": "test_real_query",
            },
        ],
        "acceptance_criteria": [
            {
                "id": "AC-PY",
                "status": "IMPLEMENTED",
                "artifact": "scripts/engine.py",
                "artifact_sha256": _sha256_file(mod_path),
                "symbols": ["Engine.run"],
            },
            {
                "id": "AC-JS",
                "status": "IMPLEMENTED",
                "artifact": "scripts/helper.js",
                "artifact_sha256": _sha256_file(js_path),
                "symbols": ["jsHelper"],
            },
        ],
    }
    ledger_path.write_text(json.dumps(valid), encoding="utf-8")

    ok, msg = issue_audit.verify_issue_audits(
        repo, issue_num=10, mode="strict", transcript_path=parent_log, verify_transcripts=True
    )
    assert ok, msg
    ok_ci, msg_ci = issue_audit.verify_issue_audits(repo, mode="strict", verify_transcripts=False)
    assert ok_ci, msg_ci

    bad_finding: dict[str, Any] = json.loads(json.dumps(valid))
    bad_finding["findings"][1]["status"] = "OPEN"
    bad_finding["findings"][1]["post_fix_sha256"] = bad_finding["findings"][1]["pre_fix_sha256"]
    bad_finding["findings"][1]["regression_test"] = "tests/test_engine.py::missing_test"
    ledger_path.write_text(json.dumps(bad_finding), encoding="utf-8")
    ok_bad, msg_bad = issue_audit.verify_issue_audits(repo, issue_num=10, mode="strict", verify_transcripts=False)
    assert not ok_bad
    assert "not REMEDIATED" in msg_bad
    assert "regression test not found" in msg_bad

    ledger_path.write_text("{bad", encoding="utf-8")
    assert not issue_audit.verify_issue_audits(repo, issue_num=10, mode="strict", verify_transcripts=False)[0]
    ledger_path.write_text("[]", encoding="utf-8")
    assert not issue_audit.verify_issue_audits(repo, issue_num=10, mode="strict", verify_transcripts=False)[0]
    ledger_path.write_text(json.dumps({"audit_phase": "WEIRD"}), encoding="utf-8")
    assert not issue_audit.verify_issue_audits(repo, issue_num=10, mode="strict", verify_transcripts=False)[0]
    ledger_path.write_text(
        json.dumps(
            {
                "audit_phase": "VERIFIED_COMPLETE",
                "subagent_conversation_id": "bad",
                "findings": [{"id": "F-1", "adjudication": "OPEN"}],
                "acceptance_criteria": [],
            }
        ),
        encoding="utf-8",
    )
    assert not issue_audit.verify_issue_audits(repo, issue_num=10, mode="strict", verify_transcripts=False)[0]

    assert issue_audit.handle_stop_hook("{bad") == {}
    assert issue_audit.handle_stop_hook(json.dumps({"continuationActive": True})) == {}
    assert issue_audit.handle_stop_hook(json.dumps({"conversationId": PARENT_UUID})) == {}
    assert (
        issue_audit.handle_stop_hook(
            json.dumps({"conversationId": "00000000-0000-4000-8000-000000000000", "cwd": str(repo)})
        )
        == {}
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "scripts/engine.py"], check=True, capture_output=True, env=_clean_git_env()
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "feat: commit", "-m", f"Antigravity-Session-ID: {PARENT_UUID}"],
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )
    stop_res = issue_audit.handle_stop_hook(
        json.dumps({"conversationId": PARENT_UUID, "cwd": str(repo), "continuationActive": False})
    )
    assert stop_res.get("decision") == "continue"

    # 5. Additional in-process edge branches (merge check + missing artifacts/ledgers + bootstrap)
    from antigravity_guard import hooks as guard_hooks

    assert issue_audit.extract_closed_issue_numbers("closes #9, fixes #9, resolves #10") == [9, 10]
    assert not issue_audit.is_bootstrap_only_paths([])
    assert issue_audit.verify_issue_audits(tmp_path / "unopted", mode="commit")[0]
    empty_reviews = tmp_path / "empty_strict"
    empty_reviews.mkdir()
    assert not issue_audit.verify_issue_audits(empty_reviews, mode="strict")[0]
    ledger_path.write_text(
        json.dumps(
            {
                "audit_phase": "VERIFIED_COMPLETE",
                "subagent_conversation_id": SUBAGENT_UUID,
                "findings": [{"id": "F-1", "adjudication": "VERIFIED", "evidence": ""}],
                "acceptance_criteria": [
                    {"id": "AC-1", "status": "IMPLEMENTED", "artifact": ""},
                    {"id": "AC-2", "status": "IMPLEMENTED", "artifact": "missing.py"},
                ],
            }
        ),
        encoding="utf-8",
    )
    ok_extra, msg_extra = issue_audit.verify_issue_audits(
        repo, issue_num=10, mode="strict", verify_transcripts=True, transcript_path=parent_log
    )
    assert not ok_extra and "missing artifact" in msg_extra
    assert guard_hooks._check_merge_issue_audits(str(repo), {"transcriptPath": str(parent_log)}) is not None
