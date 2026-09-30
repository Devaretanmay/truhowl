# Copyright 2026 Truhowl Authors

import os
import shutil
import subprocess


def git_commit_and_push(
    repo_dir: str,
    modified_files: list[str],
    branch_name: str,
    commit_message: str,
) -> bool:
    subprocess.run(["git", "config", "user.name", "Truhowl Bot"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "bot@truhowl.dev"], cwd=repo_dir, capture_output=True)

    try:
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_dir, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "checkout", branch_name], cwd=repo_dir, capture_output=True)

    rel_files = [os.path.relpath(f, repo_dir) if os.path.isabs(f) else f for f in modified_files]
    for rel_f in rel_files:
        subprocess.run(["git", "add", rel_f], cwd=repo_dir, capture_output=True)

    result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False

    push = subprocess.run(
        ["git", "push", "-u", "origin", branch_name, "--force"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return push.returncode == 0


def gh_create_pr(
    repo: str,
    branch_name: str,
    title: str,
    body: str,
) -> str | None:
    if not shutil.which("gh"):
        return None
    result = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--base", "main",
         "--head", branch_name, "--title", title, "--body", body],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    view = subprocess.run(
        ["gh", "pr", "view", branch_name, "--repo", repo, "--json", "url", "-q", ".url"],
        capture_output=True, text=True,
    )
    if view.returncode == 0 and view.stdout.strip():
        return view.stdout.strip()
    return None
