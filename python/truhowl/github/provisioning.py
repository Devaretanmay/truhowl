# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Repository provisioning: managed clone/pull cache for installed repos.

Gives the GitHub App a real checkout to analyze: install event → clone →
Day-0 index → READY. PR events resolve to the same checkout instead of the
daemon's cwd. Cloning uses a token when configured, anonymous https
otherwise (public repos), or TRUHOWL_REPO_REMOTE_* overrides (tests,
mirrors, self-hosted).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

_logger = logging.getLogger("truhowl.provisioning")


def cache_dir() -> str:
    base = os.environ.get("TRUHOWL_REPOS_DIR",
                           os.path.join(os.path.expanduser("~/.truhowl"), "repos"))
    os.makedirs(base, exist_ok=True)
    return base


def cached_path(repo_full_name: str) -> str:
    slug = repo_full_name.replace("/", "__")
    return os.path.join(cache_dir(), slug)


def remote_for(repo_full_name: str, token: str | None = None) -> str:
    """Clone URL for a repo. Env override wins (mirrors/tests), else github."""
    slug = repo_full_name.replace("/", "__").upper()
    override = os.environ.get(f"TRUHOWL_REPO_REMOTE_{slug}")
    if override:
        return override
    if token:
        return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    return f"https://github.com/{repo_full_name}.git"


def _run_git(args: list, cwd: str, timeout: int = 120,
             token: str | None = None) -> bool:
    try:
        cmd = ["git"] + _auth_args(token) + args
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode == 0
    except Exception as e:
        _logger.warning("git %s failed in %s: %s", args[:2], cwd, e)
        return False


def ensure_repo_checkout(repo_full_name: str, token: str | None = None,
                         ref: str = "main") -> str | None:
    """Clone on first sight, fast-forward on later sightings. Returns path or None."""
    dest = cached_path(repo_full_name)
    if os.path.isdir(os.path.join(dest, ".git")):
        _run_git(["fetch", "origin"], dest, token=token)
        _run_git(["checkout", ref], dest)
        _run_git(["merge", "--ff-only", f"origin/{ref}"], dest)
        return dest
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    ok = _run_git(["clone", remote_for(repo_full_name), dest], parent,
                  timeout=300, token=token)
    if not ok or not os.path.isdir(os.path.join(dest, ".git")):
        return None
    _run_git(["checkout", ref], dest)
    return dest


def workdir_for_event(payload: dict[str, Any], token: str | None = None) -> str | None:
    """Resolve a managed checkout for any webhook payload carrying a repository."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    if not repo:
        return None
    return ensure_repo_checkout(repo, token=token)


def _auth_args(token: str | None) -> list:
    if token:
        return ["-c", f"http.extraHeader=Authorization: Bearer {token}"]
    return []


def ensure_pr_checkout(repo_full_name: str, pr_number: int, head_sha: str,
                       token: str | None = None) -> tuple:
    """Check out the exact PR head SHA. Returns (path, exact).

    Fetches `pull/N/head` (exposed by GitHub for same-repo and fork PRs
    alike) and verifies the SHA before checking out detached. Any failure
    falls back to the tracked-branch checkout with exact=False — the caller
    must disclose the approximation, never silently claim head analysis.
    """
    base = ensure_repo_checkout(repo_full_name, token=token)
    if not base:
        return (None, False)
    auth = _auth_args(token)
    if not _run_git(auth + ["fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"], base):
        return (base, False)
    try:
        proc = subprocess.run(["git", "cat-file", "-e", head_sha], cwd=base,
                              capture_output=True, timeout=30)
        if proc.returncode != 0:
            return (base, False)
        if not _run_git(["checkout", "--detach", head_sha], base):
            return (base, False)
        return (base, True)
    except Exception as e:
        _logger.warning("PR checkout failed for %s#%s: %s", repo_full_name, pr_number, e)
        return (base, False)


def ensure_branch_checkout(repo_full_name: str, branch: str, sha: str,
                           token: str | None = None) -> tuple:
    """Check out an arbitrary pushed branch head. Returns (path, exact).

    Same honesty contract as ensure_pr_checkout: exact=True only when the
    SHA was verified locally, else tracked-branch fallback with exact=False.
    """
    base = ensure_repo_checkout(repo_full_name, token=token, ref=branch)
    if not base:
        return (None, False)
    auth = _auth_args(token)
    _run_git(auth + ["fetch", "origin", branch], base)
    try:
        proc = subprocess.run(["git", "cat-file", "-e", sha], cwd=base,
                              capture_output=True, timeout=30)
        if proc.returncode != 0:
            return (base, False)
        if not _run_git(["checkout", "--detach", sha], base):
            return (base, False)
        return (base, True)
    except Exception as e:
        _logger.warning("branch checkout failed for %s %s: %s", repo_full_name, branch, e)
        return (base, False)


def resolve_pr_workdir(payload: dict[str, Any], token: str | None = None,
                       fallback_fn: Any = None) -> tuple:
    """(workdir, exact_head) for a pull_request webhook payload."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    ppr = payload.get("pull_request") or {}
    number = ppr.get("number")
    sha = (ppr.get("head") or {}).get("sha", "")
    if repo and number and sha:
        path, exact = ensure_pr_checkout(repo, number, sha, token=token)
        if path:
            return (path, exact)
    if fallback_fn:
        try:
            return (fallback_fn(payload), True)
        except Exception:
            pass
    return (None, True)
