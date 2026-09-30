# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Hunt safety hardening: concurrency, identity, promotion, secrets.

Covers audit follow-ups H1/H2/L1/M5 plus M2/M3/M4 audit labeling.
Each test proves one structural guarantee with no model involved.
"""

import json
import os
import subprocess
import threading
import time

import pytest

from truhowl import hunt as hunt_agent
from truhowl import hunt_ports as hp
from truhowl.hunt import (
    HuntLock,
    list_findings,
    run_hunt,
    verify_promotion,
)


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Truhowl Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@truhowl.dev"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _make_repo(tmp_path) -> str:
    repo = str(tmp_path / "svc")
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    with open(os.path.join(repo, "package.json"), "w") as f:
        json.dump({"dependencies": {"stripe": "^11.18.0"}}, f)
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("import stripe\nstripe.api_key = 'x'\ncharge = stripe.Charge.create(amount=100)\n")
    with open(os.path.join(repo, "tests", "test_sanity.py"), "w") as f:
        f.write("def test_sanity():\n    assert 1 + 1 == 2\n")
    _init_git_repo(repo)
    return repo


def _raw_patch(abs_path: str):
    from truhowl.patch_writer import PatchResult

    return PatchResult(file_path=abs_path, success=True, lines_changed=1,
                       unified_diff="--- a\n+++ b\n+x\n", rules_applied=["r"])


class _Reasoner:
    def reason(self, ctx, prior_evidence=""):
        return hunt_agent.HuntReasoning(
            problem="drift", smallest_change="annotate",
            affected_paths=["app.py"], must_not_change=["tests/test_sanity.py"])


class _SlowAuthor:
    """Holds the run inside the critical section so overlap is observable."""

    def __init__(self, windows):
        self.windows = windows
        self.calls = 0

    def author(self, *, ctx, plan, sandbox_dir, candidate_files, reasoning_attempt):
        self.calls += 1
        start = time.monotonic()
        time.sleep(0.4)
        target = os.path.join(sandbox_dir, "app.py")
        with open(target, "a", encoding="utf-8") as f:
            f.write("# slow\n")
        sealed = hp.seal_ai_patch(_raw_patch(target), model="m")
        assert sealed is not None
        self.windows.append((start, time.monotonic()))
        return [sealed]


class _Green:
    def verify(self, sandbox_dir, timeout=180):
        return hunt_agent.VerificationEvidence(
            command="c", exit_code=0, duration_ms=1, output="ok")


class _Convinced:
    def interpret(self, reasoning, evidence):
        return hunt_agent.HuntInterpretation(solved=True, unrelated_behavior=False, cause="hunt")


class _NullPub:
    def publish(self, repair, ctx, github_repo=None):
        return None


def _ports(windows):
    return hp.HuntPorts(
        context=hunt_agent._DefaultContext(),
        reasoner=_Reasoner(),
        author=_SlowAuthor(windows),
        sandbox=hunt_agent._DefaultSandbox(),
        verifier=_Green(),
        interpreter=_Convinced(),
        publisher=_NullPub(),
    )


# ── H1: finding identity is spelling-independent ──────────────────────────

def test_finding_ids_stable_across_path_spellings(tmp_path, monkeypatch):
    """Same checkout via a symlink must yield the same finding ids.

    Regression: absolute paths leaked into ids, so /tmp/x and
    /private/tmp/x (same dir on macOS) produced different ids and
    `truhowl hunt <id>` refused real findings as unknown.
    """
    repo = _make_repo(tmp_path)
    link = str(tmp_path / "linked")
    try:
        os.symlink(repo, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    direct = sorted(f.finding_id for f in list_findings(repo))
    assert direct, "expected at least one finding"
    via_link = sorted(f.finding_id for f in list_findings(link))
    assert via_link == direct
    monkeypatch.chdir(str(tmp_path))
    via_rel = sorted(f.finding_id for f in list_findings("svc"))
    assert via_rel == direct


def test_finding_affected_files_are_repo_relative(tmp_path):
    repo = _make_repo(tmp_path)
    findings = list_findings(repo)
    assert findings
    for f in findings:
        for p in f.affected_files:
            assert not os.path.isabs(p), p

# ── L1: symlinks never cross the promotion boundary ───────────────────────

def test_promote_refuses_symlinked_source(tmp_path):
    import pytest as _pytest

    repo = _make_repo(tmp_path)
    box = hunt_agent.create_sandbox(repo, "")
    try:
        outside = str(tmp_path / "outside.py")
        with open(outside, "w") as f:
            f.write("secret = 1\n")
        link = os.path.join(box.sandbox_dir, "linked.py")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            _pytest.skip("symlinks unavailable")
        sealed = hp.seal_ai_patch(_raw_patch(link), model="m")
        assert sealed is not None
        with _pytest.raises(ValueError, match="symlink"):
            hunt_agent._promote_sandbox(box.sandbox_dir, repo, [sealed])
        assert not os.path.exists(os.path.join(repo, "linked.py"))
    finally:
        hunt_agent.destroy_sandbox(repo, box)


def test_promote_refuses_symlinked_destination(tmp_path):
    import pytest as _pytest

    repo = _make_repo(tmp_path)
    box = hunt_agent.create_sandbox(repo, "")
    try:
        target = str(tmp_path / "target.py")
        with open(target, "w") as f:
            f.write("target = 1\n")
        dest_link = os.path.join(repo, "app.py")
        os.remove(dest_link)
        try:
            os.symlink(target, dest_link)
        except (OSError, NotImplementedError):
            _pytest.skip("symlinks unavailable")
        sealed = hp.seal_ai_patch(
            _raw_patch(os.path.join(box.sandbox_dir, "app.py")), model="m")
        assert sealed is not None
        with _pytest.raises(ValueError, match="symlink"):
            hunt_agent._promote_sandbox(box.sandbox_dir, repo, [sealed])
        assert open(target).read() == "target = 1\n"
    finally:
        hunt_agent.destroy_sandbox(repo, box)


# ── M5: secrets never reach audit persistence ───────────────────────────────

def test_hunt_audit_contains_no_repo_secrets(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    with open(os.path.join(repo, "app.py"), "a") as f:
        f.write('TOKEN = "sk-live-FAKEVALUE1234567890"\n')
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "secret"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")

    class RedVerifier:
        def verify(self, sandbox_dir, timeout=180):
            return hunt_agent.VerificationEvidence(
                command="pytest -q", exit_code=1, duration_ms=1,
                output="leaked sk-live-FAKEVALUE1234567890 in output")

    ports = hp.HuntPorts(
        context=hunt_agent._DefaultContext(),
        reasoner=_Reasoner(),
        author=_SlowAuthor([]),
        sandbox=hunt_agent._DefaultSandbox(),
        verifier=RedVerifier(),
        interpreter=_Convinced(),
        publisher=_NullPub(),
    )
    finding = list_findings(repo)[0].finding_id
    report = run_hunt(repo, finding, ports=ports, max_iterations=1)
    assert report.success is False
    blob = open(report.audit_path, encoding="utf-8").read()
    assert "FAKEVALUE" not in blob


# ── M3: drift described as registry-driven, never observed ─────────────────

def test_finding_summary_admits_registry_basis(tmp_path):
    findings = list_findings(_make_repo(tmp_path))
    assert findings
    assert "registry" in findings[0].basis
    assert "not independently observed" in findings[0].basis


def test_context_labels_drift_evidence(tmp_path):
    from truhowl.hunt import gather_context

    repo = _make_repo(tmp_path)
    ctx = gather_context(repo, list_findings(repo)[0])
    assert "not independently observed" in ctx.external_change["evidence"]


# ── M2/M4: AI judgments labeled as AI judgments ────────────────────────────

def test_verified_audit_states_evidence_limits(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    ports = hp.HuntPorts(
        context=hunt_agent._DefaultContext(),
        reasoner=_Reasoner(),
        author=_SlowAuthor([]),
        sandbox=hunt_agent._DefaultSandbox(),
        verifier=_Green(),
        interpreter=_Convinced(),
        publisher=_NullPub(),
    )
    finding = list_findings(repo)[0].finding_id
    report = run_hunt(repo, finding, ports=ports, max_iterations=1)
    assert report.success is True
    audit = json.load(open(report.audit_path, encoding="utf-8"))
    assert "registry" in audit["drift_basis"]
    limits = audit["evidence_limits"]
    assert any("coverage" in line and "AI-judged" in line for line in limits)
    assert any("minimality" in line for line in limits)
    interp = audit["interpretation"][0]
    assert interp["coverage_provenance"].startswith("ai-judged")
    assert interp["cause_provenance"].startswith("ai-attributed")


# ── H2: repo-level lock ───────────────────────────────────────────────────

def test_concurrent_hunts_serialize_on_repo_lock(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    windows: list = []
    finding = list_findings(repo)[0].finding_id
    results = []

    def run():
        ports = _ports(windows)
        results.append(run_hunt(repo, finding, ports=ports, max_iterations=1).success)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert results == [True, True]
    (a_start, a_end), (b_start, b_end) = sorted(windows)
    assert a_end <= b_start, "critical sections overlapped: lock did not serialize"


def test_held_lock_refuses_loudly(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    held = HuntLock(repo)
    assert held.acquire(timeout_s=5) is True
    try:
        finding = list_findings(repo)[0].finding_id
        report = run_hunt(repo, finding, ports=_ports([]),
                          max_iterations=1, lock_timeout_s=1)
        assert report.success is False
        assert report.pr_url is None
        assert "already running" in report.reason
        audit = json.load(open(report.audit_path, encoding="utf-8"))
        assert audit["final"] == "refused_lock"
    finally:
        held.release()


def test_lock_released_after_run(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    finding = list_findings(repo)[0].finding_id
    run_hunt(repo, finding, ports=_ports([]), max_iterations=1)
    fresh = HuntLock(repo)
    assert fresh.acquire(timeout_s=5) is True
    fresh.release()


# ── H2: post-promotion verification ───────────────────────────────────────

def test_verify_promotion_accepts_clean_state(tmp_path):
    repo = _make_repo(tmp_path)
    box = hunt_agent.create_sandbox(repo, "")
    try:
        with open(os.path.join(box.sandbox_dir, "app.py"), "a") as f:
            f.write("# x\n")
        promoted = hunt_agent._promote_sandbox(box.sandbox_dir, repo, [
            hp.seal_ai_patch(_raw_patch(os.path.join(box.sandbox_dir, "app.py")), model="m")])
        ok, reason = verify_promotion(box.sandbox_dir, repo, promoted, set())
        assert ok is True, reason
    finally:
        hunt_agent.destroy_sandbox(repo, box)


def test_verify_promotion_catches_tampered_file(tmp_path):
    repo = _make_repo(tmp_path)
    box = hunt_agent.create_sandbox(repo, "")
    try:
        with open(os.path.join(box.sandbox_dir, "app.py"), "a") as f:
            f.write("# x\n")
        promoted = hunt_agent._promote_sandbox(box.sandbox_dir, repo, [
            hp.seal_ai_patch(_raw_patch(os.path.join(box.sandbox_dir, "app.py")), model="m")])
        with open(os.path.join(repo, "app.py"), "a") as f:
            f.write("# tampered after promote\n")
        ok, reason = verify_promotion(box.sandbox_dir, repo, promoted, set())
        assert ok is False
        assert "diverged" in reason
    finally:
        hunt_agent.destroy_sandbox(repo, box)


def test_verify_promotion_catches_unexpected_files_but_tolerates_baseline(tmp_path):
    repo = _make_repo(tmp_path)
    with open(os.path.join(repo, "notes.txt"), "w") as f:
        f.write("pre-existing dirt\n")
    baseline = hunt_agent._worktree_dirty_set(repo)
    assert "notes.txt" in baseline
    box = hunt_agent.create_sandbox(repo, "")
    try:
        with open(os.path.join(repo, "intruder.txt"), "w") as f:
            f.write("outside writer\n")
        ok, reason = verify_promotion(box.sandbox_dir, repo, ["app.py"], baseline)
        assert ok is False
        assert "intruder.txt" in reason
        os.remove(os.path.join(repo, "intruder.txt"))
        ok, _ = verify_promotion(box.sandbox_dir, repo, ["app.py"], baseline)
        assert ok is True
    finally:
        hunt_agent.destroy_sandbox(repo, box)
