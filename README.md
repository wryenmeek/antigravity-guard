# Antigravity Guard

[![CI](https://github.com/wryenmeek/antigravity-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/wryenmeek/antigravity-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **Deterministic AI agent commit receipts, prompt-cache telemetry reports, protected branch/worktree safety, and pre-merge validation guards for Google Antigravity.**

*(Formerly `antigravity-telemetry-guard`; `antigravity-telemetry` CLI and `@v1` GitHub Action aliases remain backwards-compatible.)*

---

## Key Capabilities

1. **Deterministic Commit Receipts**: Injects `Antigravity-Session-ID: <uuid>` trailers via `PreToolUse`, resolving `-F <commit-msg-file>` safely inside the validated `repo_root`.
2. **Ground-Truth SQLite Telemetry**: Reads protobuf varint turn records from `~/.gemini/antigravity/conversations/*.db` factoring Antigravity's 75% cached prompt discount ($\text{Uncached} + 0.25 \times \text{Cached} + \text{Output}$).
3. **Fail-Closed Pre-Merge & Push Guards**: Blocks direct pushes to `main`/`master`, blocks merges from inside `.worktrees/`, and verifies 100% session provenance + resolved review threads via GraphQL pagination.
4. **Automated Plugin Validation & Sync**: `antigravity-guard validate-plugin --strict` and `antigravity-guard sync-plugin --clean-legacy`.

---

## Installation & Local Sync

```bash
pip install git+https://github.com/wryenmeek/antigravity-guard.git
antigravity-guard sync-plugin --clean-legacy
```

For local development with `uv`:

```bash
git clone https://github.com/wryenmeek/antigravity-guard.git
cd antigravity-guard
git config core.hooksPath .githooks
uv sync --locked --dev
uv run --locked antigravity-guard sync-plugin --clean-legacy
```

---

## GitHub Actions Usage

Both `@v2` and backwards-compatible `@v1` tags are maintained automatically:

```yaml
- name: Verify Antigravity Guard
  uses: wryenmeek/antigravity-guard@v2
  with:
    pr-number: ${{ github.event.pull_request.number || github.event.issue.number }}
    post-status: 'true'
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

---

## CLI Reference

```bash
antigravity-guard post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
antigravity-guard verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--repo owner/repo] [--timeout 60]
antigravity-guard prune [--cwd /path/to/repo] [--quiet]
antigravity-guard validate-plugin [--repo-root PATH] [--strict]
antigravity-guard sync-plugin [--repo-root PATH] [--clean-legacy]
antigravity-guard version
```

---

## Quality Gates

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked actionlint
uv run --locked pytest -q --cov=antigravity_guard --cov-fail-under=90
```

## License

[MIT](LICENSE) © 2026 Wryen Meek
