# Antigravity Guard, Skill Receipt & Issue Audit Rules

## 1. Deterministic Commit Receipts & `--no-verify` Prohibition

- Every git commit executed by an Antigravity agent or subagent must include `Antigravity-Session-ID: <session-conversation-uuid>` plus any fresh `validated-by:` footers recorded by `PostToolUse`.
- `PreToolUse` denies `git commit --no-verify` (`-n`), validates `-F` commit-message files strictly inside `repo_root`, and auto-injects commit receipts.

## 2. Skill Receipts & Issue Audit Gate (3-Rule Activation)

- `PostToolUse` captures reads of `SKILL.md`, canonical `scripts/invoke-skill.sh` calls, and `invoke_subagent` delegations to `.github/hooks/state/skill-receipts.jsonl`, `$PLUGIN_DATA/skill-receipts.jsonl`, and `.git/SKILL_FOOTERS`.
- Repositories with `.github/required-skills.json` require fresh skill receipts newer than staged changes; repositories without it pass through as not opted in.
- Issue closure (`Closes|Fixes|Resolves #<N>`), `gh pr merge`, and `Stop` require `.infigraph/reviews/issue_<N>_audit.json` in `VERIFIED_COMPLETE` phase with UUIDv4 parent-corroborated `subagent_conversation_id`, 100% adjudicated & remediated findings, and 100% `IMPLEMENTED` acceptance criteria with matching SHA-256 hashes and AST symbols.

## 3. Worktree Merge Discipline & Protected Branch Push Prohibition

- Never run `gh pr merge` or `git merge` from inside a `.worktrees/` directory or push directly to `main`/`master`. Always pass `--repo <owner>/<repo>` on `gh pr merge`.
