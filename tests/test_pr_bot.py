import os
import shutil
from unittest.mock import MagicMock, patch

from truhowl.ai_planner import AIPatchPlanner
from truhowl.config import PipelinePolicy, load_config
import truhowl.github.pr_bot as pr_bot_mod
from truhowl.github.pr_bot import (
    handle_pull_request_event,
    handle_installation_event,
    handle_issue_comment_event,
    render_day0_onboarding_issue,
    make_pr_bot_handler,
    _extract_changed_files,
    _safe_preview,
)
from truhowl.llm import LLMClient, LLMResponse


def test_extract_changed_files_from_payload():
    payload_files = {
        "pull_request": {
            "files": [{"filename": "src/api.ts"}, {"filename": "package.json"}]
        }
    }
    assert _extract_changed_files(payload_files) == ["src/api.ts", "package.json"]

    payload_meta = {
        "truhowl": {"changed_files": ["lib/stripe.ts"]}
    }
    assert _extract_changed_files(payload_meta) == ["lib/stripe.ts"]

    assert _extract_changed_files({}) == []


def test_safe_preview():
    text = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8"
    preview = _safe_preview(text)
    assert len(preview.splitlines()) == 6
    assert _safe_preview("") == ""


def test_handle_pull_request_event_validation_failures():
    client = MagicMock()
    policy = PipelinePolicy()

    res_no_pr = handle_pull_request_event({}, "pull_request.opened", client, policy)
    assert res_no_pr["success"] is False
    assert "No pull_request" in res_no_pr["error"]

    res_no_repo = handle_pull_request_event(
        {"pull_request": {"number": 1}},
        "pull_request.opened",
        client,
        policy,
    )
    assert res_no_repo["success"] is False
    assert "No repository" in res_no_repo["error"]

    res_no_head = handle_pull_request_event(
        {"pull_request": {"number": 1}, "repository": {"full_name": "acme/repo"}},
        "pull_request.opened",
        client,
        policy,
    )
    assert res_no_head["success"] is False
    assert "Missing PR head info" in res_no_head["error"]


def test_make_pr_bot_handler_dispatches_events():
    client = MagicMock()
    policy = PipelinePolicy()

    handler = make_pr_bot_handler(client=client, policy=policy)

    res_unhandled = handler({}, "issues.opened")
    assert res_unhandled["success"] is True
    assert res_unhandled["handled"] is False

    with patch("truhowl.github.pr_bot.handle_pull_request_event") as mock_handle_pr:
        mock_handle_pr.return_value = {"success": True, "handled_pr": True}
        res_pr = handler({"pull_request": {}}, "pull_request.opened")
        assert res_pr["handled_pr"] is True
        mock_handle_pr.assert_called_once()

    with patch("truhowl.github.pr_bot.handle_external_change_event") as mock_handle_ext:
        mock_handle_ext.return_value = {"success": True, "handled_ext": True}
        res_ext = handler({}, "external.change.drift")
        assert res_ext["handled_ext"] is True
        mock_handle_ext.assert_called_once()

    with patch("truhowl.github.pr_bot.handle_installation_event") as mock_handle_inst:
        mock_handle_inst.return_value = {"success": True, "handled_inst": True}
        res_inst = handler({"repositories": []}, "installation.created")
        assert res_inst["handled_inst"] is True
        mock_handle_inst.assert_called_once()


def test_handle_installation_event():
    client = MagicMock()
    payload = {
        "action": "created",
        "repositories": [{"full_name": "acme/backend"}, {"full_name": "acme/frontend"}],
    }
    res = handle_installation_event(payload, "installation.created", client)
    assert res["success"] is True
    assert res["repositories_onboarded"] == ["acme/backend", "acme/frontend"]
    assert client.create_issue.call_count == 2


def test_render_day0_onboarding_issue():
    content = render_day0_onboarding_issue("acme/backend")
    assert "TRUHOWL DAY-0 REPOSITORY ONBOARDING" in content
    assert "acme/backend" in content
    assert "Continuous Guard Status:" in content


def test_handle_pull_request_event_mergeable_flag():
    client = MagicMock()
    policy = PipelinePolicy()
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/backend"},
        "pull_request": {
            "number": 5,
            "title": "Clean PR",
            "body": "No changes",
            "head": {"ref": "feat", "sha": "123"},
            "base": {"ref": "main"},
        },
    }
    with patch("truhowl.github.pr_bot.MaintenancePipeline") as mock_pipe_cls:
        mock_pipe = mock_pipe_cls.return_value
        mock_result = MagicMock()
        mock_result.status = "clean"
        mock_result.mergeable = True
        mock_result.check_state = "success"
        mock_result.status_description = "clean"
        mock_result.comment_body = "clean comment"
        mock_pipe.run.return_value = mock_result

        res = handle_pull_request_event(payload, "pull_request.opened", client, policy)
        assert res["success"] is True
        assert res["mergeable"] is True
        assert res["check_state"] == "success"


def _comment_payload(text, sender_type="User", is_pr=True):
    issue = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x/y/pulls/7"}
    return {
        "action": "created",
        "repository": {"full_name": "acme/backend"},
        "issue": issue,
        "comment": {"body": text, "user": {"login": "dev"}},
        "sender": {"type": sender_type},
    }


def test_issue_comment_without_mention_ignored():
    client = MagicMock()
    res = handle_issue_comment_event(_comment_payload("looks good"), "issue_comment.created", client)
    assert res["handled"] is False
    client.get_pull_request.assert_not_called()


def test_issue_comment_bot_sender_ignored():
    client = MagicMock()
    res = handle_issue_comment_event(
        _comment_payload("@truhowl please", sender_type="Bot"), "issue_comment.created", client)
    assert res["handled"] is False


def test_issue_comment_rerun_delegates_to_pr_handler():
    client = MagicMock()
    client.get_pull_request.return_value = {
        "number": 7, "title": "t", "body": "b",
        "head": {"ref": "feat", "sha": "abc"},
        "base": {"ref": "main"},
    }
    with patch("truhowl.github.pr_bot.handle_pull_request_event") as mock_pr:
        mock_pr.return_value = {"success": True}
        res = handle_issue_comment_event(
            _comment_payload("@truhowl please re-run"), "issue_comment.created", client)
        assert res == {"success": True}
        passed_payload = mock_pr.call_args[0][0]
        assert passed_payload["pull_request"]["head"]["sha"] == "abc"
        assert passed_payload["action"] == "synchronize"


def test_issue_comment_explain_without_creds_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    client = MagicMock()
    client.get_pull_request.return_value = {
        "number": 7, "title": "t", "body": "b",
        "head": {"ref": "feat", "sha": "abc"},
        "base": {"ref": "main"},
    }
    client.get_pull_request_files.return_value = []
    res = handle_issue_comment_event(
        _comment_payload("@truhowl explain this"), "issue_comment.created", client,
        workdir=str(tmp_path))
    assert res["success"] is False
    assert "no provider configured" in res["error"]
    client.post_pr_comment.assert_not_called()


def test_issue_comment_explain_posts_assessment(tmp_path, monkeypatch):
    shutil.copytree("trials/fixtures/taxonomy_stripe", str(tmp_path / "repo"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-testkey1234567890")
    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(
        content="## Impact\nSomething changed.\n\nConfidence: high", model="m")
    monkeypatch.setattr(pr_bot_mod.AIPatchPlanner, "from_env",
                        classmethod(lambda cls, **k: AIPatchPlanner(client=mock_client)))
    client = MagicMock()
    client.get_pull_request.return_value = {
        "number": 7, "title": "t", "body": "b",
        "head": {"ref": "feat", "sha": "abc"},
        "base": {"ref": "main"},
    }
    client.get_pull_request_files.return_value = [
        {"filename": "package.json"}, {"filename": "src/billing.ts"}]
    res = handle_issue_comment_event(
        _comment_payload("@truhowl explain this"), "issue_comment.created", client,
        workdir=str(tmp_path / "repo"))
    assert res["success"] is True
    assert res["comment_posted"] is True
    posted = client.post_pr_comment.call_args[0][2]
    assert "No code was modified." in posted


def test_pr_skipped_on_excluded_label():
    client = MagicMock()
    policy = PipelinePolicy(exclude_labels=["dependencies"])
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 3, "title": "t", "body": "b",
            "head": {"ref": "feat", "sha": "abc"},
            "base": {"ref": "main"},
            "labels": [{"name": "dependencies"}],
        },
        "repository": {"full_name": "acme/backend"},
    }
    res = handle_pull_request_event(payload, "pull_request.opened", client, policy)
    assert res.get("skipped") is True
    assert "dependencies" in res.get("reason", "")


def test_pr_skipped_when_all_files_ignored():
    client = MagicMock()
    policy = PipelinePolicy(ignore_paths=["docs/**"])
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 3, "title": "t", "body": "b",
            "head": {"ref": "feat", "sha": "abc"},
            "base": {"ref": "main"},
            "files": [{"filename": "docs/guide.md"}],
        },
        "repository": {"full_name": "acme/backend"},
    }
    with patch("truhowl.github.pr_bot.MaintenancePipeline") as mock_pipe_cls:
        res = handle_pull_request_event(payload, "pull_request.opened", client, policy)
        assert res.get("skipped") is True
        mock_pipe_cls.return_value.run.assert_not_called()


def test_bot_config_parses_filters_and_mode(tmp_path):
    cfg_path = os.path.join(str(tmp_path), "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("bot:\n  mode: consult\n  ignore_paths: ['docs/**']\n  exclude_labels: [dependencies]\n")
    policy = load_config(cfg_path).pipeline_policy()
    assert policy.mode == "consult"
    assert policy.ignore_paths == ["docs/**"]
    assert policy.exclude_labels == ["dependencies"]
