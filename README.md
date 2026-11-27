# Antigravity Guard

[![CI](https://github.com/wryenmeek/antigravity-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/wryenmeek/antigravity-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **Global Antigravity commit receipts, unforgeable skill provenance, adversarial issue completion audits, prompt-cache telemetry reports, and pre-merge guards.**

*(Formerly `antigravity-telemetry-guard`; `antigravity-telemetry` CLI and `@v1` GitHub Action aliases remain backwards-compatible.)*

---

## Key Capabilities

1. **Deterministic Commit Receipts**: Blocks `--no-verify` (`-n`), safely resolves `-F <msg-file>` inside `repo_root`, and injects `Antigravity-Session-ID: <uuid>` + `validated-by:` footers.
2. **Unforgeable Skill Receipts (`PostToolUse` + `verify-skills`)**: Captures `SKILL.md` reads, canonical `scripts/invoke-skill.sh` invocations, and `invoke_subagent` calls to `.github/hooks/state/skill-receipts.jsonl` + `$PLUGIN_DATA/skill-receipts.jsonl`.
3. **Adversarial Issue Completion Audits (`verify-issue` + `Stop`)**: Validates `.infigraph/reviews/issue_<N>_audit.json` (`VERIFIED_COMPLETE`, strict UUIDv4 + parent `transcriptPath` subagent link, 100% `VERIFIED`/`REJECTED` findings `REMEDIATED`, 100% Acceptance Criteria `IMPLEMENTED` with fresh SHA-256 hashes & AST symbol verification).
4. **Ground-Truth SQLite Telemetry**: Reads protobuf varint turn records from `~/.gemini/antigravity/conversations/*.db` factoring Antigravity's 75% cached prompt discount ($\text{Uncached} + 0.25 \times \text{Cached} + \text{Output}$).
5. **Fail-Closed Pre-Merge & Push Guards**: Blocks direct pushes to `main`/`master`, blocks merges from inside `.worktrees/`, and verifies 100% session provenance + resolved review threads via GraphQL pagination.

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
git config core.hooksPath .githooks   # Or integrate into .husky/commit-msg for Husky v9 repos
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
    verify-issue-audits: 'true'
    verify-skill-receipts: 'true'
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

---

## CLI Reference

```bash
antigravity-guard post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
antigravity-guard verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--verify-issue-audits] [--verify-skill-receipts] [--repo owner/repo]
antigravity-guard verify-skills [--cwd PATH] [--staged-only] [--session-id UUID]
antigravity-guard audit-skills [--cwd PATH] [--limit 30] [--check-promotion] [--json]
antigravity-guard verify-issue [--cwd PATH] [--issue N] [--mode commit|strict] [--commit-msg MSG] [--transcript-path PATH]
antigravity-guard lint-issue [--cwd PATH] (--issue N | --file PATH) [--repo owner/repo]
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
