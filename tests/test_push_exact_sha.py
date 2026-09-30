# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Exact-SHA honesty: pushed revision inspected when obtainable, else disclosed."""

import json
import os
import subprocess

from truhowl import cross_repo, work_graph
from truhowl.github.pr_bot import handle_push_event
from truhowl.github.push_events import parse_push_payload


def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _git_repo(path, branch="feature/payments"):
    os.makedirs(path, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "t")
    _git(path, "checkout", "-b", branch)
    with open(os.path.join(path, "api.ts"), "w") as f:
        f.write("export const v = 1;\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "change")
    return _git(path, "rev-parse", "HEAD")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "installs"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    os.makedirs(os.environ["TRUHOWL_INSTALLATIONS_DIR"], exist_ok=True)


def _push(after):
    return {"ref": "refs/heads/feature/payments", "before": "0" * 40,
            "after": after, "repository": {"full_name": "acme/api-service"},
            "pusher": {"name": "dev1"},
            "commits": [{"id": after, "added": ["api.ts"], "removed": [],
                         "modified": []}]}


def test_exact_sha_inspected_when_obtainable(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    sha = _git_repo(str(tmp_path / "upstream"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__API-SERVICE",
                       str(tmp_path / "upstream"))
    res = handle_push_event(_push(sha), client=None)
    assert res["exact_sha"] is True
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["exact_sha"] is True
    assert cand["head_sha"] == sha


def test_fallback_disclosed_when_sha_unavailable(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = handle_push_event(_push("deadbeef" * 5), client=None)
    assert res["exact_sha"] is False
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["exact_sha"] is False
    ctx = cross_repo.active_work_context(
        "acme/api-service", "feature/payments", cand,
        {"repository": "acme/admin", "branch": "feature/work"})
    assert "fallback" in ctx and "not the exact pushed revision" in ctx


def test_state_survives_reload(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    work_graph.record_push(parse_push_payload(_push("abc123")), exact_sha=True)
    work_graph.set_candidate_status("acme/api-service", "feature/payments",
                                    work_graph.POTENTIAL)
    work_graph.record_branch_activity("acme/api-service", "feature/payments",
                                      "abc123")
    raw = json.load(open(os.environ["TRUHOWL_WORK_GRAPH_FILE"]))
    cid = work_graph.candidate_id("acme/api-service", "feature/payments")
    assert raw["candidates"][cid]["head_sha"] == "abc123"
    assert raw["candidates"][cid]["exact_sha"] is True
    assert raw["branches"]["acme/api-service#feature/payments"]["pusher"] == "dev1"
    fresh = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert fresh["status"] == work_graph.POTENTIAL
    assert fresh["pushes"][0]["exact_sha"] is True
    assert work_graph.is_active_work("acme/api-service", "feature/payments") is True
