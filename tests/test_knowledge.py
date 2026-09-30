# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Repository Knowledge Base: namespaced entries, legacy fallback, failure quarantine."""

import json
import os

from truhowl.knowledge import (
    direct_rewrites_for, ensure_test_recipe, lookup,
    record_failure, upsert_learned,
)
from truhowl.maintenance import run_maintenance_cycle


def _rewrite(pattern="a(", replacement="b(", exts=(".ts",), desc="d"):
    return type("R", (), {"pattern": pattern, "replacement": replacement,
                          "file_extensions": list(exts), "description": desc})()


def test_upsert_writes_namespaced_path(tmp_path):
    e = upsert_learned(str(tmp_path), "stripe", "11.18.0", "13.0.0",
                       applied_rules=[], test_command="npm test", rewrites=[_rewrite()])
    assert os.path.isfile(tmp_path / ".truhowl" / "knowledge" / "sdk" / "stripe" / "11.18.0__13.0.0.json")
    assert e.kind == "sdk"
    assert len(direct_rewrites_for(str(tmp_path), "stripe", "11.18.0", "13.0.0")) == 1


def test_legacy_provider_path_still_readable(tmp_path):
    legacy = tmp_path / ".truhowl" / "knowledge" / "stripe"
    legacy.mkdir(parents=True)
    (legacy / "1__2.json").write_text(json.dumps({
        "migration_id": "m", "provider": "stripe", "from_version": "1", "to_version": "2",
        "patterns": [{"pattern": "p", "replacement": "r", "file_extensions": [".ts"]}],
        "test_recipe": {}, "evidence": {},
    }))
    e = lookup(str(tmp_path), "stripe", "1", "2")
    assert e is not None and len(e.patterns) == 1
    assert e.failed_patterns == [] and e.confidence == 1.0


def test_record_failure_never_creates_trusted_patterns(tmp_path):
    upsert_learned(str(tmp_path), "stripe", "1", "2", applied_rules=[],
                   test_command="t", rewrites=[_rewrite()])
    f = record_failure(str(tmp_path), "stripe", "1", "2", "ai guess failed", ["x.ts"])
    assert len(f.failed_patterns) == 1
    assert f.failed_patterns[0]["affected_paths"] == ["x.ts"]
    assert len(direct_rewrites_for(str(tmp_path), "stripe", "1", "2")) == 1


def test_failed_verification_records_avoidance_note(tmp_path):
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({
        "name": "t", "dependencies": {"stripe": "^11.18.0"},
        "scripts": {"test": "node -e \"process.exit(1)\""}}))
    (repo / "src" / "a.ts").write_text("stripe.subscriptions.del('s');\n")
    report = run_maintenance_cycle(str(repo), "stripe", from_version="11.18.0", to_version="13.0.0")
    assert not report.success
    entry = lookup(str(repo), "stripe", "11.18.0", "13.0.0")
    assert entry is not None and len(entry.failed_patterns) == 1
    assert entry.patterns == []


def test_ensure_test_recipe_seeds_without_patterns(tmp_path):
    e = ensure_test_recipe(str(tmp_path), "openai", "3.3.0", "4.0.0", "pytest -q")
    assert e.test_recipe["test_command"] == "pytest -q"
    assert direct_rewrites_for(str(tmp_path), "openai", "3.3.0", "4.0.0") == []
