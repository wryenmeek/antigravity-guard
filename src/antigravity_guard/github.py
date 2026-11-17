"""GitHub CLI & fail-closed GraphQL helpers for PR commits, review threads, and statuses."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import subprocess
from typing import Any

from antigravity_guard.git_worktree import resolve_trusted_cwd
from antigravity_guard.telemetry import DEFAULT_TIMEOUT_SEC

_REMOTE_SLUG_RE = re.compile(r"[:/]([^/]+/[^/]+?)(?:\.git)?$")
_SHA_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,40}$")

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              author { login }
              body
              url
            }
          }
        }
      }
    }
  }
}
"""

_PR_COMMITS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      headRefOid
      commits(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          commit {
            oid
            messageHeadline
            messageBody
          }
        }
      }
    }
  }
}
"""


def _cli_mod() -> Any:
    return importlib.import_module("antigravity_guard.cli")


def _parse_remote_slug(url_text: str) -> tuple[str | None, str | None]:
    match = _REMOTE_SLUG_RE.search(url_text.strip())
    if not match:
        return None, None
    slug = match.group(1)
    if "/" not in slug:
        return None, None
    owner, name = slug.split("/", 1)
    return owner, name


def _git_origin_slug(target_dir: str) -> tuple[str | None, str | None]:
    try:
        res = subprocess.run(
            ["git", "-C", target_dir, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None, None
    if res.returncode != 0:
        return None, None
    return _parse_remote_slug(res.stdout)


def _gh_repo_slug(cwd: str | None, timeout: int) -> tuple[str | None, str | None]:
    kwargs: dict[str, Any] = {"timeout": timeout, "capture_output": True, "text": True, "check": False}
    if cwd and os.path.isdir(cwd):
        kwargs["cwd"] = cwd
    try:
        res = subprocess.run(["gh", "repo", "view", "--json", "owner,name"], **kwargs)
    except Exception:
        return None, None
    if res.returncode != 0:
        return None, None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        data = json.loads(res.stdout)
        return data.get("owner", {}).get("login"), data.get("name")
    return None, None


def get_repo_owner_and_name(
    repo: str | None = None,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[str | None, str | None]:
    """Resolves owner and repo name from explicit arg, GITHUB_REPOSITORY, git remote, or gh CLI."""
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    env_repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in env_repo:
        owner, name = env_repo.split("/", 1)
        return owner, name

    trusted_cwd = resolve_trusted_cwd(cwd) if cwd else None
    target_dir = trusted_cwd or os.getcwd()
    owner, name = _git_origin_slug(target_dir)
    if owner and name:
        return owner, name
    return _gh_repo_slug(trusted_cwd, timeout)


def _decode_graphql_json(
    res: subprocess.CompletedProcess[str],
    pr_num: int,
    cursor: str | None,
    label: str,
) -> dict[str, Any] | None:
    """Validates HTTP/CLI exit status and parses GraphQL JSON, failing closed after page 1."""
    if res.returncode != 0:
        if cursor is None:
            return None
        prefix = (
            "GraphQL pagination failed" if label == "commits" else f"GraphQL pagination failed on PR #{pr_num} {label}"
        )
        detail = f"on PR #{pr_num} (cursor {cursor})" if label == "commits" else f"(cursor {cursor})"
        raise RuntimeError(f"{prefix} {detail}: {res.stderr.strip()}")
    try:
        parsed = json.loads(res.stdout)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError as exc:
        if cursor is None:
            return None
        scope = f"on PR #{pr_num}" if label == "commits" else f"on PR #{pr_num} {label}"
        raise RuntimeError(f"Malformed GraphQL response {scope} (cursor {cursor}): {exc}") from exc


def _run_graphql_page(
    owner: str,
    repo_name: str,
    pr_num: int,
    query: str,
    cursor: str | None,
    timeout: int,
    label: str,
) -> dict[str, Any] | None:
    """Executes one paginated GraphQL call via `gh api graphql`, failing closed on mid-page errors."""
    cmd = ["gh", "api", "graphql", "-F", f"owner={owner}", "-F", f"repo={repo_name}", "-F", f"pr={pr_num}"]
    if cursor:
        cmd.extend(["-F", f"cursor={cursor}"])
    cmd.extend(["-f", f"query={query}"])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"GitHub CLI timed out after {timeout}s fetching PR #{pr_num} {label} (cursor: {cursor})"
        ) from exc
    return _decode_graphql_json(res, pr_num, cursor, label)


def _extract_unresolved_from_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filters GraphQL reviewThread nodes down to open/unresolved items."""
    unresolved: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("isResolved"):
            continue
        comments = (node.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        author_obj = first.get("author")
        author_login = author_obj.get("login", "unknown") if isinstance(author_obj, dict) else "unknown"
        unresolved.append(
            {
                "id": node.get("id", ""),
                "author": author_login,
                "url": first.get("url", ""),
                "body": first.get("body", ""),
            }
        )
    return unresolved


def get_unresolved_review_threads(
    pr_num: int,
    repo: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> list[dict[str, Any]]:
    """Fetches all PR review threads via GraphQL pagination and returns unresolved threads."""
    owner, repo_name = get_repo_owner_and_name(repo, timeout=timeout)
    if not owner or not repo_name:
        return []

    unresolved: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = _run_graphql_page(owner, repo_name, pr_num, _REVIEW_THREADS_QUERY, cursor, timeout, "review threads")
        if data is None:
            return unresolved if cursor is not None else []
        pr_obj = data.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_obj:
            break
        threads_obj = pr_obj.get("reviewThreads", {})
        unresolved.extend(_extract_unresolved_from_nodes(threads_obj.get("nodes", [])))
        page_info = threads_obj.get("pageInfo", {})
        cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        if not cursor:
            break
    return unresolved


def _fetch_commits_via_graphql(
    owner: str,
    repo_name: str,
    pr_num: int,
    timeout: int,
) -> tuple[list[dict[str, Any]], str | None] | None:
    """Paginates through PR commits via GraphQL; returns None only if initial query falls back cleanly."""
    commits: list[dict[str, Any]] = []
    head_sha: str | None = None
    cursor: str | None = None
    while True:
        data = _run_graphql_page(owner, repo_name, pr_num, _PR_COMMITS_QUERY, cursor, timeout, "commits")
        if data is None:
            return None if cursor is None and not commits else (commits, head_sha)
        pr_obj = data.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_obj:
            if cursor is None and not commits:
                return None
            raise RuntimeError(f"Missing pullRequest data in GraphQL response for PR #{pr_num}")
        head_sha = head_sha or pr_obj.get("headRefOid")
        commits_obj = pr_obj.get("commits", {})
        for node in commits_obj.get("nodes", []):
            commit = node.get("commit", {})
            commits.append(
                {
                    "oid": commit.get("oid", ""),
                    "messageHeadline": commit.get("messageHeadline", ""),
                    "messageBody": commit.get("messageBody", ""),
                }
            )
        page_info = commits_obj.get("pageInfo", {})
        cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        if not cursor:
            return commits, head_sha


def get_all_pr_commits_and_head(
    pr_num: int,
    repo: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetches all PR commits via paginated GraphQL, falling back to `gh pr view` upfront only."""
    owner, repo_name = get_repo_owner_and_name(repo, timeout=timeout)
    if owner and repo_name:
        gql = _fetch_commits_via_graphql(owner, repo_name, pr_num, timeout)
        if gql is not None:
            return gql

    cmd = ["gh", "pr", "view", str(pr_num), "--json", "commits,headRefOid,headRefName"]
    if repo:
        cmd.extend(["-R", repo])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"GitHub CLI timed out after {timeout}s fetching PR #{pr_num} commits") from exc
    if res.returncode != 0:
        raise RuntimeError(f"Failed to fetch PR #{pr_num} commits: {res.stderr.strip()}")
    data = json.loads(res.stdout)
    return data.get("commits", []), data.get("headRefOid")


def get_pr_commits(pr_num: int, repo: str | None = None, timeout: int = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Returns all commits associated with PR #pr_num."""
    commits, _ = _cli_mod().get_all_pr_commits_and_head(pr_num, repo, timeout=timeout)
    return commits


def get_pr_comments(pr_num: int, repo: str | None = None, timeout: int = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Fetches all comments on a PR via `gh pr view`."""
    cmd = ["gh", "pr", "view", str(pr_num), "--json", "comments"]
    if repo:
        cmd.extend(["-R", repo])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"GitHub CLI timed out after {timeout}s fetching PR #{pr_num} comments") from exc
    if res.returncode != 0:
        raise RuntimeError(f"Failed to fetch PR #{pr_num} comments: {res.stderr.strip()}")
    data = json.loads(res.stdout)
    return data.get("comments", [])


def post_commit_status(
    sha: str,
    state: str,
    description: str,
    repo: str | None = None,
    target_url: str | None = None,
    timeout: int = 30,
) -> tuple[bool, str]:
    """Posts a GitHub commit status check against the target PR head SHA."""
    if not sha or not _SHA_HEX_RE.match(sha):
        return False, "Invalid commit SHA for status check"
    target_repo = repo or os.getenv("GITHUB_REPOSITORY")
    endpoint = f"repos/{target_repo}/statuses/{sha}" if target_repo else f"repos/:owner/:repo/statuses/{sha}"
    cmd = [
        "gh",
        "api",
        "--method",
        "POST",
        endpoint,
        "-f",
        f"state={state}",
        "-f",
        "context=Antigravity Telemetry Pre-Merge Guard",
        "-f",
        f"description={description[:140]}",
    ]
    if target_url:
        cmd.extend(["-f", f"target_url={target_url}"])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        return False, f"Error posting commit status: {exc}"
    if res.returncode != 0:
        return False, f"Failed to post commit status: {res.stderr.strip()}"
    return True, "Commit status posted successfully."
