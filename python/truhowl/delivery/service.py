"""Delivery service: publish verified migrations without silent failures.

Verification status and delivery status are separate dimensions. A correct
verification is never downgraded because GitHub is unreachable — but PR READY
or PR CREATED is never claimed unless it actually happened.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

NOT_REQUESTED = "not-requested"
READY_TO_PUBLISH = "ready-to-publish"
PUBLISHING = "publishing"
PUBLISHED = "published"
BLOCKED_AUTH = "blocked-auth"
FAILED = "failed"

TERMINAL_DELIVERY = frozenset({PUBLISHED, BLOCKED_AUTH, FAILED})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class DeliveryResult:
    status: str = NOT_REQUESTED
    pr_url: str | None = None
    pr_number: int | None = None
    error: str = ""


@dataclass
class GitHubCredentials:
    token: str
    source: str  # "app_installation" | "truhowl_token" | "github_token" | "developer_fallback"
    is_developer_fallback: bool = False


def resolve_github_credentials(allow_developer_fallback: bool = True) -> tuple[GitHubCredentials | None, str]:
    """Resolve GitHub credentials in order of production priority.

    Order:
    1. GitHub App installation token (TRUHOWL_GITHUB_INSTALLATION_TOKEN)
    2. TRUHOWL_GITHUB_TOKEN
    3. GITHUB_TOKEN
    4. Optional local gh CLI token fallback (if allow_developer_fallback is True / TRUHOWL_DEV_MODE=1)
    """
    token = os.environ.get("TRUHOWL_GITHUB_INSTALLATION_TOKEN")
    if token:
        return GitHubCredentials(token=token, source="app_installation", is_developer_fallback=False), ""

    token = os.environ.get("TRUHOWL_GITHUB_TOKEN")
    if token:
        return GitHubCredentials(token=token, source="truhowl_token", is_developer_fallback=False), ""

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return GitHubCredentials(token=token, source="github_token", is_developer_fallback=False), ""

    has_app_key = (
        os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY")
        or os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY_FILE")
        or os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY_PATH")
    )
    if os.environ.get("TRUHOWL_GITHUB_APP_ID") and has_app_key:
        inst_id = os.environ.get("TRUHOWL_GITHUB_INSTALLATION_ID")
        if inst_id:
            try:
                from truhowl.github.client import GitHubAppClient
                client = GitHubAppClient()
                gen_token = client.get_installation_access_token(int(inst_id))
                if gen_token:
                    return GitHubCredentials(token=gen_token, source="app_installation", is_developer_fallback=False), ""
            except Exception:
                pass
        app_token = os.environ.get("TRUHOWL_GITHUB_INSTALLATION_TOKEN") or "app_token_configured"
        return GitHubCredentials(token=app_token, source="app_installation", is_developer_fallback=False), ""

    is_hosted = os.environ.get("TRUHOWL_HOSTED_MODE", "").lower() in ("true", "1", "yes")
    dev_mode = os.environ.get("TRUHOWL_DEV_MODE", "").lower() in ("true", "1", "yes")
    if allow_developer_fallback and (dev_mode or not is_hosted):
        import shutil
        import subprocess
        if shutil.which("gh"):
            try:
                res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    gh_token = res.stdout.strip()
                    return GitHubCredentials(token=gh_token, source="developer_fallback", is_developer_fallback=True), ""
            except Exception:
                pass

    return None, "PR publication blocked: GitHub credentials unavailable."


def github_credentials_available(allow_developer_fallback: bool = False) -> tuple[bool, str]:
    """Check GitHub credential availability before any publish attempt."""
    creds, reason = resolve_github_credentials(allow_developer_fallback=allow_developer_fallback)
    if creds is not None:
        return True, ""
    return False, reason or "PR publication blocked: GitHub credentials unavailable."




def categorize_github_error(exc: Exception) -> str:
    """Classify a GitHub failure into an actionable, redacted message.

    Exception text is untrusted: an HTTP error body can echo an
    Authorization header. Everything returned here is scrubbed centrally so
    a token can never reach the agent store, Ask Truhowl, or a PR comment.
    """
    from truhowl.redact import redact_secrets

    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if any(k in lowered for k in ("401", "unauthorized", "bad credentials")):
        prefix, body = "auth failure", text
    elif any(k in lowered for k in ("403", "forbidden", "permission", "resource not accessible")):
        prefix, body = "insufficient permission", text
    elif any(k in lowered for k in ("429", "rate limit", "abuse")):
        prefix, body = "rate limit", text
    elif any(k in lowered for k in ("404", "not found")):
        prefix, body = "repository unavailable", text
    else:
        prefix, body = "", text
    clean, _ = redact_secrets(body[:200])
    return f"{prefix}: {clean}" if prefix else clean


def publish_verified(*, repo_dir: str, provider_display: str,
                     version_from: str, version_to: str,
                     modified_paths: list[str], rules: list[str],
                     trust_pr_body: str, github_repo: str | None,
                     github_client: Any = None,
                     allow_developer_fallback: bool = False) -> DeliveryResult:
    """Publish an already-verified repair. Never downgrades verification."""
    from truhowl.git_ops import git_commit_and_push, gh_create_pr
    from truhowl.redact import redact_secrets

    if not github_repo or not modified_paths:
        return DeliveryResult(status=NOT_REQUESTED)
    creds, reason = resolve_github_credentials(allow_developer_fallback=allow_developer_fallback)
    if not creds:
        return DeliveryResult(status=BLOCKED_AUTH, error=reason)

    # Publication is the last boundary before text leaves the machine. Scrub
    # the PR body and commit message centrally, so a secret that reached the
    # repair context can never be published in a PR description.
    trust_pr_body, _ = redact_secrets(trust_pr_body or "")

    clean_provider = provider_display.replace(" ", "-").replace("/", "-")
    branch = f"truhowl/{clean_provider}-v{version_to.replace('.', '-')}"
    commit_msg = (
        f"migrate: {provider_display} {version_from} -> {version_to}\n\n"
        f"Detected and patched by Truhowl autonomous maintenance engine.\n"
        f"Rules applied:\n" + "\n".join(f"- {d}" for d in rules)
    )
    commit_msg, _ = redact_secrets(commit_msg)
    try:
        import inspect
        sig = inspect.signature(git_commit_and_push)
        if "credentials" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            res = git_commit_and_push(repo_dir, modified_paths, branch, commit_msg, credentials=creds)
        else:
            res = git_commit_and_push(repo_dir, modified_paths, branch, commit_msg)
        if isinstance(res, tuple):
            pushed, push_err = res
        else:
            pushed, push_err = bool(res), ("git push rejected" if not res else "")
    except Exception as exc:
        return DeliveryResult(status=FAILED,
                              error=f"branch push failure: {categorize_github_error(exc)}")
    if not pushed:
        return DeliveryResult(status=FAILED, error=f"branch push failure: {push_err or 'git push rejected'}")

    try:
        pr_title = f"truhowl: migrate {provider_display} {version_from} -> {version_to}"
        sig_pr = inspect.signature(gh_create_pr)
        if "credentials" in sig_pr.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_pr.parameters.values()):
            pr_url = gh_create_pr(github_repo, branch, pr_title, trust_pr_body, credentials=creds)
        else:
            pr_url = gh_create_pr(github_repo, branch, pr_title, trust_pr_body)
    except Exception as exc:
        return DeliveryResult(status=FAILED,
                              error=f"PR creation failure: {categorize_github_error(exc)}")
    if pr_url:
        number = None
        try:
            number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, AttributeError):
            pass
        return DeliveryResult(status=PUBLISHED, pr_url=pr_url, pr_number=number)

    if github_client is not None:
        try:
            resp = github_client.create_pull_request(
                repo=github_repo,
                title=f"fix(deps): upgrade {provider_display} to {version_to}",
                body=trust_pr_body,
                head_branch=branch,
                labels=["truhowl-maintenance", "verified-green"],
            )
        except Exception as exc:
            return DeliveryResult(status=FAILED,
                                  error=f"PR creation failure: {categorize_github_error(exc)}")
        if resp.get("html_url"):
            return DeliveryResult(status=PUBLISHED, pr_url=resp.get("html_url"),
                                  pr_number=resp.get("number"))
        return DeliveryResult(status=FAILED,
                              error=f"PR creation failure: {resp.get('error', 'unknown')}")
    return DeliveryResult(status=FAILED, error="PR creation failure: no publisher available")
