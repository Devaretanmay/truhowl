# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Doctor reports GitHub / AI / index / KB / test-cmd / monitoring states."""

import os
import shutil
import subprocess
import sys

from truhowl.github.installations import REPO_READY, record_installation_event


def _run(args, env, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main"] + args,
        capture_output=True, text=True, env=env, cwd=cwd,
    )


def _env(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.abspath("python")
    env["TRUHOWL_CREDENTIALS_FILE"] = str(tmp_path / "creds.json")
    env["TRUHOWL_INSTALLATIONS_DIR"] = str(tmp_path / "inst")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY",
              "GITHUB_TOKEN", "TRUHOWL_GITHUB_TOKEN"):
        env.pop(k, None)
    return env


def test_doctor_all_states(tmp_path):
    env = _env(tmp_path)
    res = _run(["doctor"], env, cwd=str(tmp_path))
    assert res.returncode == 0
    for line in ("GitHub:", "AI provider:", "Indexed:", "Knowledge Base:",
                 "Test command:", "Monitoring:"):
        assert line in res.stdout
    assert "NOT CONFIGURED" in res.stdout
    assert "MISSING" in res.stdout


def test_doctor_ready_after_index(tmp_path):
    dst = str(tmp_path / "r")
    shutil.copytree("trials/fixtures/taxonomy_stripe", dst)
    env = _env(tmp_path)
    _run(["index", dst, "--write-graph"], env)
    res = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "doctor"],
        capture_output=True, text=True, env=env, cwd=dst,
    )
    assert res.returncode == 0
    assert "Indexed:            YES" in res.stdout
    assert "Knowledge Base:     READY" in res.stdout


def test_doctor_monitoring_names_howl(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.setenv("TRUHOWL_INSTALLATIONS_DIR", str(tmp_path / "inst"))
    record_installation_event(
        {"action": "created", "installation": {"id": 5, "account": {"login": "acme"}}},
        {"acme/backend": {"state": REPO_READY}})
    res = _run(["doctor"], env)
    assert res.returncode == 0
    assert "Howl hunting" in res.stdout


def test_app_serve_requires_secret(tmp_path):
    env = _env(tmp_path)
    env.pop("TRUHOWL_WEBHOOK_SECRET", None)
    res = subprocess.run(
        [sys.executable, "-c",
         "from truhowl.cli.main import cmd_app; "
         "import argparse; cmd_app(argparse.Namespace(app_action='serve', port=18099, secret=None, no_secret=False))"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 2
    assert "without a secret" in res.stdout
