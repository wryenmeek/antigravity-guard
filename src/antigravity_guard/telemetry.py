"""Token protobuf decoding, SQLite extraction, and PR telemetry verification/reporting."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECEIPT_START = "<!-- ANTIGRAVITY-TELEMETRY-RECEIPT-START"
RECEIPT_END = "ANTIGRAVITY-TELEMETRY-RECEIPT-END -->"
DEFAULT_TIMEOUT_SEC = 60

_METADATA_SQL = "SELECT idx, data FROM gen_metadata ORDER BY idx"
_SESSION_ID_RE = re.compile(r"Antigravity-Session-ID:\s*([a-f0-9\-]{36})", re.IGNORECASE)
_EXTERNAL_SESSION_RE = re.compile(
    r"^(?P<prefix>Claude|External)-Session-ID:\s*(?P<sid>[a-zA-Z0-9._\-]{4,128})(?:\s+\((?P<tool>[^)]+)\))?",
    re.IGNORECASE | re.MULTILINE,
)
_UUID_SAFE_RE = re.compile(r"^[a-f0-9\-]{8,64}$", re.IGNORECASE)
_DEFAULT_CONV_DIRS = (
    "~/.gemini/antigravity/conversations",
    "~/.gemini/antigravity-ide/conversations",
    "~/.gemini/antigravity-cli/conversations",
)


def _cli_mod() -> Any:
    """Resolves `antigravity_guard.cli` dynamically so `@patch.object` on CLI aliases intercepts calls."""
    return importlib.import_module("antigravity_guard.cli")


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Reads one base-128 protobuf varint starting at offset."""
    value = 0
    shift = 0
    pos = offset
    limit = len(data)
    while pos < limit:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return value, pos


def _append_wire_field(fields: dict[int, Any], field_num: int, val: bytes) -> None:
    """Stores repeated length-delimited fields as lists while keeping single fields scalar."""
    if field_num not in fields:
        fields[field_num] = val
        return
    if not isinstance(fields[field_num], list):
        fields[field_num] = [fields[field_num]]
    fields[field_num].append(val)


def parse_varints(data: bytes) -> dict[int, Any]:
    """Decodes top-level varint and length-delimited protobuf wire fields."""
    pos = 0
    fields: dict[int, Any] = {}
    limit = len(data)
    while pos < limit:
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            fields[field_num], pos = _read_varint(data, pos)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            val = data[pos : pos + length]
            pos += length
            _append_wire_field(fields, field_num, val)
        elif wire_type in {1, 5}:
            pos += 8 if wire_type == 1 else 4
        else:
            break
    return fields


def _find_conversation_db(cid: str, conv_dirs: tuple[str, ...] | None = None) -> Path | None:
    """Locates the SQLite conversation database for a validated session ID."""
    clean_cid = cid.strip()
    if not _UUID_SAFE_RE.match(clean_cid):
        return None
    dirs = conv_dirs or _DEFAULT_CONV_DIRS
    for raw_dir in dirs:
        candidate = Path(os.path.expanduser(raw_dir)) / f"{clean_cid}.db"
        if candidate.is_file():
            return candidate
    return None


def _fetch_metadata_rows(db_path: Path) -> list[tuple[Any, bytes]] | None:
    """Reads protobuf blobs from gen_metadata using a read-only SQLite connection."""
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            cursor = conn.cursor()
            cursor.execute(_METADATA_SQL)
            return list(cursor.fetchall())
    except sqlite3.Error as exc:
        sys.stderr.write(f"Warning: Failed to read {db_path}: {exc}\n")
        return None


def _decode_row_metrics(blob: bytes) -> tuple[int, int, int, int, str | None]:
    """Extracts token counts and optional model name from a single protobuf row."""
    top = parse_varints(blob)
    f1 = top.get(1)
    if not isinstance(f1, bytes):
        return 0, 0, 0, 0, None
    f1_fields = parse_varints(f1)
    model_raw = f1_fields.get(19)
    model = model_raw.decode("utf-8", errors="ignore") if isinstance(model_raw, bytes) else None
    f4 = f1_fields.get(4)
    if not isinstance(f4, bytes):
        return 0, 0, 0, 0, model
    f4_fields = parse_varints(f4)
    return (
        int(f4_fields.get(2, 0)),
        int(f4_fields.get(5, 0)),
        int(f4_fields.get(9, 0)),
        int(f4_fields.get(10, 0)),
        model,
    )


def get_session_telemetry(cid: str, conv_dirs: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """Extracts exact cached/uncached/thought/candidate token metrics from a session SQLite DB."""
    db_path = _find_conversation_db(cid, conv_dirs=conv_dirs)
    if db_path is None:
        return None
    rows = _fetch_metadata_rows(db_path)
    if rows is None:
        return None

    uncached = cached = thoughts = candidates = 0
    model_name = "gemini-3.8-flash-tiered"
    for _idx, data in rows:
        if not isinstance(data, bytes):
            continue
        u, c, th, ca, row_model = _decode_row_metrics(data)
        uncached += u
        cached += c
        thoughts += th
        candidates += ca
        if row_model:
            model_name = row_model

    output_total = thoughts + candidates
    gross_context = uncached + cached + output_total
    billed_75 = uncached + round(0.25 * cached) + output_total
    return {
        "cid": cid,
        "turns": len(rows),
        "model": model_name,
        "uncached_prompt": uncached,
        "cached_prompt": cached,
        "thoughts": thoughts,
        "candidates": candidates,
        "output_total": output_total,
        "gross_context": gross_context,
        "billed_tokens_75": billed_75,
    }


def calc_usd_cost(uncached: int, cached: int, output: int) -> float:
    """Gemini 3.8 Flash pricing: $0.30/M input, $0.075/M cached input, $1.20/M output."""
    return (uncached * 0.30 / 1_000_000) + (cached * 0.075 / 1_000_000) + (output * 1.20 / 1_000_000)


def extract_session_ids_from_text(text: str) -> list[str]:
    """Extracts unique lowercase `Antigravity-Session-ID: <UUID>` trailers in appearance order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw_match in _SESSION_ID_RE.findall(text):
        lower = raw_match.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


def extract_external_sessions_from_text(text: str) -> list[tuple[str, str]]:
    """Extracts unique `(session_id, source_tool)` pairs from `Claude-Session-ID:` / `External-Session-ID:` trailers."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for match in _EXTERNAL_SESSION_RE.finditer(text):
        sid = match.group("sid").strip().lower()
        if sid in seen:
            continue
        seen.add(sid)
        prefix = match.group("prefix").lower()
        explicit_tool = (match.group("tool") or "").strip()
        tool = explicit_tool or ("Claude Code" if prefix == "claude" else "External")
        result.append((sid, tool))
    return result


def _collect_pr_sessions(commits: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], set[str]]:
    """Collects (short_sha, session_id) pairs and the unique session ID set from PR commits."""
    committed_sessions: list[tuple[str, str]] = []
    all_cids: set[str] = set()
    for commit in commits:
        sha = str(commit.get("oid", ""))[:8]
        msg = f"{commit.get('messageHeadline', '')}\n{commit.get('messageBody', '')}"
        for cid in extract_session_ids_from_text(msg):
            committed_sessions.append((sha, cid))
            all_cids.add(cid)
    return committed_sessions, all_cids


def _collect_external_sessions(commits: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Collects external-tool session attestations (`sha`, `session_id`, `tool`) from PR commits."""
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for commit in commits:
        sha = str(commit.get("oid", ""))[:8]
        msg = f"{commit.get('messageHeadline', '')}\n{commit.get('messageBody', '')}"
        for sid, tool in extract_external_sessions_from_text(msg):
            key = (sha, sid)
            if key not in seen:
                seen.add(key)
                entries.append({"sha": sha, "session_id": sid, "tool": tool})
    return entries


def _is_trusted_receipt_comment(comment: Any) -> bool:
    """Checks whether a PR comment was authored by a trusted collaborator or GitHub Actions bot."""
    if not isinstance(comment, dict):
        return True
    association = str(comment.get("authorAssociation") or "").upper()
    author_obj = comment.get("author") or {}
    author = str(author_obj.get("login", "")) if isinstance(author_obj, dict) else ""
    return association in {"OWNER", "MEMBER", "COLLABORATOR"} or author in {
        "github-actions",
        "github-actions[bot]",
    }


def _extract_receipt_from_comments(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Returns the most recent authenticated JSON telemetry receipt from PR comments."""
    pattern = re.compile(re.escape(RECEIPT_START) + r"\s*(\{.*\})\s*" + re.escape(RECEIPT_END), re.DOTALL)
    for comment in reversed(comments):
        if not _is_trusted_receipt_comment(comment):
            continue
        body = comment.get("body", "") if isinstance(comment, dict) else str(comment)
        match = pattern.search(body)
        if not match:
            continue
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict) and "covered_session_ids" in parsed:
                return parsed
    return None


def _format_unresolved_threads_error(pr_num: int, unresolved: list[dict[str, Any]]) -> str:
    """Formats the fail-closed error report for open PR review conversation threads."""
    details = "\n".join(
        f"  - [{t.get('author')}] Thread {t.get('id')}: {t.get('url')}\n"
        f'    "{str(t.get("body", "")).replace(chr(10), " ")[:80]}..."'
        for t in unresolved
    )
    return (
        f"[ANTIGRAVITY MERGE GUARD FAILED: UNRESOLVED REVIEW COMMENTS]\n"
        f"PR #{pr_num} has {len(unresolved)} open unresolved review comment thread(s)!\n"
        f"{details}\n"
        f"Action Required: Resolve all review conversation threads on GitHub before merging."
    )


def _evaluate_receipt_coverage(
    pr_num: int,
    commits: list[dict[str, Any]],
    all_committed_cids: set[str],
    committed_sessions: list[tuple[str, str]],
    receipt_data: dict[str, Any] | None,
    head_sha: str | None,
    external_cids: set[str] | None = None,
) -> tuple[bool, str, dict[str, Any] | None, str | None]:
    """Validates committed Antigravity and external session coverage against the PR receipt payload."""
    ext_cids = external_cids or set()
    if not all_committed_cids and not ext_cids:
        if receipt_data:
            billed = receipt_data.get("total_billed_tokens", 0)
            return (
                True,
                f"[ANTIGRAVITY MERGE GUARD PASSED]\nPR #{pr_num} verified via Antigravity telemetry receipt (Billed: {billed:,}).",
                receipt_data,
                head_sha,
            )
        return True, f"No Antigravity session commits detected on PR #{pr_num}; guard passed.", None, head_sha

    if not receipt_data:
        err = (
            f"[ANTIGRAVITY MERGE GUARD FAILED: MISSING TELEMETRY REPORT]\n"
            f"PR #{pr_num} contains {len(all_committed_cids)} Antigravity session(s) across {len(committed_sessions)} commit(s),\n"
            f"but NO authenticated Antigravity Telemetry Report comment was found on the PR.\n"
            f"Committed Sessions: {sorted(all_committed_cids)}\n"
            f"Action Required: Run 'antigravity-guard post --pr {pr_num}' (or 'antigravity-telemetry post --pr {pr_num}') to post verified telemetry."
        )
        return False, err, None, head_sha

    covered_cids = {str(cid).lower() for cid in (receipt_data.get("covered_session_ids") or [])}
    covered_ext = {str(cid).lower() for cid in (receipt_data.get("external_session_ids") or [])}
    missing_cids = all_committed_cids - covered_cids
    missing_ext = ext_cids - covered_ext
    if missing_cids or missing_ext:
        err = (
            f"[ANTIGRAVITY MERGE GUARD FAILED: INCOMPLETE SESSION PROVENANCE]\n"
            f"PR #{pr_num} has commits with Session IDs that are NOT included in the PR telemetry report!\n"
            f"Missing Session IDs: {sorted(missing_cids | missing_ext)}\n"
            f"Covered Session IDs in Report: {sorted(covered_cids | covered_ext)}\n"
            f"Action Required: Re-run 'antigravity-guard post --pr {pr_num}' to aggregate all sessions."
        )
        return False, err, receipt_data, head_sha

    billed = receipt_data.get("total_billed_tokens", 0)
    cache_pct = receipt_data.get("cache_hit_pct", 0)
    msg = (
        f"[ANTIGRAVITY MERGE GUARD PASSED]\n"
        f"All {len(all_committed_cids)} committed session IDs verified across {len(commits)} PR commits.\n"
        f"Billed Tokens: {billed:,} | Cache Hit: {cache_pct}%"
    )
    return True, msg, receipt_data, head_sha


def verify_pr_telemetry(
    pr_num: int,
    repo: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    check_comments: bool = True,
) -> tuple[bool, str, dict[str, Any] | None, str | None]:
    """Validates PR review resolution and 100% Antigravity + external session provenance coverage."""
    cli = _cli_mod()
    try:
        commits, head_sha = cli.get_all_pr_commits_and_head(pr_num, repo, timeout=timeout)
    except Exception as exc:
        return False, f"Could not inspect PR commits: {exc}", None, None

    if check_comments:
        try:
            unresolved = cli.get_unresolved_review_threads(pr_num, repo=repo, timeout=timeout)
        except Exception as exc:
            return False, f"Could not inspect PR review threads: {exc}", None, head_sha
        if unresolved:
            return False, _format_unresolved_threads_error(pr_num, unresolved), None, head_sha

    committed_sessions, all_cids = _collect_pr_sessions(commits)
    external_cids = {e["session_id"] for e in _collect_external_sessions(commits)}
    try:
        comments = cli.get_pr_comments(pr_num, repo, timeout=timeout)
    except Exception as exc:
        return False, f"Could not inspect PR comments: {exc}", None, head_sha

    receipt_data = _extract_receipt_from_comments(comments)
    return _evaluate_receipt_coverage(
        pr_num, commits, all_cids, committed_sessions, receipt_data, head_sha, external_cids=external_cids
    )


def _aggregate_session_totals(covered_sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates billed tokens, prompt cache ratios, and estimated cost across session metrics."""
    if not covered_sessions:
        return {
            "uncached": 0,
            "cached": 0,
            "thoughts": 0,
            "candidates": 0,
            "output": 0,
            "gross": 0,
            "billed": 0,
            "turns": 0,
            "cost": 0.0,
            "savings_pct": 0.0,
            "cache_pct": 0.0,
            "model": "external",
        }
    uncached = sum(int(s["uncached_prompt"]) for s in covered_sessions)
    cached = sum(int(s["cached_prompt"]) for s in covered_sessions)
    thoughts = sum(int(s["thoughts"]) for s in covered_sessions)
    candidates = sum(int(s["candidates"]) for s in covered_sessions)
    output = sum(int(s["output_total"]) for s in covered_sessions)
    gross = sum(int(s["gross_context"]) for s in covered_sessions)
    billed = sum(int(s["billed_tokens_75"]) for s in covered_sessions)
    turns = sum(int(s["turns"]) for s in covered_sessions)
    cost = calc_usd_cost(uncached, cached, output)
    savings_pct = (1.0 - (billed / gross)) * 100.0 if gross > 0 else 0.0
    cache_pct = (cached / (uncached + cached) * 100.0) if (uncached + cached) > 0 else 0.0
    return {
        "uncached": uncached,
        "cached": cached,
        "thoughts": thoughts,
        "candidates": candidates,
        "output": output,
        "gross": gross,
        "billed": billed,
        "turns": turns,
        "cost": cost,
        "savings_pct": savings_pct,
        "cache_pct": cache_pct,
        "model": str(covered_sessions[-1].get("model", "gemini-3.8-flash-tiered")),
    }


def _build_external_attestation_section(external_sessions: list[dict[str, str]]) -> str:
    """Renders markdown attestation table for non-Antigravity/external tool commits."""
    if not external_sessions:
        return ""
    rows = "\n".join(f"| `{e['sha']}` | {e['tool']} | `{e['session_id']}` |" for e in external_sessions)
    return (
        "\n### External / Non-Antigravity Session Attestation\n\n"
        "| Commit | Source Tool | External Session ID |\n"
        "| :--- | :--- | :--- |\n"
        f"{rows}\n"
    )


def _build_telemetry_markdown(
    pr_num: int,
    repo_slug: str,
    covered_sessions: list[dict[str, Any]],
    totals: dict[str, Any],
    external_sessions: list[dict[str, str]] | None = None,
) -> str:
    """Renders deterministic markdown telemetry report and embedded JSON receipt without hardcoded data."""
    ext_list = external_sessions or []
    covered_ids = sorted({str(s["cid"]).lower() for s in covered_sessions})
    external_ids = sorted({e["session_id"] for e in ext_list})
    receipt_obj = {
        "version": "2.0",
        "pr": pr_num,
        "repo": repo_slug,
        "covered_session_ids": covered_ids,
        "external_session_ids": external_ids,
        "direct_session_count": len(covered_sessions),
        "external_session_count": len(ext_list),
        "total_billed_tokens": totals["billed"],
        "cache_hit_pct": round(float(totals["cache_pct"]), 1),
        "estimated_cost_usd": round(float(totals["cost"]), 2),
        "model": totals["model"],
        "verified_at": datetime.now(UTC).isoformat(),
    }
    receipt_block = f"{RECEIPT_START}\n{json.dumps(receipt_obj, indent=2)}\n{RECEIPT_END}"
    session_rows: list[str] = []
    for s in covered_sessions:
        tot_prompt = int(s["uncached_prompt"]) + int(s["cached_prompt"])
        hit_pct = (int(s["cached_prompt"]) / tot_prompt * 100.0) if tot_prompt > 0 else 0.0
        session_rows.append(
            f"| **{s.get('role', 'Agent')}**<br><sub>`{str(s['cid'])[:8]}`</sub> | `{s['model']}` | {s['turns']} | "
            f"{s['uncached_prompt']:,} | {s['cached_prompt']:,} | {hit_pct:.1f}% | "
            f"{s['thoughts']:,} | {s['candidates']:,} | **{s['billed_tokens_75']:,}** |"
        )
    rows_text = "\n".join(session_rows)
    ext_section = _build_external_attestation_section(ext_list)
    return (
        f"## Antigravity Session Token Caching & Billed Usage Report\n\n"
        f"Repository: `{repo_slug}` | PR: `#{pr_num}`\n\n"
        f"- **Active Model:** `{totals['model']}`\n"
        f"- **Prompt Cache Hit Rate:** **{totals['cache_pct']:.1f}%**\n\n"
        f"### Session Breakdown\n\n"
        f"| Session Role | Model | Turns | Uncached Prompt | Cached Prompt | Cache Hit % | Thinking Tokens | Output Tokens | Billed Tokens |\n"
        f"| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n"
        f"{rows_text}\n"
        f"| **Total** | `{totals['model']}` | **{totals['turns']}** | **{totals['uncached']:,}** | "
        f"**{totals['cached']:,}** | **{totals['cache_pct']:.1f}%** | **{totals['thoughts']:,}** | "
        f"**{totals['candidates']:,}** | **{totals['billed']:,}** |\n"
        f"{ext_section}\n"
        f"### Expenditure Summary\n\n"
        f"| Metric | Value |\n| :--- | :---: |\n"
        f"| **Uncached Prompt Tokens** | {totals['uncached']:,} |\n"
        f"| **Cached Prompt Tokens** | {totals['cached']:,} |\n"
        f"| **Output Tokens (Thinking + Gen)** | {totals['output']:,} |\n"
        f"| **Gross Context Evaluated** | {totals['gross']:,} |\n"
        f"| **Net Billed Tokens (75% Cache Discount)** | **{totals['billed']:,}** |\n"
        f"| **Estimated API Cost (USD)** | **${totals['cost']:.2f}** |\n"
        f"| **Cache Savings** | **{totals['savings_pct']:.1f}%** |\n\n"
        f"{receipt_block}\n"
    )


def _resolve_repo_slug(repo: str | None, cwd: str | None, timeout: int) -> str:
    """Resolves owner/repo slug dynamically with no hardcoded project defaults."""
    if repo:
        return repo
    cli = _cli_mod()
    owner, name = cli.get_repo_owner_and_name(repo=repo, cwd=cwd, timeout=timeout)
    if owner and name:
        return f"{owner}/{name}"
    return os.getenv("GITHUB_REPOSITORY") or "unknown/repository"


def generate_and_post_telemetry(
    pr_num: int,
    repo: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    cwd: str | None = None,
) -> bool:
    """Computes session token metrics for a PR directly from SQLite logs and posts the report."""
    cli = _cli_mod()
    print(f"Aggregating Antigravity session telemetry for PR #{pr_num}...")
    commits = cli.get_pr_commits(pr_num, repo, timeout=timeout)
    _, direct_session_ids = _collect_pr_sessions(commits)
    external_sessions = _collect_external_sessions(commits)

    covered_sessions: list[dict[str, Any]] = []
    missing: list[str] = []
    for cid in sorted(direct_session_ids):
        tel = cli.get_session_telemetry(cid)
        if tel:
            tel["role"] = "Session Agent"
            covered_sessions.append(tel)
        else:
            missing.append(cid)

    if missing or (not covered_sessions and not external_sessions):
        sys.stderr.write(f"Error: Could not resolve telemetry DBs for PR #{pr_num} sessions: {sorted(missing)}\n")
        return False

    repo_slug = _resolve_repo_slug(repo, cwd, timeout)
    totals = _aggregate_session_totals(covered_sessions)
    md = _build_telemetry_markdown(pr_num, repo_slug, covered_sessions, totals, external_sessions=external_sessions)

    comment_cmd = ["gh", "pr", "comment", str(pr_num), "--body", md]
    if repo:
        comment_cmd.extend(["-R", repo])
    res = subprocess.run(comment_cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if res.returncode != 0:
        sys.stderr.write(f"Failed to post comment to PR #{pr_num}: {res.stderr.strip()}\n")
        return False
    print(f"Successfully posted verified telemetry report with receipt to PR #{pr_num} ({res.stdout.strip()})")
    return True
