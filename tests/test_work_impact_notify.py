# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Phase 3: STABLE -> CONFIRMED -> NOTIFIED. Work-naming Issues, fan-out, dedupe."""

import json
import os
import subprocess
import time

from truhowl import cross_repo, work_graph
from truhowl.github.provisioning import cached_path
from truhowl.github.push_events import parse_push_payload
from truhowl.github.pr_bot import handle_push_event, make_pr_bot_handler
from truhowl.github.watch import watch_once


class StubPlanner:
    def __init__(self, body="impact", confidence="high"):
        self.body = body
        self.confidence = confidence
        self.calls = []

    def assess(self, **kwargs):
        self.calls.append(kwargs)
        return {"body": self.body, "confidence": self.confidence}


class FakeClient:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.closed = []

    def create_issue(self, repo, title, body, labels=None):
        self.issues.append({"repo": repo, "title": title, "body": body})
        return {"html_url": f"https://example/{repo}/issues/{len(self.issues)}",
                "number": len(self.issues)}

    def post_pr_comment(self, repo, pr_number, body):
        self.comments.append({"repo": repo, "pr": pr_number, "body": body})
        return {"id": len(self.comments)}

    def close_issue(self, repo, issue_number):
        self.closed.append({"repo": repo, "number": issue_number})
        return {"state": "closed"}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "installs"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    os.makedirs(os.environ["TRUHOWL_INSTALLATIONS_DIR"], exist_ok=True)


def _install(*repos):
    with open(os.path.join(os.environ["TRUHOWL_INSTALLATIONS_DIR"], "1.json"), "w") as f:
        json.dump({"installation_id": "1",
                   "repos": {r: {"state": "READY"} for r in repos}}, f)


def _checkout(repo):
    path = cached_path(repo)
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    return path


def _raw_push(repo, branch, after, files=("src/api.ts",)):
    return {"ref": f"refs/heads/{branch}", "before": "0" * 40, "after": after,
            "repository": {"full_name": repo}, "pusher": {"name": "dev1"},
            "commits": [{"id": after, "added": list(files), "removed": [], "modified": []}]}


def _setup(tmp_path, monkeypatch, consumers=("acme/admin",), confidence="high"):
    _env(tmp_path, monkeypatch)
    _install("acme/api-service", *consumers)
    for repo in consumers:
        co = _checkout(repo)
        with open(os.path.join(co, "client.ts"), "w") as f:
            f.write("import { x } from 'api-service';\n")
        work_graph.record_push(parse_push_payload(
            _raw_push(repo, "feature/work", "ddd", files=())))
    work_graph.record_push(parse_push_payload(
        _raw_push("acme/api-service", "feature/payments", "aaa")))
    monkeypatch.setattr(cross_repo, "scan_callsites",
                        lambda d, cfg: {"callsites": [{"file_path": "client.ts", "kind": "Import"}]}
                        if "__admin" in d or "__mobile" in d else {"callsites": []})
    stub = StubPlanner(confidence=confidence)
    monkeypatch.setattr(cross_repo.AIPatchPlanner, "from_env",
                        classmethod(lambda cls: stub))
    return stub


def _potential(tmp_path, monkeypatch, **kw):
    stub = _setup(tmp_path, monkeypatch, **kw)
    res = cross_repo.evaluate_candidate("acme/api-service", "feature/payments", planner=stub)
    assert res["status"] == work_graph.POTENTIAL
    return stub


def test_sweep_moves_only_quiet(tmp_path, monkeypatch):
    _potential(tmp_path, monkeypatch, confidence="medium")
    assert cross_repo.sweep_and_notify(FakeClient(), quiet_s=10**9) == []
    moved = work_graph.sweep_quiet(window_s=0, now=time.time() + 10**6)
    assert [m["status"] for m in moved] == [work_graph.STABLE]


def test_sweep_notifies_work_naming_issue(tmp_path, monkeypatch):
    _potential(tmp_path, monkeypatch, confidence="medium")
    client = FakeClient()
    before = open(os.path.join(
        __import__("truhowl.github.provisioning", fromlist=["cached_path"]).cached_path("acme/admin"),
        "client.ts")).read()
    out = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert out[0]["confirmed"] is True and out[0]["notified"] is True
    assert len(client.issues) == 1
    issue = client.issues[0]
    assert issue["repo"] == "acme/admin"
    assert "acme/admin" in issue["title"] and "feature/work" in issue["title"]
    assert "acme/api-service" in issue["title"] and "feature/payments" in issue["title"]
    assert "No code was modified" in issue["body"]
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["status"] == work_graph.NOTIFIED
    after = open(os.path.join(cached_path("acme/admin"), "client.ts")).read()
    assert before == after


def test_retract_leaves_no_notification(tmp_path, monkeypatch):
    _potential(tmp_path, monkeypatch, confidence="medium")
    monkeypatch.setattr(cross_repo, "scan_callsites", lambda d, cfg: {"callsites": []})
    client = FakeClient()
    out = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert out[0]["confirmed"] is False
    assert client.issues == []
    assert work_graph.get_candidate("acme/api-service", "feature/payments")["status"] == work_graph.OBSERVED


def test_fanout_and_dedupe_and_pr_link(tmp_path, monkeypatch):
    _potential(tmp_path, monkeypatch, consumers=("acme/admin", "acme/mobile"), confidence="medium")
    work_graph.record_branch_activity("acme/admin", "feature/work", "ddd", open_pr=7)
    client = FakeClient()
    out = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert len(out) == 1 and out[0]["notified"] is True
    assert len(client.issues) == 2
    assert len(client.comments) == 1
    assert client.comments[0]["pr"] == 7
    out2 = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert out2 == []
    assert len(client.issues) == 2


def test_high_confidence_fastpath_on_push(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="high")
    client = FakeClient()
    res = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert res["status"] == work_graph.NOTIFIED and res["notified"] is True
    assert len(client.issues) == 1


def test_medium_confidence_waits_for_stability(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="medium")
    client = FakeClient()
    res = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert res["status"] == work_graph.POTENTIAL and not res["notified"]
    assert client.issues == []


def test_pr_opened_fastpath(tmp_path, monkeypatch):
    _potential(tmp_path, monkeypatch, confidence="medium")
    client = FakeClient()
    res = cross_repo.pr_fastpath({
        "action": "opened",
        "repository": {"full_name": "acme/api-service"},
        "pull_request": {"number": 3, "head": {"ref": "feature/payments", "sha": "aaa"},
                         "merged": False},
    }, client)
    assert res.get("confirmed") is True and res.get("notified") is True
    assert len(client.issues) == 1


def test_pr_event_without_candidate_untouched(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    client = FakeClient()
    res = cross_repo.pr_fastpath({
        "action": "opened",
        "repository": {"full_name": "acme/unknown"},
        "pull_request": {"number": 1, "head": {"ref": "x", "sha": "y"}, "merged": False},
    }, client)
    assert res == {"fastpath": False}
    assert not os.path.exists(os.environ["TRUHOWL_WORK_GRAPH_FILE"])


def test_post_notify_retraction_closes_issue(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="high")
    client = FakeClient()
    first = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert first["status"] == work_graph.NOTIFIED
    assert len(client.issues) == 1
    monkeypatch.setattr(cross_repo, "scan_callsites", lambda d, cfg: {"callsites": []})
    second = handle_push_event(_raw_push("acme/api-service", "feature/payments", "bbb"), client)
    assert second["status"] == work_graph.OBSERVED
    assert len(client.issues) == 1  # no duplicate
    assert len(client.closed) == 1  # retracted with close
    assert client.closed[0] == {"repo": "acme/admin", "number": 1}
    assert any("retracted" in c["body"] for c in client.comments)
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["status"] == work_graph.OBSERVED
    assert cand["notified_issues"][0]["state"] == "closed"


def test_post_notify_still_affected_updates_same_issue(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="high")
    client = FakeClient()
    handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert len(client.issues) == 1
    second = handle_push_event(_raw_push("acme/api-service", "feature/payments", "bbb"), client)
    assert second["status"] == work_graph.NOTIFIED
    assert len(client.issues) == 1
    assert client.closed == []
    assert any("still affected" in c["body"] for c in client.comments)


def _git_repo(path, branch="feature/payments"):
    def run(*a):
        return subprocess.run(["git", *a], cwd=path, capture_output=True,
                              text=True, timeout=60, check=True)

    os.makedirs(path, exist_ok=True)
    run("init"), run("config", "user.email", "t@e.com"), run("config", "user.name", "t")
    run("checkout", "-b", branch)
    with open(os.path.join(path, "api.ts"), "w") as f:
        f.write("export const v = 1;\n")
    run("add", "."), run("commit", "-m", "change")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()


def test_merge_dispatch_confirms_immediately(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="medium")
    client = FakeClient()
    pushed = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert pushed["status"] == work_graph.POTENTIAL
    sha = _git_repo(str(tmp_path / "upstream"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__API-SERVICE",
                       str(tmp_path / "upstream"))
    handler = make_pr_bot_handler(client=client, policy=None)
    res = handler({
        "action": "closed",
        "repository": {"full_name": "acme/api-service"},
        "pull_request": {"number": 7, "title": "payments", "body": "",
                         "merged": True, "state": "closed",
                         "head": {"ref": "feature/payments", "sha": sha},
                         "base": {"ref": "main"}},
    }, "pull_request.closed")
    assert res["event_type"] == "pull_request.closed"
    assert len(client.issues) == 1
    assert client.issues[0]["repo"] == "acme/admin"


def test_watch_sweeper_confirms_and_notifies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, confidence="medium")
    os.makedirs(os.path.join(cached_path("acme/api-service"), ".git"), exist_ok=True)
    client = FakeClient()
    pushed = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert pushed["status"] == work_graph.POTENTIAL
    graph = work_graph.load_graph()
    cid = work_graph.candidate_id("acme/api-service", "feature/payments")
    graph["candidates"][cid]["updated_ts"] = time.time() - 10**6
    work_graph.save_graph(graph)
    outcomes = watch_once(client=client)
    swept = [o for o in outcomes if o.get("sweep")]
    assert len(swept) == 1 and swept[0]["confirmed"] is True
    assert len(client.issues) == 1
    assert client.issues[0]["repo"] == "acme/admin"
