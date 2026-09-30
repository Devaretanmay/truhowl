# Copyright 2026 Truhowl Authors

import os
import shutil
import subprocess


from typing import Any
from truhowl.redact import redact_secrets


def git_commit_and_push(
    repo_dir: str,
    modified_files: list[str],
    branch_name: str,
    commit_message: str,
    credentials: Any = None,
) -> tuple[bool, str]:
    """Commit exact modified files and push to remote using resolved credentials.

    Returns (success: bool, error_message: str).
    """
    subprocess.run(["git", "config", "user.name", "Truhowl Bot"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "bot@truhowl.dev"], cwd=repo_dir, capture_output=True)

    chk = subprocess.run(["git", "checkout", "-B", branch_name], cwd=repo_dir, capture_output=True, text=True)
    if chk.returncode != 0:
        clean_err, _ = redact_secrets(chk.stderr or "git checkout failed")
        return False, f"git checkout failed: {clean_err}"

    rel_files = [os.path.relpath(f, repo_dir) if os.path.isabs(f) else f for f in modified_files]
    for rel_f in rel_files:
        subprocess.run(["git", "add", rel_f], cwd=repo_dir, capture_output=True)

    result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        clean_err, _ = redact_secrets(result.stderr or "git commit failed")
        return False, f"git commit failed: {clean_err}"

    push_cmd = ["git"]
    token = credentials.token if credentials and getattr(credentials, "token", None) else (
        os.environ.get("TRUHOWL_GITHUB_INSTALLATION_TOKEN")
        or os.environ.get("TRUHOWL_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if token and token != "app_token_configured":
        import base64
        auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("utf-8")
        push_cmd.extend(["-c", f"http.extraheader=AUTHORIZATION: basic {auth}"])
    push_cmd.extend(["push", "-u", "origin", branch_name, "--force"])

    push = subprocess.run(
        push_cmd,
        cwd=repo_dir, capture_output=True, text=True,
    )
    if push.returncode != 0:
        clean_err, _ = redact_secrets(push.stderr or "git push rejected")
        return False, clean_err

    return True, ""


def gh_create_pr(
    repo: str,
    branch_name: str,
    title: str,
    body: str,
    credentials: Any = None,
) -> str | None:
    """Create a pull request on GitHub via gh CLI with redacted title and body."""
    if not shutil.which("gh"):
        return None
    title, _ = redact_secrets(title)
    body, _ = redact_secrets(body)

    env = dict(os.environ)
    token = credentials.token if credentials and getattr(credentials, "token", None) else (
        os.environ.get("TRUHOWL_GITHUB_INSTALLATION_TOKEN")
        or os.environ.get("TRUHOWL_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if token and token != "app_token_configured":
        env["GITHUB_TOKEN"] = token

    result = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--base", "main",
         "--head", branch_name, "--title", title, "--body", body],
        capture_output=True, text=True, env=env,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    view = subprocess.run(
        ["gh", "pr", "view", branch_name, "--repo", repo, "--json", "url", "-q", ".url"],
        capture_output=True, text=True, env=env,
    )
    if view.returncode == 0 and view.stdout.strip():
        return view.stdout.strip()
    return None
