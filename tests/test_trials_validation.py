# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Validation of Koyote on migration trial fixtures.

Exercises:
1. Fixture drift detection (Stripe v11->v13 and OpenAI v3->v4).
2. AST callsite mapping across fixture files.
3. Fail-closed refusal and rollback behavior when credentials are missing or unauthorized.
4. Isolated sandbox execution with fixture test runners.
"""

import json
import os
import shutil
import subprocess

from koyote import hunt as hunt_agent
from koyote.hunt import list_findings, run_hunt
from koyote.patch_writer import PatchResult


def test_trials_taxonomy_stripe_drift_detection():
    fixture_dir = os.path.abspath("trials/fixtures/taxonomy_stripe")
    findings = list_findings(fixture_dir)
    assert len(findings) >= 1
    stripe_finding = next((f for f in findings if f.provider == "stripe"), None)
    assert stripe_finding is not None
    assert stripe_finding.provider == "stripe"
    assert stripe_finding.finding_id.startswith("stripe-")
    assert any("billing.ts" in f for f in stripe_finding.affected_files)
    assert "Stripe" in stripe_finding.summary


def test_trials_langchainjs_openai_drift_detection():
    fixture_dir = os.path.abspath("trials/fixtures/langchainjs_openai")
    findings = list_findings(fixture_dir)
    assert len(findings) >= 1
    openai_finding = next((f for f in findings if f.provider == "openai"), None)
    assert openai_finding is not None
    assert openai_finding.provider == "openai"
    assert openai_finding.finding_id.startswith("openai-")
    assert any("openai.ts" in f for f in openai_finding.affected_files)
    assert "createChatCompletion" in openai_finding.summary or "rewrite" in openai_finding.summary


def test_trials_calcom_stripe_drift_detection():
    fixture_dir = os.path.abspath("trials/fixtures/calcom_stripe")
    findings = list_findings(fixture_dir)
    assert len(findings) >= 1
    stripe_finding = next((f for f in findings if f.provider == "stripe"), None)
    assert stripe_finding is not None
    assert stripe_finding.provider == "stripe"
    assert stripe_finding.finding_id.startswith("stripe-")


def test_trials_fail_closed_without_live_credentials(tmp_path, monkeypatch):
    """When credentials are not configured, Hunt refuses loudly and creates no PR."""
    fixture_src = os.path.abspath("trials/fixtures/langchainjs_openai")
    test_repo = str(tmp_path / "repo")
    shutil.copytree(fixture_src, test_repo)

    # Initialize git repo in the test copy
    subprocess.run(["git", "init", "-b", "main"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.dev"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=test_repo, check=True, capture_output=True)

    # Blank out any credentials
    monkeypatch.setenv("KOYOTE_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "KOYOTE_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)

    findings = list_findings(test_repo)
    assert len(findings) >= 1
    finding_id = findings[0].finding_id

    report = run_hunt(test_repo, finding_id)
    assert report.success is False
    assert report.pr_url is None
    assert "koyote auth" in report.reason or "AI repair required" in report.reason
    assert os.path.isfile(report.audit_path)


class _TrialAIPlanner:
    def __init__(self, client=None):
        self.client = client

    def plan_and_apply(self, repo_dir=None, affected_files=None, **kwargs):
        # Applies the migration to package.json and openai.ts
        pkg_file = os.path.join(repo_dir, "package.json")
        src_file = os.path.join(repo_dir, "src/chat_models/openai.ts")

        with open(pkg_file, "r") as f:
            pkg_content = f.read()
        pkg_migrated = pkg_content.replace('"^3.3.0"', '"^4.0.0"')
        with open(pkg_file, "w") as f:
            f.write(pkg_migrated)

        with open(src_file, "r") as f:
            src_content = f.read()
        src_migrated = src_content.replace("createChatCompletion", "chat.completions.create")
        with open(src_file, "w") as f:
            f.write(src_migrated)

        return [
            PatchResult(
                file_path=pkg_file,
                success=True,
                lines_changed=1,
                unified_diff="--- a/package.json\n+++ b/package.json\n",
                rules_applied=["openai v4"],
            ),
            PatchResult(
                file_path=src_file,
                success=True,
                lines_changed=1,
                unified_diff="--- a/src/chat_models/openai.ts\n+++ b/src/chat_models/openai.ts\n",
                rules_applied=["openai v4"],
            ),
        ]


def test_trials_sandbox_execution_and_verification(tmp_path, monkeypatch):
    """Verifies that an AI repair is executed in an isolated sandbox and verified by the real test runner."""
    fixture_src = os.path.abspath("trials/fixtures/langchainjs_openai")
    test_repo = str(tmp_path / "langchainjs_openai")
    shutil.copytree(fixture_src, test_repo)

    subprocess.run(["git", "init", "-b", "main"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.dev"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=test_repo, check=True, capture_output=True)

    findings = list_findings(test_repo)
    openai_finding = next(f for f in findings if f.provider == "openai")

    reasoning_dict = {
        "problem": "OpenAI v3 createChatCompletion deprecated in favor of v4 chat.completions.create",
        "current_behavior": "Uses createChatCompletion from openai v3",
        "cause": "Upstream OpenAI SDK bump to v4",
        "intended_behavior": "Uses chat.completions.create",
        "affected_paths": ["package.json", "src/chat_models/openai.ts"],
        "affected_callsites": ["src/chat_models/openai.ts:17"],
        "related_matter": [],
        "explicitly_unaffected": ["test/run.js"],
        "assumptions": [],
        "smallest_change": "Update dependency version and rewrite method call",
        "must_not_change": ["test/run.js"],
        "regression_risks": [],
        "verification_plan": "node test/run.js must pass with zero errors",
        "file_intents": [
            {
                "path": "package.json",
                "why_inspected": "dependency manifest",
                "why_affected": "openai dependency bump",
                "why_change": "bump to v4",
                "why_replacement": "bump to v4",
                "why_not_surrounding": "unchanged",
                "intent_preserved": "dependencies",
                "evidence": "manifest",
            },
            {
                "path": "src/chat_models/openai.ts",
                "why_inspected": "callsite",
                "why_affected": "uses createChatCompletion",
                "why_change": "rewrite to v4",
                "why_replacement": "chat.completions.create",
                "why_not_surrounding": "unchanged",
                "intent_preserved": "completion logic",
                "evidence": "callsite",
            },
        ],
    }

    interpret_dict = {
        "solved": True,
        "unrelated_behavior": False,
        "assumptions_false": [],
        "cause": "hunt",
        "needs_more_investigation": False,
        "rationale": "node test/run.js passed with zero errors; ChatOpenAI adheres to v4 contract",
    }

    class _Resp:
        def __init__(self, c):
            self.content = c

    class _Client:
        def __init__(self, config=None):
            pass

        def complete(self, messages=None, system_prompt=None):
            if system_prompt and "verifying its own repair" in system_prompt:
                return _Resp(json.dumps(interpret_dict))
            return _Resp(json.dumps(reasoning_dict))

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _Client)
    monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _TrialAIPlanner)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    report = run_hunt(test_repo, openai_finding.finding_id, max_iterations=2)

    assert report.success is True
    assert report.test_exit_code == 0
    assert "node test/run.js" in report.test_command
    assert os.path.isfile(report.audit_path)
