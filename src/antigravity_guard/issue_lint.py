"""Pre-`ready-for-agent` issue governance validator (`antigravity-guard lint-issue`)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from antigravity_guard.skill_receipts import _clean_env, _pattern_matches, _resolve_repo_root

_PATH_TOKEN_RE = re.compile(
    r"(?:^|[\s`\"'(])([a-zA-Z0-9_.\-]+(?:/[a-zA-Z0-9_.\-*{}]+)+(?:\.[a-zA-Z0-9]+)?|[a-zA-Z0-9_.\-]+\.[a-zA-Z0-9]{1,6})(?=$|[\s`\"'),:;])"
)
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+$")
_BLAST_RADIUS_RE = re.compile(
    r"\b(?:blast[\s\-_]?radius|infigraph|file-deps|ripple\s+effects?|impact)\b", re.IGNORECASE
)
_DOC_CRITERION_RE = re.compile(
    r"\b(?:documentation|coding_standards\.md|glossary\.md|readme\.md|docs/|adr|n/a\s*[—\-–:]\s*no\s+doc|no\s+doc(?:umentation)?\s+changes)\b",
    re.IGNORECASE,
)


def _normalize_skill(name: str) -> str:
    return name.strip().replace("-", "_").lower()


def _extract_referenced_paths(text: str) -> list[str]:
    """Extracts plausible repository-relative file paths or filenames from issue text."""
    seen: set[str] = set()
    paths: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(text):
        cleaned = raw.removeprefix("./").lstrip("/")
        if not cleaned or _VERSION_RE.match(cleaned) or cleaned.startswith("http"):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            paths.append(cleaned)
    return paths


def _matching_required_skills(repo_root: Path, paths: list[str]) -> list[str]:
    """Maps referenced paths against `.github/required-skills.json` (both gate and advisory rules)."""
    cfg_path = repo_root / ".github" / "required-skills.json"
    if not cfg_path.is_file() or not paths:
        return []
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules = cfg.get("rules", []) if isinstance(cfg, dict) else []
    matched: set[str] = set()
    for rule in rules if isinstance(rules, list) else []:
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        if any(_pattern_matches(p, pattern) for p in paths):
            for skill in rule.get("required_skills", []):
                name = str(skill).strip()
                if name:
                    matched.add(name)
    return sorted(matched)


def _fetch_issue_via_gh(issue_num: int, repo_root: Path, repo: str | None) -> dict[str, Any]:
    """Fetches issue title, body, and labels via `gh issue view`."""
    cmd = ["gh", "issue", "view", str(issue_num), "--json", "title,body,labels"]
    if repo:
        cmd.extend(["-R", repo])
    res = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=_clean_env(),
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"gh issue view #{issue_num} failed with exit {res.returncode}")
    data = json.loads(res.stdout)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected gh issue view payload for #{issue_num}")
    return data


def _skill_mentioned_in_body(skill: str, body_lower: str) -> bool:
    norm = _normalize_skill(skill)
    return skill.lower() in body_lower or norm in body_lower.replace("-", "_")


def lint_issue_body(body: str, repo_root: Path) -> tuple[bool, str]:
    """Validates blast-radius, required-skill, and documentation acceptance criteria for an issue."""
    has_skills_cfg = (repo_root / ".github" / "required-skills.json").is_file()
    has_infigraph = (repo_root / ".infigraph").is_dir()
    if not has_skills_cfg and not has_infigraph:
        return True, f"Repository {repo_root.name} is not opted in to Pre-ready-for-agent governance."

    errors: list[str] = []
    if not _BLAST_RADIUS_RE.search(body):
        errors.append("Missing blast-radius / `infigraph` verification section or note.")

    ref_paths = _extract_referenced_paths(body)
    matched_skills = _matching_required_skills(repo_root, ref_paths)
    body_lower = body.lower()
    missing_skills = [s for s in matched_skills if not _skill_mentioned_in_body(s, body_lower)]
    if missing_skills:
        errors.append(f"Missing acceptance criteria for matching required skill(s): {', '.join(missing_skills)}.")

    if not _DOC_CRITERION_RE.search(body):
        errors.append(
            "Missing documentation acceptance criterion (e.g. `CODING_STANDARDS.md`, `GLOSSARY.md`, `docs/`, `README.md`, or `N/A — no doc changes`)."
        )

    if errors:
        return False, "Pre-ready-for-agent lint failed:\n" + "\n".join(f"  - {e}" for e in errors)
    return True, f"Issue satisfies Pre-ready-for-agent governance gate (matched skills: {matched_skills or ['none']})."


def lint_issue(
    cwd: str | Path | None = None,
    *,
    issue_num: int | None = None,
    file_path: str | Path | None = None,
    repo: str | None = None,
) -> tuple[bool, str]:
    """Lints a GitHub issue (`--issue`) or local markdown file (`--file`) against governance rules."""
    repo_root = _resolve_repo_root(cwd)
    if file_path is not None:
        target = Path(file_path)
        if not target.is_file():
            return False, f"Issue markdown file not found: {target}"
        body = target.read_text(encoding="utf-8")
        return lint_issue_body(body, repo_root)

    if issue_num is None:
        return False, "Specify either --issue <N> or --file <PATH>."
    try:
        data = _fetch_issue_via_gh(issue_num, repo_root, repo)
    except Exception as exc:
        return False, str(exc)
    body = f"{data.get('title', '')}\n\n{data.get('body', '')}"
    return lint_issue_body(body, repo_root)
