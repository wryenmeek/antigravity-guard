"""Native git-history skill adoption and promotion auditing (`antigravity-guard audit-skills`)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from antigravity_guard.skill_receipts import (
    _clean_env,
    _normalize_skill_name,
    _pattern_matches,
    _resolve_repo_root,
    validate_required_skills_config,
)

_VALIDATED_LINE_RE = re.compile(r"^validated-by:\s*(?P<skill>\S+)", re.IGNORECASE | re.MULTILINE)
_SESSION_TRAILER_RE = re.compile(r"^Antigravity-Session-ID:\s*\S+", re.IGNORECASE | re.MULTILINE)
_UNBOUNDED_GLOBS = frozenset({"*", "**", "**/*", "./**/*"})


def _parse_recent_commits(repo_root: Path, limit: int) -> list[dict[str, Any]]:
    """Collects recent commit SHAs, bodies, and modified file lists in one git call."""
    res = subprocess.run(
        ["git", "-C", str(repo_root), "log", f"-n{limit}", "--name-only", "--format=__AGY_COMMIT__%n%H%x1f%B%x1f"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=_clean_env(),
    )
    if res.returncode != 0:
        return []

    commits: list[dict[str, Any]] = []
    for chunk in res.stdout.split("__AGY_COMMIT__\n"):
        if not chunk.strip() or "\x1f" not in chunk:
            continue
        parts = chunk.split("\x1f", 2)
        if len(parts) < 3:
            continue
        sha, body, files_raw = parts[0].strip(), parts[1], parts[2]
        files = [line.strip() for line in files_raw.splitlines() if line.strip()]
        validated = {_normalize_skill_name(m.group("skill")) for m in _VALIDATED_LINE_RE.finditer(body)}
        has_session = bool(_SESSION_TRAILER_RE.search(body))
        commits.append({"sha": sha[:8], "files": files, "validated": validated, "has_session": has_session})
    return commits


def _evaluate_rule(rule: dict[str, Any], commits: list[dict[str, Any]]) -> dict[str, Any]:
    """Computes adoption rate, override count, glob tightness, and promotion/health status for one rule."""
    pattern = str(rule.get("pattern") or "").strip()
    enforcement = str(rule.get("enforcement", "gate")).lower()
    req_skills = [str(s).strip() for s in rule.get("required_skills", []) if str(s).strip()]
    norm_req = {_normalize_skill_name(s) for s in req_skills}

    matching = [c for c in commits if any(_pattern_matches(f, pattern) for f in c["files"])]
    validated_count = sum(1 for c in matching if norm_req and norm_req.issubset(c["validated"]))
    overrides = sum(1 for c in matching if not c["has_session"] and not norm_req.issubset(c["validated"]))
    total = len(matching)
    adoption_rate = round((validated_count / total) * 100.0, 1) if total > 0 else 100.0
    tight_glob = pattern not in _UNBOUNDED_GLOBS

    promotion_ready = (
        enforcement == "advisory" and total >= 3 and adoption_rate >= 80.0 and overrides == 0 and tight_glob
    )
    gate_healthy = enforcement != "gate" or ((total == 0 or adoption_rate >= 80.0) and overrides == 0 and tight_glob)
    return {
        "pattern": pattern,
        "enforcement": enforcement,
        "required_skills": req_skills,
        "matching_commits": total,
        "validated_commits": validated_count,
        "adoption_rate_pct": adoption_rate,
        "manual_overrides": overrides,
        "tight_glob": tight_glob,
        "promotion_ready": promotion_ready,
        "gate_healthy": gate_healthy,
    }


def format_audit_report(report: dict[str, Any]) -> str:
    """Formats an `audit_skills` report dictionary as human-readable text."""
    lines = [f"Skill Governance Audit (window={report['limit']} commits, analyzed={report['commits_analyzed']}):"]
    for entry in report["rules"]:
        status = "HEALTHY" if entry["gate_healthy"] else "DEGRADED"
        if entry["enforcement"] == "advisory":
            status = "PROMOTION_READY" if entry["promotion_ready"] else "ADVISORY"
        lines.append(
            f"  - [{entry['enforcement']}:{status}] {entry['pattern']} -> {', '.join(entry['required_skills'])}: "
            f"{entry['validated_commits']}/{entry['matching_commits']} ({entry['adoption_rate_pct']}%), "
            f"overrides={entry['manual_overrides']}, tight_glob={entry['tight_glob']}"
        )
    return "\n".join(lines)


def audit_skills(
    cwd: str | Path | None = None,
    *,
    limit: int = 30,
    check_promotion: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Audits `.github/required-skills.json` adoption across recent git commits."""
    if limit < 1:
        return False, {
            "opted_in": True,
            "limit": limit,
            "commits_analyzed": 0,
            "rules": [],
            "errors": ["--limit must be >= 1."],
        }
    repo_root = _resolve_repo_root(cwd)
    config_file = repo_root / ".github" / "required-skills.json"
    if not config_file.is_file():
        return True, {"opted_in": False, "limit": limit, "commits_analyzed": 0, "rules": [], "errors": []}

    config_errors = validate_required_skills_config(repo_root)
    if config_errors:
        return False, {"opted_in": True, "limit": limit, "commits_analyzed": 0, "rules": [], "errors": config_errors}

    rules = json.loads(config_file.read_text(encoding="utf-8")).get("rules", [])
    commits = _parse_recent_commits(repo_root, limit)
    results = [_evaluate_rule(rule, commits) for rule in rules]

    passed = True
    if check_promotion:
        passed = all(r["gate_healthy"] for r in results)

    return passed, {
        "opted_in": True,
        "limit": limit,
        "commits_analyzed": len(commits),
        "rules": results,
        "errors": [],
    }


_DEFAULT_REQUIRED_SKILLS: dict[str, Any] = {
    "rules": [
        {"pattern": "src/**/*.py", "required_skills": ["ce-code-review", "ce-simplify-code"], "enforcement": "gate"},
        {"pattern": "*.md", "required_skills": ["ce-doc-review"], "enforcement": "advisory"},
    ]
}


def init_required_skills(cwd: str | Path | None = None, *, force: bool = False) -> tuple[bool, str]:
    """Creates a starter `.github/required-skills.json` configuration in `cwd`."""
    repo_root = _resolve_repo_root(cwd)
    target = repo_root / ".github" / "required-skills.json"
    if target.exists() and not force:
        return False, f"{target} already exists (pass --force to overwrite)."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_DEFAULT_REQUIRED_SKILLS, indent=2) + "\n", encoding="utf-8")
    return True, f"Created {target}."
