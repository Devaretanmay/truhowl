# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests attacking the Clean-Room Replay and Attempt Isolation invariants.

Invariants under test:
1. "A repair MUST NEVER be called verified unless the exact patch that will be
    exported or committed passes verification from a clean baseline."
2. Sandbox state MUST NOT accumulate across repair attempts.
3. VerifiedRepair token MUST NOT be mintable if replay_exit_code != 0 or
    if replay verification failed.
"""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from koyote import hunt as hunt_agent
from koyote.hunt import (
    HuntInterpretation,
    VerificationEvidence,
    list_findings,
    run_hunt,
)
from koyote.hunt_ports import (
    HuntPorts,
    VerifiedRepair,
    seal_ai_patch,
    seal_verified_repair,
)
from koyote.patch_writer import PatchResult


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Koyote Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@koyote.dev"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _make_multi_file_repo(tmp_path) -> str:
    repo = str(tmp_path / "multi_file_svc")
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    with open(os.path.join(repo, "package.json"), "w") as f:
        json.dump({"dependencies": {"stripe": "^11.18.0"}}, f)
    with open(os.path.join(repo, "stripe.py"), "w") as f:
        f.write("class Charge:\n    @staticmethod\n    def create(amount=0):\n        return {'amount': amount}\n")
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("import stripe\ncharge = stripe.Charge.create(amount=100)\n")
    with open(os.path.join(repo, "consumer.py"), "w") as f:
        f.write("from app import charge\ndef get_charge():\n    return charge\n")
    with open(os.path.join(repo, "tests", "test_api.py"), "w") as f:
        f.write("def test_api():\n    assert 1 + 1 == 2\n")
    _init_git_repo(repo)
    return repo


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeClient:
    def __init__(self, config=None):
        self.calls = 0

    def complete(self, messages=None, system_prompt=None):
        self.calls += 1
        if system_prompt and "verifying its own repair" in system_prompt:
            return _FakeResp(json.dumps({
                "solved": True, "unrelated_behavior": False, "assumptions_false": [],
                "cause": "hunt", "needs_more_investigation": False,
                "rationale": "clean-room replay check passed",
            }))
        return _FakeResp(json.dumps({
            "problem": "stripe upgrade drift",
            "current_behavior": "charge created",
            "cause": "Upstream upgrade",
            "intended_behavior": "charge created with note",
            "affected_paths": ["app.py", "consumer.py"],
            "affected_callsites": ["app.py:2"],
            "related_matter": [],
            "explicitly_unaffected": ["tests/test_api.py"],
            "assumptions": [],
            "smallest_change": "update app.py",
            "must_not_change": ["tests/test_api.py"],
            "regression_risks": [],
            "verification_plan": "pytest -q",
            "file_intents": [],
        }))


def test_verified_repair_token_refuses_non_zero_exit_code():
    """VerifiedRepair cannot be instantiated directly without _SEAL."""
    with pytest.raises(TypeError, match="VerifiedRepair is sealed"):
        VerifiedRepair(
            finding_id="test-1",
            provider="openai",
            version_from="3.0",
            version_to="4.0",
            files=["app.py"],
            unified_diff="--- a\n+++ b\n",
            test_command="pytest -q",
            test_exit_code=1,
            replay_exit_code=1,
            replay_command="pytest -q",
        )


def test_seal_verified_repair_refuses_failed_replay(tmp_path):
    """seal_verified_repair must return None if replay evidence indicates failure."""
    raw_evidence = VerificationEvidence(
        command="pytest -q",
        exit_code=0,
        output="passed",
        duration_ms=50,
    )
    failed_replay_evidence = VerificationEvidence(
        command="pytest -q",
        exit_code=1,
        output="failed during clean-room replay",
        duration_ms=50,
    )
    sandbox_dir = str(tmp_path / "box")
    os.makedirs(sandbox_dir, exist_ok=True)
    patch_file = os.path.join(sandbox_dir, "app.py")
    with open(patch_file, "w") as f:
        f.write("# repair\n")

    sealed_patch = seal_ai_patch(
        SimpleNamespace(
            success=True,
            file_path=patch_file,
            unified_diff="--- a/app.py\n+++ b/app.py\n+# repair\n",
            lines_changed=1,
            rules_applied=["rule1"],
        ),
        model="gpt-test",
        reasoning_attempt=1,
    )
    assert sealed_patch is not None

    interpretation = HuntInterpretation(
        solved=True,
        unrelated_behavior=False,
        assumptions_false=[],
        cause="hunt",
        needs_more_investigation=False,
        rationale="all good",
    )

    token = seal_verified_repair(
        finding_id="f1",
        provider="stripe",
        version_from="11",
        version_to="12",
        patches=[sealed_patch],
        sandbox_dir=sandbox_dir,
        evidence=raw_evidence,
        interpretation=interpretation,
        affected_paths=["app.py"],
        must_not_change=[],
        reasoning_attempt=1,
        model="gpt-test",
        baseline_sha="abc1234",
        patch_hash="patch_hash_1",
        replay_evidence=failed_replay_evidence,
    )
    assert token is None, "seal_verified_repair must refuse when replay evidence exit_code != 0"


def test_multi_attempt_state_isolation_prevents_false_verification(tmp_path, monkeypatch):
    """If Attempt 1 mutates consumer.py (fails), Attempt 2 only mutates app.py,

    Attempt 2 MUST NOT inherit Attempt 1's mutations.
    Each attempt sandbox must start strictly from the pristine baseline.
    """
    repo = _make_multi_file_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _FakeClient)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    attempt_counter = [0]

    class _LeakyMockPlanner:
        def __init__(self, client=None):
            pass

        def plan_and_apply(self, repo_dir=None, affected_files=None, **kwargs):
            attempt_counter[0] += 1
            att = attempt_counter[0]
            results = []
            if att == 1:
                # Attempt 1: only modifies consumer.py with a syntax error to make verification fail!
                consumer_abs = os.path.join(repo_dir, "consumer.py")
                with open(consumer_abs, "w") as f:
                    f.write("def broken_syntax(:\n")
                results.append(PatchResult(
                    file_path=consumer_abs, success=True, lines_changed=1,
                    unified_diff="--- a/consumer.py\n+++ b/consumer.py\n+def broken_syntax(:\n",
                    rules_applied=["attempt1-patch"],
                ))
            elif att == 2:
                # In Attempt 2: verify that consumer.py is back to PRISTINE state!
                consumer_abs = os.path.join(repo_dir, "consumer.py")
                with open(consumer_abs, "r") as f:
                    consumer_content = f.read()
                # Must be baseline version, NOT containing 'broken_syntax' from Attempt 1!
                assert "broken_syntax" not in consumer_content, "State leaked from Attempt 1 to Attempt 2 sandbox!"

                # Attempt 2 modifies app.py with a valid note
                app_abs = os.path.join(repo_dir, "app.py")
                with open(app_abs, "w") as f:
                    f.write("# hunt note\nimport stripe\ncharge = stripe.Charge.create(amount=100)\n")
                results.append(PatchResult(
                    file_path=app_abs, success=True, lines_changed=1,
                    unified_diff="--- a/app.py\n+++ b/app.py\n+# hunt note\n",
                    rules_applied=["attempt2-patch"],
                ))
            return results

    monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _LeakyMockPlanner)

    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, max_iterations=2)
    assert report.success is True
    assert report.test_exit_code == 0
    assert report.files_modified == ["app.py"]

    # Verify that repo app.py has the verified repair note and consumer.py remains pristine
    with open(os.path.join(repo, "app.py")) as f:
        assert "# hunt note" in f.read()
    with open(os.path.join(repo, "consumer.py")) as f:
        assert "broken_syntax" not in f.read()


def test_clean_room_replay_fails_closed_on_synthetic_drift(tmp_path, monkeypatch):
    """If an attempt sandbox mysteriously passed verification (e.g. dirty sandbox side effect),

    but the candidate diff fails verification when replayed on a fresh baseline,
    Koyote MUST fail closed and refuse verification.
    """
    repo = _make_multi_file_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _FakeClient)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    class _MockPlanner:
        def __init__(self, client=None):
            pass

        def plan_and_apply(self, repo_dir=None, affected_files=None, **kwargs):
            app_abs = os.path.join(repo_dir, "app.py")
            with open(app_abs, "w") as f:
                f.write("# candidate\nimport stripe\ncharge = stripe.Charge.create(amount=100)\n")
            return [PatchResult(
                file_path=app_abs,
                success=True,
                lines_changed=1,
                unified_diff="--- a/app.py\n+++ b/app.py\n+# candidate\n",
                rules_applied=["rule1"],
            )]

    monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _MockPlanner)

    class _ReplayFailingVerifier:
        def __init__(self):
            self.calls = 0

        def verify(self, sandbox_dir: str, timeout: int = 180) -> VerificationEvidence:
            self.calls += 1
            if self.calls == 1:
                # Attempt verification passes spuriously
                return VerificationEvidence(
                    command="pytest -q",
                    exit_code=0,
                    output="1 passed",
                    duration_ms=40,
                )
            # Clean-room replay verification FAILS (exposing the flaw)
            return VerificationEvidence(
                command="pytest -q",
                exit_code=1,
                output="FAILED test_api - clean baseline reproduction failed",
                duration_ms=40,
            )

    from koyote.hunt import AIPlannerAuthor, default_ports
    base_ports = default_ports(hunt_agent.LLMClient(), _MockPlanner())
    fake_verifier = _ReplayFailingVerifier()
    ports = HuntPorts(
        context=base_ports.context,
        reasoner=base_ports.reasoner,
        author=AIPlannerAuthor(_MockPlanner()),
        sandbox=base_ports.sandbox,
        verifier=fake_verifier,
        interpreter=base_ports.interpreter,
        publisher=base_ports.publisher,
    )

    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, max_iterations=1, ports=ports)

    # Must fail closed!
    assert report.success is False
    assert "could not safely verify" in report.reason

    # Audit log must record clean_room_replay_failed
    audit = json.load(open(report.audit_path, encoding="utf-8"))
    decisions = [d.get("decision") for d in audit.get("decisions", [])]
    assert any("clean_room_replay_failed" in str(d) for d in decisions)
