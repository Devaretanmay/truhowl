"""GitHub API Client & Authentication for Truhowl App."""

import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List
import urllib.error
import urllib.request

import jwt

_logger = logging.getLogger("truhowl.github.client")


def _load_env_file() -> None:
    for env_name in (".env.local", ".env"):
        if os.path.isfile(env_name):
            try:
                with open(env_name, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env_file()


def verify_webhook_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify HMAC-SHA256 signature from GitHub webhook (X-Hub-Signature-256)."""
    if not signature_header or not secret:
        return False
    
    if not signature_header.startswith("sha256="):
        return False
    
    expected_hash = signature_header[7:]
    mac = hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), expected_hash)


def _get_gh_cli_token() -> str | None:
    """Retrieve GitHub token from gh CLI if available."""
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception as e:
        _logger.warning("Failed to retrieve gh CLI token: %s", e)
    return None


class GitHubAppClient:
    """Client for GitHub REST API, supporting Personal Access Tokens and GitHub Apps."""

    def __init__(
        self,
        token: str | None = None,
        app_id: str | None = None,
        private_key: str | None = None,
        api_base_url: str = "https://api.github.com",
        readonly: bool = False,
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("TRUHOWL_GITHUB_TOKEN") or _get_gh_cli_token()
        self.app_id = app_id or os.environ.get("TRUHOWL_GITHUB_APP_ID")
        self.private_key = (
            private_key
            or os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY")
            or os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY_FILE")
            or os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY_PATH")
        )
        self.api_base_url = api_base_url.rstrip("/")
        self.readonly = readonly

    def generate_jwt(self, expiration_seconds: int = 600) -> str | None:
        """Generate a GitHub App JWT from app_id and private_key."""
        if not self.app_id or not self.private_key or jwt is None:
            return None
        try:
            now = int(time.time())
            payload = {
                "iat": now - 60,
                "exp": now + expiration_seconds,
                "iss": str(self.app_id),
            }
            # Support private key as raw PEM string or path to .pem file
            key_pem = self.private_key
            if os.path.isfile(key_pem):
                with open(key_pem, "r", encoding="utf-8") as f:
                    key_pem = f.read()

            return jwt.encode(payload, key_pem, algorithm="RS256")
        except Exception as e:
            _logger.warning("Failed to generate JWT: %s", e)
            return None

    def get_installation_access_token(self, installation_id: int) -> str | None:
        """Exchange GitHub App JWT for a repository installation access token."""
        app_jwt = self.generate_jwt()
        if not app_jwt:
            return None

        url = f"{self.api_base_url}/app/installations/{installation_id}/access_tokens"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "User-Agent": "Truhowl-Autonomous-Maintenance/1.1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("token")
        except Exception as e:
            _logger.warning("Failed to get installation access token for %s: %s", installation_id, e)
            return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Truhowl-Autonomous-Maintenance/1.1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.app_id and self.private_key:
            app_jwt = self.generate_jwt()
            if app_jwt:
                headers["Authorization"] = f"Bearer {app_jwt}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        data: Dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send authenticated HTTP request to GitHub REST API."""
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        payload_bytes = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(
            url,
            data=payload_bytes,
            headers=self._headers(),
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                if body:
                    return json.loads(body)
                return {"status": status, "success": True}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            return {
                "error": f"HTTP {e.code}: {e.reason}",
                "status": e.code,
                "details": err_body,
                "success": False,
            }
        except Exception as e:
            return {
                "error": str(e),
                "success": False,
            }

    def list_repositories(self) -> list[dict[str, Any]]:
        """List repositories accessible to the user or GitHub App installation."""
        if self.app_id and self.private_key:
            res = self._request("GET", "installation/repositories")
            if isinstance(res, dict) and "repositories" in res:
                return res["repositories"]
        res = self._request("GET", "user/repos?per_page=100&sort=updated")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "repositories" in res:
            return res["repositories"]
        return []

    def get_repo(self, repo: str) -> dict[str, Any]:
        """Get repository details."""
        return self._request("GET", f"repos/{repo}")

    def get_branch_ref(self, repo: str, branch: str) -> dict[str, Any]:
        """Get git reference for a branch."""
        return self._request("GET", f"repos/{repo}/git/ref/heads/{branch}")

    def create_branch(self, repo: str, base_branch: str, new_branch: str) -> dict[str, Any]:
        """Create a new git branch from base_branch."""
        if self.readonly:
            raise PermissionError("Howl client has read-only authority; cannot modify repository files or create PRs")
        ref_info = self.get_branch_ref(repo, base_branch)
        if not ref_info.get("object", {}).get("sha"):
            return {"error": f"Base branch {base_branch} not found", "success": False}
        
        sha = ref_info["object"]["sha"]
        data = {
            "ref": f"refs/heads/{new_branch}",
            "sha": sha,
        }
        return self._request("POST", f"repos/{repo}/git/refs", data=data)

    def create_or_update_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        commit_message: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Commit a file change to a branch."""
        if self.readonly:
            raise PermissionError("Howl client has read-only authority; cannot modify repository files or create PRs")
        data = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": branch,
        }
        if sha:
            data["sha"] = sha
        return self._request("PUT", f"repos/{repo}/contents/{path}", data=data)

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        labels: List[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Pull Request and optionally attach labels."""
        if self.readonly:
            raise PermissionError("Howl client has read-only authority; cannot modify repository files or create PRs")
        data = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
            "maintainer_can_modify": True,
        }
        res = self._request("POST", f"repos/{repo}/pulls", data=data)
        
        if res.get("number") and labels:
            pr_num = res["number"]
            self._request("POST", f"repos/{repo}/issues/{pr_num}/labels", data={"labels": labels})
            
        return res

    def set_commit_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str = "truhowl/verification",
        description: str = "Truhowl zero-blast-radius verified",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        """Set commit status check (pending, success, error, failure)."""
        data = {
            "state": state,
            "context": context,
            "description": description,
        }
        if target_url:
            data["target_url"] = target_url
        return self._request("POST", f"repos/{repo}/statuses/{sha}", data=data)

    def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Get pull request details."""
        return self._request("GET", f"repos/{repo}/pulls/{pr_number}")

    def get_pull_request_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get list of files changed in a pull request."""
        result = self._request("GET", f"repos/{repo}/pulls/{pr_number}/files")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "files" in result:
            return result["files"]
        return []

    def get_pull_request_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get existing review comments on a pull request."""
        result = self._request("GET", f"repos/{repo}/issues/{pr_number}/comments")
        if isinstance(result, list):
            return result
        return []

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> dict[str, Any]:
        """Post a comment on a pull request issue."""
        data = {"body": body}
        return self._request("POST", f"repos/{repo}/issues/{pr_number}/comments", data=data)

    def update_pr_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        """Update an existing PR comment."""
        data = {"body": body}
        return self._request("PATCH", f"repos/{repo}/issues/comments/{comment_id}", data=data)

    def delete_pr_comment(self, repo: str, comment_id: int) -> dict[str, Any]:
        """Delete a PR comment."""
        return self._request("DELETE", f"repos/{repo}/issues/comments/{comment_id}")

    def create_pull_request_review_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
        commit_sha: str,
        path: str,
        line: int | None = None,
        side: str = "right",
    ) -> dict[str, Any]:
        """Create an inline review comment on a pull request."""
        data: dict[str, Any] = {
            "body": body,
            "commit_sha": commit_sha,
            "path": path,
            "side": side,
        }
        if line is not None:
            data["line"] = line
        return self._request("POST", f"repos/{repo}/pulls/{pr_number}/comments", data=data)

    def get_commit(self, repo: str, sha: str) -> dict[str, Any]:
        """Get commit details by SHA."""
        return self._request("GET", f"repos/{repo}/commits/{sha}")

    def update_pull_request(
        self,
        repo: str,
        pr_number: int,
        state: str | None = None,
        base_ref: str | None = None,
        head_ref: str | None = None,
    ) -> dict[str, Any]:
        """Update a pull request (e.g., auto-merge state)."""
        if self.readonly:
            raise PermissionError("Howl client has read-only authority; cannot modify repository files or create PRs")
        data: dict[str, Any] = {}
        if state is not None:
            data["state"] = state
        if base_ref is not None:
            data["base"] = base_ref
        if head_ref is not None:
            data["head"] = head_ref
        if not data:
            return {"success": True}
        return self._request("PATCH", f"repos/{repo}/pulls/{pr_number}", data=data)

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: List[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue in a repository."""
        data: dict[str, Any] = {"title": title, "body": body}
        if labels:
            data["labels"] = labels
        return self._request("POST", f"repos/{repo}/issues", data=data)

    def close_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        """Close an issue (e.g. retracted impact warning)."""
        return self._request("PATCH", f"repos/{repo}/issues/{issue_number}",
                             data={"state": "closed"})
