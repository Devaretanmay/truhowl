# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Decision engine: automatic strategy selection, hidden from users (no CLI flag)."""

import subprocess
import sys

from truhowl.change_source import ChangeSource
from truhowl.intelligence import TruhowlIntelligence, Decision
from truhowl.providers.registry import find_migration_for


def test_quarantine_for_known_sdk_rewrite_without_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    d = TruhowlIntelligence().decide("/tmp", "stripe", "11.18.0", "13.0.0", has_rewrites=True)
    assert d.strategy == "QUARANTINE"
    assert d.confidence == 0.0
    assert d.reason == "no_credentials_for_ai"


def test_ai_authored_when_credentials_present(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    d = TruhowlIntelligence().decide("/tmp", "stripe", "11.18.0", "13.0.0", has_rewrites=True)
    assert d.strategy == "AI"
    assert "ai_authored" in d.reason
    assert d.confidence >= 0.95


def test_quarantine_without_creds_or_rewrites(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    d = TruhowlIntelligence().decide(str(tmp_path), "twilio", "1.0", "2.0", has_rewrites=False)
    assert d.strategy == "QUARANTINE"
    assert d.confidence == 0.0


def test_no_direct_or_ai_cli_flag():
    res = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "fix", "--help"],
        capture_output=True, text=True, env={"PYTHONPATH": "python", "PATH": "/usr/bin:/bin"},
    )
    assert "--direct" not in res.stdout
    assert "--ai" not in res.stdout


def test_find_migration_for_sdk_kind():
    m = find_migration_for(ChangeSource.sdk("stripe", version_to="13.0.0"))
    assert m is not None and m.to_version == "13.0.0"


def test_find_migration_for_future_kinds_returns_none():
    for kind in ("openapi", "graphql", "protobuf", "webhook", "mcp_server", "internal_service"):
        assert find_migration_for(ChangeSource(kind=kind, identity="x")) is None


def test_decide_for_source_future_kind_quarantines_without_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    d = TruhowlIntelligence().decide_for_source(
        str(tmp_path), ChangeSource(kind="mcp_server", identity="internal-tools"))
    assert d.strategy == "QUARANTINE"
    assert "mcp_server" in d.reason


def test_decision_fields_have_sane_defaults():
    d = Decision(strategy="DIRECT", reason="test")
    assert d.confidence == 0.0
