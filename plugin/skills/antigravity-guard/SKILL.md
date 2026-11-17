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

### 3. Verify Local Skill Receipts & Issue Completion Ledgers
```bash
antigravity-guard verify-skills [--cwd PATH] [--staged-only] [--session-id UUID]
antigravity-guard verify-issue [--cwd PATH] [--issue N] [--mode commit|strict] [--commit-msg MSG] [--transcript-path PATH] [--no-verify-transcripts]
```

### 4. Prune, Validate & Sync Local Plugin
```bash
antigravity-guard prune [--cwd <PATH>] [--quiet]
antigravity-guard validate-plugin [--repo-root PATH] [--strict]
antigravity-guard sync-plugin [--repo-root PATH] [--clean-legacy]
```

*(Backwards-compatible alias `antigravity-telemetry` supports all commands and flags.)*
