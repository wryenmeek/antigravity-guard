# Contributing & Quality Control Standards

This document establishes the quality control standards, architectural contracts, and release verification procedures for `antigravity-telemetry-guard`.

---

## 1. Scope Boundary: Plugin Repo vs. Local Config

| Category | Owning Location | Examples |
| :--- | :--- | :--- |
| **Plugin Core & Engine** | `wryenmeek/antigravity-telemetry-guard` | `src/antigravity_telemetry/cli.py`, `pyproject.toml`, `action.yml` |
| **Global Agent Rules** | `plugin/rules/AGENTS.md` (in repo) | Commit trailer requirements, merge guard protocols, worktree disciplines |
| **Skill Definitions** | `plugin/skills/` (in repo) | `antigravity-telemetry/SKILL.md` usage and flags |
| **Hook Manifests** | `plugin/hooks.json` (in repo) | `PreToolUse` and `PostToolUse` event subscriptions |
| **Workspace-Specific Rules** | `<workspace>/AGENTS.md` | Domain heuristics, project build/test commands, local CI verification scripts |
| **Local Config & State** | `~/.gemini/config/plugins/...` | `installed_version.json`, local plugin installation directory |

> **Single Source of Truth Rule**: Never edit `~/.gemini/config/plugins/antigravity-telemetry-guard/` directly as a primary authoring location. All changes to rules, skills, hooks, or code must be committed, reviewed, and merged in `wryenmeek/antigravity-telemetry-guard` first, then synced to the local installation.

---

## 2. Antigravity Harness Hook Architectural Contracts

When developing or modifying `PreToolUse` or `PostToolUse` hook handlers in `cli.py`:

1. **Top-Level `cwd` Discovery**:
   - In Antigravity's hook protocol, `run_command` passes the agent's active working directory at the top-level of the stdin payload (`payload["cwd"]` or `payload["workspacePaths"]`), **not** inside `toolCall.args`.
   - Always resolve working directory hierarchically:
     ```python
     cwd = payload.get("cwd") or (payload.get("workspacePaths", [None])[0] if payload.get("workspacePaths") else None) or (args.get("Cwd") if isinstance(args, dict) else None)
     ```

2. **Worktree Isolation & Safety**:
   - In a git worktree, `git rev-parse --git-dir` points to `.git/worktrees/<name>`, while `git rev-parse --git-common-dir` points to the primary `.git` root.
   - Any destructive command (such as `gh pr merge --delete-branch` or `git merge`) must be intercepted and denied if active inside `.worktrees/`.
   - Post-merge pruning (`antigravity-telemetry prune`) must resolve the primary repository root via `--git-common-dir` to guarantee safe cleanup regardless of invocation directory.

3. **Multi-Repo Compatibility & Dynamic Discovery**:
   - If `--repo <owner>/<repo>` or `-R` is passed on the command line, it takes highest precedence.
   - If omitted, resolve dynamically using `git -C <cwd> config --get remote.origin.url` (supporting both SSH `git@github.com:...` and HTTPS `https://github.com/...` syntax), falling back to `gh repo view --json owner,name`.
   - Never assume commands run against the current process directory; always pass explicit directory context (`-C <cwd>` or `cwd=<cwd>`).

4. **Protected Branch Push Prohibition**:
   - `PreToolUse` must intercept `git push` commands targeting `main` or `master` (explicitly or via checked-out branch default), returning `decision: deny`.
   - Tag pushes (`--tags`, `refs/tags/*`) and dedicated feature branch pushes must remain permitted.

---

## 3. Pre-Merge Verification & CI Quality Standards

Every Pull Request against `antigravity-telemetry-guard` must satisfy:

1. **Unit Test Matrix**:
   - 100% pass rate across all active Python versions (`3.10`, `3.11`, `3.12`, `3.13`).
   - Run locally before push:
     ```bash
     PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
     ```
   - Ensure tests that validate git fallback behavior isolate `os.environ["GITHUB_REPOSITORY"]` using `@patch.dict(os.environ, {"GITHUB_REPOSITORY": ""})`.

2. **Dual-Tier Validation (Mocked Unit + Real Integration)**:
   - Unit tests must mock Antigravity hook JSON stdin payloads.
   - Live integration tests must verify real worktree creation, denial, and cleanup on a real git filesystem.

3. **Pre-Merge Telemetry Gate**:
   - Every commit must include the trailer `Antigravity-Session-ID: <uuid>`.
   - Post telemetry comment: `antigravity-telemetry post --pr <PR_NUM> --repo wryenmeek/antigravity-telemetry-guard`.
   - Verify gate and commit status: `antigravity-telemetry verify --pr <PR_NUM> --post-status --repo wryenmeek/antigravity-telemetry-guard`.
   - 100% of review conversation threads must be resolved before merging.

---

## 4. Release & Local Installation Verification Procedure

After merging to `main`:

1. **Re-install via Remote Package**:
   ```bash
   pip install --force-reinstall git+https://github.com/wryenmeek/antigravity-telemetry-guard.git
   ```

2. **Sync Local Plugin Assets**:
   ```bash
   cp -r plugin/* ~/.gemini/config/plugins/antigravity-telemetry-guard/
   echo '{"version": "<VERSION>"}' > ~/.gemini/config/plugins/antigravity-telemetry-guard/installed_version.json
   ```

3. **Verify with Antigravity Language Server**:
   Query the local language server endpoint to verify live recognition:
   ```bash
   source /tmp/lsrpc.sh && lsrpc GetAllPlugins '{}' | jq '.plugins[] | select(.name == "antigravity-telemetry-guard")'
   ```
