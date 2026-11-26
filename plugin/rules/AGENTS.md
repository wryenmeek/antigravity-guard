# Antigravity Guard, Skill Receipt & Issue Audit Rules

## 1. Deterministic Commit Receipts & `--no-verify` Prohibition

- Every git commit executed by an Antigravity agent or subagent must include `Antigravity-Session-ID: <session-conversation-uuid>` plus any fresh `validated-by:` footers recorded by `PostToolUse`.
- `PreToolUse` denies `git commit --no-verify` (`-n`), validates `-F` commit-message files strictly inside `repo_root`, and auto-injects commit receipts.

## 2. Skill Receipts & Issue Audit Gate (3-Rule Activation)

- `PostToolUse` captures reads of `SKILL.md`, canonical `scripts/invoke-skill.sh` calls, and `invoke_subagent` delegations to `.github/hooks/state/skill-receipts.jsonl`, `$PLUGIN_DATA/skill-receipts.jsonl`, and `.git/SKILL_FOOTERS`.
- Repositories with `.github/required-skills.json` require fresh skill receipts newer than staged changes; repositories without it pass through as not opted in.
- **Substantive Review & Remediation Invariant:** Skill receipts must represent genuine reviews against the diff, explicit findings validation, and 100% remediation of validated defects prior to commit—never superficial timestamp-touching reads. Execute and stage formatters, static exports, and code edits *before* the final review pass.
- **Advisory & Issue Skill Review Policy:** Files matched by `"enforcement": "advisory"` in `.github/required-skills.json` and architectural skills specified in an issue's `### Skill Review Criteria` are mandatory review obligations; evaluate changes against them and cite validation in commit receipts and audit logs.
- **Token-Efficient Subagent Delegation:** Prefer `invoke_subagent` for multi-skill reviews so `PostToolUse` captures receipts natively without bloating the parent orchestrator's context window, returning a compact digest of acceptance criteria and actionable findings.
- Issue closure (`Closes|Fixes|Resolves #<N>`), `gh pr merge`, and `Stop` require `.infigraph/reviews/issue_<N>_audit.json` in `VERIFIED_COMPLETE` phase with UUIDv4 parent-corroborated `subagent_conversation_id`, 100% adjudicated & remediated findings, and 100% `IMPLEMENTED` acceptance criteria with matching SHA-256 hashes and AST symbols.

## 3. Worktree Merge Discipline & Protected Branch Push Prohibition

- Never run `gh pr merge` or `git merge` from inside a `.worktrees/` directory or push directly to `main`/`master`. Always pass `--repo <owner>/<repo>` on `gh pr merge`.

## 4. Pre-`ready-for-agent` Governance Gate

- Applies to opted-in repositories (containing `.github/required-skills.json` and/or `.infigraph/`).
- **Blast-Radius & Assumption Verification (`infigraph`):** Before transitioning an issue to `ready-for-agent` or publishing specs/tickets, run `infigraph` (`index`, `file-deps`, `search-code`, `impact`) on implicated files and symbols to validate assumptions, detect already-landed fixes, and document ripple effects.
- **Skill Best-Practice Review:** Map implicated file patterns against `.github/required-skills.json`, read each matching `SKILL.md`, and remediate alignment gaps before marking `ready-for-agent`.
- **Explicit Skill & Documentation Acceptance Criteria:** Every `ready-for-agent` issue/ticket must include acceptance criteria covering all matching required skills from `.github/required-skills.json` and impacted documentation updates (e.g., `CODING_STANDARDS.md`, `GLOSSARY.md`, `docs/`, `README.md`, or explicit `N/A — no doc/standard/glossary changes`). Use `antigravity-guard lint-issue --issue <N>` to validate before labeling.
