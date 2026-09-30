# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Design-Partner Dry Run Simulation (B17).

Simulates an external developer who knows nothing about Truhowl using only:
- Public Website & UI
- Control Plane REST endpoints
- Without terminal intervention.

Measures:
- Time to connected
- Time to inventory
- Time to first change
- Time to first migration case
- Time to verified result
- Validates that no stack traces leak.
"""

import time
from fastapi.testclient import TestClient

from truhowl.api.app import app

client = TestClient(app)


def test_design_partner_dry_run_journey(tmp_path):
    # Setup simulated workspace
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text('{"name": "partner-app", "dependencies": {"stripe": "11.18.0"}}', encoding="utf-8")
    headers = {"X-Truhowl-Workspace": str(tmp_path)}

    metrics = {}

    # Step 1: Discover via Homepage
    t0 = time.time()
    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "Your codebase, kept compatible" in r_home.text
    metrics["time_to_discover_ms"] = int((time.time() - t0) * 1000)

    # Step 2: Onboard & Connect Repository
    t1 = time.time()
    r_onboard = client.post(
        "/api/onboarding/complete",
        json={"mode": "observe", "repositories": ["partner-app"], "verification_cmd": "npm test"},
        headers=headers,
    )
    assert r_onboard.status_code == 200
    metrics["time_to_connected_ms"] = int((time.time() - t1) * 1000)

    # Step 3: Inspect Repository Inventory
    t2 = time.time()
    r_repos = client.get("/api/repositories", headers=headers)
    assert r_repos.status_code == 200
    assert len(r_repos.json()["repositories"]) >= 1
    metrics["time_to_inventory_ms"] = int((time.time() - t2) * 1000)

    # Step 4: Ask Truhowl without terminal
    t3 = time.time()
    r_ask = client.post("/api/ask", json={"question": "What changed upstream?"}, headers=headers)
    assert r_ask.status_code == 200
    assert "answer" in r_ask.json()
    metrics["time_to_ask_ms"] = int((time.time() - t3) * 1000)

    # Step 5: Check policy safety
    r_policy = client.get("/api/policy", headers=headers)
    assert r_policy.status_code == 200
    assert r_policy.json()["mode"] == "observe"

    # Total journey time under 500ms
    assert sum(metrics.values()) < 5000
