"""Validated git context resolution and merged branch/worktree pruning."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from typing import Any


def resolve_trusted_cwd(raw_cwd: object) -> str | None:
    """Validates an untrusted hook `cwd` against option injection and relative paths.

    Preserves compatibility with unit tests that patch `os.path.isdir` on synthetic
    absolute paths by canonicalizing with `os.path.realpath` when present on disk.
    """
    if not isinstance(raw_cwd, str):
        return None
    cleaned = raw_cwd.strip()
    if not cleaned or cleaned.startswith("-") or "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        return None
    normalized = os.path.normpath(cleaned)
    if not os.path.isabs(normalized) or not os.path.isdir(normalized):
        return None
    return os.path.realpath(normalized) if os.path.exists(normalized) else normalized


def _sanitize_cli_dir(cwd: str | None) -> str:
    """Validates an explicit CLI directory string without assuming mocked dirs exist on disk."""
    if not isinstance(cwd, str) or not cwd.strip():
        return os.getcwd()
    cleaned = cwd.strip()
    if cleaned.startswith("-") or "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"Invalid working directory path: {cwd!r}")
    normalized = os.path.normpath(cleaned)
    return os.path.realpath(normalized) if os.path.exists(normalized) else os.path.abspath(normalized)


def _run_git(repo_dir: str, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str] | None:
    """Executes a fixed-binary git subcommand inside `repo_dir` without changing process CWD."""
    try:
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None


def resolve_git_context(raw_cwd: object) -> tuple[str | None, bool, str]:
    """Resolves `(repo_root, is_worktree, current_branch)` from a hook `cwd` safely."""
    trusted_cwd = resolve_trusted_cwd(raw_cwd)
    if trusted_cwd is None:
        return None, False, ""

    head_res = _run_git(trusted_cwd, "rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    current_branch = head_res.stdout.strip() if head_res and head_res.returncode == 0 else ""

    git_dir_res = _run_git(trusted_cwd, "rev-parse", "--git-dir", timeout=5)
    common_res = _run_git(trusted_cwd, "rev-parse", "--git-common-dir", timeout=5)
    if not (git_dir_res and common_res and git_dir_res.returncode == 0 and common_res.returncode == 0):
        return trusted_cwd, False, current_branch

    git_dir = git_dir_res.stdout.strip()
    common_dir = common_res.stdout.strip()
    abs_git_dir = git_dir if os.path.isabs(git_dir) else os.path.abspath(os.path.join(trusted_cwd, git_dir))
    abs_common_dir = common_dir if os.path.isabs(common_dir) else os.path.abspath(os.path.join(trusted_cwd, common_dir))
    is_worktree = "worktrees" in git_dir or abs_git_dir != abs_common_dir

    if os.path.isabs(common_dir):
        repo_root = os.path.dirname(common_dir)
    else:
        repo_root = os.path.abspath(os.path.join(trusted_cwd, common_dir, ".."))
    return repo_root, is_worktree, current_branch


def _resolve_common_repo_root(start_dir: str) -> str:
    """Resolves primary repository root using `--git-common-dir` so worktree callers clean safely."""
    res = _run_git(start_dir, "rev-parse", "--git-common-dir", timeout=5)
    if res and res.returncode == 0:
        common_dir = res.stdout.strip()
        if os.path.isabs(common_dir):
            return os.path.dirname(common_dir)
        return os.path.abspath(os.path.join(start_dir, common_dir, ".."))
    return start_dir


def _parse_porcelain_worktrees(stdout: str) -> list[dict[str, str]]:
    """Parses `git worktree list --porcelain` entries into path/branch dictionaries."""
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["path"] = line.split(" ", 1)[1]
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line.split("refs/heads/", 1)[1]
    if current:
        worktrees.append(current)
    return worktrees


def _collect_prune_candidates(repo_root: str, protected: set[str], default_branch: str) -> list[str]:
    """Finds non-protected local branches that are `[gone]` upstream or merged into default_branch."""
    candidates: set[str] = set()
    vv_res = _run_git(repo_root, "branch", "-vv")
    if vv_res and vv_res.returncode == 0:
        for raw_line in vv_res.stdout.splitlines():
            line = raw_line.strip()
            if ": gone]" in line:
                branch = line.split()[0].lstrip("*+").strip()
                if branch and branch not in protected:
                    candidates.add(branch)

    merged_res = _run_git(repo_root, "branch", "--merged", default_branch)
    if merged_res and merged_res.returncode == 0:
        for raw_line in merged_res.stdout.splitlines():
            branch = raw_line.strip().lstrip("*+").strip()
            if branch and branch not in protected:
                candidates.add(branch)
    return sorted(candidates)


def _prune_single_candidate(
    repo_root: str,
    branch: str,
    branch_to_wt: dict[str, str],
    main_wt_path: str,
) -> tuple[dict[str, str] | None, bool]:
    """Removes a candidate branch's attached linked worktree (if any) and deletes the local branch."""
    pruned_wt: dict[str, str] | None = None
    if branch in branch_to_wt:
        wt_path = os.path.abspath(branch_to_wt[branch])
        if wt_path != main_wt_path:
            rm_res = _run_git(repo_root, "worktree", "remove", "--force", wt_path)
            if rm_res and rm_res.returncode == 0 or not os.path.exists(wt_path):
                pruned_wt = {"branch": branch, "path": wt_path}
            else:
                shutil.rmtree(wt_path, ignore_errors=True)
                if not os.path.exists(wt_path):
                    pruned_wt = {"branch": branch, "path": wt_path}

    del_res = _run_git(repo_root, "branch", "-D", branch)
    return pruned_wt, bool(del_res and del_res.returncode == 0)


def _print_prune_summary(pruned_branches: list[str], pruned_worktrees: list[dict[str, str]]) -> None:
    """Prints human-readable summary of pruned branches and worktrees."""
    if not pruned_branches and not pruned_worktrees:
        print("[Antigravity Prune] Working tree is clean. No stale branches or worktrees to prune.")
        return
    print(
        f"[Antigravity Prune] Successfully pruned {len(pruned_branches)} merged branch(es) and {len(pruned_worktrees)} worktree(s)."
    )
    for branch in pruned_branches:
        print(f"  - Deleted branch: {branch}")
    for wt in pruned_worktrees:
        print(f"  - Removed worktree: {wt['path']} ({wt['branch']})")


def _detect_protected_branches(repo_root: str) -> tuple[str, set[str]]:
    """Determines default branch and protected branch names for a repository."""
    head_res = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    current_branch = head_res.stdout.strip() if head_res and head_res.returncode == 0 else ""
    remote_res = _run_git(repo_root, "symbolic-ref", "refs/remotes/origin/HEAD", "--short")
    remote_head = remote_res.stdout.strip() if remote_res and remote_res.returncode == 0 else ""
    default_branch = remote_head.replace("origin/", "") if remote_head else "main"
    return default_branch, {"main", "master", "develop", default_branch, current_branch}


def _cleanup_empty_worktrees_dir(repo_root: str) -> None:
    """Removes empty `.worktrees` directory after pruning if no worktrees remain."""
    worktrees_dir = os.path.join(repo_root, ".worktrees")
    if os.path.isdir(worktrees_dir) and not os.listdir(worktrees_dir):
        with contextlib.suppress(OSError):
            os.rmdir(worktrees_dir)


def prune_merged_branches_and_worktrees(cwd: str | None = None, quiet: bool = False) -> dict[str, Any]:
    """Prunes branches merged into default branch or gone upstream, plus linked worktrees."""
    repo_root = _resolve_common_repo_root(_sanitize_cli_dir(cwd))
    _run_git(repo_root, "fetch", "--prune", timeout=30)
    default_branch, protected = _detect_protected_branches(repo_root)

    wt_res = _run_git(repo_root, "worktree", "list", "--porcelain")
    worktrees = _parse_porcelain_worktrees(wt_res.stdout) if wt_res and wt_res.returncode == 0 else []
    branch_to_wt = {wt["branch"]: wt["path"] for wt in worktrees if "branch" in wt}
    main_wt_path = os.path.abspath(worktrees[0]["path"]) if worktrees else os.path.abspath(repo_root)

    pruned_worktrees: list[dict[str, str]] = []
    pruned_branches: list[str] = []
    for branch in _collect_prune_candidates(repo_root, protected, default_branch):
        wt_entry, branch_deleted = _prune_single_candidate(repo_root, branch, branch_to_wt, main_wt_path)
        if wt_entry:
            pruned_worktrees.append(wt_entry)
        if branch_deleted:
            pruned_branches.append(branch)

    _run_git(repo_root, "worktree", "prune")
    _cleanup_empty_worktrees_dir(repo_root)

    if not quiet:
        _print_prune_summary(pruned_branches, pruned_worktrees)
    return {"pruned_branches": pruned_branches, "pruned_worktrees": pruned_worktrees}
