# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Consult Mode / Howl Bot: Explains maintenance problems without touching code.

Consult investigates contract and dependency drift across any ChangeSource,
conducts deep AI reasoning (what changed, what is affected, why, what should change,
and what must NOT change), and files a GitHub Issue.

Consult never touches files, never commits, and never opens pull requests.
Interactive mention `@howl explain` on a PR invokes Consult to explain findings
directly on the PR thread.
"""

from __future__ import annotations

import logging
from typing import Any

from truhowl.ai_planner import AIPatchPlanner, build_reasoning_context
from truhowl.credentials import has_valid_credentials
from truhowl.github.client import GitHubAppClient
from truhowl.github.pr_render import render_consult_issue, render_flow_diagram
from truhowl.pipeline import (
    AnalysisResult,
    PipelinePolicy,
    TriggerContext,
    analyze_trigger_context,
)

_logger = logging.getLogger("truhowl.consult")


class HowlBot:
    """The Consult / Advisor agent. Explains drift, assesses impact, and opens Issues."""

    def __init__(self, client: GitHubAppClient | None = None, policy: PipelinePolicy | None = None):
        if client is not None:
            self.client = client
            self.client.readonly = True
        else:
            self.client = GitHubAppClient(readonly=True)
        self.policy = policy or PipelinePolicy(mode="consult")
        self.policy.mode = "consult"

    def consult(self, ctx: TriggerContext, require_ai: bool = False) -> dict[str, Any]:
        """Execute Consult mode on any trigger: diagnose with AI and file a GitHub Issue."""
        if require_ai and not has_valid_credentials():
            return {
                "success": False,
                "error": "Consult requires AI reasoning: no provider configured",
            }

        analysis = analyze_trigger_context(ctx)

        if not analysis.has_findings:
            return {
                "success": True,
                "bot": "howl",
                "mode": "consult",
                "status": "clean",
                "findings_count": 0,
                "issue_created": False,
            }

        items = self._assess_findings(ctx, analysis)
        body = render_consult_issue(items)
        if analysis.has_findings:
            body += "\n\n" + render_flow_diagram(analysis)

        issue_number = None
        issue_url = None
        if ctx.repository:
            title = f"[Truhowl Consult] {len(analysis.findings)} maintenance issue(s) detected in {ctx.repository}"
            try:
                res = self.client.create_issue(
                    repo=ctx.repository,
                    title=title,
                    body=body,
                    labels=["truhowl", "consult"],
                )
                issue_number = res.get("number")
                issue_url = res.get("html_url")
            except Exception as e:
                _logger.warning("Howl failed to create consult issue: %s", e)

        return {
            "success": True,
            "bot": "howl",
            "mode": "consult",
            "status": "issue_created" if issue_number else "consulted",
            "findings_count": len(analysis.findings),
            "issue_number": issue_number,
            "issue_url": issue_url,
            "issue_body": body,
        }

    def explain_pull_request(self, ctx: TriggerContext, require_ai: bool = False) -> dict[str, Any]:
        """Interactive Consult on a PR thread: explains impact without modifying code."""
        if require_ai and not has_valid_credentials():
            return {
                "success": False,
                "error": "Consult explain needs AI reasoning: no provider configured",
            }

        analysis = analyze_trigger_context(ctx)

        if not analysis.has_findings:
            body = (
                "## Truhowl Consult: No Contract Impact Detected\n\n"
                f"Checked {analysis.callsites_total or len(analysis.providers_detected) or 1} external touchpoint(s). "
                "No breaking contract changes or API regressions detected.\n\n"
                "— Howl (Consult mode)"
            )
            if ctx.pr_number is not None and ctx.repository:
                try:
                    self.client.post_pr_comment(ctx.repository, ctx.pr_number, body)
                except Exception as e:
                    _logger.warning("Howl failed to post clean comment: %s", e)
            return {
                "success": True,
                "bot": "howl",
                "mode": "consult",
                "status": "clean",
                "findings_count": 0,
                "comment_body": body,
                "comment_posted": True,
            }

        items = self._assess_findings(ctx, analysis)
        body = render_consult_issue(items)
        if analysis.has_findings:
            body += "\n\n" + render_flow_diagram(analysis)
        body += f"\n\nReviewed commit: `{ctx.sha}`\nTo repair autonomously, comment `@hunt repair`."

        if ctx.pr_number is not None and ctx.repository:
            try:
                self.client.post_pr_comment(ctx.repository, ctx.pr_number, body)
            except Exception as e:
                _logger.warning("Howl failed to post advisory comment: %s", e)

        return {
            "success": True,
            "bot": "howl",
            "mode": "consult",
            "status": "explained",
            "findings_count": len(analysis.findings),
            "comment_body": body,
            "comment_posted": True,
        }

    def review_pull_request(self, ctx: TriggerContext, require_ai: bool = False) -> dict[str, Any]:
        """Backward-compatible alias for explain_pull_request."""
        return self.explain_pull_request(ctx, require_ai=require_ai)

    def _assess_findings(self, ctx: TriggerContext, analysis: AnalysisResult) -> list[dict[str, Any]]:
        planner = AIPatchPlanner.from_env() if has_valid_credentials() else None
        items = []
        for finding in analysis.findings:
            _from = finding.current_version or ""
            _to = finding.target_version or ""
            assessment_text = ""
            confidence = "high"
            if planner:
                reason_ctx = build_reasoning_context(
                    ctx.workdir, finding.provider_name, _from, _to,
                    finding.breaking_change, finding.migration_guide_url
                )
                assessment = planner.assess(
                    repo_dir=ctx.workdir,
                    provider_name=finding.provider_name,
                    from_version=_from,
                    to_version=_to,
                    migration_details=finding.breaking_change,
                    changelog_url=finding.migration_guide_url,
                    affected_files=finding.affected_files,
                    context=reason_ctx,
                )
                assessment_text = assessment.get("body", "")
                confidence = assessment.get("confidence", confidence)

            items.append({
                "display": finding.display_name,
                "version_from": _from,
                "version_to": _to,
                "breaking_change": finding.breaking_change,
                "guide_url": finding.migration_guide_url,
                "affected_files": finding.affected_files,
                "assessment_body": assessment_text,
                "auto_repairable": True,
                "confidence": confidence,
            })
        return items


ConsultBot = HowlBot
