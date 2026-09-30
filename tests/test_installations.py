# Copyright 2026 Truhowl Authors
"""Installation persistence: record → index → READY survives restarts."""

import os
import shutil
from unittest.mock import MagicMock

from truhowl.github.installations import (
    REPO_INDEXED, REPO_PENDING, REPO_READY, list_ready_repos, load_installation,
    record_installation_event, set_repo_state,
)
from truhowl.github.pr_bot import handle_installation_event


def test_record_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    rec = record_installation_event(
        {"action": "created", "installation": {"id": 42, "account": {"login": "acme"}}},
        {"acme/backend": {"state": REPO_PENDING}},
    )
    assert rec["installation_id"] == "42"
    loaded = load_installation("42")
    assert loaded["repos"]["acme/backend"]["state"] == REPO_PENDING
    mode = os.stat(tmp_path / "inst" / "42.json").st_mode & 0o777
    assert mode == 0o600


def test_install_event_indexes_local_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    workdir = str(tmp_path / "backend")
    shutil.copytree("trials/fixtures/taxonomy_stripe", workdir)
    client = MagicMock()
    payload = {"action": "created", "installation": {"id": 7, "account": {"login": "acme"}},
               "repositories": [{"full_name": "acme/backend"}]}
    res = handle_installation_event(payload, "installation.created", client,
                                    workdir_fn=lambda r: workdir)
    assert res["success"] is True
    assert res["repo_states"] == {"acme/backend": REPO_READY}
    assert os.path.isfile(os.path.join(workdir, ".truhowl", "graph.json"))
    assert client.create_issue.call_count == 1
    assert list_ready_repos("7") == ["acme/backend"]


def test_install_event_without_checkout_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    client = MagicMock()
    res = handle_installation_event(
        {"action": "created", "installation": {"id": 8},
         "repositories": [{"full_name": "acme/ghost"}]},
        "installation.created", client)
    assert res["repo_states"] == {"acme/ghost": REPO_PENDING}
    assert res["repositories_onboarded"] == ["acme/ghost"]


def test_ready_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    record_installation_event({"installation": {"id": 9}}, {"acme/a": {"state": REPO_INDEXED}})
    assert list_ready_repos("9") == []
    set_repo_state("9", "acme/a", "READY")
    assert list_ready_repos("9") == ["acme/a"]
