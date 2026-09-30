# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Production Database and Storage Adapters (B3)."""

from truhowl.api.db import (
    Attempt,
    AuditEvent,
    DeliveryRecord,
    Evidence,
    GitHubInstallation,
    PilotMetric,
    PostgresStoreAdapter,
    RelationalStoreAdapter,
    SQLiteStoreAdapter,
    User,
)


def test_sqlite_store_all_entities(tmp_path):
    db_file = str(tmp_path / "truhowl_pilot.db")
    store = SQLiteStoreAdapter(db_file)

    # 1. User
    store.add_user(
        User(
            user_id="usr_123",
            github_id="gh_456",
            username="octocat",
            email="octo@example.com",
            avatar_url="https://avatar",
        )
    )
    user = store.get_user("usr_123")
    assert user is not None
    assert user["username"] == "octocat"

    # 2. Organization & Automation Policy
    store.set_automation_policy("org_pilot", "prepare")
    org = store.get_organization("org_pilot")
    assert org is not None
    assert org["automation_mode"] == "prepare"

    # 3. GitHub Installation
    store.add_github_installation(
        GitHubInstallation(installation_id="inst_999", org_id="org_pilot", account_name="pilot-team")
    )
    inst = store.get_github_installation("inst_999")
    assert inst is not None
    assert inst["account_name"] == "pilot-team"

    # 4. Repository
    store.add_repository("org_pilot", "repo_billing", "Billing", "/path/billing", verification_cmd="npm test")
    repos = store.list_repositories("org_pilot")
    assert len(repos) == 1
    assert repos[0]["repo_key"] == "repo_billing"

    # 5. External Change
    store.record_external_change(
        "org_pilot",
        {"change_id": "chg_stripe", "provider": "stripe", "version_from": "11.18.0", "version_to": "13.0.0"},
    )
    changes = store.list_external_changes("org_pilot")
    assert len(changes) == 1
    assert changes[0]["provider"] == "stripe"

    # 6. Migration Case
    store.record_case(
        "org_pilot",
        {"case_id": "case_stripe", "provider": "stripe", "status": "verified"},
    )
    case = store.get_case("org_pilot", "case_stripe")
    assert case is not None
    assert case["status"] == "verified"

    # 7. Attempt
    store.record_attempt(
        Attempt(
            attempt_id="att_1",
            case_id="case_stripe",
            org_id="org_pilot",
            patch_hash="abc123hash",
            tier="behavioral_verified",
            verified=True,
            replay_exit_code=0,
        )
    )
    attempts = store.list_attempts("org_pilot", "case_stripe")
    assert len(attempts) == 1
    assert attempts[0]["verified"] == 1

    # 8. Evidence
    store.record_evidence(
        Evidence(
            evidence_id="evi_1",
            case_id="case_stripe",
            org_id="org_pilot",
            test_cmd="npm test",
            replay_cmd="npm test",
            diff_text="+ fixed code",
        )
    )
    evi = store.get_evidence("org_pilot", "case_stripe")
    assert evi is not None
    assert evi["test_cmd"] == "npm test"

    # 9. Delivery
    store.record_delivery(
        DeliveryRecord(
            delivery_id="del_1",
            case_id="case_stripe",
            org_id="org_pilot",
            status="published",
            pr_url="https://github.com/org/repo/pull/1",
            pr_number=1,
        )
    )
    delivery = store.get_delivery("org_pilot", "case_stripe")
    assert delivery is not None
    assert delivery["status"] == "published"
    assert delivery["pr_number"] == 1

    # 10. Audit Events
    store.record_audit_event(
        AuditEvent(
            event_id="aud_1",
            org_id="org_pilot",
            event_type="policy_change",
            actor="admin",
            details="Changed to prepare",
        )
    )
    audits = store.list_audit_events("org_pilot")
    assert len(audits) == 1
    assert audits[0]["event_type"] == "policy_change"

    # 11. Pilot Metrics
    store.record_pilot_metric(
        PilotMetric(metric_id="met_1", org_id="org_pilot", name="time_to_pr", value=42.5)
    )
    metrics = store.list_pilot_metrics("org_pilot")
    assert len(metrics) == 1
    assert metrics[0]["value"] == 42.5

    # 12. Restart Persistence Check: reopen existing file
    store2 = SQLiteStoreAdapter(db_file)
    assert store2.get_user("usr_123")["username"] == "octocat"
    assert store2.get_case("org_pilot", "case_stripe")["status"] == "verified"
    assert store2.get_delivery("org_pilot", "case_stripe")["pr_number"] == 1


def test_relational_adapter_routing(tmp_path):
    sqlite_db = str(tmp_path / "adapter.db")
    rel_store = RelationalStoreAdapter(sqlite_db)
    rel_store.add_repository("org_x", "repo_x", "RepoX", "/path")
    assert len(rel_store.list_repositories("org_x")) == 1


def test_postgres_adapter_initialization():
    # Validates PostgresStoreAdapter can be initialized with postgresql DSN
    pg_store = PostgresStoreAdapter("postgresql://user:pass@localhost:5432/truhowl_test")
    assert pg_store.dsn == "postgresql://user:pass@localhost:5432/truhowl_test"
