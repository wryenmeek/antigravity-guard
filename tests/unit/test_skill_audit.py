"""Unit tests for `.github/required-skills.json` schema validation and `audit-skills` CLI (#12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from antigravity_guard import skill_audit, skill_receipts
from tests.conftest import GitWorktreeFixture, _clean_git_env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, timeout=15, env=_clean_git_env())


def _run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "antigravity_guard.cli", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=_clean_git_env(),
    )


def test_validate_required_skills_config_catches_malformed_rules(tmp_path: Path) -> None:
    """Rejects unbalanced braces, missing required_skills, and invalid enforcement values."""
    cfg = tmp_path / ".github" / "required-skills.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "rules": [
                    {"pattern": "src/**/*.{js,ts", "required_skills": ["tdd"], "enforcement": "gate"},
                    {"pattern": "*.py", "required_skills": [], "enforcement": "bogus"},
                ]
            }
        ),
        encoding="utf-8",
    )
    errors = skill_receipts.validate_required_skills_config(tmp_path)
    assert any("brace expansion" in e for e in errors)
    assert any("required_skills" in e for e in errors)
    assert any("invalid enforcement" in e for e in errors)

    ok, msg = skill_receipts.verify_skills(tmp_path)
    assert not ok
    assert "Invalid .github/required-skills.json" in msg

    bad_cli = _run_cli(["audit-skills", "--cwd", str(tmp_path)])
    assert bad_cli.returncode == 1
    assert "ERROR:" in bad_cli.stderr

    # In-process coverage for malformed JSON, non-dict config, non-dict rule, missing pattern
    cfg.write_text("{bad", encoding="utf-8")
    assert "Malformed" in skill_receipts.validate_required_skills_config(tmp_path)[0]
    cfg.write_text(json.dumps({"rules": "not-a-list"}), encoding="utf-8")
    assert "rules" in skill_receipts.validate_required_skills_config(tmp_path)[0]
    cfg.write_text(json.dumps(["bad"]), encoding="utf-8")
    assert "rules" in skill_receipts.validate_required_skills_config(tmp_path)[0]
    cfg.write_text(json.dumps({"rules": ["not-a-dict", {"pattern": ""}]}), encoding="utf-8")
    errs = skill_receipts.validate_required_skills_config(tmp_path)
    assert any("JSON object" in e for e in errs)
    assert any("missing a non-empty `pattern`" in e for e in errs)
    assert not skill_audit.audit_skills(tmp_path)[0]


def test_audit_skills_computes_adoption_and_promotion(git_worktree_env: GitWorktreeFixture) -> None:
    """Computes adoption rates from `validated-by:` trailers and checks promotion/health."""
    repo = git_worktree_env.repo_dir

    # Non-opted-in repo passes cleanly
    unopted_text = _run_cli(["audit-skills", "--cwd", str(repo)])
    assert unopted_text.returncode == 0
    assert "not opted in" in unopted_text.stdout.lower()
    unopted_res = _run_cli(["audit-skills", "--cwd", str(repo), "--json"])
    assert unopted_res.returncode == 0
    assert json.loads(unopted_res.stdout)["opted_in"] is False

    cfg = repo / ".github" / "required-skills.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "rules": [
                    {"pattern": "src/**/*.py", "required_skills": ["tdd"], "enforcement": "gate"},
                    {"pattern": "docs/**/*.md", "required_skills": ["writing-for-agents"], "enforcement": "advisory"},
                ]
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".github/required-skills.json")
    _git(repo, "commit", "-m", "chore: add required-skills")

    # 3 validated advisory docs commits -> promotion_ready=True
    docs_dir = repo / "docs"
    docs_dir.mkdir()
    for i in range(3):
        (docs_dir / f"d{i}.md").write_text(f"# Doc {i}\n", encoding="utf-8")
        _git(repo, "add", f"docs/d{i}.md")
        _git(
            repo,
            "commit",
            "-m",
            f"docs: add d{i}",
            "-m",
            "validated-by: writing-for-agents at 2026-11-26T00:00:00Z (sessionId s1, resultType read_file)\n"
            "Antigravity-Session-ID: aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )

    # 1 unvalidated gate commit without session ID -> gate degraded (0% adoption, 1 override)
    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/mod.py")
    _git(repo, "commit", "-m", "feat: unvalidated src change")

    ok_pass, report = skill_audit.audit_skills(repo, limit=10, check_promotion=False)
    assert ok_pass
    assert report["rules"][0]["gate_healthy"] is False
    assert report["rules"][0]["manual_overrides"] == 1
    assert report["rules"][1]["promotion_ready"] is True
    assert report["rules"][1]["adoption_rate_pct"] == 100.0

    # --check-promotion exits 1 because active gate degraded
    fail_res = _run_cli(["audit-skills", "--cwd", str(repo), "--limit", "10", "--check-promotion"])
    assert fail_res.returncode == 1
    assert "DEGRADED" in fail_res.stdout
    assert "PROMOTION_READY" in fail_res.stdout

    # In-process CLI + report formatting coverage
    import argparse

    from antigravity_guard import cli

    assert "DEGRADED" in skill_audit.format_audit_report(report)
    assert cli._run_audit_skills(argparse.Namespace(cwd=repo, limit=10, check_promotion=True, as_json=False)) == 1
    assert cli._run_audit_skills(argparse.Namespace(cwd=repo, limit=10, check_promotion=False, as_json=True)) == 0
    assert (
        cli._run_audit_skills(argparse.Namespace(cwd=repo.parent, limit=10, check_promotion=False, as_json=False)) == 0
    )


def test_init_skills_creates_and_protects_config(tmp_path: Path) -> None:
    """init-skills creates starter .github/required-skills.json and refuses overwrite without --force."""
    import argparse

    from antigravity_guard import cli

    assert cli._run_init_skills(argparse.Namespace(cwd=tmp_path, force=False)) == 0
    cfg = tmp_path / ".github" / "required-skills.json"
    assert cfg.is_file()
    assert skill_receipts.validate_required_skills_config(tmp_path) == []
    assert cli._run_init_skills(argparse.Namespace(cwd=tmp_path, force=False)) == 1
    assert cli._run_init_skills(argparse.Namespace(cwd=tmp_path, force=True)) == 0
