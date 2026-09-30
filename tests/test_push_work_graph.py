# Copyright 2026 Truhowl Authors
"""Phase 1: push is an observation, never an alert."""

import time

from truhowl import work_graph
from truhowl.github.push_events import parse_push_payload
from truhowl.github.pr_bot import handle_push_event


def _push(repo="acme/api-service", branch="feature/payments", after="abc123"):
    return {
        "ref": f"refs/heads/{branch}",
        "before": "0000000000000000000000000000000000000000",
        "after": after,
        "created": True, "forced": False,
        "repository": {"full_name": repo},
        "pusher": {"name": "dev1"},
        "compare": "http://example/compare",
        "commits": [{"id": after, "message": "wip",
                      "author": {"name": "dev1"},
                      "added": ["a.py"], "removed": [], "modified": []}],
    }


def test_parse_normal_and_ignores(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    obs = parse_push_payload(_push())
    assert obs["repository"] == "acme/api-service"
    assert obs["branch"] == "feature/payments"
    assert parse_push_payload({"ref": "refs/tags/v1"}) is None
    deleted = _push()
    deleted["after"] = "0" * 40
    assert parse_push_payload(deleted) is None


def test_parse_forced_push_updates_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    first = _push(after="aaa")
    forced = _push(after="bbb")
    forced["forced"] = True
    obs = parse_push_payload(forced)
    assert obs["forced"] is True and obs["after"] == "bbb"
    c1 = work_graph.record_push(parse_push_payload(first))
    c2 = work_graph.record_push(obs)
    assert c2["candidate_id"] == c1["candidate_id"]
    assert c2["head_sha"] == "bbb"


def test_parse_ignores_non_branch_refs_and_malformed():
    assert parse_push_payload({"ref": "refs/notes/review"}) is None
    assert parse_push_payload({"ref": "refs/pull/1/head"}) is None
    assert parse_push_payload({}) is None
    assert parse_push_payload({"repository": {"full_name": "acme/x"}}) is None


def test_candidate_grouping_and_reeval(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    c1 = work_graph.record_push(parse_push_payload(_push(after="aaa")))
    assert c1["status"] == work_graph.OBSERVED
    c2 = work_graph.record_push(parse_push_payload(_push(after="bbb")))
    assert c2["candidate_id"] == c1["candidate_id"]
    assert len(c2["pushes"]) == 2
    work_graph.set_candidate_status("acme/api-service", "feature/payments", work_graph.STABLE)
    c3 = work_graph.record_push(parse_push_payload(_push(after="ccc")))
    assert c3["status"] == work_graph.POTENTIAL


def test_active_work_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    assert work_graph.is_active_work("acme/api-service", "feature/payments") is False
    work_graph.record_push(parse_push_payload(_push()))
    assert work_graph.is_active_work("acme/api-service", "feature/payments") is True
    far_future = time.time() + work_graph.DEFAULT_ACTIVE_WINDOW_S + 1
    assert work_graph.is_active_work("acme/api-service", "feature/payments",
                                     now=far_future) is False


def test_handler_ingests_without_notify(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    res = handle_push_event(_push(), client=None)
    assert res["success"] is True and res["handled"] is True
    assert res["notified"] is False
    assert "candidate_id" in res
