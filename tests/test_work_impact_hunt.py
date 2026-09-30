# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Phase 4: explicit opt-in Hunt repair. Gated, verified, never automatic."""

import json
import os
import subprocess

from truhowl import cross_repo, work_graph
from truhowl.github.provisioning import cached_path
from truhowl.github.push_events import parse_push_payload
from truhowl.github.pr_bot import handle_push_event
from truhowl.knowledge import lookup
from truhowl.patch_writer import PatchResult
from truhowl.repo_identity import STATE_ACTIVE, register_repository, set_bot_state


class AssessStub:
    def __init__(self, confidence="high"):
        self.confidence = confidence

    def assess(self, **kwargs):
        return {"body": "impact", "confidence": self.confidence}


class RepairStub(AssessStub):
    """Stub AI that really writes, so snapshot restore is verifiable."""

    def plan_and_apply(self, repo_dir, affected_files, **kwargs):
        out = []
        for f in affected_files:
            path = f if os.path.isabs(f) else os.path.join(repo_dir, f)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n# hunt repair\n")
            out.append(PatchResult(file_path=path, success=True,
                                   lines_changed=1, unified_diff="diff"))
        return out


class FakeClient:
    def __init__(self):
        self.issues = []
        self.prs = []

    def create_issue(self, repo, title, body, labels=None):
        self.issues.append({"repo": repo, "title": title})
        return {"html_url": f"https://example/{repo}/issues/1", "number": 1}

    def create_pull_request(self, repo, title, body, head_branch,
                            base_branch="main", labels=None):
        self.prs.append({"repo": repo, "title": title, "body": body,
                         "head": head_branch, "base": base_branch})
        return {"html_url": f"https://example/{repo}/pull/1", "number": 1}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "installs"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_DIR", str(tmp_path / "truhowl_home"))
    for d in ("installs", "repos", "truhowl_home"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)


def _raw_push(repo, branch, after, files=("src/api.ts",)):
    return {"ref": f"refs/heads/{branch}", "before": "0" * 40, "after": after,
            "repository": {"full_name": repo}, "pusher": {"name": "dev1"},
            "commits": [{"id": after, "added": list(files), "removed": [], "modified": []}]}


def _notified(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    with open(os.path.join(os.environ["TRUHOWL_INSTALLATIONS_DIR"], "1.json"), "w") as f:
        json.dump({"installation_id": "1", "repos": {
            "acme/api-service": {"state": "READY"},
            "acme/admin": {"state": "READY"}}}, f)
    admin = cached_path("acme/admin")
    os.makedirs(os.path.join(admin, ".git"), exist_ok=True)
    client_ts = os.path.join(admin, "client.ts")
    with open(client_ts, "w") as f:
        f.write("import { x } from 'api-service';\n")
    work_graph.record_push(parse_push_payload(_raw_push("acme/admin", "feature/work", "ddd", ())))
    monkeypatch.setattr(cross_repo, "scan_callsites",
                        lambda d, cfg: {"callsites": [{"file_path": "client.ts", "kind": "Import"}]}
                        if d.endswith("acme__admin") else {"callsites": []})
    monkeypatch.setattr(cross_repo.AIPatchPlanner, "from_env",
                        classmethod(lambda cls: AssessStub()))
    client = FakeClient()
    res = handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), client)
    assert res["status"] == work_graph.NOTIFIED
    register_repository("acme/admin")
    return client, admin, client_ts


def _repair_env(monkeypatch, exit_code=0):
    monkeypatch.setattr(cross_repo, "_detect_test_command", lambda d: "pytest -q")
    monkeypatch.setattr(cross_repo, "_run_tests",
                        lambda d, cmd, timeout=120: subprocess.CompletedProcess(
                            args=cmd, returncode=exit_code))
    pushed = []
    monkeypatch.setattr(cross_repo, "git_commit_and_push",
                        lambda d, files, branch, msg: pushed.append(branch) or True)
    return pushed


def test_gate_closed_by_default(tmp_path, monkeypatch):
    client, _, _ = _notified(tmp_path, monkeypatch)
    res = cross_repo.request_hunt_repair(
        "acme/api-service", "feature/payments", "acme/admin", "feature/work",
        client, planner=RepairStub())
    assert res == {"hunt": False, "reason": "hunt_not_enabled_for_repo"}
    assert client.prs == []


def test_green_repair_opens_pr_on_affected_branch(tmp_path, monkeypatch):
    client, _, _ = _notified(tmp_path, monkeypatch)
    set_bot_state("acme/admin", "hunt", STATE_ACTIVE)
    pushed = _repair_env(monkeypatch)
    res = cross_repo.request_hunt_repair(
        "acme/api-service", "feature/payments", "acme/admin", "feature/work",
        client, planner=RepairStub())
    assert res["hunt"] is True and res["branch"] == "truhowl/work-impact-api-service"
    assert pushed == ["truhowl/work-impact-api-service"]
    assert len(client.prs) == 1
    pr = client.prs[0]
    assert pr["base"] == "feature/work"  # PR targets the affected work, not main
    assert "feature/work" in pr["title"] and "feature/payments" in pr["title"]
    assert "issues/1" in pr["body"]  # links the Howl issue


def test_red_verification_restores_and_records(tmp_path, monkeypatch):
    client, _, client_ts = _notified(tmp_path, monkeypatch)
    set_bot_state("acme/admin", "hunt", STATE_ACTIVE)
    _repair_env(monkeypatch, exit_code=1)
    before = open(client_ts).read()
    res = cross_repo.request_hunt_repair(
        "acme/api-service", "feature/payments", "acme/admin", "feature/work",
        client, planner=RepairStub())
    assert res == {"hunt": False, "reason": "verification_failed"}
    assert client.prs == []
    assert open(client_ts).read() == before
    entry = lookup(cached_path("acme/admin"),
                   "acme/api-service", "00000000", "aaa")
    assert entry is not None and len(entry.failed_patterns) == 1


def test_no_test_command_fails_closed(tmp_path, monkeypatch):
    client, _, client_ts = _notified(tmp_path, monkeypatch)
    set_bot_state("acme/admin", "hunt", STATE_ACTIVE)
    monkeypatch.setattr(cross_repo, "_detect_test_command", lambda d: "")
    before = open(client_ts).read()
    res = cross_repo.request_hunt_repair(
        "acme/api-service", "feature/payments", "acme/admin", "feature/work",
        client, planner=RepairStub())
    assert res == {"hunt": False, "reason": "no_test_command_fail_closed"}
    assert client.prs == []
    assert open(client_ts).read() == before


def test_no_notified_impact_refused(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    register_repository("acme/admin")
    set_bot_state("acme/admin", "hunt", STATE_ACTIVE)
    res = cross_repo.request_hunt_repair(
        "acme/api-service", "feature/payments", "acme/admin", "feature/work",
        FakeClient(), planner=RepairStub())
    assert res == {"hunt": False, "reason": "no_notified_impact"}


def test_push_sweep_pr_paths_never_auto_trigger_hunt(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(cross_repo, "request_hunt_repair",
                        lambda *a, **k: calls.append((a, k)) or {"hunt": True})
    handle_push_event(_raw_push("acme/api-service", "feature/payments", "aaa"), FakeClient())
    cross_repo.sweep_and_notify(FakeClient(), quiet_s=0)
    cross_repo.pr_fastpath({
        "action": "opened", "repository": {"full_name": "acme/api-service"},
        "pull_request": {"number": 1, "head": {"ref": "feature/payments", "sha": "aaa"},
                         "merged": False}}, FakeClient())
    assert calls == []
