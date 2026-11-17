"""Shared pytest fixtures for unit and real integration tests."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


def encode_varint(value: int) -> bytes:
    """Encodes a non-negative integer into protobuf base-128 varint bytes."""
    buf = bytearray()
    remaining = value
    while True:
        byte = remaining & 0x7F
        remaining >>= 7
        if remaining:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            break
    return bytes(buf)


def encode_length_delimited(field_num: int, payload: bytes) -> bytes:
    """Encodes a length-delimited protobuf field."""
    tag = (field_num << 3) | 2
    return encode_varint(tag) + encode_varint(len(payload)) + payload


def encode_varint_field(field_num: int, value: int) -> bytes:
    """Encodes a varint protobuf field."""
    tag = field_num << 3
    return encode_varint(tag) + encode_varint(value)


def create_telemetry_db(
    db_path: Path,
    *,
    uncached: int = 1000,
    cached: int = 4000,
    thoughts: int = 200,
    candidates: int = 300,
    model: str = "gemini-3.8-flash-tiered",
) -> Path:
    """Creates a real Antigravity SQLite conversation database with encoded protobuf rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    f4_bytes = (
        encode_varint_field(2, uncached)
        + encode_varint_field(5, cached)
        + encode_varint_field(9, thoughts)
        + encode_varint_field(10, candidates)
    )
    f1_bytes = encode_length_delimited(4, f4_bytes) + encode_length_delimited(19, model.encode("utf-8"))
    top_bytes = encode_length_delimited(1, f1_bytes)

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS gen_metadata (idx INTEGER, data BLOB)")
        conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (0, top_bytes))
        conn.commit()
    return db_path


@dataclass(frozen=True)
class GitWorktreeFixture:
    """Real temporary git repository with bare origin remote and linked worktree."""

    origin_dir: Path
    repo_dir: Path
    worktree_dir: Path
    branch_name: str


def _clean_git_env() -> dict[str, str]:
    """Strips outer git hook env vars (e.g. GIT_DIR) so temp git repos stay isolated."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        env=_clean_git_env(),
    )
    return res.stdout.strip()


@pytest.fixture
def git_worktree_env(tmp_path: Path) -> GitWorktreeFixture:
    """Creates a real bare remote, local main checkout, merged feature branch, and linked worktree."""
    git_env = _clean_git_env()
    origin_dir = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin_dir)],
        check=True,
        capture_output=True,
        env=git_env,
    )

    repo_dir = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin_dir), str(repo_dir)], check=True, capture_output=True, env=git_env)
    _git(repo_dir, "config", "user.name", "Test Agent")
    _git(repo_dir, "config", "user.email", "test@example.com")
    # Ensure local symbolic-ref origin/HEAD is defined
    _git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "chore: initial commit")
    _git(repo_dir, "push", "-u", "origin", "main")

    branch_name = "feat/worktree-test"
    wt_dir = repo_dir / ".worktrees" / "wt-feat"
    _git(repo_dir, "worktree", "add", "-b", branch_name, str(wt_dir), "main")
    (wt_dir / "feature.txt").write_text("hello worktree\n", encoding="utf-8")
    _git(wt_dir, "add", "feature.txt")
    _git(wt_dir, "commit", "-m", "feat: add feature in worktree")

    # Fast-forward main on local & origin so feat/worktree-test is merged and eligible for pruning
    _git(repo_dir, "merge", "--ff-only", branch_name)
    _git(repo_dir, "push", "origin", "main")
    _git(repo_dir, "remote", "set-head", "origin", "main")

    return GitWorktreeFixture(
        origin_dir=origin_dir,
        repo_dir=repo_dir,
        worktree_dir=wt_dir,
        branch_name=branch_name,
    )
