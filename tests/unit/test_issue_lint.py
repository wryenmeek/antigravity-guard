"""Unit tests for Pre-ready-for-agent issue governance (`antigravity-guard lint-issue`, #14)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from antigravity_guard import issue_lint
from tests.conftest import _clean_git_env


def _write_required_skills(repo: Path) -> None:
    cfg = repo / ".github" / "required-skills.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "rules": [
                    {"pattern": "src/**/*.py", "required_skills": ["tdd", "code-review"], "enforcement": "gate"},
                    {"pattern": "README.md", "required_skills": ["writing-for-agents"], "enforcement": "advisory"},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_lint_issue_passes_through_for_non_opted_in_repo(tmp_path: Path) -> None:
    """Repositories without `.github/required-skills.json` or `.infigraph/` pass through."""
    issue_md = tmp_path / "issue.md"
    issue_md.write_text("Minimal issue without sections\n", encoding="utf-8")
    ok, msg = issue_lint.lint_issue(tmp_path, file_path=issue_md)
    assert ok, msg
    assert "not opted in" in msg.lower()


def test_lint_issue_fails_when_blast_radius_skills_or_docs_missing(tmp_path: Path) -> None:
    """Opted-in repo fails closed when blast-radius, matched skills, or doc criteria are missing."""
    _write_required_skills(tmp_path)
    issue_md = tmp_path / "issue.md"
    issue_md.write_text("Modify `src/app/handler.py` to fix bug.\n", encoding="utf-8")

    ok, msg = issue_lint.lint_issue(tmp_path, file_path=issue_md)
    assert not ok
    assert "blast-radius" in msg.lower()
    assert "code-review" in msg
    assert "tdd" in msg
    assert "documentation" in msg.lower()


def test_lint_issue_passes_when_all_governance_criteria_present(tmp_path: Path) -> None:
    """Issue with infigraph blast-radius, matched skills (including advisory), and doc criteria passes."""
    _write_required_skills(tmp_path)
    issue_md = tmp_path / "issue.md"
    issue_md.write_text(
        "## Blast Radius (infigraph)\n"
        "- `src/app/handler.py` and `README.md` checked via `infigraph file-deps`.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Skill reviews completed: `tdd`, `code-review`, `writing-for-agents`.\n"
        "- [ ] Documentation: N/A — no doc changes beyond README.md.\n",
        encoding="utf-8",
    )
    ok, msg = issue_lint.lint_issue(tmp_path, file_path=issue_md)
    assert ok, msg


def test_lint_issue_cli_fetches_via_gh_and_exits_one_on_failure(tmp_path: Path) -> None:
    """CLI `lint-issue --file` exits 0 on valid issue and 1 on incomplete issue."""
    _write_required_skills(tmp_path)
    bad_md = tmp_path / "bad.md"
    bad_md.write_text("Touch `src/core.py` only.\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", "lint-issue", "--cwd", str(tmp_path), "--file", str(bad_md)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=_clean_git_env(),
    )
    assert proc.returncode == 1
    assert "Pre-ready-for-agent lint failed" in proc.stderr

    gh_payload = {
        "title": "feat: update handler",
        "body": "Blast radius checked with infigraph for `src/core.py`.\nSkills: tdd, code-review.\nDocumentation: README.md updated.",
        "labels": [{"name": "ready-for-agent"}],
    }
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(gh_payload), stderr="")):
        ok, msg = issue_lint.lint_issue(tmp_path, issue_num=14)
    assert ok, msg
