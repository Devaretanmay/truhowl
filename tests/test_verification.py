# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Canonical verification service: replay enforcement regression tests.

No VERIFIED without clean-room replay. No PR-ready without the same
evidence. These tests prove the contract at three levels: the service
primitives, require_verified, and the maintenance/agent entry points.
"""

import os

import pytest

from truhowl.verification import service as v


def _repo(tmp_path, files=None):
    repo = str(tmp_path / "r")
    os.makedirs(repo, exist_ok=True)
    for rel, content in (files or {"a.txt": "hello\n"}).items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return repo


def test_seal_success_round_trip(tmp_path):
    repo = _repo(tmp_path)
    session = v.begin(repo, ["a.txt"])
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("hello world\n")
    res = v.seal(session, "exit 0", scope_allow=["a.txt"])
    assert res.verified is True
    assert res.replay_exit_code == 0
    assert res.replay_command == "exit 0"
    assert len(res.patch_hash) == 64
    assert res.files == ["a.txt"]
    assert res.scope_ok is True
    assert open(os.path.join(repo, "a.txt")).read() == "hello world\n"


def test_seal_hash_stable(tmp_path):
    repo = _repo(tmp_path)
    s1 = v.begin(repo, ["a.txt"])
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("v2\n")
    r1 = v.seal(s1, "exit 0", scope_allow=["a.txt"])
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("hello\n")
    s2 = v.begin(repo, ["a.txt"])
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("v2\n")
    r2 = v.seal(s2, "exit 0", scope_allow=["a.txt"])
    assert r1.verified and r2.verified
    assert r1.patch_hash == r2.patch_hash


def test_replay_failure_refuses_and_restores(tmp_path):
    repo = _repo(tmp_path)
    script = os.path.join(repo, "run.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\nif [ -f .ran ]; then exit 1; fi\ntouch .ran\nexit 0\n")
    os.chmod(script, 0o755)
    session = v.begin(repo, ["a.txt"])
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("changed\n")
    res = v.seal(session, "sh run.sh", scope_allow=["a.txt"])
    assert res.verified is False
    assert "replay" in res.reason
    assert open(os.path.join(repo, "a.txt")).read() == "hello\n"


def test_scope_mismatch_refuses(tmp_path):
    repo = _repo(tmp_path, {"a.txt": "hello\n", "b.txt": "other\n"})
    session = v.begin(repo, ["a.txt", "b.txt"])
    with open(os.path.join(repo, "b.txt"), "w") as f:
        f.write("tampered\n")
    res = v.seal(session, "exit 0", scope_allow=["a.txt"])
    assert res.verified is False
    assert "scope" in res.reason
    assert open(os.path.join(repo, "b.txt")).read() == "other\n"


def test_no_command_refuses(tmp_path):
    repo = _repo(tmp_path)
    session = v.begin(repo, ["a.txt"])
    res = v.seal(session, "", scope_allow=["a.txt"])
    assert res.verified is False


def test_no_changes_refuses(tmp_path):
    repo = _repo(tmp_path)
    session = v.begin(repo, ["a.txt"])
    res = v.seal(session, "exit 0", scope_allow=["a.txt"])
    assert res.verified is False


def test_verify_candidate_end_to_end(tmp_path):
    repo = _repo(tmp_path)
    res = v.verify_candidate(repo, {"a.txt": "new content\n"},
                             scope_allow=["a.txt"], test_command="exit 0")
    assert res.verified is True
    assert open(os.path.join(repo, "a.txt")).read() == "new content\n"


def test_verify_candidate_failing_command_restores(tmp_path):
    repo = _repo(tmp_path)
    res = v.verify_candidate(repo, {"a.txt": "new content\n"},
                             scope_allow=["a.txt"], test_command="exit 1")
    assert res.verified is False
    assert open(os.path.join(repo, "a.txt")).read() == "hello\n"


def test_require_verified_enforcement():
    good = v.VerificationResult(verified=True, tier=v.BEHAVIORAL,
                                test_command="t", test_exit_code=0,
                                replay_exit_code=0, replay_command="t",
                                patch_hash="h" * 64, files=["a"], scope_ok=True)
    assert v.require_verified(good) is good
    with pytest.raises(ValueError):
        v.require_verified(v.VerificationResult(verified=False, reason="x"))
    no_replay = v.VerificationResult(verified=True, test_command="t",
                                     test_exit_code=0, patch_hash="h" * 64,
                                     files=["a"], scope_ok=True)
    with pytest.raises(ValueError):
        v.require_verified(no_replay)
    no_hash = v.VerificationResult(verified=True, test_command="t",
                                   test_exit_code=0, replay_exit_code=0,
                                   replay_command="t", files=["a"], scope_ok=True)
    with pytest.raises(ValueError):
        v.require_verified(no_hash)


def test_maintenance_flows_through_seal(tmp_path, monkeypatch):
    """run_maintenance_cycle cannot mint success without replay evidence."""
    import json
    from unittest.mock import MagicMock, patch
    from truhowl.ai_planner import AIPatchPlanner
    from truhowl.llm import LLMClient, LLMResponse
    from truhowl.maintenance import run_maintenance_cycle

    repo = _repo(tmp_path, {
        "package.json": json.dumps({"name": "t", "dependencies": {"stripe": "^1.0.0"},
                                    "scripts": {"test": "exit 0"}}),
        "src/a.ts": "stripe.charges.create({});\nhello\n",
    })
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(
        content="<<<<<<< SEARCH\nhello\n=======\nhello world\n>>>>>>> REPLACE",
        model="test",
    )
    with patch("truhowl.intelligence.has_valid_credentials", lambda: True):
        with patch("truhowl.maintenance.AIPatchPlanner.from_env",
                   classmethod(lambda cls, **k: AIPatchPlanner(client=client))):
            report = run_maintenance_cycle(repo_dir=repo, provider_name="stripe",
                                           from_version="1.0.0", to_version="2.0.0")
    assert report.success is True
    assert report.replay_command != ""
    assert report.replay_exit_code == 0
    assert len(report.patch_hash) == 64
    assert report.verification_tier in (v.BEHAVIORAL, v.COMPILE_ONLY)
