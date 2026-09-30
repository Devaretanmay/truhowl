# Copyright 2026 Truhowl Authors
"""Incremental indexing: fresh-skip, change detection, discovery deltas."""

import os
import shutil
import time

from truhowl.audit import changed_since_index, read_index_state, run_audit


def _seed_repo(dst: str):
    os.makedirs(os.path.join(dst, "src"), exist_ok=True)
    with open(os.path.join(dst, "package.json"), "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0"}}')
    with open(os.path.join(dst, "src", "a.ts"), "w") as f:
        f.write("import Stripe from 'stripe';\n")


def test_first_index_writes_state(tmp_path):
    dst = str(tmp_path / "r")
    shutil.copytree("trials/fixtures/taxonomy_stripe", dst)
    run_audit(dst, output_format="json", write_graph=True)
    state = read_index_state(dst)
    assert state is not None and state["version"] == 1
    assert state["providers"] >= 1


def test_no_changes_means_fresh(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    run_audit(dst, output_format="json", write_graph=True)
    info = changed_since_index(dst)
    assert info["indexed"] is True and info["fresh"] is True
    assert info["changed_files"] == []


def test_new_file_detected_and_manifests_flagged(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    run_audit(dst, output_format="json", write_graph=True)
    time.sleep(0.02)
    with open(os.path.join(dst, "src", "refunds.ts"), "w") as f:
        f.write("import Stripe from 'stripe';\n")
    with open(os.path.join(dst, "package.json"), "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0", "openai": "^4.0.0"}}')
    info = changed_since_index(dst)
    assert info["fresh"] is False
    assert any("refunds.ts" in c for c in info["changed_files"])
    assert "package.json" in info["manifests_changed"]
    run_audit(dst, output_format="json", write_graph=True)
    assert changed_since_index(dst)["fresh"] is True


def test_unindexed_repo_reports_not_indexed(tmp_path):
    info = changed_since_index(str(tmp_path / "empty"))
    assert info == {"indexed": False, "fresh": False, "changed_files": [], "manifests_changed": []}
