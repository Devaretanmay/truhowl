# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Delivery states: verification and publication stay separate, PRs fail loud."""

import os

from truhowl.delivery import service as d
from truhowl.delivery.service import (
    BLOCKED_AUTH,
    FAILED,
    NOT_REQUESTED,
    PUBLISHED,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean_env(monkeypatch):
    for k in ("GITHUB_TOKEN", "TRUHOWL_GITHUB_TOKEN",
              "TRUHOWL_GITHUB_APP_ID", "TRUHOWL_GITHUB_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_no_credentials_blocked_loud(monkeypatch):
    _clean_env(monkeypatch)
    ok, reason = d.github_credentials_available()
    assert ok is False
    assert "GitHub credentials unavailable" in reason


def test_token_present_ok(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    ok, _ = d.github_credentials_available()
    assert ok is True


def test_publish_without_repo_not_requested(monkeypatch):
    _clean_env(monkeypatch)
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=[],
                             trust_pr_body="b", github_repo=None)
    assert res.status == NOT_REQUESTED


def test_publish_without_credentials_blocked(monkeypatch):
    _clean_env(monkeypatch)
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=[],
                             trust_pr_body="b", github_repo="o/r")
    assert res.status == BLOCKED_AUTH
    assert "GitHub credentials unavailable" in res.error
    assert res.pr_url is None


def test_publish_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr("truhowl.git_ops.git_commit_and_push", lambda *a, **k: True)
    monkeypatch.setattr("truhowl.git_ops.gh_create_pr",
                        lambda *a, **k: "https://github.com/o/r/pull/7")
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=[],
                             trust_pr_body="b", github_repo="o/r")
    assert res.status == PUBLISHED
    assert res.pr_number == 7


def test_publish_push_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr("truhowl.git_ops.git_commit_and_push", lambda *a, **k: False)
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=[],
                             trust_pr_body="b", github_repo="o/r")
    assert res.status == FAILED
    assert "push" in res.error


def test_error_categorization():
    assert d.categorize_github_error(Exception("401 Unauthorized")).startswith("auth failure")
    assert "permission" in d.categorize_github_error(Exception("403 Resource not accessible"))
    assert d.categorize_github_error(Exception("429 rate limit")).startswith("rate limit")
    assert "unavailable" in d.categorize_github_error(Exception("404 Not Found"))


def test_agent_records_blocked_delivery_without_downgrade(tmp_path, monkeypatch):
    """VERIFIED stays VERIFIED; delivery BLOCKED_AUTH is recorded alongside."""
    from truhowl.agent import models as m
    from truhowl.agent import service as svc
    from truhowl.maintenance import MaintenanceRunReport

    monkeypatch.chdir(tmp_path)
    fixture = os.path.join(REPO_ROOT, "trials", "fixtures", "taxonomy_stripe")

    def _verified_blocked(**kw):
        return MaintenanceRunReport(
            success=True, provider_name="stripe", from_version="11.18.0",
            to_version="13.0.0", repository_path=kw["repo_dir"],
            files_scanned=5, files_modified=1, unintended_files_modified=0,
            blast_radius_verified=True, test_exit_code=0, test_duration_ms=10,
            unified_diff="d", trust_pr_body="b",
            replay_exit_code=0, replay_command="npm test", patch_hash="h",
            verification_tier="behavioral_verified",
            delivery_status=BLOCKED_AUTH,
            delivery_error="PR publication blocked: GitHub credentials unavailable.")

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _verified_blocked)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [fixture])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    assert out["state"] == m.VERIFIED
    text = svc.explain_case(str(tmp_path), case.case_id)
    assert "delivery: blocked-auth" in text
    assert "GitHub credentials unavailable" in text


def test_credential_hierarchy_resolution(monkeypatch):
    _clean_env(monkeypatch)
    # 1. App installation token
    monkeypatch.setenv("TRUHOWL_GITHUB_INSTALLATION_TOKEN", "ghs_app123")
    monkeypatch.setenv("TRUHOWL_GITHUB_TOKEN", "ght_123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_123")
    creds, _ = d.resolve_github_credentials()
    assert creds.source == "app_installation"
    assert creds.token == "ghs_app123"

    # 2. Truhowl token
    monkeypatch.delenv("TRUHOWL_GITHUB_INSTALLATION_TOKEN")
    creds, _ = d.resolve_github_credentials()
    assert creds.source == "truhowl_token"
    assert creds.token == "ght_123"

    # 3. GITHUB_TOKEN
    monkeypatch.delenv("TRUHOWL_GITHUB_TOKEN")
    creds, _ = d.resolve_github_credentials()
    assert creds.source == "github_token"
    assert creds.token == "ghp_123"


def test_git_push_redacts_credentials_on_error(tmp_path, monkeypatch):
    from truhowl import git_ops
    secret_token = "ghp_supersecrettoken12345"
    creds = d.GitHubCredentials(token=secret_token, source="github_token")

    # Mock subprocess.run to simulate a git push error that echoes the token
    def _mock_run(cmd, cwd=None, capture_output=True, text=True):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = f"fatal: authentication failed for bearer {secret_token}"
        return Proc()

    monkeypatch.setattr("subprocess.run", _mock_run)
    ok, err = git_ops.git_commit_and_push(
        repo_dir=str(tmp_path),
        modified_files=["a.ts"],
        branch_name="truhowl/test",
        commit_message="test",
        credentials=creds,
    )
    assert ok is False
    assert secret_token not in err
    assert "[REDACTED]" in err or "authentication failed" in err
