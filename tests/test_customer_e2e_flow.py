# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

import json
import os
from unittest.mock import MagicMock, patch

from truhowl.ai_planner import AIPatchPlanner
from truhowl.credentials import save_credentials
from truhowl.github.client import GitHubAppClient
from truhowl.github.howl_bot import HowlBot
from truhowl.knowledge import lookup
from truhowl.llm import LLMClient, LLMResponse
from truhowl.maintenance import run_maintenance_cycle
from truhowl.pipeline import TriggerContext
from truhowl.repo_identity import (
    derive_repository_key,
    get_active_repo,
    get_repository,
    register_repository,
    set_active_repo,
    unregister_repository,
    STATE_AVAILABLE,
)


def _setup_fixture_repo(tmp_path) -> str:
    repo_dir = str(tmp_path / "billing_repo")
    os.makedirs(os.path.join(repo_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(repo_dir, "test"), exist_ok=True)

    pkg_json = {
        "name": "billing-service",
        "version": "1.0.0",
        "dependencies": {
            "stripe": "^11.18.0",
        },
        "scripts": {
            "test": "node test/run.js",
        },
    }
    with open(os.path.join(repo_dir, "package.json"), "w") as f:
        json.dump(pkg_json, f, indent=2)
    src_content = (
        'import Stripe from "stripe";\n'
        'const stripe = new Stripe(process.env.STRIPE_SECRET || "");\n\n'
        'export async function cancelSubscription(subId: string) {\n'
        '  return stripe.subscriptions.del(subId);\n'
        '}\n'
    )
    with open(os.path.join(repo_dir, "src", "billing.ts"), "w") as f:
        f.write(src_content)

    test_content = (
        'const fs = require("fs");\n'
        'const path = require("path");\n'
        'const file = fs.readFileSync(path.join(__dirname, "../src/billing.ts"), "utf8");\n'
        'if (file.includes("stripe.subscriptions.del(")) {\n'
        '  console.error("FAIL: deprecated del method still present");\n'
        '  process.exit(1);\n'
        '} else if (file.includes("stripe.subscriptions.cancel(")) {\n'
        '  console.log("PASS: upgraded to cancel");\n'
        '  process.exit(0);\n'
        '} else {\n'
        '  console.error("FAIL: unexpected file content");\n'
        '  process.exit(1);\n'
        '}\n'
    )
    with open(os.path.join(repo_dir, "test", "run.js"), "w") as f:
        f.write(test_content)

    return repo_dir


def test_customer_e2e_green_flow(tmp_path, monkeypatch):
    home_dir = str(tmp_path / "user_home")
    os.makedirs(home_dir, exist_ok=True)
    truhowl_dir = os.path.join(home_dir, ".truhowl")
    monkeypatch.setenv("HOME", home_dir)
    monkeypatch.setenv("TRUHOWL_DIR", truhowl_dir)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", os.path.join(truhowl_dir, "credentials.json"))

    save_credentials(provider="groq", api_key="gsk_test_mock_12345", model="llama-3.3-70b-versatile")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_mock_12345")

    repo_full_name = "acme-corp/billing-service"
    reg = register_repository(repo_full_name, repo_id="555001")
    set_active_repo(repo_full_name)

    repo_key = derive_repository_key(repo_full_name, repo_id="555001")
    assert reg["repo_key"] == repo_key
    assert reg["repo_key"].startswith("kyp_")
    assert reg["howl_state"] == STATE_AVAILABLE
    assert reg["hunt_state"] == STATE_AVAILABLE
    assert get_active_repo() == repo_full_name

    repo_dir = _setup_fixture_repo(tmp_path)
    initial_billing = open(os.path.join(repo_dir, "src", "billing.ts")).read()

    mock_gh_client = MagicMock(spec=GitHubAppClient)
    mock_gh_client.readonly = True
    mock_gh_client.create_issue.return_value = {"number": 101, "html_url": "https://github.com/acme-corp/billing-service/issues/101"}

    howl = HowlBot(client=mock_gh_client)
    trigger_ctx = TriggerContext(
        event_id="evt_123",
        event_type="drift_scan",
        repository=repo_full_name,
        ref="refs/heads/main",
        sha="abc1234",
        workdir=repo_dir,
        provider_name="stripe",
        from_version="11.18.0",
        to_version="13.0.0",
    )
    consult_res = howl.consult(trigger_ctx)

    assert consult_res["success"] is True
    assert consult_res["bot"] == "howl"
    assert consult_res["mode"] == "consult"
    assert consult_res["issue_number"] == 101
    assert open(os.path.join(repo_dir, "src", "billing.ts")).read() == initial_billing

    mock_llm_client = MagicMock(spec=LLMClient)
    mock_llm_client.complete.return_value = LLMResponse(
        content=(
            "<<<<<<< SEARCH\n"
            "  return stripe.subscriptions.del(subId);\n"
            "=======\n"
            "  return stripe.subscriptions.cancel(subId);\n"
            ">>>>>>> REPLACE"
        ),
        model="groq/llama-3.3-70b-versatile",
    )

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_e2e_test_token")
    with patch("truhowl.maintenance.AIPatchPlanner.from_env",
               classmethod(lambda cls, **k: AIPatchPlanner(client=mock_llm_client))):
        # Publication is owned by the delivery service now; it resolves the
        # git helpers from truhowl.git_ops at call time.
        with patch("truhowl.git_ops.git_commit_and_push", return_value=True):
            with patch("truhowl.git_ops.gh_create_pr", return_value="https://github.com/acme/billing/pull/42"):
                report = run_maintenance_cycle(
                    repo_dir=repo_dir,
                    provider_name="stripe",
                    from_version="11.18.0",
                    to_version="13.0.0",
                    create_pr=True,
                    github_repo=repo_full_name,
                )

    assert report.success is True
    assert report.repair_path == "ai-reasoning"
    assert report.blast_radius_verified is True
    assert report.unintended_files_modified == 0
    assert report.test_exit_code == 0
    assert report.pr_number == 42

    updated_billing = open(os.path.join(repo_dir, "src", "billing.ts")).read()
    assert "stripe.subscriptions.cancel(subId)" in updated_billing
    assert "stripe.subscriptions.del(subId)" not in updated_billing

    learned = lookup(repo_dir, "stripe", "11.18.0", "13.0.0")
    assert learned is not None

    removed = unregister_repository(repo_full_name)
    assert removed is True
    assert get_repository(repo_full_name) is None
    assert get_active_repo() is None


def test_customer_e2e_red_path_test_failure_aborts_pr(tmp_path, monkeypatch):
    home_dir = str(tmp_path / "user_home_red")
    truhowl_dir = os.path.join(home_dir, ".truhowl")
    monkeypatch.setenv("HOME", home_dir)
    monkeypatch.setenv("TRUHOWL_DIR", truhowl_dir)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", os.path.join(truhowl_dir, "credentials.json"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_mock_12345")

    repo_dir = _setup_fixture_repo(tmp_path)
    initial_billing = open(os.path.join(repo_dir, "src", "billing.ts")).read()

    bad_llm_client = MagicMock(spec=LLMClient)
    bad_llm_client.complete.return_value = LLMResponse(
        content=(
            "<<<<<<< SEARCH\n"
            "  return stripe.subscriptions.del(subId);\n"
            "=======\n"
            "  return syntax_error_breaking_patch(;\n"
            ">>>>>>> REPLACE"
        ),
        model="groq/llama-3.3-70b-versatile",
    )

    with patch("truhowl.maintenance.AIPatchPlanner.from_env",
               classmethod(lambda cls, **k: AIPatchPlanner(client=bad_llm_client))):
        report = run_maintenance_cycle(
            repo_dir=repo_dir,
            provider_name="stripe",
            from_version="11.18.0",
            to_version="13.0.0",
            create_pr=True,
            github_repo="acme/billing-service",
        )

    assert report.success is False
    assert report.pr_number is None
    assert report.pr_url is None
    assert open(os.path.join(repo_dir, "src", "billing.ts")).read() == initial_billing
