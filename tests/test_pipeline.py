from unittest.mock import MagicMock, patch

from truhowl.config import PipelinePolicy
from truhowl.pipeline import (
    TriggerContext,
    DriftFinding,
    AnalysisResult,
    PipelineResult,
    surface_result,
)


def test_trigger_context_from_pull_request_event():
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/repo"},
        "pull_request": {
            "number": 42,
            "title": "Upgrade deps",
            "body": "Bumping Stripe",
            "head": {"ref": "feature/upgrade", "sha": "abcdef123456"},
            "base": {"ref": "main"},
        },
    }
    ctx = TriggerContext.from_pull_request_event(payload, workdir="/tmp/fake")
    assert ctx.event_type == "pull_request.opened"
    assert ctx.repository == "acme/repo"
    assert ctx.pr_number == 42
    assert ctx.ref == "feature/upgrade"
    assert ctx.sha == "abcdef123456"
    assert ctx.base_ref == "main"
    assert ctx.workdir == "/tmp/fake"


def test_trigger_context_from_external_change():
    ctx = TriggerContext.from_external_change(
        provider_name="stripe",
        from_version="11.0.0",
        to_version="13.0.0",
        repository="acme/repo",
        ref="main",
        sha="11223344",
        workdir="/tmp/fake",
    )
    assert ctx.event_type == "external.change.stripe"
    assert ctx.provider_name == "stripe"
    assert ctx.from_version == "11.0.0"
    assert ctx.to_version == "13.0.0"
    assert ctx.repository == "acme/repo"


def test_pipeline_policy_auto_fix_rules():
    policy_default = PipelinePolicy()
    ctx_pr = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        pr_number=1,
    )
    assert policy_default.auto_fix_enabled_for(ctx_pr) is False

    policy_pr_fix = PipelinePolicy(pr_auto_fix=True)
    assert policy_pr_fix.auto_fix_enabled_for(ctx_pr) is True

    policy_scoped = PipelinePolicy(pr_auto_fix=True, auto_fix_providers=["stripe"])
    ctx_stripe = TriggerContext(
        event_id="e2",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        provider_name="stripe",
    )
    ctx_other = TriggerContext(
        event_id="e3",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        provider_name="openai",
    )
    assert policy_scoped.auto_fix_enabled_for(ctx_stripe) is True
    assert policy_scoped.auto_fix_enabled_for(ctx_other) is False


def test_surface_result_clean_when_no_findings():
    ctx = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        pr_number=10,
    )
    analysis = AnalysisResult(context=ctx, findings=[])
    client = MagicMock()
    policy = PipelinePolicy(always_report_clean=True)

    surface = surface_result(ctx, analysis, policy, client)
    assert surface.status_description == "Truhowl: no external contract impact detected"
    client.post_pr_comment.assert_called_once()
    assert "Truhowl checked 1 external API touchpoint(s)" in surface.comment_body
    assert "No contract violations detected. No changes made." in surface.comment_body


def test_surface_result_with_drift_findings():
    ctx = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        pr_number=10,
    )
    finding = DriftFinding(
        provider_name="stripe",
        display_name="Stripe Node SDK",
        package_name="stripe",
        current_version="11.0.0",
        target_version="13.0.0",
        breaking_change="v11 to v13 breaking changes",
        migration_guide_url="https://example.com",
        affected_files=["src/stripe.ts"],
        is_auto_repairable=True,
    )
    analysis = AnalysisResult(context=ctx, findings=[finding])
    client = MagicMock()
    policy = PipelinePolicy(inline_comments=False)

    surface = surface_result(ctx, analysis, policy, client)
    assert "Truhowl found 1 maintenance issue(s)" in surface.status_description
    client.post_pr_comment.assert_called_once()
    assert "TRUHOWL FOUND A MAINTENANCE ISSUE" in surface.comment_body


def test_surface_result_verified_autofix():
    ctx = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        pr_number=10,
    )
    analysis = AnalysisResult(
        context=ctx,
        findings=[],
        modified_files=["/tmp/src/stripe.ts"],
        verified=True,
        test_command="npm test",
        test_exit_code=0,
        trust_pr_body="## Blast Radius Containment Receipt\n[VERIFIED] Autonomous Maintenance",
    )
    client = MagicMock()
    policy = PipelinePolicy(pr_auto_fix=True)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "abc123456789"
        surface = surface_result(ctx, analysis, policy, client)

    assert "autonomous repair verified" in surface.status_description
    assert "Blast Radius Containment Receipt" in surface.comment_body
    assert surface.mergeable is True
    client.post_pr_comment.assert_called_once()


def test_surface_result_unconfigured_tests():
    ctx = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
        pr_number=10,
    )
    analysis = AnalysisResult(
        context=ctx,
        findings=[],
        modified_files=["/tmp/src/stripe.ts"],
        verified=True,
        test_command="",
        test_exit_code=0,
        trust_pr_body="## Blast Radius Containment Receipt\n[UNVERIFIED: NO AUTOMATED TEST SUITE]",
    )
    client = MagicMock()
    policy = PipelinePolicy(pr_auto_fix=True)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "abc123456789"
        surface = surface_result(ctx, analysis, policy, client)

    assert "test suite unconfigured" in surface.status_description
    assert "[UNVERIFIED: NO AUTOMATED TEST SUITE]" in surface.comment_body
    assert surface.mergeable is False
    client.post_pr_comment.assert_called_once()


def test_pipeline_result_mergeability_rules():
    ctx = TriggerContext(
        event_id="e1",
        event_type="pull_request.opened",
        repository="acme/repo",
        ref="main",
        sha="abc",
        workdir="/tmp",
    )

    clean_res = PipelineResult(
        context=ctx,
        analysis=AnalysisResult(context=ctx, findings=[]),
        status="clean",
        comment_body="clean",
        status_description="no impact",
    )
    assert clean_res.mergeable is True
    assert clean_res.check_state == "success"

    verified_res = PipelineResult(
        context=ctx,
        analysis=AnalysisResult(
            context=ctx,
            findings=[],
            test_command="pytest",
            test_exit_code=0,
        ),
        status="verified_fix",
        comment_body="verified",
        status_description="verified fix",
    )
    assert verified_res.mergeable is True
    assert verified_res.check_state == "success"

    unverified_res = PipelineResult(
        context=ctx,
        analysis=AnalysisResult(
            context=ctx,
            findings=[],
            test_command="",
            test_exit_code=0,
        ),
        status="unverified_fix",
        comment_body="unverified",
        status_description="no tests",
    )
    assert unverified_res.mergeable is False
    assert unverified_res.check_state == "failure"

    failed_res = PipelineResult(
        context=ctx,
        analysis=AnalysisResult(
            context=ctx,
            findings=[],
            test_command="pytest",
            test_exit_code=1,
        ),
        status="unverified_fix",
        comment_body="failed",
        status_description="failed tests",
    )
    assert failed_res.mergeable is False
    assert failed_res.check_state == "failure"
