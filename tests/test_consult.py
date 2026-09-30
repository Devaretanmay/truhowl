# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Consult vs Work: one reasoning engine, mode controls authority only."""

import os
import subprocess
import sys
from unittest.mock import MagicMock

from truhowl.ai_planner import AIPatchPlanner, build_reasoning_context
from truhowl.cli import main as cli_main
from truhowl.config import BotConfig, PipelinePolicy, load_config
from truhowl.github.pr_render import render_consult_issue
from truhowl.llm import LLMClient, LLMResponse
from truhowl.pipeline import MaintenancePipeline, TriggerContext
import truhowl.pipeline as pipe_mod


ASSESS_BODY = (
    "## Impact\nStripe v13 removes `subscriptions.del`.\n\n"
    "Affected: `src/billing.ts:5`.\n\nConfidence: high"
)


def _seed_repo(dst):
    os.makedirs(os.path.join(dst, "src"), exist_ok=True)
    with open(os.path.join(dst, "package.json"), "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0"}}')
    with open(os.path.join(dst, "src", "billing.ts"), "w") as f:
        f.write("import Stripe from 'stripe';\nconst s = new Stripe('x');\n"
                "export const cancel = (id) => s.subscriptions.del(id);\n")


def _mock_assess_client():
    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(content=ASSESS_BODY, model="m")
    return mock_client


def _cli_env(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = "python"
    env["TRUHOWL_CREDENTIALS_FILE"] = str(tmp_path / "creds.json")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        env.pop(k, None)
    return env


def test_assess_shares_context_with_repair(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    ctx = build_reasoning_context(dst, "stripe", "11.18.0", "13.0.0", "d")
    assert any("billing.ts" in c for c in ctx["callsites"])
    seen = {}
    mock_client = _mock_assess_client()

    def _capture(messages, system_prompt=None):
        seen["user"] = messages[0]["content"]
        return LLMResponse(content=ASSESS_BODY, model="m")

    mock_client.complete.side_effect = _capture
    out = AIPatchPlanner(client=mock_client).assess(
        repo_dir=dst, provider_name="stripe", from_version="11.18.0",
        to_version="13.0.0", context=ctx)
    assert out["confidence"] == "high"
    assert "billing.ts" in seen["user"]
    assert out["body"] == ASSESS_BODY


def test_assess_never_writes(tmp_path):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    before = {}
    for dirpath, _, filenames in os.walk(dst):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            before[fp] = open(fp, "rb").read()
    AIPatchPlanner(client=_mock_assess_client()).assess(
        repo_dir=dst, provider_name="stripe", from_version="1", to_version="2",
        affected_files=["src/billing.ts"])
    for fp, content in before.items():
        assert open(fp, "rb").read() == content


def test_render_consult_issue_declares_no_modification():
    body = render_consult_issue([{
        "display": "Stripe", "version_from": "11.18.0", "version_to": "13.0.0",
        "breaking_change": "del removed", "guide_url": "https://x",
        "affected_files": ["src/billing.ts"], "assessment_body": ASSESS_BODY,
        "auto_repairable": True, "confidence": "high"}])
    assert "No code was modified." in body
    assert "src/billing.ts" in body
    assert "repair this automatically" in body


def test_consult_refuses_without_credentials(tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "consult", "."],
        capture_output=True, text=True, env=_cli_env(tmp_path))
    assert res.returncode == 1
    assert "AUTHENTICATION REQUIRED" in res.stdout


def test_consult_opens_issue_without_modifying(tmp_path, monkeypatch):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "creds.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234567890")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(cli_main.AIPatchPlanner, "from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=_mock_assess_client())))
    opened = {}
    monkeypatch.setattr(cli_main.GitHubAppClient, "create_issue",
                        lambda self, repo, title, body, labels=None: opened.update(
                            repo=repo, title=title, body=body) or {"html_url": "https://x/1"})
    args = MagicMock()
    args.path = dst
    args.repo = "acme/backend"
    cli_main.cmd_consult(args)
    assert opened["repo"] == "acme/backend"
    assert "No code was modified." in opened["body"]
    assert "— Howl, Consult bot" in opened["body"]
    assert 'del(' in open(os.path.join(dst, "src", "billing.ts")).read()


def test_consult_requires_repo(tmp_path, monkeypatch):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "creds.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234567890")
    args = MagicMock()
    args.path = dst
    args.repo = None
    with open(os.devnull, "w"):
        try:
            cli_main.cmd_consult(args)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("expected SystemExit")


def test_mode_defaults_to_work_and_parses_consult(tmp_path):
    assert BotConfig().mode == "work"
    assert PipelinePolicy().mode == "work"
    cfg_path = os.path.join(str(tmp_path), "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("bot:\n  mode: consult\n")
    assert load_config(cfg_path).pipeline_policy().mode == "consult"
    with open(cfg_path, "w") as f:
        f.write("bot:\n  mode: nonsense\n")
    assert load_config(cfg_path).pipeline_policy().mode == "work"


def test_pipeline_consult_branch_reports_without_patching(tmp_path, monkeypatch):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "creds.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    policy = PipelinePolicy(mode="consult")
    ctx = TriggerContext(event_id="e", event_type="external.change.stripe",
                         repository="acme/backend", ref="main", sha="",
                         workdir=dst, provider_name="stripe",
                         from_version="11.18.0", to_version="13.0.0")
    result = MaintenancePipeline(client=MagicMock(), policy=policy).run(ctx)
    assert result.status == "refused"
    assert result.analysis.modified_files == []


def test_pipeline_consult_assesses_with_mock_planner(tmp_path, monkeypatch):
    dst = str(tmp_path / "r")
    _seed_repo(dst)
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "creds.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234567890")
    monkeypatch.setattr(pipe_mod.AIPatchPlanner, "from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=_mock_assess_client())))
    policy = PipelinePolicy(mode="consult")
    ctx = TriggerContext(event_id="e", event_type="external.change.stripe",
                         repository="acme/backend", ref="main", sha="",
                         workdir=dst, provider_name="stripe",
                         from_version="11.18.0", to_version="13.0.0")
    result = MaintenancePipeline(client=MagicMock(), policy=policy).run(ctx)
    assert result.status == "consulted"
    assert "No code was modified." in result.comment_body
    assert result.analysis.modified_files == []
    assert 'del(' in open(os.path.join(dst, "src", "billing.ts")).read()
