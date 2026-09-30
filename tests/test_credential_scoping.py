# Copyright 2026 Truhowl Authors
"""Credential scoping: env → scoped file → global file; secrets never leak to repo state."""

import os
import shutil

from truhowl.credentials import (
    has_valid_credentials, load_credentials, save_credentials, scoped_credentials_path,
)
from truhowl.maintenance import run_maintenance_cycle


def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "creds.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_scoped_resolution_order(tmp_path, monkeypatch):
    _clean_env(monkeypatch, tmp_path)
    assert not has_valid_credentials("inst-1")
    save_credentials("openai", "sk-global-1234567890", installation_id=None)
    assert has_valid_credentials("inst-1")
    save_credentials("anthropic", "sk-ant-scoped-1234567890", installation_id="inst-1")
    creds = load_credentials("inst-1")
    assert creds["provider"] == "anthropic"
    assert load_credentials("inst-2")["provider"] == "openai"
    mode = os.stat(scoped_credentials_path("inst-1")) .st_mode & 0o777
    assert mode == 0o600


def test_no_secret_in_repo_state_after_fix(tmp_path, monkeypatch):
    _clean_env(monkeypatch, tmp_path)
    secret = "sk-ant-leakprobe-99998888"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    dst = str(tmp_path / "r")
    shutil.copytree("trials/fixtures/taxonomy_stripe", dst)
    run_maintenance_cycle(dst, "stripe", from_version="11.18.0", to_version="13.0.0")
    for dirpath, _, filenames in os.walk(os.path.join(dst, ".truhowl")):
        for fn in filenames:
            content = open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace").read()
            assert secret not in content, f"secret leaked into {fn}"
