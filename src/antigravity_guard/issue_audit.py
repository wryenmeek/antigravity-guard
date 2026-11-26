"""Adversarial issue completion audit verification and Stop hook gate."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from antigravity_guard.skill_receipts import _clean_env, _git_files, _resolve_repo_root

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CLOSE_ISSUE_RE = re.compile(r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)\b")
_BOOTSTRAP_PREFIXES = ("tests/", ".infigraph/", "docs/", "references/")
_BOOTSTRAP_FILES = frozenset(
    {".github/required-skills.json", ".github/labeler.yml", "AGENTS.md", "README.md", "SKILL.md", "CONTRIBUTING.md"}
)


def extract_closed_issue_numbers(text: str) -> list[int]:
    """Extracts unique integer issue IDs referenced by `Closes/Fixes/Resolves #<N>`."""
    seen: set[int] = set()
    nums: list[int] = []
    for raw in _CLOSE_ISSUE_RE.findall(text):
        num = int(raw)
        if num not in seen:
            seen.add(num)
            nums.append(num)
    return nums


def is_bootstrap_only_paths(paths: list[str]) -> bool:
    """Returns True when every path is test, audit ledger, config metadata, or Markdown docs."""
    if not paths:
        return False
    for path in paths:
        if path in _BOOTSTRAP_FILES or path.endswith(".md") or path.startswith(_BOOTSTRAP_PREFIXES):
            continue
        return False
    return True


def _entry_invoked_subagent(entry: dict[str, Any], subagent_uuid: str) -> bool:
    """Checks a single non-error transcript entry for an explicit `invoke_subagent` call/result."""
    for tc in entry.get("tool_calls", []):
        if isinstance(tc, dict) and tc.get("name") == "invoke_subagent" and subagent_uuid in json.dumps(tc).lower():
            return True
    tool_name = entry.get("tool") or entry.get("name")
    if tool_name == "invoke_subagent" and subagent_uuid in json.dumps(entry).lower():
        return "[ANTIGRAVITY ISSUE" not in str(entry.get("content", ""))
    return False


def _parent_has_subagent_invocation(parent_log: Path, subagent_uuid: str) -> bool:
    """Confirms `subagent_uuid` appears inside an `invoke_subagent` step in parent `transcriptPath`."""
    if not parent_log.is_file():
        return False
    for raw_line in parent_log.read_text(encoding="utf-8", errors="ignore").splitlines():
        if subagent_uuid not in raw_line.lower():
            continue
        with contextlib.suppress(json.JSONDecodeError):
            entry = json.loads(raw_line)
            if not isinstance(entry, dict) or str(entry.get("status", "")).upper() == "ERROR":
                continue
            if _entry_invoked_subagent(entry, subagent_uuid):
                return True
    return False


def _verify_subagent_transcript(subagent_id: Any, parent_transcript: Path | None) -> list[str]:
    """Validates strict UUIDv4 subagent ID, local transcript file, and parent invocation link (S2)."""
    if not isinstance(subagent_id, str) or not _UUID4_RE.match(subagent_id.strip().lower()):
        return [f"Invalid or non-UUIDv4 subagent_conversation_id: {subagent_id!r}"]
    clean_id = subagent_id.strip().lower()
    brain_root = Path(os.path.expanduser("~/.gemini/antigravity/brain")).resolve()
    sub_log = (brain_root / clean_id / ".system_generated" / "logs" / "transcript.jsonl").resolve()
    if not sub_log.is_relative_to(brain_root) or not sub_log.is_file():
        return [f"Missing Antigravity subagent transcript log for {clean_id} at {sub_log}"]
    if parent_transcript is not None and not _parent_has_subagent_invocation(parent_transcript, clean_id):
        return [f"Subagent {clean_id} was not invoked by active parent transcript {parent_transcript}"]
    return []


def _python_ast_has_symbol(py_file: Path, symbol: str) -> bool:
    """Checks via Python AST whether top-level function, class, or Class.method exists."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
    except (OSError, SyntaxError):
        return False
    cls_name, _, method_name = symbol.partition(".")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not method_name and node.name == symbol:
                return True
            if method_name and isinstance(node, ast.ClassDef) and node.name == cls_name:
                return any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name
                    for child in node.body
                )
    return False


def _symbol_exists_in_repo(repo_root: Path, rel_artifact: str, symbol: str) -> bool:
    """Checks symbol presence using AST for `.py` files and bounded regex/substring otherwise."""
    target = (repo_root.resolve() / rel_artifact).resolve()
    if not target.is_relative_to(repo_root.resolve()) or not target.is_file():
        return False
    if target.suffix == ".py":
        return _python_ast_has_symbol(target, symbol)
    return symbol in target.read_text(encoding="utf-8", errors="ignore")


def _verify_regression_test(repo_root: Path, test_ref: str) -> bool:
    """Verifies a `file.py::test_name` or `test_name` regression test exists via Python AST."""
    if "::" in test_ref:
        rel_path, fn_name = test_ref.split("::", 1)
        return _symbol_exists_in_repo(repo_root, rel_path, fn_name)
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return False
    return any(_python_ast_has_symbol(py_file, test_ref) for py_file in tests_dir.rglob("*.py"))


def _verify_finding(finding: dict[str, Any], repo_root: Path) -> list[str]:
    """Validates 100% finding adjudication (`VERIFIED`/`REJECTED`) and full remediation proof."""
    fid = finding.get("id", "unknown")
    adj = str(finding.get("adjudication") or "").upper()
    if adj not in {"VERIFIED", "REJECTED"}:
        return [f"Finding {fid} has unadjudicated state: {adj!r}"]
    if not str(finding.get("evidence") or "").strip():
        return [f"Finding {fid} missing supporting verification evidence"]
    if adj != "VERIFIED":
        return []

    errors: list[str] = []
    if str(finding.get("status") or "").upper() != "REMEDIATED":
        errors.append(f"VERIFIED finding {fid} is not REMEDIATED (status={finding.get('status')!r})")
    pre_sha = str(finding.get("pre_fix_sha256") or "")
    post_sha = str(finding.get("post_fix_sha256") or "")
    if not pre_sha or not post_sha or pre_sha == post_sha:
        errors.append(f"VERIFIED finding {fid} lacks distinct pre/post fix SHA-256 diff proof")
    reg_test = str(finding.get("regression_test") or "").strip()
    if reg_test and not _verify_regression_test(repo_root, reg_test):
        errors.append(f"VERIFIED finding {fid} regression test not found in AST: {reg_test}")
    return errors


def _verify_acceptance_criterion(ac: dict[str, Any], repo_root: Path) -> list[str]:
    """Validates every acceptance criterion is IMPLEMENTED with fresh hash and existing AST symbols."""
    ac_id = ac.get("id", "AC")
    status = str(ac.get("status") or "").upper()
    if status != "IMPLEMENTED":
        return [f"Acceptance criterion {ac_id} is not IMPLEMENTED (status={status!r})"]

    rel_artifact = str(ac.get("artifact") or "").strip()
    if not rel_artifact:
        return []
    artifact = (repo_root.resolve() / rel_artifact).resolve()
    if not artifact.is_relative_to(repo_root.resolve()) or not artifact.is_file():
        return [f"AC {ac_id} references missing artifact: {rel_artifact}"]

    errors: list[str] = []
    expected_sha = str(ac.get("artifact_sha256") or "").strip()
    if expected_sha:
        actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            errors.append(
                f"AC {ac_id} stale artifact_sha256 for {rel_artifact}: expected {actual_sha}, got {expected_sha}"
            )
    for sym in ac.get("symbols", []):
        if not _symbol_exists_in_repo(repo_root, rel_artifact, str(sym)):
            errors.append(f"AC {ac_id} missing AST symbol `{sym}` in {rel_artifact}")
    return errors


def _validate_subagent_field(
    ledger_name: str,
    sub_id: Any,
    verify_transcripts: bool,
    parent_transcript: Path | None,
) -> list[str]:
    if verify_transcripts:
        return _verify_subagent_transcript(sub_id, parent_transcript)
    if not isinstance(sub_id, str) or not _UUID4_RE.match(sub_id.strip().lower()):
        return [f"{ledger_name} has invalid UUIDv4 subagent_conversation_id: {sub_id!r}"]
    return []


def _validate_ledger_entries(ledger_name: str, data: dict[str, Any], repo_root: Path) -> list[str]:
    errors: list[str] = []
    findings = data.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                errors.extend(_verify_finding(finding, repo_root))

    criteria = data.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or not criteria:
        errors.append(f"{ledger_name} must contain non-empty acceptance_criteria")
    else:
        for ac in criteria:
            if isinstance(ac, dict):
                errors.extend(_verify_acceptance_criterion(ac, repo_root))
    return errors


def validate_audit_ledger(
    ledger_path: Path,
    repo_root: Path,
    *,
    allow_seeded: bool,
    verify_transcripts: bool,
    parent_transcript: Path | None,
) -> list[str]:
    """Validates one `.infigraph/reviews/issue_<N>_audit.json` ledger file."""
    if not ledger_path.is_file():
        return [f"Missing issue audit ledger: {ledger_path.relative_to(repo_root)}"]
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"Malformed JSON in {ledger_path.name}: {exc}"]
    if not isinstance(data, dict):
        return [f"Audit ledger {ledger_path.name} must be a JSON object"]

    phase = str(data.get("audit_phase") or "VERIFIED_COMPLETE").upper()
    if phase == "SEEDED_OPEN":
        return [] if allow_seeded else [f"{ledger_path.name} is still in `SEEDED_OPEN` phase"]
    if phase != "VERIFIED_COMPLETE":
        return [f"{ledger_path.name} has unknown audit_phase: {phase!r}"]

    return [
        *_validate_subagent_field(
            ledger_path.name, data.get("subagent_conversation_id"), verify_transcripts, parent_transcript
        ),
        *_validate_ledger_entries(ledger_path.name, data, repo_root),
    ]


def _find_default_merge_base(repo_root: Path) -> str | None:
    """Finds merge-base commit between HEAD and the repository's default branch, if any."""
    head_res = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=_clean_env(),
    )
    if head_res.returncode == 0 and head_res.stdout.strip() in {"main", "master"}:
        return "HEAD"
    for ref in ("main", "master", "origin/main", "origin/master"):
        res = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "HEAD", ref],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_clean_env(),
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


def _is_ledger_dirty_or_branch_changed(repo_root: Path, ledger: Path) -> bool:
    """Returns True if ledger is untracked/modified in working tree or changed on current branch."""
    rel = str(ledger.relative_to(repo_root))
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--", rel],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=_clean_env(),
    )
    if status.returncode == 0 and status.stdout.strip():
        return True
    merge_base = _find_default_merge_base(repo_root)
    if not merge_base:
        return False
    diff = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", f"{merge_base}..HEAD", "--", rel],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=_clean_env(),
    )
    return diff.returncode == 0 and bool(diff.stdout.strip())


def _read_ledger_phase(ledger: Path) -> str:
    """Reads `audit_phase` from a ledger file, returning `UNKNOWN` if unreadable."""
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("audit_phase") or "VERIFIED_COMPLETE").upper()
    except (OSError, json.JSONDecodeError):
        pass
    return "UNKNOWN"


def _select_target_ledgers(repo_root: Path, issue_num: int | None, closed_ids: list[int]) -> list[Path]:
    reviews_dir = repo_root / ".infigraph" / "reviews"
    if issue_num is not None:
        return [reviews_dir / f"issue_{issue_num}_audit.json"]
    if closed_ids:
        return [reviews_dir / f"issue_{num}_audit.json" for num in closed_ids]
    if not reviews_dir.is_dir():
        return []
    return [
        ledger
        for ledger in sorted(reviews_dir.glob("issue_*_audit.json"))
        if _read_ledger_phase(ledger) != "VERIFIED_COMPLETE" or _is_ledger_dirty_or_branch_changed(repo_root, ledger)
    ]


def verify_issue_audits(
    cwd: str | Path | None = None,
    *,
    issue_num: int | None = None,
    mode: str = "commit",
    commit_msg: str = "",
    transcript_path: str | Path | None = None,
    verify_transcripts: bool = True,
    extra_staged_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Enforces `.infigraph/reviews/issue_<N>_audit.json` completeness per 3-rule activation."""
    repo_root = _resolve_repo_root(cwd)
    closed_ids = extract_closed_issue_numbers(commit_msg)
    existing_ledgers = sorted((repo_root / ".infigraph" / "reviews").glob("issue_*_audit.json"))

    if mode != "strict" and issue_num is None and not existing_ledgers and not closed_ids:
        return True, f"Repository {repo_root.name} is not opted in to issue audits."

    staged = sorted(set(_git_files(repo_root, staged_only=True)) | set(extra_staged_paths or [])) if mode == "commit" else []
    if mode == "commit" and not closed_ids and ("Gate-3-Red: true" in commit_msg or is_bootstrap_only_paths(staged)):
        return True, "Bootstrap / Gate-3-Red commit allowed without closing issue audits."

    allow_seeded = mode == "commit" and not closed_ids and issue_num is None
    targets = _select_target_ledgers(repo_root, issue_num, closed_ids)
    if not targets:
        if existing_ledgers and issue_num is None and not closed_ids:
            return True, "All active issue audit ledgers verified (clean historical ledgers skipped)."
        return False, "Strict issue audit enabled, but no .infigraph/reviews/issue_<N>_audit.json files found."

    parent_log = Path(transcript_path).resolve() if transcript_path else None
    all_errors: list[str] = []
    for ledger in targets:
        all_errors.extend(
            validate_audit_ledger(
                ledger,
                repo_root,
                allow_seeded=allow_seeded,
                verify_transcripts=verify_transcripts,
                parent_transcript=parent_log,
            )
        )

    if all_errors:
        return False, "Issue completion audit failed:\n" + "\n".join(f"  - {e}" for e in all_errors)
    return True, f"Verified {len(targets)} issue audit ledger(s) cleanly."


def _session_commit_files(repo_root: Path, conv_id: str) -> list[str]:
    """Lists non-bootstrap files touched in recent commits carrying `Antigravity-Session-ID: <conv_id>`."""
    if not conv_id:
        return []
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "log",
            "-n",
            "20",
            f"--grep=Antigravity-Session-ID: {conv_id}",
            "--name-only",
            "--format=",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=_clean_env(),
    )
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def handle_stop_hook(stdin_text: str | None = None) -> dict[str, Any]:
    """Antigravity Stop hook reminding once if session commits leave SEEDED_OPEN/failing audits."""
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict) or payload.get("continuationActive"):
        return {}

    conv_id = str(payload.get("conversationId") or "").strip()
    raw_cwd = payload.get("cwd") or (payload.get("workspacePaths") or [None])[0]
    if not raw_cwd:
        return {}
    repo_root = _resolve_repo_root(str(raw_cwd))
    committed_files = _session_commit_files(repo_root, conv_id)
    if not committed_files or is_bootstrap_only_paths(committed_files):
        return {}

    ledgers = sorted((repo_root / ".infigraph" / "reviews").glob("issue_*_audit.json"))
    if not ledgers:
        return {}
    transcript = payload.get("transcriptPath")
    passed, detail = verify_issue_audits(
        repo_root,
        mode="strict",
        transcript_path=str(transcript) if transcript else None,
        verify_transcripts=True,
    )
    if passed:
        return {}
    return {
        "decision": "continue",
        "reason": f"[ANTIGRAVITY STOP GUARD]\nSession {conv_id} made production commits while issue audits remain incomplete:\n{detail}",
    }
