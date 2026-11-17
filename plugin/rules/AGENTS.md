# Antigravity Guard & Pre-Merge Provenance Rules

## 1. Deterministic Commit Receipts

- Every git commit executed by an Antigravity agent or subagent must include the standard git trailer:
  `Antigravity-Session-ID: <session-conversation-uuid>`
- The Antigravity lifecycle harness hook (`PreToolUse`) automatically enforces and injects this trailer on all `git commit` commands executed via `run_command` (with strict path-traversal guards on `-F` message files).

## 2. Pre-Merge Verification Gate

- Pull requests cannot merge to `main` without a verified Antigravity Token Spend Telemetry Report comment posted to the pull request.
- `antigravity-guard verify --pr <PR_NUM>` (and alias `antigravity-telemetry verify --pr <PR_NUM>`) validates that 100% of committed session IDs are covered and 100% of PR review conversation threads are resolved.
- Run `antigravity-guard post --pr <PR_NUM>` before merging.
- **Explicit Repository Context on Merge:** Always pass `--repo <owner>/<repo>` (or `-R <owner>/<repo>`) on `gh pr merge`.

## 3. Worktree Merge Discipline & Automated Post-Merge Pruning

- Never run `gh pr merge` or `git merge` from inside a `.worktrees/` directory; switch to the primary repository root first.
- After `gh pr merge` or `git merge` completes, `PostToolUse` runs `antigravity-guard prune` automatically.

## 4. Protected Branch Push Prohibition

- Direct pushes to `main` or `master` (`git push origin main`) are intercepted and blocked by `PreToolUse`. Push to a feature branch and open a pull request instead.
