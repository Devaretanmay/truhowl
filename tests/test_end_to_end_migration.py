# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end migration integration: plan -> author -> verify -> replay -> seal.

The author is SCRIPTED here on purpose: these tests measure the migration engine
and its fail-closed gates, not model quality. A scripted author that follows the
same migration plan a model receives is the cleanest way to assert that

* a COMPLETE multi-file migration is sealed and promoted, and
* an INCOMPLETE one is refused, with the surviving migration unit named.

Real (model-authored) development runs live in scripts/run_development_cases.py.
"""

import json
import os
import shutil
import subprocess

import pytest

from truhowl import hunt as hunt_agent
from truhowl.failure_taxonomy import (
    CALLSITE_NOT_MIGRATED,
    IMPORT_NOT_MIGRATED,
    classify_from_audit,
    read_audit,
)
from truhowl.hunt import list_findings, run_hunt
from truhowl.patch_writer import PatchResult

FIXTURE = os.path.join("trials", "fixtures", "langchainjs_openai")


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeClient:
    """Reasoning + interpretation stub (authoring is scripted separately)."""

    def __init__(self, config=None):
        self.config = config

    def complete(self, messages=None, system_prompt=None):
        if system_prompt and "verifying its own repair" in system_prompt:
            return _FakeResp(json.dumps({
                "solved": True, "unrelated_behavior": False, "assumptions_false": [],
                "cause": "hunt", "needs_more_investigation": False,
                "rationale": "verified in a clean sandbox",
            }))
        return _FakeResp(json.dumps({
            "problem": "openai v4 migration",
            "current_behavior": "v3 client usage",
            "cause": "upstream breaking change",
            "intended_behavior": "same behavior on v4",
            "affected_paths": [
                "src/chat_models/openai.ts", "src/embeddings/openai.ts", "package.json"],
            "affected_callsites": [],
            "related_matter": [],
            "explicitly_unaffected": [],
            "assumptions": [],
            "smallest_change": "migrate the whole client boundary",
            "must_not_change": [],
            "regression_risks": [],
            "verification_plan": "node test/run.js",
            "file_intents": [],
        }))


# The exact migration the plan's knowledge prescribes for this provider pair.
# A complete follower applies all of it; a partial follower omits the last rule.
_COMPLETE_RULES: list[tuple[str, str]] = [
    ("import { Configuration, OpenAIApi } from 'openai';", "import OpenAI from 'openai';"),
    ("private client: OpenAIApi;", "private client: OpenAI;"),
    ("const config = new Configuration({ apiKey });\n    this.client = new OpenAIApi(config);",
     "this.client = new OpenAI({ apiKey });"),
    ("createChatCompletion(", "chat.completions.create("),
    ("createEmbedding(", "embeddings.create("),
    ("response.data.choices", "response.choices"),
    ("response.data.data", "response.data"),
]
# Drops the import/type rule: the callsite and manifest still move, so the
# repository's own harness passes, but legacy symbols survive in both files.
# This is precisely the "callsite fixed, old types remain" failure pattern.
_PARTIAL_RULES = _COMPLETE_RULES[1:]


class _ScriptedFollower:
    """Applies a fixed rule set per file; mimics an author that follows a plan."""

    def __init__(self, rules):
        self.rules = rules
        self.last_error = ""

    def plan_and_apply(self, repo_dir=None, affected_files=None, provider_name="", **kwargs):
        results = []
        for path in affected_files or []:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                original = f.read()
            current = original
            for old, new in self.rules:
                current = current.replace(old, new)
            if path.endswith("package.json"):
                current = current.replace('"openai": "^3.3.0"', '"openai": "^4.0.0"')
            if current == original:
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(current)
            results.append(PatchResult(
                file_path=path, success=True, lines_changed=1,
                unified_diff=f"--- a/{os.path.basename(path)}\n+++ b/{os.path.basename(path)}\n",
                rules_applied=["scripted plan follower"],
            ))
        return results


@pytest.fixture()
def langchain_repo(tmp_path):
    repo = str(tmp_path / "langchainjs_openai")
    shutil.copytree(FIXTURE, repo, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "K"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "k@k.dev"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)
    return repo


def _harness(repo) -> int:
    return subprocess.run(["node", "test/run.js"], cwd=repo,
                          capture_output=True, text=True).returncode


def _run(repo, monkeypatch, rules, monkeypatch_planner=True):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    monkeypatch.setattr(hunt_agent, "LLMClient", _FakeClient)
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)
    if monkeypatch_planner:
        class _Planner(_ScriptedFollower):
            def __init__(self, client=None):
                super().__init__(rules)
        monkeypatch.setattr(hunt_agent, "AIPatchPlanner", _Planner)
    findings = list_findings(repo)
    assert findings, "fixture must produce a finding"
    return run_hunt(repo, findings[0].finding_id, max_iterations=2)


def test_complete_multi_file_migration_is_verified_and_promoted(langchain_repo, monkeypatch):
    """Baseline is green; after the migration the fixture harness must still pass."""
    assert _harness(langchain_repo) == 0, "baseline must be green"

    report = _run(langchain_repo, monkeypatch, _COMPLETE_RULES)

    assert report.success is True, report.reason
    assert report.test_exit_code == 0
    promoted = {f.replace(os.sep, "/") for f in report.files_modified}
    assert {"src/chat_models/openai.ts", "src/embeddings/openai.ts"} <= promoted
    assert report.verified is not None

    # The repository's own behavioral harness now passes on the migrated state.
    assert _harness(langchain_repo) == 0
    for rel in ("src/chat_models/openai.ts", "src/embeddings/openai.ts"):
        with open(os.path.join(langchain_repo, rel)) as f:
            content = f.read()
        assert "chat.completions.create" in content or "embeddings.create" in content
        assert "OpenAIApi" not in content, "no legacy symbol may survive a verified repair"


def test_partial_migration_is_refused_and_names_the_surviving_unit(langchain_repo, monkeypatch):
    """The failure this pass exists to fix: a migration that stops half way.

    The repository harness alone would accept this candidate (the manifest and
    the callsite moved), so only the residual-unit check can catch it.
    """
    report = _run(langchain_repo, monkeypatch, _PARTIAL_RULES)

    assert report.success is False, "an incomplete migration must never be sealed"
    assert report.pr_url is None
    audit = read_audit(report.audit_path)
    assert audit.get("final") == "refused_unverified"
    residual = audit.get("residual_drift") or []
    assert residual, "the surviving migration unit must be recorded"
    olds = {h["old_symbol"] for entry in residual for h in entry["hits"]}
    assert olds, olds
    classification = classify_from_audit(audit)
    assert classification.category in (CALLSITE_NOT_MIGRATED, IMPORT_NOT_MIGRATED), classification


def test_repository_harness_alone_would_have_accepted_the_partial_migration(langchain_repo):
    """Evidence that the residual gate (not the harness) is what closes the hole."""
    for old, new in _PARTIAL_RULES:
        pass  # rules applied below, manually, to keep the harness un-run
    for rel in ("src/chat_models/openai.ts", "src/embeddings/openai.ts"):
        path = os.path.join(langchain_repo, rel)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for old, new in _PARTIAL_RULES:
            content = content.replace(old, new)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    pkg = os.path.join(langchain_repo, "package.json")
    with open(pkg, encoding="utf-8") as f:
        content = f.read().replace('"openai": "^3.3.0"', '"openai": "^4.0.0"')
    with open(pkg, "w", encoding="utf-8") as f:
        f.write(content)

    assert _harness(langchain_repo) == 0, (
        "the fixture's own harness accepts this partial migration, which is why "
        "verification alone cannot detect an incomplete migration unit")
