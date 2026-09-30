# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Pilot secret hygiene: known secrets never reach an outbound or stored surface.

The pilot standard is narrow and absolute: a recognised secret shape must
never appear in a log line, PR description, commit message, GitHub issue
body, audit JSON, agent store, or Ask Truhowl answer. These tests cover the
boundaries that Phase 3 added (delivery, the agent store, explanations) plus
the pre-existing ones they sit beside (LLM submission, Hunt audit).
"""

import os

from truhowl.agent import models as m
from truhowl.agent import service as svc
from truhowl.delivery import service as d
from truhowl.maintenance import MaintenanceRunReport
from truhowl.redact import redact_record, redact_secrets

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "trials", "fixtures", "taxonomy_stripe")

# One representative per family the scrubber claims to cover.
SECRETS = [
    "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF",
    "sk-proj-abcdefghijklmnopqrstuvwx",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "github_pat_11ABCDEFG0abcdefghijklmnop",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-1234567890-abcdefghijkl",
]


def test_scrubber_covers_known_secret_shapes():
    for secret in SECRETS:
        clean, n = redact_secrets(f"value is {secret} here")
        assert n >= 1, f"not detected: {secret}"
        assert secret not in clean


def test_private_key_block_is_scrubbed():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
           "-----END RSA PRIVATE KEY-----")
    clean, n = redact_secrets(f"key={pem}")
    assert n >= 1
    assert "MIIEowIBAAKCAQEA" not in clean


def test_redact_record_handles_nested_audit_shapes():
    record = {"a": [{"b": f"token {SECRETS[2]}"}], "c": {"d": SECRETS[1]}, "e": 7}
    clean, n = redact_record(record)
    assert n >= 2
    assert SECRETS[2] not in str(clean)
    assert SECRETS[1] not in str(clean)
    assert clean["e"] == 7


def _ok_report(repo_dir, error="", body="body", diff="diff --git a/b"):
    return MaintenanceRunReport(
        success=True, provider_name="stripe",
        from_version="11.18.0", to_version="13.0.0",
        repository_path=repo_dir, files_scanned=5, files_modified=1,
        unintended_files_modified=0, blast_radius_verified=True,
        test_exit_code=0, test_duration_ms=10,
        unified_diff=diff, trust_pr_body=body,
        replay_exit_code=0, replay_command="npm test",
        patch_hash="abc123", verification_tier="behavioral_verified",
        error=error)


# ── Publication boundary ─────────────────────────────────────────────────

def test_pr_body_and_commit_message_are_scrubbed(monkeypatch):
    """A secret in repair context must not be published in a PR description."""
    seen = {}

    def _capture_push(repo_dir, paths, branch, commit_msg):
        seen["commit_msg"] = commit_msg
        return True

    def _capture_pr(repo, branch, title, pr_body):
        seen["pr_body"] = pr_body
        return "https://github.com/o/r/pull/1"

    body = f"History:\n  leaked {SECRETS[0]}\nand {SECRETS[2]}"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setattr("truhowl.git_ops.git_commit_and_push", _capture_push)
    monkeypatch.setattr("truhowl.git_ops.gh_create_pr", _capture_pr)
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=["migrated"],
                             trust_pr_body=body, github_repo="o/r")
    assert res.status == d.PUBLISHED
    assert seen["pr_body"].count("[REDACTED") >= 2
    for secret in SECRETS:
        assert secret not in seen["pr_body"]
        assert secret not in seen["commit_msg"]


# ── Persisted state ──────────────────────────────────────────────────────

def test_refusal_reason_is_scrubbed_in_store(monkeypatch, tmp_path):
    """Upstream error text is untrusted; the store must not carry secrets."""
    monkeypatch.chdir(tmp_path)
    leaky = f"provider rejected: Authorization: Bearer {SECRETS[2]}"

    def _fail(**kw):
        rep = _ok_report(kw["repo_dir"], error=leaky)
        rep.success = False
        rep.test_exit_code = 1
        return rep

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _fail)
    monkeypatch.setattr("truhowl.test_runner._detect_test_command", lambda p: "npm test")
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                       confirmed=True)
    assert out["state"] == m.REFUSED
    assert SECRETS[2] not in out["reason"]
    store_file = m.store_path(str(tmp_path))
    raw = open(store_file, encoding="utf-8").read()
    for secret in SECRETS:
        assert secret not in raw, f"{secret} persisted into the agent store"


def test_github_error_text_is_scrubbed():
    exc = Exception("401 Unauthorized: token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    msg = d.categorize_github_error(exc)
    assert msg.startswith("auth failure")
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in msg


def test_delivery_result_never_echoes_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    def _boom(*a, **k):
        raise Exception("403 Forbidden ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")

    monkeypatch.setattr("truhowl.git_ops.git_commit_and_push", _boom)
    res = d.publish_verified(repo_dir="/tmp", provider_display="Stripe",
                             version_from="1", version_to="2",
                             modified_paths=["a.ts"], rules=[],
                             trust_pr_body="b", github_repo="o/r")
    assert res.status == d.FAILED
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in res.error


# ── Explanation boundary (Ask Truhowl) ───────────────────────────────────

def test_explain_case_is_scrubbed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    leaky = f"upstream said: api_key={SECRETS[1]}"

    def _fail(**kw):
        rep = _ok_report(kw["repo_dir"], error=leaky)
        rep.success = False
        rep.test_exit_code = 1
        return rep

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _fail)
    monkeypatch.setattr("truhowl.test_runner._detect_test_command", lambda p: "npm test")
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    text = svc.explain_case(str(tmp_path), case.case_id)
    for secret in SECRETS:
        assert secret not in text


def test_explain_case_scrubs_delivery_error(tmp_path):
    """A blocked delivery explanation must not echo the credential text."""
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    key = case.repos[0].repo_key
    store = svc.load_store(str(tmp_path))
    store.cases[case.case_id]["repos"][0]["state"] = m.VERIFIED
    store.delivery[f"{case.case_id}\x00{key}"] = {
        "status": "failed",
        "error": f"PR creation failure: unauthorized {SECRETS[2]}",
        "pr_url": None, "pr_number": None, "updated_at": "t"}
    svc.save_store(str(tmp_path), store)
    text = svc.explain_case(str(tmp_path), case.case_id)
    assert "failed" in text
    assert SECRETS[2] not in text


def test_upstream_failure_text_is_scrubbed(tmp_path):
    """Poll failures are persisted and later answered by `ask`."""
    from truhowl.changes.monitor import poll_upstream
    from truhowl.changes.sources import UpstreamCheck

    def _leaky(package, timeout=15):
        return UpstreamCheck(package=package, ok=False,
                             error=f"registry 401 token={SECRETS[2]}")

    out = poll_upstream(str(tmp_path), providers=["stripe"], fetcher=_leaky)
    assert len(out["failures"]) == 1
    assert SECRETS[2] not in out["failures"][0]["error"]
    raw = open(m.store_path(str(tmp_path)), encoding="utf-8").read()
    assert SECRETS[2] not in raw
