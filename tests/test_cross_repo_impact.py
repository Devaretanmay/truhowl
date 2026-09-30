# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Phase 2: cheap match -> active-work filter -> gated AI -> POTENTIAL. Silent."""

import json
import os
import time

from truhowl import cross_repo, work_graph
from truhowl.github.pr_bot import handle_push_event
from truhowl.github.provisioning import cached_path
from truhowl.github.push_events import parse_push_payload


class StubPlanner:
    def __init__(self, body="impact confirmed"):
        self.body = body
        self.calls = []

    def assess(self, **kwargs):
        self.calls.append(kwargs)
        return {"body": self.body, "confidence": "high"}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "installs"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    os.makedirs(os.environ["TRUHOWL_INSTALLATIONS_DIR"], exist_ok=True)


def _install(*repos):
    rec = {"installation_id": "1", "repos": {r: {"state": "READY"} for r in repos}}
    with open(os.path.join(os.environ["TRUHOWL_INSTALLATIONS_DIR"], "1.json"), "w") as f:
        json.dump(rec, f)


def _checkout(repo, with_git=True):
    path = cached_path(repo)
    os.makedirs(os.path.join(path, ".git") if with_git else path, exist_ok=True)
    return path


def _push(repo="acme/api-service", branch="feature/payments", after="abc123"):
    return parse_push_payload({
        "ref": f"refs/heads/{branch}", "before": "0" * 40, "after": after,
        "repository": {"full_name": repo}, "pusher": {"name": "dev1"},
        "commits": [{"id": after, "message": "change api",
                      "added": ["src/api.ts"], "removed": [], "modified": []}],
    })


def test_changed_files_union(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    work_graph.record_push(_push(after="aaa"))
    cand = work_graph.record_push({**_push(after="bbb"),
                                   "commits": [{"id": "bbb", "added": ["src/other.ts"],
                                                "removed": [], "modified": ["src/api.ts"]}]})
    assert sorted(work_graph.candidate_changed_files(cand)) == ["src/api.ts", "src/other.ts"]


def _consumer_setup(tmp_path, monkeypatch, active=True):
    _env(tmp_path, monkeypatch)
    _install("acme/api-service", "acme/admin", "acme/billing")
    admin = _checkout("acme/admin")
    with open(os.path.join(admin, "client.ts"), "w") as f:
        f.write("import { x } from 'api-service';\n")
    billing = _checkout("acme/billing")
    with open(os.path.join(billing, "inv.ts"), "w") as f:
        f.write("console.log('unrelated');\n")
    work_graph.record_push(_push())
    work_graph.record_push({**_push(repo="acme/admin", branch="feature/checkout", after="ddd"),
                            "commits": []})
    if not active:
        g = work_graph.load_graph()
        g["branches"]["acme/admin#feature/checkout"]["last_push_ts"] = (
            time.time() - work_graph.DEFAULT_ACTIVE_WINDOW_S - 1)
        work_graph.save_graph(g)
    monkeypatch.setattr(cross_repo, "scan_callsites",
                        lambda d, cfg: {"callsites": [{"file_path": "client.ts", "kind": "Import"}]}
                        if d.endswith("acme__admin") else {"callsites": []})
    return admin


def test_matcher_filters_to_active_consumers(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch)
    targets = cross_repo.find_plausible_targets("acme/api-service", "feature/payments")
    assert [(t["repository"], t["branch"]) for t in targets] == [("acme/admin", "feature/checkout")]


def test_stale_branch_excluded(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch, active=False)
    assert cross_repo.find_plausible_targets("acme/api-service", "feature/payments") == []


def test_ai_gated_to_plausible_pairs_only(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch)
    stub = StubPlanner()
    res = cross_repo.evaluate_candidate("acme/api-service", "feature/payments", planner=stub)
    assert res["status"] == work_graph.POTENTIAL
    assert len(stub.calls) == 1
    assert stub.calls[0]["provider_name"] == "acme/api-service"
    assert "Active Work Context" in stub.calls[0]["migration_details"]
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["status"] == work_graph.POTENTIAL
    assert cand["potential_targets"][0]["repository"] == "acme/admin"


def test_ai_never_called_without_plausible_target(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _install("acme/api-service")
    work_graph.record_push(_push())
    stub = StubPlanner()
    res = cross_repo.evaluate_candidate("acme/api-service", "feature/payments", planner=stub)
    assert res["status"] == work_graph.OBSERVED
    assert stub.calls == []


def test_empty_assessment_retracts(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch)
    res = cross_repo.evaluate_candidate("acme/api-service", "feature/payments",
                                        planner=StubPlanner(body=""))
    assert res["status"] == work_graph.OBSERVED


def test_no_credentials_fail_closed(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cross_repo.AIPatchPlanner, "from_env", classmethod(lambda cls: None))
    res = cross_repo.evaluate_candidate("acme/api-service", "feature/payments")
    assert res == {"evaluated": False, "reason": "no_credentials_for_ai"}
    assert work_graph.get_candidate("acme/api-service", "feature/payments")["status"] == work_graph.OBSERVED


class FakeIssueClient:
    def __init__(self):
        self.issues = []

    def create_issue(self, repo, title, body, labels=None):
        self.issues.append({"repo": repo, "title": title})
        return {"html_url": f"https://example/{repo}/issues/{len(self.issues)}",
                "number": len(self.issues)}

    def post_pr_comment(self, repo, pr_number, body):
        return {"id": 1}


def test_push_storm_bounds_ai_evaluations(tmp_path, monkeypatch):
    _consumer_setup(tmp_path, monkeypatch)
    stub = StubPlanner()
    monkeypatch.setattr(cross_repo.AIPatchPlanner, "from_env",
                        classmethod(lambda cls: stub))
    for i in range(100):
        res = handle_push_event(_push_raw(after=f"head{i:03d}"), client=None)
        assert res["handled"] is True
    assert len(stub.calls) == 1  # first push only; rest coalesced
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["head_sha"] == "head099"
    assert cand["eval_pending"] is True
    out = cross_repo.sweep_and_notify(FakeIssueClient(), quiet_s=0,
                                      now=time.time() + 10**6)
    assert len(stub.calls) == 2  # sweep evaluates the latest head only
    assert stub.calls[-1]["to_version"] == "head099"
    assert out[0]["confirmed"] is True
    assert len(out[0]["issues"]) == 1


def _push_raw(repo="acme/api-service", branch="feature/payments", after="abc123"):
    return {
        "ref": f"refs/heads/{branch}", "before": "0" * 40, "after": after,
        "repository": {"full_name": repo}, "pusher": {"name": "dev1"},
        "commits": [{"id": after, "added": ["src/api.ts"], "removed": [], "modified": []}],
    }


def test_string_hits_are_not_consumption(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cross_repo, "scan_callsites", lambda d, cfg: {"callsites": [
        {"file_path": "src/proxy.rs", "kind": None, "matched_pattern": "api.api-service.com"},
        {"file_path": "src/docs.rs", "kind": "", "matched_pattern": "api-service"},
    ]})
    assert cross_repo.consumes_source(str(tmp_path), "acme/api-service") == []
