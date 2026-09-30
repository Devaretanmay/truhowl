# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Truhowl Control Plane API endpoints."""

from fastapi.testclient import TestClient

from truhowl.api.app import app

client = TestClient(app)


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["product"] == "Truhowl"
    assert data["version"] == "1.2.0"


def test_render_ui():
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>Truhowl" in response.text
    assert "Activity" in response.text


def test_session_auth():
    response = client.get("/api/auth/session")
    assert response.status_code == 200
    data = response.json()
    assert "user_id" in data
    assert "org_id" in data


def test_repositories_endpoints(tmp_path):
    # Test list repositories
    headers = {"X-Truhowl-Workspace": str(tmp_path)}
    resp = client.get("/api/repositories", headers=headers)
    assert resp.status_code == 200
    assert "repositories" in resp.json()

    # Test add repository
    add_resp = client.post("/api/repositories", json={"path": str(tmp_path), "repo_key": "test-repo"}, headers=headers)
    assert add_resp.status_code == 200
    assert add_resp.json()["repo_key"] == "test-repo"


def test_policy_endpoints(tmp_path):
    headers = {"X-Truhowl-Workspace": str(tmp_path)}
    # Get policy (default OBSERVE)
    resp = client.get("/api/policy", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "observe"

    # Set policy to PREPARE
    set_resp = client.post("/api/policy", json={"mode": "prepare"}, headers=headers)
    assert set_resp.status_code == 200
    assert set_resp.json()["mode"] == "prepare"

    # Reject invalid mode
    bad_resp = client.post("/api/policy", json={"mode": "yolo"}, headers=headers)
    assert bad_resp.status_code == 400


def test_ask_endpoint(tmp_path):
    headers = {"X-Truhowl-Workspace": str(tmp_path)}
    resp = client.post("/api/ask", json={"question": "What changed upstream?"}, headers=headers)
    assert resp.status_code == 200
    assert "answer" in resp.json()
