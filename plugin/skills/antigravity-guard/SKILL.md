---
name: antigravity-guard
description: Generate, post, and verify Antigravity session token spend telemetry reports, pre-merge receipts, plugin sync, and repository guardrails across pull requests.
---

# Antigravity Guard & Pre-Merge Provenance

Use this skill to compute cached token expenditure, publish PR telemetry reports, validate plugin bundles, sync local Antigravity installations, and satisfy pre-merge provenance gates.

## Commands

### 1. Post or Refresh PR Telemetry Report
```bash
antigravity-guard post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
```

### 2. Verify PR Pre-Merge Guard
```bash
antigravity-guard verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--repo owner/repo] [--timeout 60]
```

### 3. Prune Merged Branches and Worktrees
```bash
antigravity-guard prune [--cwd <PATH>] [--quiet]
```

### 4. Validate & Sync Local Plugin
```bash
antigravity-guard validate-plugin [--strict]
antigravity-guard sync-plugin [--clean-legacy]
```

*(Backwards-compatible alias `antigravity-telemetry` supports all legacy flags.)*
