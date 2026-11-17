"""Real SQLite protobuf integration tests for conversation token extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

import antigravity_guard.cli as guard_cli
from tests.conftest import create_telemetry_db


def test_real_sqlite_protobuf_telemetry_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extracts exact cached/uncached/thought/candidate metrics from real SQLite protobuf DB."""
    cid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    db_path = tmp_path / ".gemini" / "antigravity" / "conversations" / f"{cid}.db"
    create_telemetry_db(
        db_path,
        uncached=1000,
        cached=4000,
        thoughts=200,
        candidates=300,
        model="gemini-3.8-flash-tiered",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    result = guard_cli.get_session_telemetry(cid)
    assert result is not None
    assert result["cid"] == cid
    assert result["turns"] == 1
    assert result["model"] == "gemini-3.8-flash-tiered"
    assert result["uncached_prompt"] == 1000
    assert result["cached_prompt"] == 4000
    assert result["thoughts"] == 200
    assert result["candidates"] == 300
    assert result["output_total"] == 500
    assert result["gross_context"] == 5500
    # 1000 + round(0.25 * 4000) + 500 = 2500
    assert result["billed_tokens_75"] == 2500


def test_real_sqlite_corrupt_or_missing_db_fails_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing table or missing DB file returns None without leaking open handles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert guard_cli.get_session_telemetry("00000000-0000-4000-8000-000000000000") is None

    bad_cid = "11111111-1111-4111-8111-111111111111"
    bad_db = tmp_path / ".gemini" / "antigravity" / "conversations" / f"{bad_cid}.db"
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    bad_db.write_text("not a sqlite db", encoding="utf-8")
    assert guard_cli.get_session_telemetry(bad_cid) is None
