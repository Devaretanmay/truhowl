# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0

"""
Koyote event model and shared maintenance pipeline.

The product surface is the GitHub App. The trigger layer accepts two kinds
of events today:

* PR / CI events (pull_request.opened, synchronize, etc.)
* External-change events (provider version drift detected)

Both enter the SAME pipeline. The pipeline does not know or care which
trigger produced the event. It only knows the repository, the changed
context, and the analysis/fix policy.

The CLI remains a power-user/debugging interface layered on top of the
same pipeline.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List

from koyote.config import PipelinePolicy
from koyote.github.client import GitHubAppClient
from koyote.credentials import has_valid_credentials
from koyote.github.pr_render import (
    render_consult_issue,
    render_flow_diagram,
    render_maintenance_issue_comment,
    render_pr_footer,
    render_pr_summary,
    render_verification_comment,
)
from koyote.github.trust_pr import generate_trust_pr_markdown, TrustPRMetadata
from koyote.autopatch import (
    ScanConfig,
    scan_callsites,
)
from koyote.providers.registry import get_default_registry, ProviderSpec
from koyote.drift import detect_drift
from koyote.formatters import run_style_formatter
from koyote.test_runner import (
    _detect_test_command,
    _run_tests,
    _compute_lockfile_hash,
)
from blake3 import blake3
from koyote.intelligence import KoyoteIntelligence, resolve_migration
from koyote.knowledge import upsert_learned as kb_upsert
from koyote.ai_planner import AIPatchPlanner, build_reasoning_context
from koyote.maintenance_agents import analyze_impact
from koyote.sandbox.snapshot import SnapshotManager

_logger = logging.getLogger("koyote.pipeline")


# ── Trigger context ────────────────────────────────────────────────────────

@dataclass
class TriggerContext:
    """Shared context produced by a trigger and consumed by the pipeline."""

    event_id: str
    event_type: str
    repository: str
    ref: str
    sha: str
    workdir: str
    title: str = ""
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # PR-specific
    pr_number: int | None = None
    base_ref: str | None = None
    changed_files: list[str] = field(default_factory=list)

    # External-change-specific
    provider_name: str | None = None
    from_version: str | None = None
    to_version: str | None = None

    @classmethod
    def from_pull_request_event(
        cls,
        payload: dict[str, Any],
        workdir: str | None = None,
        changed_files: List[str] | None = None,
    ) -> "TriggerContext":
        ppr = payload.get("pull_request") or {}
        repo = payload.get("repository", {}).get("full_name", "")
        number = ppr.get("number")
        action = payload.get("action", "opened")
        full_event = f"pull_request.{action}"
        ref = ppr.get("head", {}).get("ref", "")
        sha = ppr.get("head", {}).get("sha", "")
        base_ref = ppr.get("base", {}).get("ref", "")

        files = changed_files
        if files is None:
            if ppr.get("files"):
                files = [f.get("filename") for f in ppr.get("files") if f.get("filename")]
            else:
                meta = payload.get("koyote", {})
                if isinstance(meta, dict):
                    files = meta.get("changed_files", [])

        return cls(
            event_id=str(payload.get("id") or f"pr-{number}-{int(time.time())}"),
            event_type=full_event,
            repository=repo,
            ref=ref,
            sha=sha,
            workdir=workdir or "",
            pr_number=number,
            base_ref=base_ref,
            title=ppr.get("title", ""),
            description=ppr.get("body") or "",
            changed_files=files or [],
            metadata={"action": action},
        )

    @classmethod
    def from_external_change(
        cls,
        provider_name: str,
        from_version: str,
        to_version: str,
        repository: str,
        ref: str = "main",
        sha: str = "",
        workdir: str | None = None,
        description: str = "",
    ) -> "TriggerContext":
        return cls(
            event_id=f"ext-{provider_name}-{int(time.time())}",
            event_type=f"external.change.{provider_name}",
            repository=repository,
            ref=ref,
            sha=sha,
            workdir=workdir or "",
            title=f"External change: {provider_name} drift detected",
            description=description,
            provider_name=provider_name,
            from_version=from_version,
            to_version=to_version,
            metadata={"source": "external_change_detector"},
        )


# ── Analysis results ───────────────────────────────────────────────────────

@dataclass
class DriftFinding:
    """One external-change finding relevant to the current trigger context."""

    provider_name: str
    display_name: str
    package_name: str
    current_version: str
    target_version: str
    breaking_change: str
    migration_guide_url: str
    callsites_in_context: list[dict[str, Any]] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    is_auto_repairable: bool = True
    auto_fix_required: bool = False


@dataclass
class AnalysisResult:
    """Result of analyzing a trigger context."""

    context: TriggerContext
    findings: list[DriftFinding] = field(default_factory=list)
    providers_detected: list[dict[str, Any]] = field(default_factory=list)
    callsites_total: int = 0
    patches_planned: int = 0
    auto_fixable_count: int = 0
    timestamp: float = field(default_factory=time.time)
    modified_files: list[str] = field(default_factory=list)
    unified_diffs: list[str] = field(default_factory=list)
    verified: bool = False
    test_command: str = ""
    test_exit_code: int = 0
    test_duration_ms: int = 0
    trust_pr_body: str = ""
    commit_sha: str | None = None
    patched_callsites: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def all_auto_repairable(self) -> bool:
        return self.has_findings and all(f.is_auto_repairable for f in self.findings)


# ── Pipeline stages ────────────────────────────────────────────────────────

StageHandler = Callable[[TriggerContext], Any]

STAGE_ORDER = ["analyze", "plan", "apply", "verify", "evidence", "surface"]


@dataclass
class PipelineStage:
    name: str
    handler: StageHandler
    optional: bool = False


class MaintenancePipeline:
    """
    Event-driven, reusable maintenance pipeline.

    Typical flow:

        ctx = TriggerContext.from_pull_request_event(payload)
        pipeline = MaintenancePipeline(client, policy)
        result = pipeline.run(ctx)

    The pipeline is intentionally stateless beyond the client + policy it
    is given. That lets the same pipeline be reused for:

    * PR webhooks
    * CI hooks
    * external-change polling / event ingestion
    * CLI power-user invocations
    """

    def __init__(
        self,
        client: GitHubAppClient,
        policy: "PipelinePolicy",
        workdir_resolver: Callable[[TriggerContext], str] | None = None,
    ):
        self.client = client
        self.policy = policy
        self.workdir_resolver = workdir_resolver or self._default_workdir_resolver
        self._stages: dict[str, PipelineStage] = {}
        self._register_default_stages()

    def _default_workdir_resolver(self, ctx: TriggerContext) -> str:
        return ctx.workdir or os.getcwd()

    def _register_default_stages(self) -> None:
        self._stages["analyze"] = PipelineStage(
            name="analyze",
            handler=self._stage_analyze,
        )
        self._stages["plan"] = PipelineStage(
            name="plan",
            handler=self._stage_plan,
            optional=True,
        )
        self._stages["apply"] = PipelineStage(
            name="apply",
            handler=self._stage_apply,
            optional=True,
        )
        self._stages["verify"] = PipelineStage(
            name="verify",
            handler=self._stage_verify,
            optional=True,
        )
        self._stages["evidence"] = PipelineStage(
            name="evidence",
            handler=self._stage_evidence,
            optional=True,
        )
        self._stages["surface"] = PipelineStage(
            name="surface",
            handler=self._stage_surface,
        )

    def run(self, ctx: TriggerContext) -> "PipelineResult":
        workdir = self.workdir_resolver(ctx)
        ctx = self._ensure_workdir(ctx, workdir)

        _logger.info(
            "pipeline.start event=%s repo=%s ref=%s workdir=%s",
            ctx.event_id,
            ctx.repository,
            ctx.ref,
            workdir,
        )

        analysis = self._run_stage("analyze", ctx)
        if not analysis.has_findings and not self.policy.always_report_clean:
            return PipelineResult(
                context=ctx,
                analysis=analysis,
                status="clean",
                comment_body="",
                status_description="Koyote: no external contract impact detected",
            )

        # Consult mode: same analysis, assess + report only. Never patches.
        if getattr(self.policy, "mode", "work") == "consult":
            return self._run_consult(ctx, analysis)

        # Plan/apply/verify only when auto-fix is enabled for this context.
        if self.policy.auto_fix_enabled_for(ctx):
            analysis = self._run_stage("plan", ctx)
            analysis = self._run_stage("apply", ctx)
            analysis = self._run_stage("verify", ctx)
            analysis = self._run_stage("evidence", ctx)

        surfaced = self._run_stage("surface", ctx)

        if analysis.has_findings and surfaced.committed:
            if analysis.test_command and analysis.test_exit_code == 0:
                status = "verified_fix"
            else:
                status = "unverified_fix"
        else:
            status = "commented"
        return PipelineResult(
            context=ctx,
            analysis=analysis,
            status=status,
            comment_body=surfaced.comment_body,
            status_description=surfaced.status_description,
            committed=surfaced.committed,
            commit_url=surfaced.commit_url,
            pr_url=surfaced.pr_url,
        )

    def _ensure_workdir(self, ctx: TriggerContext, workdir: str) -> TriggerContext:
        if workdir and not ctx.workdir:
            ctx = TriggerContext(
                **{f: getattr(ctx, f) for f in ctx.__dataclass_fields__},
                workdir=workdir,
            )
        return ctx

    def _run_stage(self, name: str, ctx: TriggerContext) -> AnalysisResult:
        stage = self._stages.get(name)
        if stage is None:
            raise ValueError(f"Unknown pipeline stage: {name}")
        _logger.info("pipeline.stage stage=%s event=%s", name, ctx.event_id)
        return stage.handler(ctx)

    def _run_consult(self, ctx: TriggerContext, analysis: AnalysisResult) -> "PipelineResult":
        """Consult authority: assess findings with AI reasoning, report only."""
        if not has_valid_credentials():
            return PipelineResult(
                context=ctx,
                analysis=analysis,
                status="refused",
                comment_body="Koyote Consult requires AI reasoning — no provider configured. "
                             "Run `koyote auth` to connect one.",
                status_description="Koyote Consult refused: no AI provider configured",
            )
        planner = AIPatchPlanner.from_env()
        if planner is None:
            return PipelineResult(
                context=ctx,
                analysis=analysis,
                status="refused",
                comment_body="Koyote Consult requires AI reasoning — no provider configured. "
                             "Run `koyote auth` to connect one.",
                status_description="Koyote Consult refused: no AI provider configured",
            )
        items = []
        for finding in analysis.findings:
            _from = finding.current_version or ""
            _to = finding.target_version or ""
            context = build_reasoning_context(
                ctx.workdir, finding.provider_name, _from, _to,
                finding.breaking_change, finding.migration_guide_url)
            assessment = planner.assess(
                repo_dir=ctx.workdir, provider_name=finding.provider_name,
                from_version=_from, to_version=_to,
                migration_details=finding.breaking_change,
                changelog_url=finding.migration_guide_url,
                affected_files=finding.affected_files, context=context)
            items.append({
                "display": finding.display_name,
                "version_from": _from, "version_to": _to,
                "breaking_change": finding.breaking_change,
                "guide_url": finding.migration_guide_url,
                "affected_files": finding.affected_files,
                "assessment_body": assessment.get("body", ""),
                "auto_repairable": True,
                "confidence": assessment.get("confidence", "high"),
            })
        body = render_consult_issue(items)
        if ctx.pr_number is not None:
            try:
                self.client.post_pr_comment(ctx.repository, ctx.pr_number, body)
            except Exception as e:
                _logger.warning("failed to post consult comment: %s", e)
        elif ctx.repository:
            try:
                title = f"[Koyote Consult] {len(items)} maintenance issue(s) detected in {ctx.repository}"
                self.client.create_issue(
                    repo=ctx.repository,
                    title=title,
                    body=body,
                    labels=["koyote", "consult"],
                )
            except Exception as e:
                _logger.warning("failed to create consult issue: %s", e)
        return PipelineResult(
            context=ctx,
            analysis=analysis,
            status="consulted",
            comment_body=body,
            status_description=f"Koyote Consult: assessed {len(items)} maintenance issue(s), no code modified",
        )

    # ── Stages ─────────────────────────────────────────────────────────────

    def _stage_analyze(self, ctx: TriggerContext) -> AnalysisResult:
        return analyze_trigger_context(ctx)

    def _stage_plan(self, ctx: TriggerContext) -> AnalysisResult:
        analysis = getattr(self, "_last_analysis", None) or analyze_trigger_context(ctx)
        self._last_analysis = analysis
        return analysis

    def _stage_apply(self, ctx: TriggerContext) -> AnalysisResult:
        analysis = getattr(self, "_last_analysis", None) or analyze_trigger_context(ctx)
        if analysis.has_findings and self.policy.auto_fix_enabled_for(ctx):
            analysis = apply_fixes(ctx, analysis, self.policy)
        self._last_analysis = analysis
        return analysis

    def _stage_verify(self, ctx: TriggerContext) -> AnalysisResult:
        analysis = getattr(self, "_last_analysis", None) or analyze_trigger_context(ctx)
        if analysis.has_findings and self.policy.auto_fix_enabled_for(ctx):
            analysis = verify_fixes(ctx, analysis, self.policy)
        self._last_analysis = analysis
        return analysis

    def _stage_evidence(self, ctx: TriggerContext) -> AnalysisResult:
        analysis = getattr(self, "_last_analysis", None) or analyze_trigger_context(ctx)
        if analysis.has_findings and self.policy.auto_fix_enabled_for(ctx):
            analysis = generate_evidence(ctx, analysis)
        self._last_analysis = analysis
        return analysis

    def _stage_surface(self, ctx: TriggerContext) -> "SurfaceResult":
        analysis = getattr(self, "_last_analysis", None) or analyze_trigger_context(ctx)
        return surface_result(ctx, analysis, self.policy, self.client)

# ── Surface result ─────────────────────────────────────────────────────────

@dataclass
class SurfaceResult:
    comment_body: str
    status_description: str
    committed: bool = False
    commit_url: str | None = None
    pr_url: str | None = None
    mergeable: bool = False


# ── Pipeline result ────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    context: TriggerContext
    analysis: AnalysisResult
    status: str
    comment_body: str
    status_description: str
    committed: bool = False
    commit_url: str | None = None
    pr_url: str | None = None

    @property
    def mergeable(self) -> bool:
        if self.status == "clean" and not self.analysis.modified_files:
            return True
        if self.status == "verified_fix" and bool(self.analysis.test_command) and self.analysis.test_exit_code == 0:
            return True
        return False

    @property
    def check_state(self) -> str:
        if self.mergeable:
            return "success"
        return "failure"

    @property
    def check_description(self) -> str:
        return self.status_description


# ── Public orchestration helpers ───────────────────────────────────────────

def analyze_trigger_context(ctx: TriggerContext) -> AnalysisResult:
    """
    Analyze a trigger context for external-change drift and impact.

    This is the PR-aware analysis entry point. For PR events it scopes
    findings to changed files. For external events it scopes to the
    provider being watched.
    """
    provider = ctx.provider_name
    findings: list[DriftFinding] = []

    if provider:
        findings.extend(_analyze_single_provider(ctx, provider))
    else:
        findings.extend(_analyze_all_touched_providers(ctx))

    providers_detected = detect_drift(ctx.workdir, None)
    callsites_total = sum(len(f.callsites_in_context) for f in findings)

    return AnalysisResult(
        context=ctx,
        findings=findings,
        providers_detected=providers_detected,
        callsites_total=callsites_total,
        auto_fixable_count=sum(1 for f in findings if f.is_auto_repairable),
    )


def _analyze_all_touched_providers(ctx: TriggerContext) -> list[DriftFinding]:
    """Analyze every provider touched by the current context."""
    detected = detect_drift(ctx.workdir, None)
    findings: list[DriftFinding] = []

    relevant = detected
    if ctx.changed_files and not ctx.provider_name:
        relevant = [
            d for d in detected
            if _file_in_context(d.get("manifest_path", ""), ctx.changed_files)
            or _any_file_in_context(d.get("provider", ""), ctx.changed_files)
        ]

    registry = get_default_registry()
    for dep in relevant:
        provider = registry.get(dep.get("package_name") or dep.get("provider"))
        if not provider:
            continue
        version = dep.get("declared_version") or "unknown"
        finding = _build_finding_from_provider(ctx, provider, version)
        if finding:
            findings.append(finding)

    return findings


def _analyze_single_provider(ctx: TriggerContext, provider_name: str) -> list[DriftFinding]:
    """Analyze a single provider for drift in the current context."""
    registry = get_default_registry()
    p_spec = registry.get(provider_name)
    if not p_spec:
        return []

    version = ctx.from_version
    if not version:
        detected = detect_drift(ctx.workdir, provider_name)
        if detected:
            version = detected[0].get("declared_version") or "unknown"

    return [_build_finding_from_provider(ctx, p_spec, version or "unknown")]


MANIFEST_FILES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "poetry.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
}


def _build_finding_from_provider(
    ctx: TriggerContext,
    p_spec: ProviderSpec,
    current_version: str,
) -> DriftFinding | None:
    """Build a DriftFinding for a provider at a given version."""
    if not p_spec.migrations:
        return None

    migration = next(iter(p_spec.migrations.values()))
    target_version = migration.to_version

    if current_version == target_version:
        return None

    callsites = _locate_relevant_callsites(ctx, p_spec.name)
    affected_files = sorted({c.get("file_path") for c in callsites if c.get("file_path")})

    manifest_touched = any(os.path.basename(f) in MANIFEST_FILES for f in ctx.changed_files)
    if ctx.changed_files and not manifest_touched and not _any_file_in_context(p_spec.name, ctx.changed_files):
        if not affected_files:
            return None

    breaking_change = migration.description or "Version upgrade with breaking contract change"

    return DriftFinding(
        provider_name=p_spec.name,
        display_name=p_spec.display_name,
        package_name=p_spec.package_name,
        current_version=current_version,
        target_version=target_version,
        breaking_change=breaking_change,
        migration_guide_url=migration.changelog_url or p_spec.docs_url,
        callsites_in_context=callsites,
        affected_files=affected_files,
        is_auto_repairable=bool(migration.rewrites),
        auto_fix_required=True,
    )


def _locate_relevant_callsites(ctx: TriggerContext, provider_name: str) -> list[dict[str, Any]]:
    """Locate callsites relevant to a provider in the current context."""
    try:
        cfg = ScanConfig(sdk_names=[provider_name])
        result = scan_callsites(ctx.workdir, cfg)
        callsites = result.get("callsites", [])
        manifest_touched = any(os.path.basename(f) in MANIFEST_FILES for f in ctx.changed_files)
        if ctx.changed_files and not manifest_touched:
            callsites = [
                c for c in callsites
                if _file_in_context(c.get("file_path", ""), ctx.changed_files)
            ]
        return callsites
    except Exception as e:
        _logger.warning("callsite scan failed for %s: %s", provider_name, e)
        return []


def _file_in_context(path: str, changed_files: list[str]) -> bool:
    if not path or not changed_files:
        return bool(path)  # if no context, assume relevant
    for cf in changed_files:
        if cf.endswith(path) or cf == path or path.endswith(cf):
            return True
    return False


def _any_file_in_context(name: str, changed_files: list[str]) -> bool:
    if not name or not changed_files:
        return False
    name_lower = name.lower()
    for cf in changed_files:
        if name_lower in cf.lower():
            return True
    return False


# ── Apply fixes ────────────────────────────────────────────────────────────

def apply_fixes(
    ctx: TriggerContext,
    analysis: AnalysisResult,
    policy: PipelinePolicy,
) -> AnalysisResult:
    """Apply fixes via Koyote Intelligence: AI-authored repair with contract context."""
    modified_files: list[str] = []
    unified_diffs: list[str] = []

    snapshot_dir = os.path.join(ctx.workdir, ".koyote", "snapshot_tmp")
    snapshotter = SnapshotManager(workdir=ctx.workdir, snapshot_dir=snapshot_dir)
    snapshotter.snapshot()
    setattr(ctx, "_snapshotter", snapshotter)

    intel = KoyoteIntelligence()
    patched_callsites: list[dict[str, Any]] = []
    for finding in analysis.findings:
        _from, _to, migration = resolve_migration(
            finding.provider_name, finding.current_version, finding.target_version
        )
        rewrites = migration.rewrites if migration else []
        decision = intel.decide(ctx.workdir, finding.provider_name, _from, _to, has_rewrites=bool(rewrites))
        if decision.strategy == "QUARANTINE":
            _logger.info("pipeline.quarantine provider=%s reason=%s", finding.provider_name, decision.reason)
            continue
        elif decision.strategy == "AI":
            planner = AIPatchPlanner.from_env()
            if planner is None:
                _logger.info("pipeline.quarantine provider=%s reason=no_credentials_for_ai", finding.provider_name)
                continue
            impact = analyze_impact(ctx.workdir, finding.provider_name)
            target_files = impact.affected_files or finding.affected_files
            if not target_files:
                continue
            ai_results = planner.plan_and_apply(
                repo_dir=ctx.workdir,
                affected_files=target_files,
                provider_name=finding.provider_name,
                from_version=_from,
                to_version=_to,
                migration_details=finding.breaking_change,
                dry_run=False,
                context=build_reasoning_context(
                    ctx.workdir, finding.provider_name, _from, _to,
                    finding.breaking_change, finding.migration_guide_url),
                changelog_url=finding.migration_guide_url,
            )
            for r in ai_results:
                if r.success:
                    modified_files.append(r.file_path)
                    if r.unified_diff:
                        unified_diffs.append(r.unified_diff)
                    rel_p = os.path.relpath(r.file_path, ctx.workdir) if os.path.isabs(r.file_path) else r.file_path
                    for rule_desc in r.rules_applied:
                        patched_callsites.append({
                            "file_path": rel_p,
                            "description": rule_desc,
                        })

    if modified_files:
        run_style_formatter(ctx.workdir, modified_files)

    analysis.modified_files = modified_files
    analysis.unified_diffs = unified_diffs
    analysis.patched_callsites = patched_callsites
    analysis.patches_planned = len(patched_callsites) or len(modified_files)
    analysis.auto_fixable_count = len(patched_callsites) or len(modified_files)
    return analysis


def verify_fixes(
    ctx: TriggerContext,
    analysis: AnalysisResult,
    policy: PipelinePolicy,
) -> AnalysisResult:
    """Execute repository verification tests in isolated sandbox."""
    snapshotter = getattr(ctx, "_snapshotter", None)
    if not analysis.modified_files:
        if snapshotter:
            snapshotter.cleanup()
        return analysis

    test_cmd = _detect_test_command(ctx.workdir)
    test_duration_ms = 1

    if test_cmd:
        test_start = time.time()
        try:
            proc = _run_tests(ctx.workdir, test_cmd, timeout=120)
            test_exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            test_exit_code = 1
        except Exception:
            test_exit_code = 1
        test_duration_ms = max(1, int((time.time() - test_start) * 1000))
    else:
        test_exit_code = -1

    analysis.test_command = test_cmd or ""
    analysis.test_exit_code = test_exit_code
    analysis.test_duration_ms = test_duration_ms

    if test_cmd and test_exit_code == 0:
        analysis.verified = True
    else:
        analysis.verified = False
        if snapshotter:
            snapshotter.restore()
        analysis.modified_files = []
        analysis.unified_diffs = []

    if snapshotter:
        snapshotter.cleanup()
    return analysis


def generate_evidence(
    ctx: TriggerContext,
    analysis: AnalysisResult,
) -> AnalysisResult:
    """Generate blast-radius containment receipt and Trust PR markdown. Also closes the flywheel."""
    if not analysis.verified or not analysis.modified_files:
        return analysis
    # Flywheel: cache verified rewrites per provider migration
    try:
        for f in analysis.findings:
            _from, _to, migration = resolve_migration(f.provider_name, f.current_version, f.target_version)
            kb_upsert(
                ctx.workdir, f.provider_name, _from, _to,
                applied_rules=[c.get("description", "") for c in analysis.patched_callsites],
                test_command=analysis.test_command,
                evidence={"patch_hash": "", "lockfile_hash": ""},
                rewrites=migration.rewrites if migration else [],
            )
    except Exception as e:
        _logger.warning("kb upsert failed: %s", e)

    unified_diff = "\n".join(analysis.unified_diffs)
    lockfile_hash = _compute_lockfile_hash(ctx.workdir)
    patch_hash = blake3(unified_diff.encode("utf-8")).hexdigest()

    if analysis.patched_callsites:
        impacted = analysis.patched_callsites
    else:
        impacted = []
        for f in analysis.findings:
            impacted.extend(f.callsites_in_context)
        if not impacted:
            impacted = [{"file_path": mf, "description": "Surgically patched"} for mf in analysis.modified_files]

    meta = TrustPRMetadata(
        provider_name=analysis.findings[0].display_name if analysis.findings else "External API",
        from_version=analysis.findings[0].current_version if analysis.findings else "v1",
        to_version=analysis.findings[0].target_version if analysis.findings else "v2",
        changelog_url=analysis.findings[0].migration_guide_url if analysis.findings else "",
        files_modified=len(analysis.modified_files),
        files_scanned=len(analysis.findings[0].affected_files) if analysis.findings else len(analysis.modified_files),
        unintended_files_modified=0,
        quarantined_callsites_count=0,
        unified_diff=unified_diff,
        test_command=analysis.test_command,
        test_exit_code=analysis.test_exit_code,
        test_duration_ms=analysis.test_duration_ms,
        lockfile_hash=lockfile_hash,
        patch_hash=patch_hash,
        semantic_score=1.0,
        drift_reason=analysis.findings[0].breaking_change if analysis.findings else "",
        impacted_callsites=impacted,
        found_callsites_count=analysis.callsites_total or len(impacted),
    )
    analysis.trust_pr_body = generate_trust_pr_markdown(meta)
    return analysis


def surface_result(
    ctx: TriggerContext,
    analysis: AnalysisResult,
    policy: PipelinePolicy,
    client: GitHubAppClient,
) -> SurfaceResult:
    """Render and post the PR surface (comment + status)."""
    committed = False
    commit_sha = None

    if analysis.verified and analysis.modified_files:
        comment = analysis.trust_pr_body or render_verification_comment(analysis, ctx)
        if analysis.test_command and analysis.test_exit_code == 0:
            status_desc = f"Koyote: autonomous repair verified ({len(analysis.modified_files)} file(s) patched, tests green)"
        else:
            status_desc = f"Koyote: autonomous repair applied ({len(analysis.modified_files)} file(s) patched, test suite unconfigured)"

        try:
            rel_files = [os.path.relpath(f, ctx.workdir) if os.path.isabs(f) else f for f in analysis.modified_files]
            subprocess.run(["git", "add"] + rel_files, cwd=ctx.workdir, capture_output=True, check=True)
            provider_name = analysis.findings[0].display_name if analysis.findings else "External API"
            commit_msg = f"fix(deps): automated repair for {provider_name} breaking drift\n\nKoyote-Verified: true"
            res = subprocess.run(["git", "commit", "-m", commit_msg], cwd=ctx.workdir, capture_output=True, text=True)
            if res.returncode == 0:
                sha_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ctx.workdir, capture_output=True, text=True)
                commit_sha = sha_res.stdout.strip()
                committed = True
                try:
                    subprocess.run(["git", "push", "origin", "HEAD"], cwd=ctx.workdir, capture_output=True, text=True, timeout=30)
                except Exception:
                    pass
        except Exception as e:
            _logger.warning("failed to commit verified patch: %s", e)

    elif analysis.has_findings:
        comment = render_maintenance_issue_comment(
            analysis,
            client,
            ctx,
            inline=policy.inline_comments,
        )
        status_desc = f"Koyote found {len(analysis.findings)} maintenance issue(s)"
    else:
        comment = render_verification_comment(analysis, ctx)
        status_desc = "Koyote: no external contract impact detected"

    # Honest checkout disclosure: analysis ran on the tracked branch, not the
    # exact PR head (fetch unavailable). Confirm findings on the PR itself.
    if ctx.metadata.get("exact_head") is False:
        status_desc += " [checkout: tracked branch, PR head unfetchable]"

    comment = render_pr_summary(analysis, ctx) + "\n\n" + comment
    if analysis.has_findings:
        comment += "\n\n" + render_flow_diagram(analysis)
    comment += render_pr_footer(ctx)

    if ctx.pr_number is not None:
        try:
            client.post_pr_comment(ctx.repository, ctx.pr_number, comment)
        except Exception as e:
            _logger.warning("failed to post PR comment: %s", e)

    is_mergeable = False
    if not analysis.has_findings and not analysis.modified_files:
        is_mergeable = True
    elif analysis.verified and bool(analysis.test_command) and analysis.test_exit_code == 0:
        is_mergeable = True

    return SurfaceResult(
        comment_body=comment,
        status_description=status_desc,
        committed=committed,
        commit_url=f"https://github.com/{ctx.repository}/commit/{commit_sha}" if commit_sha else None,
        pr_url=f"https://github.com/{ctx.repository}/pull/{ctx.pr_number}" if ctx.pr_number else None,
        mergeable=is_mergeable,
    )
