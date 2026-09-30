# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automation policy: conservative default, policy is the ceiling.

The product thesis requires autonomy, and autonomy requires a bound. These
tests pin the bound: detection is on by default, repair needs either a
one-shot confirmation or an explicit standing policy, and publication is
reachable only under DELIVER — no flag escalates past the policy.
"""

import os
import subprocess
import sys

from truhowl.agent import automation as auto
from truhowl.agent import models as m
from truhowl.agent import service as svc
from truhowl.maintenance import MaintenanceRunReport

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "trials", "fixtures", "taxonomy_stripe")


def _run_cli(args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    env.setdefault("TRUHOWL_LLM_KEY", "sk-ant-test-credential-key")
    return subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main"] + args,
        capture_output=True, text=True, env=env, cwd=str(cwd))


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


# ── Mode semantics ───────────────────────────────────────────────────────

def test_default_mode_is_observe(tmp_path):
    assert auto.get_mode(str(tmp_path)) == auto.OBSERVE
    assert auto.DEFAULT_MODE == auto.OBSERVE


def test_policy_persists_and_survives_reload(tmp_path):
    auto.set_mode(str(tmp_path), auto.DELIVER)
    assert auto.get_mode(str(tmp_path)) == auto.DELIVER
    assert auto.get_policy(str(tmp_path))["updated_at"]


def test_mode_normalization_and_rejection():
    assert auto.normalize_mode(" PREPARE ") == auto.PREPARE
    assert auto.normalize_mode(None) == auto.OBSERVE
    assert auto.normalize_mode("") == auto.OBSERVE
    for bad in ("turbo", "yolo", "full-auto"):
        try:
            auto.normalize_mode(bad)
            raise AssertionError(f"{bad} must be rejected")
        except ValueError:
            pass


def test_capability_matrix():
    assert auto.allows_repair(auto.OBSERVE) is False
    assert auto.allows_repair(auto.PREPARE) is True
    assert auto.allows_repair(auto.DELIVER) is True
    assert auto.allows_publish(auto.OBSERVE) is False
    assert auto.allows_publish(auto.PREPARE) is False
    assert auto.allows_publish(auto.DELIVER) is True


def test_corrupt_persisted_mode_falls_back_to_observe(tmp_path):
    """A hand-edited store must never widen permissions."""
    from truhowl.agent.models import load_store, save_store
    store = load_store(str(tmp_path))
    store.org["automation"] = {"mode": "godmode"}
    save_store(str(tmp_path), store)
    assert auto.get_mode(str(tmp_path)) == auto.OBSERVE


# ── OBSERVE refuses to repair ────────────────────────────────────────────

def test_observe_refuses_repair_without_confirmation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: called.append(1) or _ok_report(kw["repo_dir"]))
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key)
    assert out["state"] == m.NEEDS_ATTENTION
    assert "OBSERVE" in out["reason"]
    assert called == [], "OBSERVE must not invoke the repair engine at all"
    assert len(svc.cases_needing_attention(str(tmp_path))) == 1


def test_observe_repairs_when_explicitly_confirmed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: _ok_report(kw["repo_dir"]))
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                       confirmed=True)
    assert out["state"] == m.VERIFIED


# ── PREPARE repairs but can never publish ────────────────────────────────

def test_prepare_repairs_without_manual_confirmation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle",
                        lambda **kw: _ok_report(kw["repo_dir"]))
    auto.set_mode(str(tmp_path), auto.PREPARE)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key)
    assert out["state"] == m.VERIFIED


def test_prepare_cannot_publish_even_when_asked(monkeypatch, tmp_path):
    """The policy ceiling outranks the caller: create_pr is dropped."""
    monkeypatch.chdir(tmp_path)
    seen = {}

    def _capture(**kw):
        seen["create_pr"] = kw.get("create_pr")
        return _ok_report(kw["repo_dir"])

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _capture)
    auto.set_mode(str(tmp_path), auto.PREPARE)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                       create_pr=True, confirmed=True)
    assert seen["create_pr"] is False
    assert out["state"] == m.VERIFIED
    assert out.get("pr_url") is None


def test_observe_cannot_publish_even_when_confirmed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def _capture(**kw):
        seen["create_pr"] = kw.get("create_pr")
        return _ok_report(kw["repo_dir"])

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _capture)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                 create_pr=True, confirmed=True)
    assert seen["create_pr"] is False


# ── DELIVER publishes only after verification ────────────────────────────

def test_deliver_grants_publish(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def _pr(**kw):
        assert kw.get("create_pr") is True
        rep = _ok_report(kw["repo_dir"])
        rep.pr_url = "https://github.com/o/r/pull/9"
        rep.pr_number = 9
        return rep

    monkeypatch.setattr("truhowl.maintenance.run_maintenance_cycle", _pr)
    auto.set_mode(str(tmp_path), auto.DELIVER)
    case = svc.watch(str(tmp_path), "stripe", "11.18.0", "13.0.0", [FIXTURE])
    out = svc.run_repo(str(tmp_path), case.case_id, case.repos[0].repo_key,
                       create_pr=True)
    assert out["state"] == m.PR_READY
    assert out["pr_url"].endswith("/pull/9")


# ── CLI surface ──────────────────────────────────────────────────────────

def test_cli_policy_show_set_and_reject(tmp_path):
    show = _run_cli(["agent", "policy"], tmp_path)
    assert show.returncode == 0
    assert "OBSERVE" in show.stdout

    setm = _run_cli(["agent", "policy", "prepare"], tmp_path)
    assert setm.returncode == 0
    assert "PREPARE" in setm.stdout

    bad = _run_cli(["agent", "policy", "yolo"], tmp_path)
    assert bad.returncode == 0
    assert "Refused" in bad.stdout
    assert auto.get_mode(str(tmp_path)) == auto.PREPARE


def test_cli_ask_act_requires_yes(tmp_path):
    """`ask --act` without --yes refuses instead of repairing silently."""
    watch = _run_cli(["agent", "watch", "--provider", "stripe",
                      "--from", "11.18.0", "--to", "13.0.0", "--repo", FIXTURE], tmp_path)
    assert watch.returncode == 0

    bare = _run_cli(["ask", "--path", str(tmp_path), "--act",
                     "migrate everything you can safely verify"], tmp_path)
    assert bare.returncode == 0
    assert "Refused" in bare.stdout
    assert "--yes" in bare.stdout

    stored = svc.list_cases(str(tmp_path))[0]
    assert set(stored["repo_states"].values()) == {m.DETECTED}


def test_cli_ask_confirm_yes_executes(tmp_path):
    watch = _run_cli(["agent", "watch", "--provider", "stripe",
                      "--from", "11.18.0", "--to", "13.0.0", "--repo", FIXTURE], tmp_path)
    assert watch.returncode == 0
    # No LLM credentials here, so the repair engine may refuse — but it ran,
    # which is the point: --yes is what unlocks execution.
    confirmed = _run_cli(["ask", "--path", str(tmp_path), "--act", "--yes",
                          "migrate everything you can safely verify"], tmp_path)
    assert confirmed.returncode == 0
    stored = svc.list_cases(str(tmp_path))[0]
    assert set(stored["repo_states"].values()) != {m.DETECTED}


def test_cli_ask_answers_delivery_question(tmp_path):
    watch = _run_cli(["agent", "watch", "--provider", "stripe",
                      "--from", "11.18.0", "--to", "13.0.0", "--repo", FIXTURE], tmp_path)
    assert watch.returncode == 0
    ask = _run_cli(["ask", "--path", str(tmp_path),
                    "why didn't you open the PR?"], tmp_path)
    assert ask.returncode == 0
    assert "PR" in ask.stdout


def test_cli_ask_answers_upstream_question(tmp_path):
    ask = _run_cli(["ask", "--path", str(tmp_path), "what changed upstream?"], tmp_path)
    assert ask.returncode == 0
    assert "upstream" in ask.stdout.lower()
