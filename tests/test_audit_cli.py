# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import subprocess
import json


def _run_truhowl_cli(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = "python"
    env.setdefault("TRUHOWL_LLM_KEY", "sk-ant-test-credential-key")
    return subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main"] + args,
        capture_output=True,
        text=True,
        env=env
    )


def test_cli_audit_default():
    result = _run_truhowl_cli(["audit", "trials/fixtures/taxonomy_stripe/"])
    assert result.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in result.stdout
    assert "Stripe" in result.stdout


def test_cli_audit_github_issue():
    result = _run_truhowl_cli(["audit", "trials/fixtures/taxonomy_stripe/", "--format=github-issue"])
    assert result.returncode == 0
    assert "# Truhowl: External Dependency Map & Risk Register" in result.stdout
    assert "| **Stripe** |" in result.stdout


def test_cli_audit_json():
    result = _run_truhowl_cli(["audit", "trials/fixtures/taxonomy_stripe/", "--format=json"])
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "total_providers_detected" in data
    assert "at_risk" in data


def test_cli_graph():
    result = _run_truhowl_cli(["graph", "trials/fixtures/taxonomy_stripe/"])
    assert result.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY GRAPH" in result.stdout


def test_cli_check_default():
    result = _run_truhowl_cli(["check", "trials/fixtures/taxonomy_stripe/"])
    assert result.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in result.stdout
    assert "Stripe" in result.stdout


def test_cli_fix_detect():
    result = _run_truhowl_cli(["fix", "trials/fixtures/taxonomy_stripe/", "--detect"])
    assert result.returncode == 0
    assert "TRUHOWL AUTONOMOUS MAINTENANCE LOOP" in result.stdout


def test_cli_at_howl_alias():
    result = _run_truhowl_cli(["@howl", "--help"])
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "consult" in result.stdout  # alias routes to the consult parser


def test_cli_at_hunt_alias():
    result = _run_truhowl_cli(["@hunt", "trials/fixtures/taxonomy_stripe/", "--detect"])
    assert result.returncode == 0
    assert "TRUHOWL AUTONOMOUS MAINTENANCE LOOP" in result.stdout


def test_cli_top_level_help_focus():
    result = _run_truhowl_cli(["--help"])
    assert result.returncode == 0
    assert "Autonomous SDK/API Migration Worker" in result.stdout
    assert "truhowl check" in result.stdout
    assert "truhowl hunt" in result.stdout
    assert "truhowl consult" in result.stdout
    assert "--workflow" not in result.stdout
    assert "--run" not in result.stdout


def test_cli_hunt_help():
    result = _run_truhowl_cli(["hunt", "--help"])
    assert result.returncode == 0
    assert "usage: truhowl hunt" in result.stdout
    assert "--provider" in result.stdout
    assert "--finding" in result.stdout


def test_cli_hunt_detect():
    result = _run_truhowl_cli(["hunt", "trials/fixtures/taxonomy_stripe/", "--detect"])
    assert result.returncode == 0
    assert "TRUHOWL AUTONOMOUS MAINTENANCE LOOP" in result.stdout


def test_cli_migrate_help():
    result = _run_truhowl_cli(["migrate", "--help"])
    assert result.returncode == 0
    assert "usage: truhowl migrate" in result.stdout
    assert "--provider" in result.stdout
    assert "--finding" in result.stdout


def test_cli_migrate_detect():
    result = _run_truhowl_cli(["migrate", "trials/fixtures/taxonomy_stripe/", "--detect"])
    assert result.returncode == 0
    assert "TRUHOWL AUTONOMOUS MAINTENANCE LOOP" in result.stdout


def test_cli_login_help():
    result = _run_truhowl_cli(["login", "--help"])
    assert result.returncode == 0
    assert "usage: truhowl login" in result.stdout
    assert "--provider" in result.stdout
    assert "--api-key" in result.stdout


def test_cli_ask_help():
    result = _run_truhowl_cli(["ask", "--help"])
    assert result.returncode == 0
    assert "usage: truhowl ask" in result.stdout


def test_cli_ask_impact():
    result = _run_truhowl_cli(["ask", "--path", "trials/fixtures/taxonomy_stripe/",
                               "what breaks if we upgrade stripe?"])
    assert result.returncode == 0
    assert "stripe" in result.stdout.lower()
    assert "callsite" in result.stdout.lower()


def test_cli_ask_no_history():
    result = _run_truhowl_cli(["ask", "--path", "trials/fixtures/taxonomy_stripe/",
                               "why did the migration fail?"])
    assert result.returncode == 0
    assert "truhowl migrate" in result.stdout


def test_cli_top_level_help_phase2():
    result = _run_truhowl_cli(["--help"])
    assert result.returncode == 0
    assert "truhowl migrate" in result.stdout
    assert "truhowl ask" in result.stdout
    assert "truhowl login" in result.stdout




def test_audit_drops_string_only_drift(tmp_path):
    import os
    from truhowl import audit as audit_mod
    repo = str(tmp_path / "r")
    os.makedirs(os.path.join(repo, "src"))
    with open(os.path.join(repo, "src", "proxy.rs"), "w") as f:
        f.write('const URL: &str = "https://api.openai.com";\n')
    out = audit_mod.run_audit(repo_root=repo, output_format="cli")
    assert "[CRITICAL]" not in out


def test_is_code_evidence_classifier():
    from truhowl.audit import is_code_evidence
    assert is_code_evidence({"kind": "Import", "matched_pattern": "x",
                             "line_content": "import x"}) is True
    assert is_code_evidence({"kind": None, "matched_pattern": "stripe",
                             "line_content": "return stripe.charges.create({...});"}) is True
    assert is_code_evidence({"kind": None, "matched_pattern": "api.openai.com",
                             "line_content": 'const U: &str = "https://api.openai.com";'}) is False
    assert is_code_evidence({"kind": None, "matched_pattern": "Anthropic",
                             "line_content": 'println!("  For Anthropic models:");'}) is False
    assert is_code_evidence({"kind": None, "matched_pattern": "",
                             "line_content": "code"}) is False
