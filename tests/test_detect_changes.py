# Copyright 2026 Truhowl Authors
"""Detection-vs-repair split: detect_changes classifies without patching."""

import os
import shutil

from truhowl.audit import run_audit
from truhowl.change_source import IMPACT_AI, IMPACT_QUARANTINE
from truhowl.drift import detect_changes


def test_stripe_fixture_detects_ai_impact_with_credentials(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    dets = detect_changes("trials/fixtures/taxonomy_stripe", "stripe")
    assert len(dets) == 1
    d = dets[0]
    assert d.outcome == IMPACT_AI
    assert d.callsite_count > 0 and d.affected_files
    assert d.ai_dependent is True
    assert d.confidence >= 0.9


def test_stripe_fixture_quarantines_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    dets = detect_changes("trials/fixtures/taxonomy_stripe", "stripe")
    assert len(dets) == 1
    d = dets[0]
    assert d.outcome == IMPACT_QUARANTINE
    assert d.ai_dependent is False


def test_empty_repo_detects_nothing(tmp_path):
    assert detect_changes(str(tmp_path)) == []


def test_check_writes_nothing_outside_truhowl(tmp_path):
    dst = str(tmp_path / "r")
    shutil.copytree("trials/fixtures/taxonomy_stripe", dst)
    before = {}
    for dirpath, dirnames, filenames in os.walk(dst):
        dirnames[:] = [d for d in dirnames if d not in {".truhowl"}]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            before[fp] = open(fp, "rb").read()
    run_audit(dst, output_format="json", write_graph=True)
    for fp, content in before.items():
        assert open(fp, "rb").read() == content
