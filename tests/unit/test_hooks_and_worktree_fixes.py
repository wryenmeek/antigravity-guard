"""Regression tests for PR 1 (#15, #11, #17): command segmentation, worktree roots, -F parsing, and ledger scoping."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import antigravity_guard.cli as cli
from antigravity_guard import git_worktree, hooks, issue_audit
from tests.conftest import GitWorktreeFixture, _clean_git_env

SUBAGENT_UUID = "11111111-2222-4333-8444-555555555555"
PARENT_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=_clean_git_env(),
    )
    return res.stdout.strip()


def test_direct_push_guard_scoped_to_push_segment() -> None:
    """#15: Chained `gh pr create --base main` does not trigger direct-push-to-main guard."""
    assert not hooks.is_direct_push_to_main(
        "git push -u origin docs/my-branch && gh pr create --base main --head docs/my-branch"
    )
    assert hooks.is_direct_push_to_main("git push origin main && gh pr create --base main")
    assert hooks.is_direct_push_to_main("git push origin HEAD:main")


def test_quoted_commit_inside_gh_issue_create_is_ignored(tmp_path: Path) -> None:
    """#17: Quoted `&& git commit` inside `--title` does not trigger commit gate."""
    payload = {
        "conversationId": PARENT_UUID,
        "cwd": str(tmp_path),
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": 'gh issue create --title "fix && git commit -m foo" --body "ok"'},
        },
    }
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
        cli.handle_pre_tool_hook()
    assert json.loads(out.getvalue()) == {"decision": "allow"}


def test_chained_git_add_docs_before_commit_hits_bootstrap_exemption(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#17: `git add docs/report.md && git commit` is recognized as bootstrap-only before git add runs."""
    repo = git_worktree_env.repo_dir
    ledger = repo / ".infigraph" / "reviews" / "issue_1_audit.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"issue_number": 1, "audit_phase": "SEEDED_OPEN"}), encoding="utf-8")
    doc = repo / "docs" / "report.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Report\n", encoding="utf-8")

    payload = {
        "conversationId": PARENT_UUID,
        "cwd": str(repo),
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": 'git add docs/report.md && git commit -m "docs: add report"'},
        },
    }
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
        cli.handle_pre_tool_hook()
    res = json.loads(out.getvalue())
    assert res["decision"] == "allow", res


def test_linked_worktree_uses_worktree_index_for_bootstrap_and_skills(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#17 Bug 3: Linked worktree checks its own staged index instead of main checkout's empty index."""
    wt = git_worktree_env.worktree_dir
    ledger = wt / ".infigraph" / "reviews" / "issue_2_audit.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"issue_number": 2, "audit_phase": "SEEDED_OPEN"}), encoding="utf-8")

    doc = wt / "docs" / "wt_note.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Worktree Note\n", encoding="utf-8")
    _git(wt, "add", "docs/wt_note.md")

    ok, msg = issue_audit.verify_issue_audits(wt, mode="commit", commit_msg="docs: worktree note")
    assert ok, msg


def test_clean_historical_verified_complete_ledger_skipped(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#11 & #17: Clean committed VERIFIED_COMPLETE ledgers are not re-hashed against later edits."""
    repo = git_worktree_env.repo_dir
    app = repo / "app.py"
    app.write_text("v = 1\n", encoding="utf-8")
    sha_v1 = hashlib.sha256(app.read_bytes()).hexdigest()

    ledger = repo / ".infigraph" / "reviews" / "issue_5_audit.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "issue_number": 5,
                "audit_phase": "VERIFIED_COMPLETE",
                "subagent_conversation_id": SUBAGENT_UUID,
                "findings": [],
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "status": "IMPLEMENTED",
                        "artifact": "app.py",
                        "artifact_sha256": sha_v1,
                        "symbols": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "app.py", ".infigraph/reviews/issue_5_audit.json")
    _git(repo, "commit", "-m", "feat: close #5")

    # Later session edits app.py on a feature branch; historical issue_5_audit.json is clean and untouched
    _git(repo, "checkout", "-b", "feat/later-work")
    app.write_text("v = 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")

    ok_commit, msg_commit = issue_audit.verify_issue_audits(repo, mode="commit", commit_msg="feat: update app")
    assert ok_commit, msg_commit
    ok_strict, msg_strict = issue_audit.verify_issue_audits(repo, mode="strict", verify_transcripts=False)
    assert ok_strict, msg_strict


def test_dash_f_commit_message_parsed_for_closes_issue(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#11: `-F <msg-file>` containing `Closes #9` is parsed and blocked against SEEDED_OPEN ledger."""
    repo = git_worktree_env.repo_dir
    ledger = repo / ".infigraph" / "reviews" / "issue_9_audit.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"issue_number": 9, "audit_phase": "SEEDED_OPEN"}), encoding="utf-8")
    doc = repo / "docs" / "notes.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("notes\n", encoding="utf-8")
    _git(repo, "add", "docs/notes.md")

    msg_file = repo / ".git" / "COMMIT_MSG_TEST"
    msg_file.write_text("fix: resolve issue\n\nCloses #9\n", encoding="utf-8")

    payload = {
        "conversationId": PARENT_UUID,
        "cwd": str(repo),
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -F .git/COMMIT_MSG_TEST"}},
    }
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
        cli.handle_pre_tool_hook()
    res = json.loads(out.getvalue())
    assert res["decision"] == "deny"
    assert "SEEDED_OPEN" in res["reason"]


def test_parent_transcript_ignores_error_denial_entries(tmp_path: Path) -> None:
    """#11: Prior ERROR/denial messages containing subagent UUID do not self-corroborate."""
    parent_log = tmp_path / "transcript.jsonl"
    error_entry = {
        "step_index": 2,
        "status": "ERROR",
        "content": f"[ANTIGRAVITY ISSUE GATE BLOCKED] Subagent {SUBAGENT_UUID} was not invoked by active parent",
    }
    parent_log.write_text(json.dumps(error_entry) + "\n", encoding="utf-8")
    assert not issue_audit._parent_has_subagent_invocation(parent_log, SUBAGENT_UUID)


def test_git_merge_abort_exempted_from_merge_guard(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#11: `git merge --abort` is allowed even from a worktree or with open ledgers."""
    wt = git_worktree_env.worktree_dir
    payload = {
        "conversationId": PARENT_UUID,
        "cwd": str(wt),
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git merge --abort"}},
    }
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
        cli.handle_pre_tool_hook()
    assert json.loads(out.getvalue()) == {"decision": "allow"}


def test_prune_removes_untracked_verified_complete_ledgers_only(
    git_worktree_env: GitWorktreeFixture,
) -> None:
    """#11: `prune_merged_branches_and_worktrees` deletes untracked VERIFIED_COMPLETE ledgers, keeps tracked ones."""
    repo = git_worktree_env.repo_dir
    reviews = repo / ".infigraph" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)

    tracked_ledger = reviews / "issue_1_audit.json"
    tracked_ledger.write_text(json.dumps({"issue_number": 1, "audit_phase": "VERIFIED_COMPLETE"}), encoding="utf-8")
    _git(repo, "add", ".infigraph/reviews/issue_1_audit.json")
    _git(repo, "commit", "-m", "chore: track issue 1 ledger")

    untracked_done = reviews / "issue_2_audit.json"
    untracked_done.write_text(json.dumps({"issue_number": 2, "audit_phase": "VERIFIED_COMPLETE"}), encoding="utf-8")
    untracked_open = reviews / "issue_3_audit.json"
    untracked_open.write_text(json.dumps({"issue_number": 3, "audit_phase": "SEEDED_OPEN"}), encoding="utf-8")

    res = git_worktree.prune_merged_branches_and_worktrees(str(repo), quiet=True)
    assert ".infigraph/reviews/issue_2_audit.json" in res["pruned_ledgers"]
    assert not untracked_done.exists()
    assert untracked_open.exists()
    assert tracked_ledger.exists()
