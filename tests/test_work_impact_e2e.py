# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Authoritative E2E: real matcher, real candidate machine, real assess path.

Only the model call (LLMClient.complete) and the GitHub API (FakeClient)
are faked. Work graph, matcher, evaluation, state machine, and Howl
orchestration are all real.
"""

import json
import os
import re
import time

from truhowl import cross_repo, work_graph
from truhowl.github.pr_bot import handle_push_event
from truhowl.github.provisioning import cached_path
from truhowl.github.push_events import parse_push_payload
from truhowl.llm import LLMClient, LLMResponse


class FakeClient:
    def __init__(self):
        self.issues = []

    def create_issue(self, repo, title, body, labels=None):
        self.issues.append({"repo": repo, "title": title, "body": body})
        return {"html_url": f"https://example/{repo}/issues/{len(self.issues)}",
                "number": len(self.issues)}

    def post_pr_comment(self, repo, pr_number, body):
        return {"id": 1}

    def close_issue(self, repo, issue_number):
        return {"state": "closed"}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "installs"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_e2e_only")
    for d in ("installs", "repos", "home"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)
    with open(os.path.join(os.environ["TRUHOWL_INSTALLATIONS_DIR"], "1.json"), "w") as f:
        json.dump({"installation_id": "1", "repos": {
            "acme/api-service": {"state": "READY"},
            "acme/admin": {"state": "READY"},
            "acme/billing": {"state": "READY"}}}, f)


def _checkout(repo, filename, content):
    path = cached_path(repo)
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    full = os.path.join(path, filename)
    with open(full, "w") as f:
        f.write(content)
    return path


def _record_branch(repo, branch):
    work_graph.record_push(parse_push_payload({
        "ref": f"refs/heads/{branch}", "before": "0" * 40, "after": "ddd",
        "repository": {"full_name": repo}, "pusher": {"name": "dev"},
        "commits": []}))


def _push(branch_file):
    return {"ref": "refs/heads/feature/payments", "before": "0" * 40,
            "after": "pay001", "repository": {"full_name": "acme/api-service"},
            "pusher": {"name": "dev-a"}, "compare": "http://example/cmp",
            "commits": [{"id": "pay001", "added": [branch_file], "removed": [],
                         "modified": []}]}


IMPACT_MD = ("## Impact\nAdmin checkout calls the changed endpoint.\n"
             "Confidence: medium")


def _fake_model(monkeypatch, seen):
    def complete(self, messages, system_prompt=None):
        content = messages[0]["content"]
        seen.append(content)
        latest = re.search(r"Latest push \S+: (.*)", content)
        latest_files = latest.group(1) if latest else ""
        if "acme/admin" in content and "src/payments.ts" in latest_files:
            return LLMResponse(content=IMPACT_MD, model="fake")
        return LLMResponse(content="", model="fake")
    monkeypatch.setattr(LLMClient, "complete", complete)


def test_cross_repo_e2e_full_chain(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    admin = _checkout("acme/admin", "client.ts",
                      "import { x } from 'api-service';\nconst r = x.get('/users');\n")
    billing = _checkout("acme/billing", "invoice.ts",
                        "console.log('unrelated');\n")
    _record_branch("acme/admin", "feature/checkout")
    _record_branch("acme/billing", "feature/invoice")
    before_admin = open(os.path.join(admin, "client.ts")).read()
    before_billing = open(os.path.join(billing, "invoice.ts")).read()
    seen = []
    _fake_model(monkeypatch, seen)
    client = FakeClient()

    pushed = handle_push_event(_push("src/payments.ts"), client)
    assert pushed["handled"] is True and pushed["notified"] is False
    cand = work_graph.get_candidate("acme/api-service", "feature/payments")
    assert cand["status"] == work_graph.POTENTIAL
    assert cand["head_sha"] == "pay001"

    assert seen, "AI was never consulted"
    admin_prompts = [c for c in seen if "acme/admin" in c]
    assert admin_prompts, "admin work was never reasoned about"
    prompt = admin_prompts[0]
    for needle in ("Active Work Context", "acme/api-service", "feature/payments",
                   "acme/admin", "feature/checkout", "Is this impact provisional"):
        assert needle in prompt, needle

    out = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert out[0]["confirmed"] is True
    assert len(client.issues) == 1
    issue = client.issues[0]
    assert issue["repo"] == "acme/admin"
    assert "acme/admin" in issue["title"] and "feature/checkout" in issue["title"]
    assert "acme/api-service" in issue["title"] and "feature/payments" in issue["title"]
    assert "Confidence: medium" in issue["body"]
    assert "No code was modified" in issue["body"]
    assert open(os.path.join(admin, "client.ts")).read() == before_admin
    assert open(os.path.join(billing, "invoice.ts")).read() == before_billing

    out2 = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6 + 5)
    assert out2 == []
    assert len(client.issues) == 1


def test_cross_repo_e2e_transient_retracts(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _checkout("acme/admin", "client.ts",
              "import { x } from 'api-service';\nconst r = x.get('/users');\n")
    _checkout("acme/billing", "invoice.ts", "console.log('unrelated');\n")
    _record_branch("acme/admin", "feature/checkout")
    _record_branch("acme/billing", "feature/invoice")
    seen = []
    _fake_model(monkeypatch, seen)
    client = FakeClient()

    first = handle_push_event(_push("src/payments.ts"), client)
    assert first["status"] == work_graph.POTENTIAL
    rewrite = _push("docs/notes.md")
    rewrite["after"] = "pay002"
    rewrite["commits"][0]["id"] = "pay002"
    second = handle_push_event(rewrite, client)
    assert second["handled"] is True
    out = cross_repo.sweep_and_notify(client, quiet_s=0, now=time.time() + 10**6)
    assert out[0]["confirmed"] is False
    assert client.issues == []
    assert work_graph.get_candidate(
        "acme/api-service", "feature/payments")["status"] == work_graph.OBSERVED
