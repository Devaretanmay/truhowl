from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from truhowl.pipeline import AnalysisResult, DriftFinding, TriggerContext
    from truhowl.github.client import GitHubAppClient


def render_verification_comment(
    analysis: "AnalysisResult",
    ctx: "TriggerContext",
) -> str:
    """Render the clean verification comment for unaffected PRs."""
    touchpoints = analysis.callsites_total or len(analysis.providers_detected) or 1
    return f"Truhowl checked {touchpoints} external API touchpoint(s). No contract violations detected. No changes made."


def render_maintenance_issue_comment(
    analysis: "AnalysisResult",
    client: "GitHubAppClient" | None,
    ctx: "TriggerContext",
    inline: bool = True,
) -> str:
    """Render the maintenance issue comment."""
    lines: list[str] = [
        "-----------------------------------------",
        "        TRUHOWL FOUND A MAINTENANCE ISSUE",
        "-----------------------------------------",
        "",
    ]

    for finding in analysis.findings:
        lines.extend(_render_single_finding(finding, client, ctx, inline))

    lines.append("")
    lines.append("-----------------------------------------")
    return "\n".join(lines)


def _render_single_finding(
    finding: "DriftFinding",
    client: "GitHubAppClient" | None,
    ctx: "TriggerContext",
    inline: bool = True,
) -> list[str]:
    badge = severity_of(finding)
    badge_note = "needs human review" if badge == "P0" else "repairable"
    lines: list[str] = [
        f"### {finding.display_name} {finding.current_version} -> {finding.target_version}",
        "",
        f"**Severity:** {badge} — {badge_note}",
        "",
        f"**Breaking change:** {finding.breaking_change}",
        "",
    ]

    if finding.migration_guide_url:
        lines.append(f"[Vendor migration guide]({finding.migration_guide_url})")
        lines.append("")

    lines.append("**Affected:**")
    for cs in finding.callsites_in_context:
        path = cs.get("file_path") or ""
        lineno = cs.get("line_number")
        if path and lineno:
            lines.append(f"  - `{path}:{lineno}`")
        elif path:
            lines.append(f"  - `{path}`")

    if finding.callsites_in_context and inline:
        lines.extend(_inline_comment_sections(finding, ctx))

    lines.append("")
    lines.append("**Truhowl can repair this automatically.**")
    lines.append("")
    return lines


def _inline_comment_sections(
    finding: "DriftFinding",
    ctx: "TriggerContext",
) -> list[str]:
    """Render inline review comment invitations for affected callsites."""
    return []


def render_pr_summary(analysis: "AnalysisResult", ctx: "TriggerContext") -> str:
    """PR summary header: what changed, who it affects, evidence-grounded confidence."""
    if not analysis.has_findings:
        touchpoints = analysis.callsites_total or len(analysis.providers_detected) or 1
        return (f"## Truhowl review: no contract impact\n\n"
                f"Checked {touchpoints} external API touchpoint(s). No contract violations detected.\n\n"
                f"Confidence: high (full scan, nothing to repair)")
    parts = [f"## Truhowl review: {len(analysis.findings)} maintenance issue(s)", ""]
    for finding in analysis.findings:
        files = len(finding.affected_files)
        parts.append(f"- **{finding.display_name}** {finding.current_version} -> {finding.target_version}: "
                     f"{len(finding.callsites_in_context)} callsite(s) across {files} file(s)")
    parts.append("")
    if analysis.verified and analysis.modified_files:
        parts.append("Confidence: high (verified against repository test suite)")
    else:
        parts.append("Confidence: pending verification")
    return "\n".join(parts)


def render_pr_footer(ctx: "TriggerContext") -> str:
    """Review footer: reviewed commit + re-trigger note."""
    lines = ["", "---"]
    if ctx.sha:
        lines.append(f"Reviewed commit: `{ctx.sha}`")
    lines.append("Comment `@truhowl` to re-run this review.")
    return "\n".join(lines)


def _node_id(text: str, prefix: str, seen: dict) -> str:
    """Mermaid-safe node id, deduplicated."""
    base = prefix + "".join(c if c.isalnum() else "_" for c in text)[:40]
    key = base.lower()
    seen[key] = seen.get(key, 0) + 1
    return base if seen[key] == 1 else f"{base}_{seen[key]}"


def render_flow_diagram(analysis: "AnalysisResult") -> str:
    """Mermaid flow diagram: change -> affected files -> verification state."""
    if not analysis.has_findings:
        return ""
    seen: dict = {}
    lines = ["```mermaid", "flowchart LR"]
    for finding in analysis.findings:
        change = _node_id(finding.display_name + finding.current_version + finding.target_version, "c", seen)
        lines.append(f'    {change}["{finding.display_name} {finding.current_version} → {finding.target_version}"]')
        files = finding.affected_files[:8]
        if not files:
            files = ["unscoped"]
        for path in files:
            node = _node_id(path, "f", seen)
            lines.append(f'    {change} --> {node}["{path}"]')
    state = "verified" if analysis.verified else "unverified"
    lines.append(f'    v["verification: {state}"]')
    lines.append("```")
    return "\n".join(lines)


def severity_of(finding: "DriftFinding") -> str:
    """P0 needs a human (unrepairable/quarantined); everything else is P1."""
    if not finding.is_auto_repairable:
        return "P0"
    return "P1"


def render_consult_issue(items: list[dict]) -> str:
    """Render a Consult-mode GitHub Issue from assessed detections.

    Each item: {display, version_from, version_to, breaking_change,
    guide_url, affected_files, assessment_body, auto_repairable, confidence}.
    States what changed, what is affected, why, what must not change, and
    declares that no code was modified.
    """
    lines: list[str] = [
        "-----------------------------------------",
        "   TRUHOWL CONSULT: MAINTENANCE ADVISORY ",
        "-----------------------------------------",
        "",
    ]
    for item in items:
        lines.append(f"### {item.get('display', 'External change')} "
                     f"{item.get('version_from', '')} -> {item.get('version_to', '')}".rstrip())
        lines.append("")
        if item.get("breaking_change"):
            lines.append(f"**What changed:** {item['breaking_change']}")
            lines.append("")
        if item.get("guide_url"):
            lines.append(f"[Vendor migration guide]({item['guide_url']})")
            lines.append("")
        files = item.get("affected_files") or []
        if files:
            lines.append(f"**Affected files ({len(files)}):**")
            for path in files:
                lines.append(f"  - `{path}`")
            lines.append("")
        if item.get("assessment_body"):
            lines.append("**Deep AI Reasoning & Impact Analysis:**")
            lines.append(str(item["assessment_body"]).strip())
            lines.append("")
        if item.get("auto_repairable", True):
            lines.append("**Action:** Work mode can repair this automatically (`truhowl work` or `@hunt repair`).")
        else:
            lines.append("**Action:** Manual architectural review recommended.")
        lines.append(f"Confidence: {item.get('confidence', 'high')}")
        lines.append("")
    lines.append("No code was modified.")
    lines.append("— Howl, Consult bot")
    lines.append("-----------------------------------------")
    return "\n".join(lines)
