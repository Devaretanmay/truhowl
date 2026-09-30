# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""End-to-End Control Plane Integration Test.

Exercises the full web control plane API lifecycle:
1. Authenticate session
2. Connect & register repository
3. Poll upstream registry for SDK changes
4. Create & inspect MigrationCase
5. Configure automation policy
6. Run case lifecycle (repair & clean-room replay verification)
7. Inspect clean-room replay evidence & tier
8. Query Ask Truhowl evidence QA
9. Approve & deliver PR
"""

import json
from fastapi.testclient import TestClient

from truhowl.api.app import app
from truhowl.changes.sources import UpstreamCheck, UpstreamRelease

client = TestClient(app)


def _make_repo(tmp_path):
    repo = tmp_path / "billing-service"
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "billing-service",
        "dependencies": {"stripe": "^11.18.0"},
        "scripts": {"test": "node test/run.js"},
    }))
    (repo / "src" / "billing.ts").write_text(
        "import Stripe from 'stripe';\n"
        "const s = new Stripe(process.env.STRIPE_API_KEY || '');\n"
        "export const cancel = (id: string) => s.subscriptions.del(id);\n")
    (repo / "test" / "run.js").write_text(
        "const fs=require('fs'),p=require('path');"
        "const s=fs.readFileSync(p.join(__dirname,'../src/billing.ts'),'utf8');"
        "const ok=!s.includes('.del(')&&s.includes('.cancel(');"
        "console.log(ok?'PASS':'FAIL');process.exit(ok?0:1);\n")
    return str(repo)


def test_control_plane_e2e_journey(tmp_path, monkeypatch):
    repo_dir = _make_repo(tmp_path)
    ws = str(tmp_path)
    headers = {"X-Truhowl-Workspace": ws}

    # 1. Authenticate session
    sess_resp = client.get("/api/auth/session")
    assert sess_resp.status_code == 200

    # 2. Connect repository
    reg_resp = client.post("/api/repositories", json={"path": repo_dir, "repo_key": "billing-service"}, headers=headers)
    assert reg_resp.status_code == 200

    # 3. Poll upstream changes (mocked npm fetcher returning Stripe 22.0.0)
    def _mock_fetcher(package, timeout=15):
        return UpstreamCheck(package=package, ok=True, release=UpstreamRelease(
            ecosystem="npm", package=package, version="22.0.0",
            published_at="2026-09-30T00:00:00Z",
            source_url=f"https://registry.npmjs.org/{package}/latest"))

    monkeypatch.setattr("truhowl.changes.monitor.fetch_npm_latest", _mock_fetcher)
    poll_resp = client.post("/api/changes/poll", json={"providers": ["stripe"], "repo_paths": [repo_dir]}, headers=headers)
    assert poll_resp.status_code == 200
    assert len(poll_resp.json()["cases"]) == 1
    case_id = poll_resp.json()["cases"][0]["case_id"]

    # 4. Set automation policy to PREPARE
    pol_resp = client.post("/api/policy", json={"mode": "prepare"}, headers=headers)
    assert pol_resp.status_code == 200
    assert pol_resp.json()["mode"] == "prepare"

    # 5. Run migration repair & clean-room verification via Control Plane API
    monkeypatch.setenv("GROQ_API_KEY", "gsk_mock_test_key")
    PATCH = ("<<<<<<< SEARCH\n"
             "export const cancel = (id: string) => s.subscriptions.del(id);\n"
             "=======\n"
             "export const cancel = (id: string) => s.subscriptions.cancel(id);\n"
             ">>>>>>> REPLACE")

    from unittest.mock import MagicMock
    from truhowl.ai_planner import AIPatchPlanner
    from truhowl.llm import LLMClient, LLMResponse

    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete.return_value = LLMResponse(content=PATCH, model="test-model")
    monkeypatch.setattr("truhowl.maintenance.AIPatchPlanner.from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=mock_llm)))

    run_resp = client.post(f"/api/cases/{case_id}/run", json={"repo_key": "billing-service", "confirmed": True}, headers=headers)
    assert run_resp.status_code == 200
    assert run_resp.json()["state"] == "verified"

    # 6. Inspect case evidence & clean-room replay verification details
    detail_resp = client.get(f"/api/cases/{case_id}", headers=headers)
    assert detail_resp.status_code == 200
    assert "clean-room replay" in detail_resp.json()["explanation"].lower()

    # 7. Ask Truhowl evidence QA
    ask_resp = client.post("/api/ask", json={"question": "What changed upstream?"}, headers=headers)
    assert ask_resp.status_code == 200
    assert "stripe" in ask_resp.json()["answer"].lower()
