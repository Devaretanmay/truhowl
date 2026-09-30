# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""PR surface parity: summary, badges, diagrams, footer, assembly order."""

from unittest.mock import MagicMock

from truhowl.config import PipelinePolicy
from truhowl.github.pr_render import (
    render_flow_diagram,
    render_pr_footer,
    render_pr_summary,
    severity_of,
)
from truhowl.pipeline import AnalysisResult, DriftFinding, TriggerContext, surface_result


def _ctx(**kwargs):
    base = dict(event_id="e", event_type="pull_request.opened", repository="a/b",
                ref="x", sha="abc123", workdir="/tmp", pr_number=1)
    base.update(kwargs)
    return TriggerContext(**base)


def _finding(**kwargs):
    base = dict(provider_name="stripe", display_name="Stripe", package_name="stripe",
                current_version="11.18.0", target_version="13.0.0",
                breaking_change="del removed", migration_guide_url="",
                callsites_in_context=[{"file_path": "src/b.ts", "line_number": 5}],
                affected_files=["src/b.ts"], is_auto_repairable=True)
    base.update(kwargs)
    return DriftFinding(**base)


def _analysis(**kwargs):
    base = dict(context=_ctx(), findings=[_finding()], callsites_total=1)
    base.update(kwargs)
    return AnalysisResult(**base)


def test_summary_clean_names_touchpoints():
    body = render_pr_summary(_analysis(findings=[]), _ctx())
    assert "no contract impact" in body
    assert "Confidence: high" in body


def test_summary_findings_lists_transitions():
    body = render_pr_summary(_analysis(), _ctx())
    assert "1 maintenance issue(s)" in body
    assert "11.18.0 -> 13.0.0" in body
    assert "Confidence: pending verification" in body


def test_summary_verified_is_high():
    analysis = _analysis()
    analysis.verified = True
    analysis.modified_files = ["src/b.ts"]
    assert "Confidence: high" in render_pr_summary(analysis, _ctx())


def test_severity_badges():
    assert severity_of(_finding(is_auto_repairable=False)) == "P0"
    assert severity_of(_finding(is_auto_repairable=True)) == "P1"


def test_flow_diagram_fenced_and_named():
    body = render_flow_diagram(_analysis())
    assert body.startswith("```mermaid")
    assert body.endswith("```")
    assert "src/b.ts" in body
    assert render_flow_diagram(_analysis(findings=[])) == ""


def test_footer_names_commit_and_rerun():
    body = render_pr_footer(_ctx())
    assert "abc123" in body
    assert "@truhowl" in body


def test_surface_assembly_order():
    result = surface_result(_ctx(), _analysis(), PipelinePolicy(), MagicMock())
    body = result.comment_body
    assert body.index("## Truhowl review") < body.index("TRUHOWL FOUND A MAINTENANCE ISSUE")
    assert body.index("```mermaid") > body.index("TRUHOWL FOUND A MAINTENANCE ISSUE")
    assert body.rstrip().endswith("Comment `@truhowl` to re-run this review.")
