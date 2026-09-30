# Copyright 2026 Truhowl Authors
"""Watch loop: polls READY checkouts, fires pipeline only on new findings."""

import os
import shutil
import subprocess
from unittest.mock import MagicMock

from truhowl.github.installations import REPO_READY, record_installation_event
from truhowl.github.watch import watch_once


def _seed_repo_with_remote(tmp_path, name="backend"):
    remote = str(tmp_path / f"{name}-remote")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    work = str(tmp_path / f"{name}-work")
    subprocess.run(["git", "clone", "-q", remote, work], check=True)
    subprocess.run(["git", "-C", work, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", work, "config", "user.name", "t"], check=True)
    for entry in os.listdir("trials/fixtures/taxonomy_stripe"):
        src = os.path.join("trials/fixtures/taxonomy_stripe", entry)
        dst = os.path.join(work, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    subprocess.run(["git", "-C", work, "add", "."], check=True)
    subprocess.run(["git", "-C", work, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", work, "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", work, "push", "-q", "origin", "main"], check=True)
    return remote


def test_watch_fires_once_then_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", _seed_repo_with_remote(tmp_path))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    record_installation_event(
        {"action": "created", "installation": {"id": 21, "account": {"login": "acme"}}},
        {"acme/backend": {"state": REPO_READY}})
    client = MagicMock()
    client.token = None
    first = watch_once(client=client)
    assert any(o.get("new_findings", 0) > 0 for o in first), first
    second = watch_once(client=client)
    assert all(o.get("new_findings", 0) == 0 for o in second), second


def test_watch_skips_non_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    record_installation_event(
        {"installation": {"id": 22}}, {"acme/pending": {"state": "PENDING"}})
    assert watch_once(client=MagicMock()) == []
