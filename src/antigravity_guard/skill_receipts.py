"""Skill execution receipt capture (PostToolUse) and freshness verification."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from antigravity_guard.git_worktree import _GIT_HOOK_ENV_VARS, resolve_trusted_cwd, resolve_worktree_root

_SKILL_PATH_RE = re.compile(r"(?:^|/)(?:(?:\.github|\.agent|\.agents)/skills|skills)/([^/]+)/SKILL\.md$")
_ROOT_SKILL_RE = re.compile(r"(?:^|/)SKILL\.md$")
_INVOKE_SKILL_RE = re.compile(r"(?:^|[;&|]\s*)(?:bash\s+)?((?:[\w./-]*/)?invoke-skill\.sh)\s+([a-zA-Z0-9_-]+)")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*['\"]?([a-zA-Z0-9_.-]+)['\"]?\s*$", re.MULTILINE)
_VALIDATED_BY_RE = re.compile(
    r"^validated-by:\s+(?P<skill>\S+)\s+at\s+(?P<ts>\S+)\s+\(sessionId\s+(?P<sid>\S+),\s+resultType\s+(?P<rtype>\S+)\)$",
    re.IGNORECASE,
)


def _clean_env() -> dict[str, str]:
    """Removes outer git hook environment overrides so git -C targets the requested repo."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_ENV_VARS}


def _resolve_repo_root(cwd: str | Path | None) -> Path:
    """Resolves canonical active worktree/checkout root from an optional path."""
    if cwd is None:
        return Path.cwd().resolve()
    trusted = resolve_trusted_cwd(str(cwd))
    base = Path(trusted) if trusted else Path(str(cwd)).resolve()
    wt_root = resolve_worktree_root(str(base))
    return Path(wt_root).resolve() if wt_root else base.resolve()


def _resolve_git_dir(repo_root: Path) -> Path:
    """Returns canonical .git directory (or linked worktree gitdir) for storing SKILL_FOOTERS."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_clean_env(),
        )
        if res.returncode == 0 and res.stdout.strip():
            raw = Path(res.stdout.strip())
            return raw.resolve() if raw.is_absolute() else (repo_root / raw).resolve()
    except Exception:
        return (repo_root / ".git").resolve()
    return (repo_root / ".git").resolve()


def _parse_frontmatter_skill_name(skill_file: Path, fallback: str) -> str:
    """Extracts YAML frontmatter `name:` from a SKILL.md if present."""
    if not skill_file.is_file():
        return fallback
    with contextlib.suppress(OSError):
        head = "\n".join(skill_file.read_text(encoding="utf-8", errors="ignore").splitlines()[:25])
        match = _FRONTMATTER_NAME_RE.search(head)
        if match:
            return match.group(1)
    return fallback


def _is_allowed_skill_file(target: Path, repo_root: Path) -> bool:
    """Verifies a SKILL.md path resides inside the repo or local Antigravity skills roots."""
    resolved_repo = repo_root.resolve()
    allowed_roots = [
        resolved_repo,
        Path(os.path.expanduser("~/.gemini")).resolve(),
        Path(os.path.expanduser("~/.agents")).resolve(),
        Path(os.path.expanduser("~/.agent")).resolve(),
    ]
    return any(target.is_relative_to(root) for root in allowed_roots)


def extract_skill_from_read(args: dict[str, Any], repo_root: Path) -> tuple[str | None, Path | None]:
    """Extracts `(skill_name, skill_path)` from `read_file` or `view_file` args."""
    raw = str(args.get("AbsolutePath") or args.get("Path") or args.get("path") or "").strip()
    if not raw:
        return None, None
    path_match = _SKILL_PATH_RE.search(raw)
    root_match = _ROOT_SKILL_RE.search(raw) if path_match is None else None
    if path_match is None and root_match is None:
        return None, None

    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
    if not _is_allowed_skill_file(resolved, repo_root):
        return None, None

    if path_match is not None:
        return path_match.group(1), resolved
    if resolved == (repo_root / "SKILL.md").resolve():
        return _parse_frontmatter_skill_name(resolved, repo_root.name), resolved
    return None, None


def extract_skill_from_command(args: dict[str, Any], repo_root: Path) -> tuple[str | None, Path | None]:
    """Extracts `(skill_name, script_path)` from canonical `scripts/invoke-skill.sh` calls."""
    cmd = str(args.get("CommandLine") or args.get("command") or "")
    match = _INVOKE_SKILL_RE.search(cmd)
    if not match:
        return None, None
    script_rel, skill_name = match.group(1), match.group(2)
    raw_cwd = args.get("Cwd") or args.get("cwd")
    base_dir = Path(str(raw_cwd)).resolve() if raw_cwd else repo_root.resolve()
    script_path = Path(script_rel)
    resolved_script = script_path.resolve() if script_path.is_absolute() else (base_dir / script_path).resolve()
    canonical = (repo_root.resolve() / "scripts" / "invoke-skill.sh").resolve()
    if resolved_script != canonical:
        return None, None
    return skill_name, resolved_script


def _extract_receipt_event(tool_name: str, args: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    """Identifies skill or subagent activation metadata from one PostToolUse tool call."""
    if tool_name in {"read_file", "view_file"}:
        skill, skill_path = extract_skill_from_read(args, repo_root)
        return {"skill": skill, "path": skill_path} if skill else None
    if tool_name == "run_command":
        skill, script_path = extract_skill_from_command(args, repo_root)
        return {"skill": skill, "path": script_path} if skill else None
    if tool_name == "invoke_subagent":
        type_name = str(args.get("TypeName") or "subagent").strip()
        role = str(args.get("Role") or "").strip()
        skill_label = f"subagent:{type_name}"
        return {"skill": skill_label, "path": None, "typeName": type_name, "role": role}
    return None


def _append_jsonl_line(target: Path, record: dict[str, Any]) -> None:
    """Appends one JSON line to a local receipt file cleanly."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.write_text(f"{existing}{json.dumps(record, separators=(',', ':'))}\n", encoding="utf-8")


def _append_footer_line(git_dir: Path, footer_line: str) -> None:
    """Appends a validated-by footer to `<git-dir>/SKILL_FOOTERS` without duplicates."""
    footer_file = git_dir / "SKILL_FOOTERS"
    footer_file.parent.mkdir(parents=True, exist_ok=True)
    existing = footer_file.read_text(encoding="utf-8").splitlines() if footer_file.is_file() else []
    if footer_line not in existing:
        existing.append(footer_line)
        footer_file.write_text("\n".join(existing) + "\n", encoding="utf-8")


def _file_sha256(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_skill_receipt(
    *,
    skill: str,
    tool_name: str,
    repo_root: Path,
    session_id: str,
    artifact_path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persists a skill receipt to repo state JSONL, `<git-dir>/SKILL_FOOTERS`, and `$PLUGIN_DATA`."""
    iso_ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record: dict[str, Any] = {
        "skill": skill,
        "tool": tool_name,
        "timestamp": iso_ts,
        "epoch": time.time(),
        "sessionId": session_id,
        "repoRoot": str(repo_root.resolve()),
        "artifactSha256": _file_sha256(artifact_path),
    }
    if extra:
        record.update(extra)

    repo_jsonl = repo_root / ".github" / "hooks" / "state" / "skill-receipts.jsonl"
    _append_jsonl_line(repo_jsonl, record)

    plugin_data = os.getenv("PLUGIN_DATA", "").strip()
    if plugin_data:
        _append_jsonl_line(Path(plugin_data).resolve() / "skill-receipts.jsonl", record)

    if not skill.startswith("subagent:"):
        footer = f"validated-by: {skill} at {iso_ts} (sessionId {session_id or 'unknown'}, resultType {tool_name})"
        _append_footer_line(_resolve_git_dir(repo_root), footer)
    return record


def capture_post_tool_skill_receipt(payload: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    """Evaluates a PostToolUse hook payload and writes a matching skill/subagent receipt."""
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "")
    raw_args = tool_call.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    event = _extract_receipt_event(tool_name, args, repo_root)
    if event is None:
        return None

    extra: dict[str, Any] = {}
    if event.get("typeName"):
        extra["typeName"] = event["typeName"]
    if event.get("role"):
        extra["role"] = event["role"]
    return record_skill_receipt(
        skill=str(event["skill"]),
        tool_name=tool_name,
        repo_root=repo_root,
        session_id=str(payload.get("conversationId") or ""),
        artifact_path=event.get("path"),
        extra=extra,
    )


def _read_receipt_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parses valid JSON objects from a skill-receipts.jsonl file."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def load_skill_receipts(repo_root: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    """Loads repo skill receipts first, falling back to `$PLUGIN_DATA` filtered by repo and session."""
    resolved_repo = repo_root.resolve()
    repo_rows = _read_receipt_jsonl(resolved_repo / ".github" / "hooks" / "state" / "skill-receipts.jsonl")
    plugin_data = os.getenv("PLUGIN_DATA", "").strip()
    plugin_rows: list[dict[str, Any]] = []
    if plugin_data:
        for row in _read_receipt_jsonl(Path(plugin_data).resolve() / "skill-receipts.jsonl"):
            row_repo = str(row.get("repoRoot") or "")
            if row_repo and Path(row_repo).resolve() != resolved_repo:
                continue
            if session_id and str(row.get("sessionId") or "") != session_id:
                continue
            plugin_rows.append(row)
    return [*repo_rows, *plugin_rows]


def collect_pending_footers(repo_root: Path, session_id: str | None = None) -> list[str]:
    """Reads validated-by footers from `<git-dir>/SKILL_FOOTERS` for commit injection."""
    footer_file = _resolve_git_dir(repo_root) / "SKILL_FOOTERS"
    if not footer_file.is_file():
        return []
    footers: list[str] = []
    for raw_line in footer_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        match = _VALIDATED_BY_RE.match(line)
        if not match:
            continue
        if session_id and match.group("sid") != session_id:
            continue
        if line not in footers:
            footers.append(line)
    return footers


def _git_files(repo_root: Path, staged_only: bool) -> list[str]:
    """Returns staged paths (excluding deletions) or all tracked files inside `repo_root`."""
    args = ["diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged_only else ["ls-files"]
    res = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=_clean_env(),
    )
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _normalize_skill_name(name: str) -> str:
    """Normalizes skill identifiers so kebab-case and snake_case match equivalently."""
    return name.strip().replace("-", "_").lower()


def _expand_braces(pattern: str) -> list[str]:
    """Expands bash-style `{a,b}` brace alternatives in a glob pattern."""
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for alt in match.group(1).split(","):
        expanded.extend(_expand_braces(f"{prefix}{alt.strip()}{suffix}"))
    return expanded


def _match_single_pattern(rel_path: str, pattern: str) -> bool:
    """Matches a single expanded glob pattern against a repository-relative POSIX path."""
    if PurePosixPath(rel_path).match(pattern) or fnmatch(rel_path, pattern):
        return True
    if "**/" in pattern and _match_single_pattern(rel_path, pattern.replace("**/", "", 1)):
        return True
    if "/" not in pattern:
        return fnmatch(PurePosixPath(rel_path).name, pattern)
    return False


def _pattern_matches(rel_path: str, pattern: str) -> bool:
    """Matches repository relative paths against glob rules (`*.py`, `src/**/*.{js,ts}`, etc.)."""
    return any(_match_single_pattern(rel_path, expanded) for expanded in _expand_braces(pattern))


def _file_timestamp(repo_root: Path, rel_path: str, *, staged_only: bool) -> float:
    """Returns filesystem mtime for staged checks, or git commit timestamp for non-staged/CI checks."""
    if not staged_only:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_clean_env(),
        )
        if res.returncode == 0 and res.stdout.strip().isdigit():
            return float(res.stdout.strip())
    return (repo_root / rel_path).stat().st_mtime


def _required_skill_mtimes(
    repo_root: Path,
    files: list[str],
    rules: list[dict[str, Any]],
    *,
    staged_only: bool = True,
) -> dict[str, float]:
    """Maps each required non-advisory skill name to the newest timestamp of matching changed files."""
    required: dict[str, float] = {}
    for rule in rules:
        if str(rule.get("enforcement", "gate")).lower() == "advisory":
            continue
        pattern = str(rule.get("pattern") or "")
        matched_mtimes = [
            _file_timestamp(repo_root, f, staged_only=staged_only)
            for f in files
            if pattern and _pattern_matches(f, pattern) and (repo_root / f).is_file()
        ]
        if not matched_mtimes:
            continue
        newest = max(matched_mtimes)
        for skill in rule.get("required_skills", []):
            name = str(skill).strip()
            if name:
                required[name] = max(required.get(name, 0.0), newest)
    return required


def _has_balanced_braces(pattern: str) -> bool:
    """Checks that `{...}` brace groups in a glob pattern are balanced and non-empty."""
    depth = 0
    for ch in pattern:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and "{}" not in pattern


def _validate_single_rule(idx: int, rule: Any) -> list[str]:
    """Validates one `.github/required-skills.json` rule entry."""
    if not isinstance(rule, dict):
        return [f"Rule #{idx} must be a JSON object."]
    errors: list[str] = []
    pattern = str(rule.get("pattern") or "").strip()
    if not pattern:
        errors.append(f"Rule #{idx} is missing a non-empty `pattern`.")
    elif not _has_balanced_braces(pattern):
        errors.append(f"Rule #{idx} has malformed or empty brace expansion in pattern {pattern!r}.")
    skills = rule.get("required_skills")
    if not isinstance(skills, list) or not [s for s in skills if str(s).strip()]:
        errors.append(f"Rule #{idx} ({pattern or 'unknown'}) must define a non-empty `required_skills` list.")
    enforcement = str(rule.get("enforcement", "gate")).lower()
    if enforcement not in {"gate", "advisory"}:
        errors.append(
            f"Rule #{idx} ({pattern or 'unknown'}) has invalid enforcement {enforcement!r} (expected 'gate' or 'advisory')."
        )
    return errors


def validate_required_skills_config(repo_root: Path) -> list[str]:
    """Validates `.github/required-skills.json` schema and pathspec syntax."""
    config_file = repo_root / ".github" / "required-skills.json"
    if not config_file.is_file():
        return []
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Malformed .github/required-skills.json: {exc}"]
    if not isinstance(cfg, dict) or not isinstance(cfg.get("rules"), list):
        return [".github/required-skills.json must be a JSON object with a `rules` array."]
    errors: list[str] = []
    for idx, rule in enumerate(cfg["rules"], start=1):
        errors.extend(_validate_single_rule(idx, rule))
    return errors


def _collect_missing_or_stale_skills(
    required: dict[str, float],
    receipts: list[dict[str, Any]],
) -> list[str]:
    """Compares required skill timestamps against recorded receipts."""
    missing_or_stale: list[str] = []
    for skill_name, newest_mtime in sorted(required.items()):
        norm_required = _normalize_skill_name(skill_name)
        matching = [
            float(r.get("epoch", 0.0))
            for r in receipts
            if _normalize_skill_name(str(r.get("skill") or "")) == norm_required
        ]
        if not matching or max(matching) < newest_mtime:
            state = "stale receipt" if matching else "missing receipt"
            missing_or_stale.append(f"{skill_name} ({state})")
    return missing_or_stale


def verify_skills(
    cwd: str | Path | None = None,
    *,
    staged_only: bool = True,
    session_id: str | None = None,
) -> tuple[bool, str]:
    """Verifies `.github/required-skills.json` receipts exist and are fresher than staged edits."""
    repo_root = _resolve_repo_root(cwd)
    config_file = repo_root / ".github" / "required-skills.json"
    if not config_file.is_file():
        return True, f"Repository {repo_root.name} is not opted in (no .github/required-skills.json)."

    config_errors = validate_required_skills_config(repo_root)
    if config_errors:
        return False, "Invalid .github/required-skills.json:\n" + "\n".join(f"  - {e}" for e in config_errors)

    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    rules = cfg.get("rules", []) if isinstance(cfg, dict) else []
    files = _git_files(repo_root, staged_only=staged_only)
    required = _required_skill_mtimes(
        repo_root,
        files,
        rules if isinstance(rules, list) else [],
        staged_only=staged_only,
    )
    if not required:
        return True, "No staged files require skill execution receipts."

    receipts = load_skill_receipts(repo_root, session_id=session_id)
    missing_or_stale = _collect_missing_or_stale_skills(required, receipts)
    if missing_or_stale:
        return (
            False,
            f"Missing or stale skill receipts for: {', '.join(missing_or_stale)}. Read each required SKILL.md before committing.",
        )
    return True, f"Verified fresh skill receipts for: {', '.join(sorted(required))}."
