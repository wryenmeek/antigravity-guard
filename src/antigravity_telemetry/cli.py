"""Backwards-compatible CLI module alias forwarding to `antigravity_guard.cli`."""

from __future__ import annotations

import importlib
import sys

from antigravity_guard.cli import (
    DEFAULT_TIMEOUT_SEC as DEFAULT_TIMEOUT_SEC,
)
from antigravity_guard.cli import (
    RECEIPT_END as RECEIPT_END,
)
from antigravity_guard.cli import (
    RECEIPT_START as RECEIPT_START,
)
from antigravity_guard.cli import (
    calc_usd_cost as calc_usd_cost,
)
from antigravity_guard.cli import (
    extract_session_ids_from_text as extract_session_ids_from_text,
)
from antigravity_guard.cli import (
    generate_and_post_telemetry as generate_and_post_telemetry,
)
from antigravity_guard.cli import (
    get_all_pr_commits_and_head as get_all_pr_commits_and_head,
)
from antigravity_guard.cli import (
    get_pr_comments as get_pr_comments,
)
from antigravity_guard.cli import (
    get_pr_commits as get_pr_commits,
)
from antigravity_guard.cli import (
    get_repo_owner_and_name as get_repo_owner_and_name,
)
from antigravity_guard.cli import (
    get_session_telemetry as get_session_telemetry,
)
from antigravity_guard.cli import (
    get_unresolved_review_threads as get_unresolved_review_threads,
)
from antigravity_guard.cli import (
    handle_post_tool_hook as handle_post_tool_hook,
)
from antigravity_guard.cli import (
    handle_pre_tool_hook as handle_pre_tool_hook,
)
from antigravity_guard.cli import (
    is_direct_push_to_main as is_direct_push_to_main,
)
from antigravity_guard.cli import (
    main as main,
)
from antigravity_guard.cli import (
    parse_varints as parse_varints,
)
from antigravity_guard.cli import (
    post_commit_status as post_commit_status,
)
from antigravity_guard.cli import (
    prune_merged_branches_and_worktrees as prune_merged_branches_and_worktrees,
)
from antigravity_guard.cli import (
    verify_pr_telemetry as verify_pr_telemetry,
)

sys.modules[__name__] = importlib.import_module("antigravity_guard.cli")
