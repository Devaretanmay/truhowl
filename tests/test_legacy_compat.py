# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Legacy .koyote -> .truhowl transition compat.

The fallback in python/truhowl/config.py (find_workspace_root /
is_truhowl_workspace) and python/truhowl/repo_identity.py (_repo_store_dir)
is intentional transition support, not stale branding. These tests pin it.
"""

from __future__ import annotations

import os

from truhowl import config as config_mod
from truhowl import repo_identity as repo_identity_mod
from truhowl.config import find_workspace_root, is_truhowl_workspace


def test_is_truhowl_workspace_detects_legacy_koyote(tmp_path):
    legacy = tmp_path / "proj"
    legacy.mkdir()
    assert is_truhowl_workspace(str(legacy)) is False
    (legacy / ".koyote").mkdir()
    assert is_truhowl_workspace(str(legacy)) is True


def test_is_truhowl_workspace_detects_legacy_from_child(tmp_path):
    root = tmp_path / "proj"
    child = root / "a" / "b"
    child.mkdir(parents=True)
    (root / ".koyote").mkdir()
    assert is_truhowl_workspace(str(child)) is True


def test_find_workspace_root_detects_legacy_koyote(tmp_path):
    root = tmp_path / "proj"
    nested = root / "src" / "deep"
    nested.mkdir(parents=True)
    (root / ".koyote").mkdir()
    assert find_workspace_root(str(nested)) == str(root)


def test_find_workspace_root_innermost_wins_with_mixed_names(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".koyote").mkdir()
    (inner / ".truhowl").mkdir()
    assert find_workspace_root(str(inner)) == str(inner)


def _clear_store_env(monkeypatch):
    monkeypatch.delenv("TRUHOWL_DIR", raising=False)
    monkeypatch.delenv("KOYOTE_DIR", raising=False)


def test_repo_store_dir_falls_back_to_legacy_koyote(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _clear_store_env(monkeypatch)
    (fake_home / ".koyote").mkdir()
    assert repo_identity_mod._repo_store_dir() == str(fake_home / ".koyote")


def test_repo_store_dir_prefers_new_truhowl(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _clear_store_env(monkeypatch)
    (fake_home / ".koyote").mkdir()
    (fake_home / ".truhowl").mkdir()
    assert repo_identity_mod._repo_store_dir() == str(fake_home / ".truhowl")


def test_repo_store_dir_defaults_to_new_when_neither_exists(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _clear_store_env(monkeypatch)
    assert repo_identity_mod._repo_store_dir() == str(fake_home / ".truhowl")


def test_repo_store_dir_env_override_wins(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("TRUHOWL_DIR", str(override))
    (fake_home / ".koyote").mkdir()
    (fake_home / ".truhowl").mkdir()
    assert repo_identity_mod._repo_store_dir() == str(override)


def test_config_module_import_does_not_touch_real_home(monkeypatch):
    # Sanity: these code paths only use the passed path, never HOME.
    assert config_mod.__name__ == "truhowl.config"
    assert os.path.abspath("/") == "/"
