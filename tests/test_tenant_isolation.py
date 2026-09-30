# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Tenant Isolation & Organization Boundaries."""

from truhowl.api.auth import generate_session_token, validate_session, verify_tenant_access
from truhowl.api.db import RelationalStoreAdapter


def test_tenant_session_validation():
    token = generate_session_token(user_id="user_a", org_id="org_alpha", username="alpha")
    session = validate_session(token)
    assert session is not None
    assert session["user_id"] == "user_a"
    assert session["org_id"] == "org_alpha"


def test_tenant_access_control():
    token_a = generate_session_token(user_id="user_a", org_id="org_alpha", username="alpha")
    session_a = validate_session(token_a)
    
    assert verify_tenant_access(session_a, "org_alpha") is True
    assert verify_tenant_access(session_a, "org_beta") is False


def test_relational_storage_tenant_isolation():
    db = RelationalStoreAdapter(":memory:")
    db.add_repository("org_alpha", "repo_a", "repo_a", "/path/a")
    db.add_repository("org_beta", "repo_b", "repo_b", "/path/b")

    repos_alpha = db.list_repositories("org_alpha")
    repos_beta = db.list_repositories("org_beta")

    assert len(repos_alpha) == 1
    assert repos_alpha[0]["repo_key"] == "repo_a"

    assert len(repos_beta) == 1
    assert repos_beta[0]["repo_key"] == "repo_b"


def test_api_tenant_authorization_isolation():
    from fastapi.testclient import TestClient
    from truhowl.api.app import app

    client = TestClient(app)

    # Session for Org Alpha
    token_a = generate_session_token(user_id="user_a", org_id="org_alpha", username="alpha")
    client.cookies.set("truhowl_session", token_a)

    sess = client.get("/api/auth/session").json()
    assert sess["org_id"] == "org_alpha"
    assert verify_tenant_access(sess, "org_beta") is False

