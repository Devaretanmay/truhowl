# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""PR-head checkout: exact SHA when fetchable, disclosed fallback otherwise."""

import os
import subprocess
from unittest.mock import MagicMock

from truhowl.github.provisioning import ensure_pr_checkout, resolve_pr_workdir
from truhowl.pipeline import AnalysisResult, DriftFinding, TriggerContext, surface_result


def _seed_repo_with_pr_ref(tmp_path):
    remote = str(tmp_path / "remote")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    work = str(tmp_path / "work")
    subprocess.run(["git", "clone", "-q", remote, work], check=True)
    subprocess.run(["git", "-C", work, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", work, "config", "user.name", "t"], check=True)
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("base\n")
    subprocess.run(["git", "-C", work, "add", "."], check=True)
    subprocess.run(["git", "-C", work, "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", work, "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", work, "push", "-q", "origin", "main"], check=True)
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("head-change\n")
    subprocess.run(["git", "-C", work, "commit", "-qam", "pr"], check=True)
    sha = subprocess.run(["git", "-C", work, "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", work, "push", "-q", "origin",
                    f"{sha}:refs/pull/42/head"], check=True)
    return remote, sha


def test_pr_head_exact_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    remote, sha = _seed_repo_with_pr_ref(tmp_path)
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", remote)
    path, exact = ensure_pr_checkout("acme/backend", 42, sha)
    assert exact is True
    with open(os.path.join(path, "a.txt")) as f:
        assert f.read() == "head-change\n"


def test_pr_head_missing_sha_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    remote, _ = _seed_repo_with_pr_ref(tmp_path)
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", remote)
    path, exact = ensure_pr_checkout("acme/backend", 42, "0" * 40)
    assert exact is False and path is not None


def test_resolve_pr_workdir_threads_exactness(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_REPOS_DIR", str(tmp_path / "repos"))
    remote, sha = _seed_repo_with_pr_ref(tmp_path)
    monkeypatch.setenv("TRUHOWL_REPO_REMOTE_ACME__BACKEND", remote)
    payload = {"repository": {"full_name": "acme/backend"},
               "pull_request": {"number": 42, "head": {"sha": sha}}}
    path, exact = resolve_pr_workdir(payload)
    assert exact is True and path is not None


def test_approximate_checkout_disclosed_in_status():
    ctx = TriggerContext(event_id="e", event_type="pull_request.opened", repository="a/b",
                         ref="x", sha="y", workdir="/tmp", pr_number=1,
                         metadata={"exact_head": False})
    analysis = AnalysisResult(
        context=ctx,
        findings=[DriftFinding(provider_name="stripe", display_name="Stripe",
                               package_name="stripe", current_version="1", target_version="2",
                               breaking_change="b", migration_guide_url="")],
    )
    res = surface_result(ctx, analysis, MagicMock(), MagicMock())
    assert "[checkout: tracked branch, PR head unfetchable]" in res.status_description
