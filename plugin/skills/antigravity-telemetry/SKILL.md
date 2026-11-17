---
name: antigravity-telemetry
description: Generate, post, and verify Antigravity session token spend telemetry reports and pre-merge receipts across pull requests.
---

# Antigravity Telemetry & Pre-Merge Guard

Use this skill to inspect session token expenditure, publish verified PR telemetry reports factoring Antigravity's multi-turn session prompt caching, and verify pre-merge guards.

## Commands

### 1. Post or Refresh PR Telemetry Report
```bash
antigravity-telemetry post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
```
- Extracts all `Antigravity-Session-ID` trailers from PR commits.
- Queries Antigravity session SQLite databases (`~/.gemini/antigravity/conversations/<id>.db`).
- Calculates uncached prompt, cached prompt hits, thinking, candidate tokens, net billed tokens, and estimated cost.
- Validates 100% session provenance.
- Posts/updates the report comment on GitHub with an embedded verification receipt.

### 2. Verify PR Pre-Merge Guard
```bash
antigravity-telemetry verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--repo owner/repo] [--timeout 60]
```
- Validates that a verified telemetry receipt exists on the PR.
- Verifies that all committed session IDs on the PR are present in the receipt.
- Verifies that 100% of PR review conversation threads are resolved (blocks merge if unresolved comments remain).
- Optional `--ignore-unresolved-comments` bypasses review comment checks if needed.
- Optional `--post-status` posts a GitHub commit status check directly to the PR head commit.
- Optional `--repo <owner>/<repo>` supplies repository context explicitly for GraphQL discovery.
- Returns exit code `0` if passed; exits `1` if blocked.

### 3. Prune Merged Branches and Worktrees
```bash
antigravity-telemetry prune [--cwd <PATH>] [--quiet]
```
- Fetches and prunes remote tracking branches (`git fetch --prune`).
- Safely removes any git worktrees associated with merged or deleted remote branches.
- Deletes local merged feature branches while protecting `main`/active branches.
- Cleans up orphaned worktree metadata (`git worktree prune`).
- Automatically triggered via `PostToolUse` hook after `gh pr merge` or `git merge`.

## Agent Best Practices

- **Explicit Repo Context on Merge**: When running `gh pr merge`, always provide `--repo <owner>/<repo>` (or `-R <owner>/<repo>`) so the pre-tool harness hook resolves repository commit history without local git discovery issues.
- **Worktree Safety**: Avoid running `gh pr merge --delete-branch` with working directory set inside a `.worktrees/` directory. Detach to root first.
- **Protected Branch Push Prohibition**: Never push directly to `main` or `master`. The harness hook intercepts and blocks direct pushes; all modifications must be pushed to a feature branch and merged via `gh pr create` and `gh pr merge`.
