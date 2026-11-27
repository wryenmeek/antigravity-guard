# Contributing & Quality Control Standards

This document establishes the quality control standards, architectural contracts, and release verification procedures for `antigravity-guard`.

---

## 1. Scope Boundary: Plugin Repo vs. Local Config

| Category | Owning Location | Examples |
| :--- | :--- | :--- |
| **Plugin Core & Engine** | `wryenmeek/antigravity-guard` | `src/antigravity_guard/*.py`, `pyproject.toml`, `action.yml` |
| **Legacy Compatibility Shims** | `src/antigravity_telemetry/` | Module alias + `antigravity-telemetry` console entrypoint |
| **Global Agent Rules** | `plugin/rules/AGENTS.md` | Commit receipts, merge guards, worktree & push discipline |
| **Skill Definitions** | `plugin/skills/antigravity-guard/SKILL.md` | CLI subcommands and workflow usage |
| **Hook Manifests** | `plugin/hooks.json` | `PreToolUse` and `PostToolUse` event subscriptions |
| **Local Config & State** | `~/.gemini/config/plugins/antigravity-guard/` | Installed plugin bundle synced via `antigravity-guard sync-plugin` |

> **Single Source of Truth Rule**: Never author directly inside `~/.gemini/config/plugins/antigravity-guard/`. Commit changes in `wryenmeek/antigravity-guard`, merge to `main`, and run `antigravity-guard sync-plugin --clean-legacy`.

---

## 2. Antigravity Harness Hook Architectural Contracts

1. **Validated Working Directory (`resolve_trusted_cwd`)**:
   - Resolve CWD hierarchically from `payload["cwd"]`, `payload["workspacePaths"][0]`, or `toolCall.args["Cwd"]`.
   - Never call `os.chdir()` inside hooks; always pass `-C <cwd>` or `cwd=<cwd>` to `subprocess.run`.
   - Reject empty, relative, newline/null-containing, and leading-`-` paths before passing to `git -C`.
2. **Commit Message Path-Traversal Protection**:
   - Resolve `git commit -F <path>` relative to the validated repository root and enforce `is_relative_to(repo_root)`. Deny escapes loudly rather than swallowing `OSError`.
3. **Complexity & Security Budget**:
   - Every function in `src/antigravity_guard/` MUST keep McCabe AST complexity `<= 10` (`0` violations `>= 15` under `infigraph`).
   - `infigraph security` and `infigraph taint` must report `0` findings.
4. **Coverage & Dual-Tier Validation**:
   - `pytest --cov=antigravity_guard --cov-fail-under=90` across both `tests/unit/` and `tests/integration/` (real git worktrees, real SQLite protobuf DBs, and clean wheel builds).

---

## 3. Local Development & Release Workflow

Configure local hooks and run the full quality gate before pushing:

```bash
git config core.hooksPath .githooks   # For Husky v9 repos, add UUIDv4 check in .husky/commit-msg instead
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked actionlint
uv run --locked pytest -q --cov=antigravity_guard --cov-fail-under=90
uv run --locked antigravity-guard validate-plugin --strict
```

Release tags (`vX.Y.Z`) automatically verify version parity (`pyproject.toml` == `plugin/plugin.json` == tag), run CI checks, build wheels/sdists, publish the GitHub Release, and advance floating `v1`/`v2` compatibility tags.
