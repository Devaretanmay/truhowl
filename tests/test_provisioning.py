# Copyright 2026 Truhowl Authors
"""Provisioning: clone-on-install, pull-on-sighting, payload resolution. No network."""

import os
import subprocess

from truhowl.github.provisioning import (
    cached_path, ensure_repo_checkout, remote_for, workdir_for_event,
)


def _seed_remote(tmp_path) -> str:
    remote = str(tmp_path / "remote")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    work = str(tmp_path / "work")
    subprocess.run(["git", "clone", "-q", remote, work], check=True)
    subprocess.run(["git", "-C", work, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", work, "config", "user.name", "t"], check=True)
    with open(os.path.join(work, "package.json"), "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0"}}')
    subprocess.run(["git", "-C", work, "add", "."], check=True)
    subprocess.run(["git", "-C", work, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", work, "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", work, "push", "-q", "origin", "main"], check=True)
    return remote


def test_clone_and_cached_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", _seed_remote(tmp_path))
    dest = ensure_repo_checkout("acme/backend")
    assert dest == cached_path("acme/backend")
    assert os.path.isfile(os.path.join(dest, "package.json"))
    assert ensure_repo_checkout("acme/backend") == dest


def test_unreachable_remote_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_GHOST__NOPE", "/nonexistent/path/xyz")
    assert ensure_repo_checkout("ghost/nope") is None


def test_workdir_for_event(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", _seed_remote(tmp_path))
    assert workdir_for_event({"repository": {"full_name": "acme/backend"}}) is not None
    assert workdir_for_event({}) is None


def test_remote_for_prefers_token_and_override(tmp_path, monkeypatch):
    assert remote_for("a/b", token="tok") == "https://x-access-token:tok@github.com/a/b.git"
    assert remote_for("a/b") == "https://github.com/a/b.git"
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_A__B", "/mirror/b")
    assert remote_for("a/b") == "/mirror/b"
