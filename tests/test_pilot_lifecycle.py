# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end local pilot simulation: watch -> case -> repair -> verify -> deliver.

This is the pass's acceptance test. It exercises the full product lifecycle
through the real services (upstream monitor, agent service, deterministic
planner, repair engine, canonical verification with clean-room replay, and
the delivery service) against a real repository on disk — mocking only the
LLM and the GitHub HTTP boundary, which are external systems.

Every step asserts on persisted state, so the test fails if any step becomes
decorative.
"""

import json
import os

from truhowl.agent import automation as auto
from truhowl.agent import models as m
from truhowl.agent import service as svc
from truhowl.changes.monitor import poll_and_watch
from truhowl.changes.sources import UpstreamCheck, UpstreamRelease
from truhowl.llm import LLMClient, LLMResponse

PATCH = ("<<<<<<< SEARCH\n"
         "export const cancel = (id: string) => s.subscriptions.del(id);\n"
         "=======\n"
         "export const cancel = (id: string) => s.subscriptions.cancel(id);\n"
         ">>>>>>> REPLACE")


def _make_repo(tmp_path):
    """A minimal real repository: manifest, callsite, and a runnable test."""
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


def _fetcher(package, timeout=15):
    """Authoritative upstream metadata, stand-in for registry.npmjs.org."""
    return UpstreamCheck(package=package, ok=True, release=UpstreamRelease(
        ecosystem="npm", package=package, version="22.0.0",
        published_at="2026-09-30T00:00:00Z",
        source_url=f"https://registry.npmjs.org/{package}/latest"))


def _install_mock_llm(monkeypatch, content=PATCH):
    from unittest.mock import MagicMock

    from truhowl.ai_planner import AIPatchPlanner

    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(content=content, model="test-model")
    monkeypatch.setattr("truhowl.maintenance.AIPatchPlanner.from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=client)))


def _clean_github_env(monkeypatch):
    for key in ("GITHUB_TOKEN", "TRUHOWL_GITHUB_TOKEN",
                "TRUHOWL_GITHUB_APP_ID", "TRUHOWL_GITHUB_PRIVATE_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_pilot_lifecycle_watch_to_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = str(tmp_path)
    repo_dir = _make_repo(tmp_path)
    _clean_github_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_pilot_key")
    _install_mock_llm(monkeypatch)

    # ── 1-6. Watch polls upstream, finds a release, opens a Case ─────────
    out = poll_and_watch(workspace, providers=["stripe"], repo_paths=[repo_dir],
                         fetcher=_fetcher)
    assert len(out["releases"]) == 1, "new upstream release must be detected"
    release = out["releases"][0]
    assert release["current_version"] == "22.0.0"
    assert release["breaking_candidate"] is True
    assert release["source"].startswith("https://registry.npmjs.org/")
    assert out["failures"] == []

    assert len(out["cases"]) == 1, f"expected one case, skipped={out['skipped']}"
    case_id = out["cases"][0]["case_id"]

    store = m.load_store(workspace)
    assert any(c["provider"] == "stripe" and c["version_to"] == "22.0.0"
               for c in store.changes), "ExternalChange must be persisted"
    assert "billing-service" in store.repos, "repository must be registered"
    assert case_id in store.cases, "MigrationCase must be in the persisted store"
    assert store.cases[case_id]["repos"][0]["state"] == m.DETECTED
    assert any(u.get("files") for u in store.usages), "affected usage must be recorded"

    # ── 7-11. Plan, repair, canonical verification, replay, VERIFIED ─────
    auto.set_mode(workspace, auto.PREPARE)
    result = svc.run_repo(workspace, case_id, store.cases[case_id]["repos"][0]["repo_key"])
    assert result["state"] == m.VERIFIED, result.get("reason")

    store = m.load_store(workspace)
    evidence = store.evidence[-1]
    assert evidence["replay_command"], "clean-room replay must have run"
    assert evidence["replay_exit_code"] == 0, "clean-room replay must pass"
    assert evidence["patch_hash"], "candidate must be content-addressed"
    assert evidence["blast_radius_zero"] is True

    # The repair is real: the file changed on disk.
    assert "subscriptions.cancel(id)" in open(
        os.path.join(repo_dir, "src", "billing.ts")).read()

    # ── 12. Deliver requested with no credentials -> BLOCKED_AUTH ────────
    auto.set_mode(workspace, auto.DELIVER)
    store = m.load_store(workspace)
    key = store.cases[case_id]["repos"][0]["repo_key"]
    second = svc.run_repo(workspace, case_id, key,
                          create_pr=True, github_repo="acme/billing-service")
    assert second["state"] == m.VERIFIED, "verification must never be downgraded"
    assert second["delivery"]["status"] == "blocked-auth"
    assert "GitHub credentials unavailable" in second["delivery"]["error"]

    # ── 13. Ask Truhowl explains the delivery failure ────────────────────
    text = svc.explain_case(workspace, case_id)
    assert "delivery: blocked-auth" in text
    assert "clean-room replay: passed" in text
    attention = svc.cases_needing_attention(workspace)
    assert any("blocked-auth" in str(v.get("reason", ""))
               for c in attention for v in c["repos"].values())

    # ── 14. With credentials, delivery reaches PUBLISHED ────────────────
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_pilot_test_token")
    monkeypatch.setattr("truhowl.git_ops.git_commit_and_push", lambda *a, **k: True)
    monkeypatch.setattr("truhowl.git_ops.gh_create_pr",
                        lambda *a, **k: "https://github.com/acme/billing-service/pull/7")
    store = m.load_store(workspace)
    third = svc.run_repo(workspace, case_id, key,
                         create_pr=True, github_repo="acme/billing-service")
    assert third["state"] == m.PR_READY
    assert third["delivery"]["status"] == "published"
    assert third["delivery"]["pr_url"].endswith("/pull/7")
    assert "delivery: published" in svc.explain_case(workspace, case_id)


def test_pilot_lifecycle_refusal_path_is_loud(tmp_path, monkeypatch):
    """The other legal ending: a repair that cannot be proven is REFUSED."""
    monkeypatch.chdir(tmp_path)
    workspace = str(tmp_path)
    repo_dir = _make_repo(tmp_path)
    _clean_github_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_pilot_key")
    # A patch that leaves the deprecated call in place: tests stay red.
    _install_mock_llm(monkeypatch, content=(
        "<<<<<<< SEARCH\nconst s = new Stripe(\n=======\nconst s = new Stripe(\n"
        ">>>>>>> REPLACE"))

    auto.set_mode(workspace, auto.PREPARE)
    out = poll_and_watch(workspace, providers=["stripe"], repo_paths=[repo_dir],
                         fetcher=_fetcher)
    case_id = out["cases"][0]["case_id"]
    store = m.load_store(workspace)
    key = store.cases[case_id]["repos"][0]["repo_key"]
    result = svc.run_repo(workspace, case_id, key)
    assert result["state"] in (m.REFUSED, m.NEEDS_ATTENTION)
    assert svc.get_case(workspace, case_id)["repos"][0]["state"] in (
        m.REFUSED, m.NEEDS_ATTENTION)
    # Refused means no PR state and no verified record.
    store = m.load_store(workspace)
    assert not any(v.get("repo_key") == key for v in store.verified)
    assert result.get("pr_url") is None
