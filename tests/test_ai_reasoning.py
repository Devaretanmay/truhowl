# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""AI-first reasoning: deep context assembly and hybrid escalation."""

import os
from unittest.mock import MagicMock

from truhowl.ai_planner import (
    AIPatchPlanner, MAX_PROMPT_FILE_CHARS, bound_file_content, build_reasoning_context,
)
from truhowl.knowledge import upsert_learned, record_failure
from truhowl.llm import LLMClient, LLMResponse
import truhowl.maintenance as mnt


def _seed_repo(dst: str):
    os.makedirs(os.path.join(dst, "src"), exist_ok=True)
    with open(os.path.join(dst, "package.json"), "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0"},'
                '"scripts": {"test": "node -e \\"process.exit(0)\\""}}')
    with open(os.path.join(dst, "src", "billing.ts"), "w") as f:
        f.write("import Stripe from 'stripe';\nconst s = new Stripe('x');\n")


def test_context_assembles_repo_change_and_memory(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    R = type("R", (), {"pattern": "p", "replacement": "r",
                       "file_extensions": [".ts"], "description": "verified-shape"})
    upsert_learned(dst, "stripe", "11.18.0", "13.0.0", applied_rules=[],
                   test_command="npm test", rewrites=[R()])
    record_failure(dst, "stripe", "11.18.0", "13.0.0", "bad-guess", ["x.ts"])
    ctx = build_reasoning_context(dst, "stripe", "11.18.0", "13.0.0", "desc")
    assert ctx["test_command"] == "npm test"
    assert any("billing.ts" in c for c in ctx["callsites"])
    assert ctx["verified_patterns"] == ["verified-shape"]
    assert ctx["failed_patterns"] == ["bad-guess"]


def test_context_needs_no_credentials_and_never_raises(tmp_path):
    ctx = build_reasoning_context(str(tmp_path / "missing"), "nope", "1", "2")
    assert ctx["callsites"] == [] and ctx["verified_patterns"] == []


def test_file_content_bounded_with_notice():
    text, truncated = bound_file_content("x" * (MAX_PROMPT_FILE_CHARS + 1))
    assert truncated is True and len(text) == MAX_PROMPT_FILE_CHARS
    text, truncated = bound_file_content("small")
    assert truncated is False and text == "small"


def test_huge_file_prompt_announces_window(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    big = os.path.join(dst, "src", "big.ts")
    with open(big, "w") as f:
        f.write("// stripe usage\n" + ("const v = 1;\n" * 2000))
    seen = {}
    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.side_effect = lambda messages, system_prompt=None: (
        seen.update(user=messages[0]["content"]), LLMResponse(content="x", model="m"))[1]
    AIPatchPlanner(client=mock_client).plan_and_apply(
        repo_dir=dst, affected_files=["src/big.ts"], provider_name="stripe",
        from_version="1", to_version="2", migration_details="d", dry_run=True)
    assert "[File truncated to first" in seen["user"]
    assert "reason only over shown lines" in seen["user"]


def test_prompt_carries_reasoning_and_memory(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    seen = {}

    mock_client = MagicMock(spec=LLMClient)

    def _capture(messages, system_prompt=None):
        seen["system"] = system_prompt
        seen["user"] = messages[0]["content"]
        return LLMResponse(content="no blocks here", model="m")

    mock_client.complete.side_effect = _capture
    planner = AIPatchPlanner(client=mock_client)
    planner.plan_and_apply(
        repo_dir=dst, affected_files=["src/billing.ts"], provider_name="stripe",
        from_version="11.18.0", to_version="13.0.0", migration_details="d",
        context=build_reasoning_context(dst, "stripe", "11.18.0", "13.0.0", "d"),
        dry_run=True)
    assert "determine what must change" in seen["system"]
    assert "Known usage across the repo" in seen["user"]
    assert "Repo verification" in seen["user"]


def test_ai_repair_completion_for_untouched_files(tmp_path, monkeypatch):
    """AI repairs code including unusual files detected by impact analysis."""
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    target = os.path.join(dst, "src", "unusual.ts")
    with open(target, "w") as f:
        f.write("// stripe usage below\nconst v = 1;\n")

    real_impact = mnt.analyze_impact(dst, "stripe")
    assert "src/unusual.ts" not in real_impact.affected_files

    class _Impact:
        affected_files = list(real_impact.affected_files) + ["src/unusual.ts"]

    monkeypatch.setattr(mnt, "analyze_impact",
                        lambda r, p: _Impact())
    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(
        content="<<<<<<< SEARCH\nconst v = 1;\n=======\nconst v = 2;\n>>>>>>> REPLACE",
        model="m")
    monkeypatch.setattr(mnt.AIPatchPlanner, "from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=mock_client)))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))

    report = mnt.run_maintenance_cycle(dst, "stripe", from_version="11.18.0", to_version="13.0.0")
    assert report.success
    assert "const v = 2;" in open(target).read()
    history = mnt.get_migration_history(dst)
    assert history and history[-1]["strategy"] == "AI"
    assert report.repair_path == "ai-reasoning"
