# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Pilot Controls, Observability, and Metrics (B13, B14, B18)."""

from fastapi.testclient import TestClient

from truhowl.api.app import app
from truhowl.api.pilot import (
    ObservabilityTracker,
    PilotLimitsConfig,
    PilotLimitsEnforcer,
    PilotMetricsTracker,
)

client = TestClient(app)


def test_pilot_limits_enforcer():
    cfg = PilotLimitsConfig(
        enabled=True,
        allowed_orgs=["partner_org"],
        max_orgs=2,
        max_repos_per_org=2,
        max_daily_migrations=3,
        max_concurrent_repairs=1,
    )
    enforcer = PilotLimitsEnforcer(config=cfg)

    # Allowlist check
    ok, _ = enforcer.check_organization_access("partner_org")
    assert ok is True
    ok, reason = enforcer.check_organization_access("unauthorized_org")
    assert ok is False
    assert "not in the design-partner pilot allowlist" in reason

    # Repo quota
    ok, _ = enforcer.check_can_add_repo("partner_org", 1)
    assert ok is True
    ok, reason = enforcer.check_can_add_repo("partner_org", 2)
    assert ok is False
    assert "Pilot repository limit reached" in reason

    # Concurrency quota
    ok, _ = enforcer.check_can_start_migration("partner_org")
    assert ok is True
    enforcer.record_migration_started("partner_org")
    ok, reason = enforcer.check_can_start_migration("partner_org")
    assert ok is False
    assert "Max concurrent repairs reached" in reason
    enforcer.record_migration_finished("partner_org")
    ok, _ = enforcer.check_can_start_migration("partner_org")
    assert ok is True


def test_observability_tracker():
    obs = ObservabilityTracker()
    obs.record_error("verification", "clean-room replay failed exit 1", {"case": "stripe-1"})
    obs.record_duration("test_run", 1.25)
    obs.record_duration("test_run", 0.75)

    summary = obs.get_summary()
    assert summary["error_counts"]["verification"] == 1
    assert summary["average_durations_sec"]["test_run"] == 1.0
    assert len(summary["recent_errors"]) == 1


def test_pilot_metrics_tracker_and_observational_audit():
    tracker = PilotMetricsTracker()
    tracker.increment("org_a", "changes_detected", 5)

    # Legitimate verified run
    valid = tracker.record_verification_observation("org_a", claimed_verified=True, independent_replay_exit_code=0, scope_ok=True)
    assert valid is True
    metrics = tracker.get_metrics("org_a")
    assert metrics["verified_migrations"] == 1
    assert metrics["false_verified"] == 0

    # Contradicted verification run (e.g. replay failed)
    invalid = tracker.record_verification_observation("org_a", claimed_verified=True, independent_replay_exit_code=1, scope_ok=True)
    assert invalid is False
    metrics = tracker.get_metrics("org_a")
    assert metrics["false_verified"] == 1



def test_pilot_api_endpoints(tmp_path):
    headers = {"X-Truhowl-Workspace": str(tmp_path)}

    r_metrics = client.get("/api/pilot/metrics", headers=headers)
    assert r_metrics.status_code == 200
    assert "metrics" in r_metrics.json()
    assert r_metrics.json()["metrics"]["false_verified"] == 0

    r_obs = client.get("/api/pilot/observability", headers=headers)
    assert r_obs.status_code == 200
    assert "observability" in r_obs.json()

    # Onboarding complete
    r_onboard = client.post(
        "/api/onboarding/complete",
        json={"mode": "prepare", "repositories": ["billing"], "verification_cmd": "npm test"},
        headers=headers,
    )
    assert r_onboard.status_code == 200
    assert r_onboard.json()["status"] == "completed"
    assert r_onboard.json()["policy"]["mode"] == "prepare"
