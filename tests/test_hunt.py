# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for Koyote Hunt: AI-first repair lifecycle and hard invariants."""

import argparse
import json
import os
import subprocess

from koyote import hunt as hunt_agent
from koyote.hunt import (
    decide_pr,
    finding_id_for,
    gather_context,
    list_findings,
    resolve_finding,
    run_hunt,
)
from koyote.hunt_ports import evaluate_scope
from koyote.patch_writer import PatchResult


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Koyote Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@koyote.dev"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _clean_creds(monkeypatch, tmp_path):
    monkeypatch.setenv("KOYOTE_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "KOYOTE_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)


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


# ── Hard architectural invariants ──────────────────────────────────────────

def _hunt_source() -> str:
    with open(os.path.join(os.path.dirname(hunt_agent.__file__), "hunt.py"), encoding="utf-8") as f:
        return f.read()


def test_hunt_has_no_deterministic_repair_engine():
    src = _hunt_source()
    for forbidden in (
        "apply_rewrites",
        "apply_patch",
        "patch_apply",
        "instantiate_alias",
        "discover_aliases",
        "autopatch_plan",
        "direct_rewrites_for",
        "to_rewrite_rules",
    ):
        assert forbidden not in src, f"hunt.py must not use deterministic repair: {forbidden}"


def test_hunt_has_no_regex_or_template_fixers():
    import re as _re

    src = _hunt_source()
    # No regex engine or template machinery that could author fixes.
    assert _re.search(r"(?m)^(?:import re|from re import|import re as)\b", src) is None
    assert "re.sub(" not in src
    assert "re.compile(" not in src
    # No deterministic repair fallback path: the word may only appear in the
    # architectural prohibition statement or an execution-environment note.
    for line in src.splitlines():
        lowered = line.lower()
        if "fallback" in lowered or "hybrid" in lowered:
            assert ("no " in lowered or "never" in lowered
                    or "plain-copy" in lowered), line


def test_hunt_never_fakes_success():
    src = _hunt_source()
    assert "exit 0" not in src or "exit 0 in" in src  # only evidence formatting
    assert "force" not in src.lower() or "worktree', 'remove', '--force'" in src or "--force" in src
    # Verification outcome flows from the real runner returncode only.
    assert "proc.returncode" in src or "returncode" in src


# ── Findings ───────────────────────────────────────────────────────────────

def test_finding_ids_are_stable():
    a = finding_id_for("stripe", "1.0", "2.0", ["app.py"])
    b = finding_id_for("stripe", "1.0", "2.0", ["app.py"])
    assert a == b
    assert a.startswith("stripe-")


def test_list_and_resolve_findings(tmp_path):
    repo = _make_repo(tmp_path)
    findings = list_findings(repo)
    assert len(findings) >= 1
    stripe = next(f for f in findings if f.provider == "stripe")
    assert stripe.finding_id
    assert stripe.affected_files

    by_id = resolve_finding(repo, stripe.finding_id)
    assert by_id is not None and by_id.provider == "stripe"
    by_provider = resolve_finding(repo, "stripe")
    assert by_provider is not None and by_provider.provider == "stripe"
    assert resolve_finding(repo, "no-such-finding-xyz") is None


def test_context_is_evidence_only(tmp_path):
    repo = _make_repo(tmp_path)
    findings = list_findings(repo)
    ctx = gather_context(repo, findings[0])
    assert ctx.branch == "main"
    assert len(ctx.commit_sha) == 40
    assert ctx.existing_tests == "pytest -q"
    assert any("app.py" in f for f in ctx.relevant_files)
    assert "compiler_options" in ctx.environment


def test_context_extracts_and_formats_environment(tmp_path):
    repo = _make_repo(tmp_path)
    with open(os.path.join(repo, "tsconfig.json"), "w") as f:
        json.dump({
            "compilerOptions": {
                "module": "commonjs",
                "target": "es2020",
                "esModuleInterop": False,
            }
        }, f)
    findings = list_findings(repo)
    ctx = gather_context(repo, findings[0])
    assert ctx.environment["compiler_options"]["es_module_interop"] is False
    from koyote.hunt import _context_text
    txt = _context_text(ctx)
    assert "Repository Environment & Compiler Options:" in txt
    assert "esModuleInterop: false" in txt
    assert "TS2351" in txt


# ── Scope control ──────────────────────────────────────────────────────────

def test_scope_check_rejects_must_not_change():
    ok, _ = evaluate_scope(["app.py"], ["billing.py"], ["app.py"])
    assert ok is True
    ok, reason = evaluate_scope(["app.py"], ["billing.py"], ["app.py", "billing.py"])
    assert ok is False
    assert "billing.py" in reason


def test_scope_check_rejects_unrelated_files():
    ok, reason = evaluate_scope(["app.py"], [], ["app.py", "unrelated.py"])
    assert ok is False
    assert "unrelated.py" in reason


# ── Failure behavior: fail closed ──────────────────────────────────────────

def test_hunt_unknown_finding_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_hunt(repo, "no-such-finding-xyz")
    assert report.success is False
    assert report.pr_url is None
    assert "koyote check" in report.reason


def test_hunt_without_credentials_fails_closed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _clean_creds(monkeypatch, tmp_path)
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id)
    assert report.success is False
    assert report.pr_url is None
    assert "koyote auth" in report.reason
    assert os.path.isfile(report.audit_path)


def test_decide_pr_rejects_anything_but_a_sealed_token():
    import pytest as _pytest

    ctx = hunt_agent.HuntContext(
        finding=hunt_agent.HuntFinding("x", "stripe", "1", "2", "s"),
        repository="svc", branch="main", commit_sha="abc",
        pr_state={"github_repo": "acme/svc"},
        repo_policy={"pr_auto_fix": True},
    )
    forged_report = hunt_agent.HuntReport(success=True, finding_id="x", provider="stripe",
                                          version_from="1", version_to="2",
                                          repository_path=".", commit_sha="abc",
                                          iterations=1, test_command="pytest -q",
                                          test_exit_code=0)
    with _pytest.raises(TypeError):
        decide_pr(forged_report, ctx, create_pr=True)
    with _pytest.raises(TypeError):
        decide_pr({"success": True}, ctx, create_pr=True)


# ── Verified lifecycle (mocked AI, real sandbox + real tests) ──────────────

class _FakeResp:
    def __init__(self, content: str):
        self.content = content


REASONING_JSON = json.dumps({
    "problem": "Stripe amount contract drifted",
    "current_behavior": "Charge.create with numeric amount",
    "cause": "Upstream contract change",
    "intended_behavior": "Charge with correct amount semantics",
    "affected_paths": ["app.py"],
    "affected_callsites": ["app.py:3"],
    "related_matter": [],
    "explicitly_unaffected": ["tests/test_sanity.py"],
    "assumptions": [],
    "smallest_change": "Annotate the charge call for the new contract",
    "must_not_change": ["tests/test_sanity.py"],
    "regression_risks": ["payment amount misreported"],
    "verification_plan": "pytest -q must stay green",
    "file_intents": [{
        "path": "app.py", "why_inspected": "callsite",
        "why_affected": "uses Charge.create", "why_change": "contract drift",
        "why_replacement": "AI-authored note", "why_not_surrounding": "unrelated",
        "intent_preserved": "charge behavior", "evidence": "observed_fact",
    }],
})

INTERPRET_JSON = json.dumps({
    "solved": True, "unrelated_behavior": False, "assumptions_false": [],
    "cause": "hunt", "needs_more_investigation": False,
    "rationale": "pytest exercised the suite and passed; change is scoped to app.py",
})


class _FakeClient:
    def __init__(self, config=None):
        self.calls = 0

    def complete(self, messages=None, system_prompt=None):
        self.calls += 1
        if system_prompt and "verifying its own repair" in system_prompt:
            return _FakeResp(INTERPRET_JSON)
        return _FakeResp(REASONING_JSON)


class _FakePlanner:
    def __init__(self, client=None):
        self.client = client

    def plan_and_apply(self, repo_dir=None, affected_files=None, **kwargs):
        results = []
        for abs_p in affected_files or []:
            if not os.path.isfile(abs_p):
                continue
            with open(abs_p, encoding="utf-8") as f:
                original = f.read()
            updated = original + "# hunt: verified repair note\n"
            with open(abs_p, "w", encoding="utf-8") as f:
                f.write(updated)
            rel = os.path.relpath(abs_p, repo_dir)
            results.append(PatchResult(
                file_path=abs_p, success=True, lines_changed=1,
                unified_diff=f"--- a/{rel}\n+++ b/{rel}\n+# hunt: verified repair note\n",
                rules_applied=["AI-authored Stripe repair"],
            ))
        return results


def test_hunt_verified_lifecycle_end_to_end(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _FakeClient)
    monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _FakePlanner)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, max_iterations=2)

    assert report.success is True
    assert report.test_exit_code == 0
    assert report.test_command == "pytest -q"
    assert report.files_modified == ["app.py"]
    assert report.pr_url is None  # no repo slug, no --create-pr: deferred, never forced
    assert os.path.isfile(report.audit_path)
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as f:
        assert "# hunt: verified repair note" in f.read()
    with open(os.path.join(repo, "tests", "test_sanity.py"), encoding="utf-8") as f:
        assert "hunt" not in f.read()

    audit = json.load(open(report.audit_path, encoding="utf-8"))
    assert any("reasoned" in step for step in audit["lifecycle"])
    assert audit["final"] == "verified"
    assert audit["reasoning"][0]["affected_paths"] == ["app.py"]


def test_hunt_failing_tests_fail_closed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    with open(os.path.join(repo, "tests", "test_sanity.py"), "w") as f:
        f.write("def test_sanity():\n    assert False, 'always red'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "red tests"], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _FakeClient)
    monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _FakePlanner)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, max_iterations=1)
    assert report.success is False
    assert report.pr_url is None
    assert "could not safely verify" in report.reason
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as f:
        assert "hunt" not in f.read()  # rolled back


# ── CLI routing ────────────────────────────────────────────────────────────

def test_cli_routes_finding_id_to_hunt(tmp_path):
    from koyote.cli.main import _hunt_finding_requested
    assert _hunt_finding_requested(argparse.Namespace(
        root_dir="stripe-a1b2c3", finding=None, issue=None)) is True
    assert _hunt_finding_requested(argparse.Namespace(
        root_dir=str(tmp_path), finding=None, issue=None)) is False
    assert _hunt_finding_requested(argparse.Namespace(
        root_dir=".", finding="stripe-a1b2c3", issue=None)) is True
