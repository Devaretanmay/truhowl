# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for Consult (Howl) and Work (Hunt) mode boundaries."""

import os
import subprocess
from unittest.mock import MagicMock, patch
from truhowl.github.howl_bot import HowlBot, ConsultBot
from truhowl.github.hunt_bot import HuntBot, WorkBot
from truhowl.pipeline import (
    AnalysisResult,
    DriftFinding,
    PipelinePolicy,
    PipelineResult,
    TriggerContext,
)


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Truhowl Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@truhowl.dev"], cwd=path, check=True, capture_output=True)


def test_consult_creates_github_issue_and_never_modifies_files(tmp_path):
    repo_dir = str(tmp_path)
    _init_git_repo(repo_dir)

    target_file = os.path.join(repo_dir, "app.py")
    original_code = "import openai\nres = openai.ChatCompletion.create(model='gpt-3.5-turbo', messages=[])\n"
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(original_code)

    subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)

    client = MagicMock()
    client.create_issue.return_value = {"number": 101, "html_url": "https://github.com/acme/service/issues/101"}
    consult = ConsultBot(client=client)

    finding = DriftFinding(
        provider_name="openai",
        display_name="OpenAI SDK",
        package_name="openai",
        current_version="0.28.0",
        target_version="1.0.0",
        breaking_change="OpenAI v1 migrated to client instances",
        affected_files=["app.py"],
        migration_guide_url="https://github.com/openai/openai-python/releases/tag/v1.0.0",
        is_auto_repairable=True,
    )
    analysis = AnalysisResult(
        context=MagicMock(),
        findings=[finding],
        providers_detected=[{"provider": "openai"}],
    )

    ctx = TriggerContext(
        event_id="evt-consult-1",
        event_type="external.change.drift",
        repository="acme/service",
        ref="main",
        sha="abc1234",
        workdir=repo_dir,
    )

    mock_planner = MagicMock()
    mock_planner.assess.return_value = {
        "body": "1. What changed: OpenAI v1 uses client instances.\n2. Affected: app.py:2.\n3. What must NOT change: Messages payload.\n4. Recommended: client = OpenAI(); client.chat.completions.create(...)\nConfidence: high",
        "confidence": "high",
    }

    with patch("truhowl.github.howl_bot.analyze_trigger_context", return_value=analysis), \
         patch("truhowl.github.howl_bot.has_valid_credentials", return_value=True), \
         patch("truhowl.github.howl_bot.AIPatchPlanner.from_env", return_value=mock_planner):
        res = consult.consult(ctx)

    assert res["success"] is True
    assert res["bot"] == "howl"
    assert res["mode"] == "consult"
    assert res["status"] == "issue_created"
    assert res["findings_count"] == 1
    assert res["issue_number"] == 101
    assert res["issue_url"] == "https://github.com/acme/service/issues/101"

    client.create_issue.assert_called_once()
    call_args = client.create_issue.call_args[1]
    assert call_args["repo"] == "acme/service"
    assert "[Truhowl Consult]" in call_args["title"]
    assert "No code was modified" in call_args["body"]
    assert "What changed" in call_args["body"]
    assert "Action:" in call_args["body"]

    client.create_pull_request.assert_not_called()
    client.create_branch.assert_not_called()
    client.create_or_update_file.assert_not_called()

    with open(target_file, "r", encoding="utf-8") as f:
        assert f.read() == original_code

    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
    assert git_status.stdout.strip() == ""

    commit_count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
    assert commit_count.stdout.strip() == "1"


def test_consult_interactive_pr_explains_without_touching_files(tmp_path):
    repo_dir = str(tmp_path)
    client = MagicMock()
    howl = HowlBot(client=client)

    finding = DriftFinding(
        provider_name="stripe",
        display_name="Stripe SDK",
        package_name="stripe",
        current_version="5.0.0",
        target_version="10.0.0",
        breaking_change="Charge.create replaced with PaymentIntent",
        migration_guide_url="https://stripe.com/docs/upgrades",
        affected_files=["billing.py"],
        is_auto_repairable=True,
    )
    analysis = AnalysisResult(
        context=MagicMock(),
        findings=[finding],
        providers_detected=[{"provider": "stripe"}],
    )

    ctx = TriggerContext(
        event_id="evt-explain-1",
        event_type="issue_comment.howl",
        repository="acme/service",
        ref="pr-12",
        sha="def5678",
        workdir=repo_dir,
        pr_number=42,
    )

    mock_planner = MagicMock()
    mock_planner.assess.return_value = {
        "body": "Stripe migration requires PaymentIntent creation.",
        "confidence": "high",
    }

    with patch("truhowl.github.howl_bot.analyze_trigger_context", return_value=analysis), \
         patch("truhowl.github.howl_bot.has_valid_credentials", return_value=True), \
         patch("truhowl.github.howl_bot.AIPatchPlanner.from_env", return_value=mock_planner):
        res = howl.explain_pull_request(ctx)

    assert res["success"] is True
    assert res["mode"] == "consult"
    assert res["status"] == "explained"
    client.post_pr_comment.assert_called_once()
    assert "@hunt repair" in res["comment_body"]
    client.create_issue.assert_not_called()
    client.create_pull_request.assert_not_called()


def test_consult_returns_clean_when_no_drift(tmp_path):
    client = MagicMock()
    howl = HowlBot(client=client)
    ctx = TriggerContext(
        event_id="evt-clean",
        event_type="external.change.drift",
        repository="acme/service",
        ref="main",
        sha="abc1234",
        workdir=str(tmp_path),
    )
    res = howl.consult(ctx)
    assert res["success"] is True
    assert res["status"] == "clean"
    assert res["findings_count"] == 0
    client.create_issue.assert_not_called()
    client.create_pull_request.assert_not_called()


def test_work_bot_delivers_verified_pr(tmp_path):
    client = MagicMock()
    work_bot = WorkBot(client=client)
    assert work_bot.policy.mode == "work"

    ctx = TriggerContext(
        event_id="evt-work-1",
        event_type="external.change.drift",
        repository="acme/service",
        ref="main",
        sha="abc1234",
        workdir=str(tmp_path),
    )

    mock_result = PipelineResult(
        context=ctx,
        analysis=MagicMock(test_command="pytest", test_exit_code=0),
        status="verified_fix",
        status_description="Truhowl: verified against test suite",
        committed=True,
        commit_url="https://github.com/acme/service/commit/12345",
        pr_url="https://github.com/acme/service/pull/99",
        comment_body="Verified PR opened",
    )

    with patch("truhowl.github.hunt_bot.MaintenancePipeline.run", return_value=mock_result):
        res = work_bot.execute_repair(ctx)

    assert res["success"] is True
    assert res["bot"] == "hunt"
    assert res["mode"] == "work"
    assert res["status"] == "verified_fix"
    assert res["committed"] is True
    assert res["pr_url"] == "https://github.com/acme/service/pull/99"
    assert "verified against test suite" in res["status_description"]


def test_bot_aliases_and_policy_defaults():
    assert ConsultBot is HowlBot
    assert WorkBot is HuntBot
    consult_policy = PipelinePolicy(mode="consult")
    assert consult_policy.mode == "consult"
    assert HowlBot(policy=consult_policy).policy.mode == "consult"
    work_policy = PipelinePolicy(mode="work")
    assert work_policy.mode == "work"
    assert HuntBot(policy=work_policy).policy.mode == "work"

