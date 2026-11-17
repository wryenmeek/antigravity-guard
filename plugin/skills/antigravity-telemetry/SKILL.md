---
name: antigravity-telemetry
description: Legacy alias for antigravity-guard. Generate, post, and verify Antigravity session token spend telemetry reports and pre-merge receipts across pull requests.
---

# Antigravity Telemetry (Legacy Alias for `antigravity-guard`)

`antigravity-telemetry` forwards cleanly to `antigravity-guard`. Prefer `antigravity-guard` for new workflows.

```bash
antigravity-guard post --pr <PR_NUMBER> [--repo owner/repo] [--timeout 60]
antigravity-guard verify --pr <PR_NUMBER> [--post-status] [--ignore-unresolved-comments] [--repo owner/repo] [--timeout 60]
antigravity-guard prune [--cwd <PATH>] [--quiet]
```
