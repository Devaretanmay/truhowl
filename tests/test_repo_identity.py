# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

import os

from truhowl.repo_identity import (
    derive_repository_key,
    get_active_repo,
    get_repository,
    load_all_repositories,
    register_repository,
    set_active_repo,
    set_bot_state,
    unregister_repository,
    STATE_ACTIVE,
    STATE_AVAILABLE,
)


def test_deterministic_cross_device_identity(tmp_path, monkeypatch):
    home_a = tmp_path / "device_a"
    home_b = tmp_path / "device_b"
    home_a.mkdir()
    home_b.mkdir()

    (home_a / ".truhowl").mkdir()
    (home_a / ".truhowl" / ".truhowl_salt").write_bytes(os.urandom(32))
    (home_b / ".truhowl").mkdir()
    (home_b / ".truhowl" / ".truhowl_salt").write_bytes(os.urandom(32))

    monkeypatch.setenv("HOME", str(home_a))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_a / ".truhowl"))
    key_a = derive_repository_key("octocat/Hello-World", repo_id="1296269")

    monkeypatch.setenv("HOME", str(home_b))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_b / ".truhowl"))
    key_b = derive_repository_key("octocat/Hello-World", repo_id="1296269")

    assert key_a == key_b
    assert key_a.startswith("kyp_")
    assert len(key_a) == 32


def test_multiple_devices_register_same_repo_no_duplicates(tmp_path, monkeypatch):
    home_a = tmp_path / "device_a"
    home_b = tmp_path / "device_b"

    monkeypatch.setenv("HOME", str(home_a))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_a / ".truhowl"))
    rec_a = register_repository("acme/payments-service", repo_id="98765", installation_id="111")

    monkeypatch.setenv("HOME", str(home_b))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_b / ".truhowl"))
    rec_b = register_repository("acme/payments-service", repo_id="98765", installation_id="111")

    assert rec_a["repo_key"] == rec_b["repo_key"]
    assert rec_a["repo_name"] == rec_b["repo_name"]

    all_repos = load_all_repositories()
    assert len(all_repos) == 1
    assert "acme/payments-service" in all_repos


def test_active_repository_is_local_context(tmp_path, monkeypatch):
    home_a = tmp_path / "device_a"
    home_b = tmp_path / "device_b"

    monkeypatch.setenv("HOME", str(home_a))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_a / ".truhowl"))
    register_repository("org/repo-one", repo_id="1")
    register_repository("org/repo-two", repo_id="2")
    set_active_repo("org/repo-one")
    assert get_active_repo() == "org/repo-one"

    monkeypatch.setenv("HOME", str(home_b))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_b / ".truhowl"))
    register_repository("org/repo-one", repo_id="1")
    register_repository("org/repo-two", repo_id="2")
    set_active_repo("org/repo-two")
    assert get_active_repo() == "org/repo-two"

    monkeypatch.setenv("HOME", str(home_a))
    monkeypatch.setenv("TRUHOWL_DIR", str(home_a / ".truhowl"))
    assert get_active_repo() == "org/repo-one"


def test_unregister_and_clear_active_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRUHOWL_DIR", str(tmp_path / ".truhowl"))

    register_repository("org/cleanup-target", repo_id="44")
    set_active_repo("org/cleanup-target")
    assert get_active_repo() == "org/cleanup-target"

    unregistered = unregister_repository("org/cleanup-target")
    assert unregistered is True
    assert get_repository("org/cleanup-target") is None
    assert get_active_repo() is None


def test_bot_state_transitions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRUHOWL_DIR", str(tmp_path / ".truhowl"))

    rec = register_repository("org/watcher-test", repo_id="77")
    assert rec["howl_state"] == STATE_AVAILABLE
    assert rec["hunt_state"] == STATE_AVAILABLE

    updated_howl = set_bot_state("org/watcher-test", "howl", STATE_ACTIVE)
    assert updated_howl["howl_state"] == STATE_ACTIVE

    updated_hunt = set_bot_state("org/watcher-test", "hunt", STATE_ACTIVE)
    assert updated_hunt["hunt_state"] == STATE_ACTIVE
