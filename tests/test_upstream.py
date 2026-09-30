# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Upstream monitoring: real registry polling, dedup, failure tolerance."""

import os
import sys
import subprocess

from truhowl.agent.models import load_store
from truhowl.changes.monitor import poll_and_watch, poll_upstream
from truhowl.changes.sources import (
    UpstreamCheck,
    UpstreamRelease,
    compare_versions,
    is_major_bump,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "trials", "fixtures", "taxonomy_stripe")


def _fake_fetcher(versions, fail=None):
    def _fetch(package, timeout=15):
        if fail and package in fail:
            return UpstreamCheck(package=package, ok=False, error=fail[package])
        return UpstreamCheck(package=package, ok=True, release=UpstreamRelease(
            ecosystem="npm", package=package, version=versions.get(package, "1.0.0"),
            published_at="t", source_url=f"https://registry.npmjs.org/{package}/latest"))
    return _fetch


def test_compare_versions():
    assert compare_versions("19.0.0", "20.0.0") == 1
    assert compare_versions("20.0.0", "20.0.0") == 0
    assert compare_versions("20.1.0", "19.9.9") == -1
    assert compare_versions("^11.18.0", "13.0.0") == 1
    assert compare_versions("not-a-version", "1.0.0") is None
    assert is_major_bump("19.0.0", "20.0.0") is True
    assert is_major_bump("19.0.0", "19.1.0") is False


def test_poll_detects_new_release(tmp_path):
    out = poll_upstream(str(tmp_path), providers=["stripe"],
                        fetcher=_fake_fetcher({"stripe": "22.0.0"}))
    assert len(out["releases"]) == 1
    assert out["releases"][0]["current_version"] == "22.0.0"
    assert out["releases"][0]["breaking_candidate"] is True
    assert out["failures"] == []
    store = load_store(str(tmp_path))
    assert store.org["upstream"]["stripe"]["version"] == "22.0.0"


def test_poll_no_duplicate_when_unchanged(tmp_path):
    fetch = _fake_fetcher({"stripe": "22.0.0"})
    first = poll_upstream(str(tmp_path), providers=["stripe"], fetcher=fetch)
    second = poll_upstream(str(tmp_path), providers=["stripe"], fetcher=fetch)
    assert len(first["releases"]) == 1
    assert second["releases"] == []


def test_poll_failure_recorded_not_raised(tmp_path):
    out = poll_upstream(str(tmp_path), providers=["stripe"],
                        fetcher=_fake_fetcher({}, fail={"stripe": "boom"}))
    assert out["releases"] == []
    assert len(out["failures"]) == 1
    assert "boom" in out["failures"][0]["error"]
    store = load_store(str(tmp_path))
    assert "error" in store.org["upstream"]["stripe"]


def test_poll_malformed_version_skipped(tmp_path):
    out = poll_upstream(str(tmp_path), providers=["stripe"],
                        fetcher=_fake_fetcher({"stripe": "latest-ish!!"}))
    assert out["releases"] == []
    assert len(out["failures"]) == 1


def test_poll_and_watch_opens_case(tmp_path):
    out = poll_and_watch(str(tmp_path), providers=["stripe"], repo_paths=[FIXTURE],
                         fetcher=_fake_fetcher({"stripe": "22.0.0"}))
    assert len(out["cases"]) == 1
    assert out["cases"][0]["provider"] == "stripe"
    assert out["cases"][0]["version_to"] == "22.0.0"


def test_poll_and_watch_dedups_second_run(tmp_path):
    fetch = _fake_fetcher({"stripe": "22.0.0"})
    first = poll_and_watch(str(tmp_path), providers=["stripe"], repo_paths=[FIXTURE], fetcher=fetch)
    assert len(first["cases"]) == 1
    # Simulate lost upstream state with the case still open: the release
    # re-reports, but no second case may open.
    store = load_store(str(tmp_path))
    store.org["upstream"]["stripe"]["version"] = "11.18.0"
    from truhowl.agent.models import save_store
    save_store(str(tmp_path), store)
    second = poll_and_watch(str(tmp_path), providers=["stripe"], repo_paths=[FIXTURE], fetcher=fetch)
    assert second["cases"] == []
    assert any(s["reason"] == "case already open" for s in second["skipped"])


def test_poll_and_watch_no_usage_no_case(tmp_path):
    repo = tmp_path / "empty"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("print('hi')\n")
    out = poll_and_watch(str(tmp_path), providers=["stripe"], repo_paths=[str(repo)],
                         fetcher=_fake_fetcher({"stripe": "22.0.0"}))
    assert out["cases"] == []
    assert any(s["reason"] == "no affected usage" for s in out["skipped"])


def test_cli_agent_watch_poll(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    env.setdefault("TRUHOWL_LLM_KEY", "sk-ant-test-credential-key")
    # Point the fake registry at stripe via env is not supported; exercise
    # the explicit path here and poll plumbing via service tests above.
    res = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "agent", "watch",
         "--provider", "stripe", "--from", "11.18.0", "--to", "13.0.0",
         "--repo", FIXTURE],
        capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert res.returncode == 0
    assert "detected" in res.stdout
