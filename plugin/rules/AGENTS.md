# Antigravity Telemetry & Pre-Merge Guard Rules

## 1. Deterministic Commit Receipts

- Every git commit executed by an Antigravity agent or subagent must include the standard git trailer:
  `Antigravity-Session-ID: <session-conversation-uuid>`
- The Antigravity lifecycle harness hook (`PreToolUse`) automatically enforces and injects this trailer on all `git commit` commands executed via `run_command`.

## 2. Pre-Merge Verification Gate

- Pull requests cannot be merged to `main` without a verified Antigravity Token Spend Telemetry Report comment posted to the pull request.
- The pre-merge verification guard (`antigravity-telemetry verify --pr <PR_NUM>`) validates that 100% of all Antigravity session IDs committed in the PR are accounted for in the aggregated telemetry calculations.
- Merges via `gh pr merge` or `git merge` will be intercepted and hard-blocked by the harness (`PreToolUse` deny) if verified telemetry is missing or if any commit is missing session provenance.
- To satisfy the guard, run:
  `antigravity-telemetry post --pr <PR_NUM>`
- **Explicit Repository Context on Merge:** When executing `gh pr merge`, always pass the target repository explicitly via `--repo <owner>/<repo>` (or `-R <owner>/<repo>`) (e.g. `gh pr merge <PR_NUM> --repo <owner>/<repo> --merge --delete-branch`). This ensures the harness pre-tool guard resolves PR commit provenance and review threads deterministically through the GitHub API/GraphQL without relying on local git working directory discovery.

## 3. Worktree Merge Discipline & Automated Post-Merge Pruning

- **Never merge from inside a worktree**: Merging via `gh pr merge --delete-branch` while the current working directory (`Cwd`) is inside a mounted `.worktrees/` directory causes Git deletion failures. The harness `PreToolUse` hook intercepts and denies `gh pr merge` if invoked from within `.worktrees/`. Always switch to the primary repository root and detach the worktree first.
- **Automated Pruning**: Whenever `gh pr merge` or `git merge` completes, the Antigravity lifecycle harness hook (`PostToolUse`) automatically executes `antigravity-telemetry prune`.
- Any local branch or linked `.worktrees/` directory whose remote tracking branch has been deleted/merged is automatically pruned, and leftover empty worktree folders are cleaned up.
