# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Truhowl agent: domain model, lifecycle, orchestration."""

import os
import subprocess
import sys

import pytest

from truhowl.agent import models as m
from truhowl.agent import service as svc
from truhowl.maintenance import MaintenanceRunReport

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "trials", "fixtures", "taxonomy_stripe")


def _run_agent_cli(args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    env.setdefault("TRUHOWL_LLM_KEY", "sk-ant-test-credential-key")
    return subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main"] + args,
        capture_output=True, text=True, env=env, cwd=str(cwd),
    )


def _ok_report(repo_dir):
    return MaintenanceRunReport(
        success=True, provider_name="stripe",
        from_version="11.18.0", to_version="13.0.0",
        repository_path=repo_dir, files_scanned=5, files_modified=1,
        unintended_files_modified=0, blast_radius_verified=True,
        test_exit_code=0, test_duration_ms=10,
        unified_diff="diff --git a/b", trust_pr_body="body",
        replay_exit_code=0, replay_command="npm test",
        patch_hash="abc123", verification_tier="behavioral_verified",
    )


def _fail_report(repo_dir):
    rep = _ok_report(repo_dir)
    rep.success = False
    rep.test_exit_code = 1
    rep.error = "tests failed"
    return rep


def test_lifecycle_legal_path():
    r = m.CaseRepo(repo_key="a", path="/tmp/a")
    for s in (m.ANALYZING, m.PLANNING, m.REPAIRING, m.VERIFYING, m.VERIFIED, m.PR_READY):
        r.transition(s)
    assert r.state == m.PR_READY


def test_lifecycle_illegal_jump():
    r = m.CaseRepo(repo_key="a", path="/tmp/a")
    with pytest.raises(ValueError):
        r.transition(m.VERIFIED)


def test_lifecycle_retry_from_refused():
    r = m.CaseRepo(repo_key="a", path="/tmp/a", state=m.REFUSED)
    r.transition(m.ANALYZING)
    assert r.state == m.ANALYZING


def test_watch_creates_case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    assert case.provider == "stripe"
    assert len(case.repos) == 1
    assert case.repos[0].state == m.DETECTED
    stored = svc.get_case(str(tmp_path), case.case_id)
    assert stored is not None
    usage_files = svc.load_store(str(tmp_path)).usages[0]["files"]
    assert len(usage_files) > 0


def test_run_repo_verified(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: _ok_report(kw["repo_dir"]))
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    key = case.repos[0].repo_key
    out = svc.run_repo(str(tmp_path), case.case_id, key, confirmed=True)
    assert out["state"] == m.VERIFIED
    text = svc.explain_case(str(tmp_path), case.case_id)
    assert "verified" in text
    assert "blast-radius-zero=True" in text


def test_run_repo_pr_ready(monkeypatch, tmp_path):
    from truhowl.agent import automation

    monkeypatch.chdir(tmp_path)

    def _pr(**kw):
        rep = _ok_report(kw["repo_dir"])
        rep.pr_url = "https://github.com/o/r/pull/1"
        rep.pr_number = 1
        return rep

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _pr)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    # Publishing requires the DELIVER policy; a flag alone is never enough.
    automation.set_mode(str(tmp_path), automation.DELIVER)
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                       create_pr=True, confirmed=True)
    assert out["state"] == m.PR_READY
    assert out["pr_url"].endswith("/pull/1")


def test_run_repo_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: _fail_report(kw["repo_dir"]))
    monkeypatch.setattr("truhowl.test_runner._detect_test_command", lambda p: "npm test")
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    assert out["state"] == m.REFUSED
    assert "tests failed" in out["reason"]
    assert len(svc.cases_needing_attention(str(tmp_path))) == 1
    assert "reason" in svc.explain_case(str(tmp_path), case.case_id)


def test_run_repo_needs_attention_without_tests(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: _fail_report(kw["repo_dir"]))
    monkeypatch.setattr("truhowl.test_runner._detect_test_command", lambda p: "")
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    assert out["state"] == m.NEEDS_ATTENTION


def test_run_repo_no_usage_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "empty"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("print('hello')\n")
    (repo / "package.json").write_text('{"name": "t", "dependencies": {}}')
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [str(repo)])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    assert out["state"] == m.REFUSED
    assert "nothing to migrate" in out["reason"]


def test_run_repo_fabricated_success_without_replay_refused(monkeypatch, tmp_path):
    """A success report without clean-room replay evidence can never verify."""
    monkeypatch.chdir(tmp_path)

    def _no_replay(**kw):
        rep = _ok_report(kw["repo_dir"])
        rep.replay_exit_code = -1
        rep.replay_command = ""
        rep.patch_hash = ""
        return rep

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _no_replay)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key, confirmed=True)
    assert out["state"] == m.REFUSED
    stored = svc.get_case(str(tmp_path), case.case_id)
    assert stored["repos"][0]["state"] == m.REFUSED


def test_list_cases_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    assert len(svc.list_cases(str(tmp_path))) == 1
    assert svc.list_cases(str(tmp_path), state=m.VERIFIED) == []
    assert len(svc.list_cases(str(tmp_path), state=m.DETECTED)) == 1
    assert case.case_id.startswith("stripe-")


def test_cli_agent_watch_cases_show(tmp_path):
    watch = _run_agent_cli(["agent", "watch", "--provider", "stripe",
                            "--from", "11.18.0", "--to", "13.0.0",
                            "--repo", FIXTURE], tmp_path)
    assert watch.returncode == 0
    assert "detected" in watch.stdout
    case_id = [t for t in watch.stdout.split() if t.startswith("stripe-")][0].rstrip(":")

    cases = _run_agent_cli(["agent", "cases"], tmp_path)
    assert cases.returncode == 0
    assert case_id in cases.stdout

    show = _run_agent_cli(["agent", "show", case_id], tmp_path)
    assert show.returncode == 0
    assert "detected" in show.stdout


def test_cli_ask_uses_case_state(tmp_path):
    watch = _run_agent_cli(["agent", "watch", "--provider", "stripe",
                            "--from", "11.18.0", "--to", "13.0.0",
                            "--repo", FIXTURE], tmp_path)
    assert watch.returncode == 0
    case_id = [t for t in watch.stdout.split() if t.startswith("stripe-")][0].rstrip(":")

    ask = _run_agent_cli(["ask", "--path", str(tmp_path),
                          "what repos are affected by stripe?"], tmp_path)
    assert ask.returncode == 0
    assert case_id in ask.stdout

    propose = _run_agent_cli(["ask", "--path", str(tmp_path),
                              "migrate everything you can safely verify"], tmp_path)
    assert propose.returncode == 0
    assert f"truhowl agent run {case_id}" in propose.stdout

    attention = _run_agent_cli(["ask", "--path", str(tmp_path),
                                "show me migrations waiting for attention"], tmp_path)
    assert attention.returncode == 0
    assert "Nothing waiting for attention" in attention.stdout
