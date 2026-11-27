---
name: antigravity-guard
description: Enforce commit receipts, unforgeable skill receipts, adversarial issue completion audits, PR spend telemetry, plugin sync, and pre-merge provenance gates.
---

# Antigravity Guard & Provenance Gate

Use this skill to compute cached token expenditure, record/verify skill execution receipts, validate `.infigraph/reviews/issue_<N>_audit.json` ledgers, sync the local Antigravity plugin, and satisfy pre-merge provenance gates.

## Commands

### 1. Post or Refresh PR Telemetry Report
```bash
antigravity-guard post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
```

### 2. Verify PR Pre-Merge Guard & CI Audits
```bash
antigravity-guard verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--verify-issue-audits] [--verify-skill-receipts] [--repo owner/repo] [--timeout 60]
```

### 3. Verify Local Skill Receipts, Issue Completion Ledgers & Triage Readiness
```bash
antigravity-guard verify-skills [--cwd PATH] [--staged-only] [--session-id UUID]
antigravity-guard audit-skills [--cwd PATH] [--limit 30] [--check-promotion] [--json]
antigravity-guard verify-issue [--cwd PATH] [--issue N] [--mode commit|strict] [--commit-msg MSG] [--transcript-path PATH] [--no-verify-transcripts]
antigravity-guard lint-issue [--cwd PATH] (--issue N | --file PATH) [--repo owner/repo]
```

#### Substantive Review & Findings Validation Lifecycle
1. Run formatters, static exports, and code edits *before* the final skill review pass.
2. Delegate multi-skill reviews via `invoke_subagent` so `PostToolUse` records receipts natively without bloating parent context.
3. Evaluate both `gate` and `advisory` rules in `.github/required-skills.json` plus any issue `### Skill Review Criteria`, validate findings, and remediate 100% of verified defects before committing.
4. Before marking an issue `ready-for-agent` in opted-in repos, verify blast radius with `infigraph` and run `antigravity-guard lint-issue --issue <N>`.

### 4. Prune, Validate & Sync Local Plugin
```bash
antigravity-guard prune [--cwd <PATH>] [--quiet]
antigravity-guard validate-plugin [--repo-root PATH] [--strict]
antigravity-guard sync-plugin [--repo-root PATH] [--clean-legacy]
```

### 5. Skill Governance, Promotion & Glob Sizing Rubric
- **Advisory → Gate Promotion (`audit-skills --check-promotion`):** Promote an `advisory` rule to `gate` only after $\ge 80\%$ adoption across $\ge 3$ commits/PRs, `0` manual overrides, clean CI runs, and tight globs.
- **Glob Sizing:** Balance *co-change coupling* (avoid missing related files) against *relevance dilution* (avoid unbounded `**/*` wildcards that cause context thrashing). Prefer scoped brace globs like `src/**/*.{js,ts}`.
- **Husky v9 Integration:** In Node/TS repos using Husky v9 (`npm install` resets `core.hooksPath` to `.husky/`), enforce `Antigravity-Session-ID:` UUIDv4 trailers inside `.husky/commit-msg` rather than `git config core.hooksPath .githooks`.

*(Backwards-compatible alias `antigravity-telemetry` supports all commands and flags.)*
