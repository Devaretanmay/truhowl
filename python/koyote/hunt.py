# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Koyote Hunt: autonomous AI-first repair agent.

Central architectural rule (absolute):

    AI owns semantic reasoning and semantic code changes. Deterministic
    systems may provide context, evidence, execution, and verification,
    but they must never decide or author a semantic repair.

This module therefore contains NO AST rewrite engine, NO regex-based
fixer, NO hardcoded migration rule that authors source changes, NO patch
template, and NO deterministic semantic-edit fallback. There is no
"safe deterministic fallback" and no hybrid repair mode: Hunt is AI-first
end to end.

Deterministic infrastructure used here (graph/call-site indexes,
dependency data, commit history, GitHub metadata, test runner, snapshot
isolation) provides evidence, execution, and verification only. The model
decides what changes and authors every semantic edit.

Lifecycle implemented here::

    Finding -> Context -> AI reasoning -> Proposed intent -> Files selected
    -> Patch (AI-authored) -> Sandbox execution -> Verification evidence
    -> AI interpretation -> Final decision -> PR decision

Every stage fails closed: when correctness cannot be established, Hunt
refuses and no PR is created.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms only
    fcntl = None  # type: ignore

from koyote.change_source import IMPACT_AI, IMPACT_QUARANTINE
from koyote.credentials import has_valid_credentials
from koyote.drift import detect_changes
from koyote.git_ops import gh_create_pr, git_commit_and_push
from koyote.github.trust_pr import TrustPRMetadata, generate_trust_pr_markdown
from koyote.hunt_ports import (
    AIAuthoredPatch,
    HuntPorts,
    PRPublisher,
    RepairPlan,
    VerifiedRepair,
    evaluate_scope,
    is_sealed,
    seal_ai_patch,
    seal_verified_repair,
)
from koyote.knowledge import lookup as kb_lookup
from koyote.knowledge import record_failure
from koyote.redact import redact_record
from koyote.llm import LLMClient, resolve_llm_config
from koyote.sandbox.snapshot import SnapshotManager
from koyote.test_runner import (
    _compute_lockfile_hash,
    _detect_test_command,
    _run_install,
    _run_tests,
)
from koyote.test_feedback import extract_structured_test_feedback

try:
    from koyote.ai_planner import AIPatchPlanner, build_reasoning_context
except Exception:  # pragma: no cover - planner unavailable without LLM deps
    AIPatchPlanner = None  # type: ignore
    build_reasoning_context = None  # type: ignore

try:
    from koyote.autopatch import ScanConfig, scan_callsites
except Exception:  # pragma: no cover
    ScanConfig = None  # type: ignore
    scan_callsites = None  # type: ignore

try:
    from koyote.graph import build_dependency_graph
except Exception:  # pragma: no cover
    build_dependency_graph = None  # type: ignore

try:
    from koyote import work_graph
except Exception:  # pragma: no cover
    work_graph = None  # type: ignore

try:
    from koyote.intelligence import resolve_migration
except Exception:  # pragma: no cover
    resolve_migration = None  # type: ignore


MAX_ITERATIONS = 3
HUNT_DIRNAME = "hunt"

_EVIDENCE_KINDS = (
    "observed_fact",
    "inferred_relationship",
    "ai_hypothesis",
    "intended_behavior",
    "proposed_change",
    "verification_evidence",
)


# ── Findings ──────────────────────────────────────────────────────────────

@dataclass
class HuntFinding:
    """A Koyote finding Hunt can start from. Starting point only."""

    finding_id: str
    provider: str
    version_from: str
    version_to: str
    summary: str
    affected_files: list[str] = field(default_factory=list)
    callsite_count: int = 0
    guide_url: str = ""
    outcome: str = ""
    confidence: float = 0.0
    # Provenance of the change claim itself: manifest versions plus
    # registry metadata. The external change is INFERRED from the
    # registry, never observed at the vendor.
    basis: str = "registry metadata (change not independently observed)"


def finding_id_for(provider: str, version_from: str, version_to: str, affected: list[str]) -> str:
    seed = "|".join([
        (provider or "").lower(),
        version_from or "",
        version_to or "",
        ",".join(sorted(affected or [])),
    ])
    digest = hashlib.blake3(seed.encode("utf-8")).hexdigest()[:6] if hasattr(hashlib, "blake3") else \
        hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
    safe = "".join(c if c.isalnum() else "-" for c in (provider or "finding").lower())[:24] or "finding"
    return f"{safe}-{digest}"


def list_findings(repo_dir: str) -> list[HuntFinding]:
    """Enumerate current actionable findings (evidence-only, zero AI tokens).

    Finding identity is derived from repo-RELATIVE paths over the
    canonical checkout location: the same finding keeps the same id
    whether the repo is addressed via a symlink, a relative path, or
    any other spelling of the same directory.
    """
    repo_dir = os.path.realpath(os.path.abspath(repo_dir))
    try:
        detections = detect_changes(repo_dir)
    except Exception:
        return []
    findings: list[HuntFinding] = []
    for det in detections:
        if det.outcome not in (IMPACT_AI, IMPACT_QUARANTINE):
            continue
        src = det.source
        # Repo-relative identity: absolute spellings must not leak into ids.
        rel_affected = [
            os.path.relpath(p, repo_dir) if os.path.isabs(p) else p
            for p in (det.affected_files or [])
        ]
        fid = finding_id_for(
            src.provider, src.version_from, src.version_to, rel_affected)
        findings.append(HuntFinding(
            finding_id=fid,
            provider=src.provider,
            version_from=src.version_from or "unknown",
            version_to=src.version_to or "unknown",
            summary=(src.metadata.get("breaking_change", "")
                    or "Registry-reported contract drift (change not independently observed)"),
            affected_files=rel_affected,
            callsite_count=det.callsite_count,
            guide_url=src.metadata.get("migration_guide_url", "") or "",
            outcome=det.outcome,
            confidence=det.confidence,
        ))
    return findings


def persist_findings(repo_dir: str, findings: list[HuntFinding]) -> str:
    path = os.path.join(os.path.abspath(repo_dir), ".koyote", HUNT_DIRNAME, "findings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in findings], f, indent=2)
        f.write("\n")
    return path


def resolve_finding(repo_dir: str, finding_ref: str) -> HuntFinding | None:
    """Resolve a user-supplied finding reference to a live finding.

    Accepts a finding id, a provider name, or a GitHub issue reference
    (``issue:<n>``, ``#<n>``) whose body names a provider. Live detection
    is re-run: stored identifiers are hints, never trusted context.
    """
    ref = (finding_ref or "").strip()
    if not ref:
        return None
    findings = list_findings(repo_dir)
    if not findings:
        return None
    lowered = ref.lower()
    for f in findings:
        if f.finding_id.lower() == lowered:
            return f
    for f in findings:
        if f.provider.lower() == lowered:
            return f
    if lowered.startswith("issue:") or lowered.startswith("#"):
        digits = "".join(c for c in lowered if c.isdigit())
        issue_finding = _finding_from_github_issue(repo_dir, digits)
        if issue_finding is not None:
            return issue_finding
        # Fall back: an issue reference without a resolvable body still
        # selects the single actionable finding, if there is exactly one.
        if len(findings) == 1:
            return findings[0]
        return None
    # Prefix match on the stable id, for terminal ergonomics.
    for f in findings:
        if f.finding_id.lower().startswith(lowered):
            return f
    return None


def _finding_from_github_issue(repo_dir: str, issue_number: str) -> HuntFinding | None:
    """Best-effort: map a GitHub issue body onto a live finding (evidence only)."""
    if not issue_number:
        return None
    body = ""
    try:
        from koyote.github.client import GitHubAppClient  # lazy: keeps import graph light
        import urllib.request

        client = GitHubAppClient()
        repo = _github_repo_from_remote(repo_dir)
        if repo and client.token:
            url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {client.token}",
                "User-Agent": "Koyote-Hunt/1.1",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8")).get("body", "") or ""
    except Exception:
        body = ""
    if not body:
        return None
    lowered_body = body.lower()
    for f in list_findings(repo_dir):
        if f.provider.lower() in lowered_body:
            return f
    return None


def _github_repo_from_remote(workdir: str) -> str | None:
    try:
        proc = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                              cwd=workdir, capture_output=True, text=True, timeout=10)
        remote = proc.stdout.strip()
        if "github.com" in remote:
            return remote.split("github.com")[-1].lstrip(":").lstrip("/").removesuffix(".git") or None
    except Exception:
        pass
    return None


# ── Context acquisition (evidence only) ───────────────────────────────────

@dataclass
class HuntContext:
    finding: HuntFinding
    repository: str
    branch: str
    commit_sha: str
    recent_commits: list[str] = field(default_factory=list)
    recent_pushes: list[dict[str, Any]] = field(default_factory=list)
    active_work: list[dict[str, Any]] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    symbols_callsites: list[str] = field(default_factory=list)
    dependency_edges: list[str] = field(default_factory=list)
    cross_repo: list[dict[str, Any]] = field(default_factory=list)
    external_change: dict[str, Any] = field(default_factory=dict)
    existing_tests: str = ""
    maintenance_history: list[dict[str, Any]] = field(default_factory=list)
    verified_memory: dict[str, Any] = field(default_factory=dict)
    pr_state: dict[str, Any] = field(default_factory=dict)
    repo_policy: dict[str, Any] = field(default_factory=dict)


def _git(repo: str, *args: str, timeout: int = 15) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=repo,
                              capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def gather_context(repo_dir: str, finding: HuntFinding) -> HuntContext:
    """Build the focused reasoning context. Evidence only: never decides."""
    repo_dir = os.path.abspath(repo_dir)
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    sha = _git(repo_dir, "rev-parse", "HEAD")
    log_raw = _git(repo_dir, "log", "--oneline", "-8")
    status_raw = _git(repo_dir, "status", "--porcelain=v1")

    recent_pushes: list[dict[str, Any]] = []
    active_work: list[dict[str, Any]] = []
    if work_graph is not None:
        try:
            graph = work_graph.load_graph()
            for cid, cand in (graph.get("candidates", {}) or {}).items():
                pushes = cand.get("pushes", []) or []
                if pushes:
                    recent_pushes.append({
                        "candidate": cid,
                        "status": cand.get("status", ""),
                        "head": (cand.get("head_sha", "") or "")[:8],
                        "pushes": len(pushes),
                    })
            for key, entry in (graph.get("branches", {}) or {}).items():
                active_work.append({"branch": key, "head": (entry.get("head_sha", "") or "")[:8]})
        except Exception:
            pass
    recent_pushes = recent_pushes[:8]
    active_work = active_work[:8]

    symbols: list[str] = []
    relevant = list(dict.fromkeys(finding.affected_files or []))[:40]
    if scan_callsites is not None and ScanConfig is not None:
        try:
            res = scan_callsites(repo_dir, ScanConfig(sdk_names=[finding.provider])) or {}
            for c in (res.get("callsites", []) or [])[:40]:
                line = str(c.get("line_content", "")).strip()[:200]
                symbols.append(
                    f"{c.get('file_path')}:{c.get('line_number')} [{c.get('kind')}] {line}")
                fp = c.get("file_path", "")
                if fp and fp not in relevant and len(relevant) < 60:
                    relevant.append(fp)
        except Exception:
            pass

    dep_edges: list[str] = []
    if build_dependency_graph is not None:
        try:
            graph = build_dependency_graph(repo_dir) or {}
            for w in (graph.get("wrappers", []) or [])[:10]:
                wf = w.get("wrapper_file") or w.get("file_path") or ""
                if wf:
                    dep_edges.append(f"wrapper {wf} wraps {w.get('wraps_provider', finding.provider)}")
                    if wf not in relevant and len(relevant) < 60:
                        relevant.append(wf)
        except Exception:
            pass

    cross_repo: list[dict[str, Any]] = []
    try:
        from koyote import cross_repo as _xr  # lazy: optional surface
        for repo in (_xr.installed_repos() or [])[:10]:
            checkout = _xr.consumer_checkout(repo)
            if checkout:
                cross_repo.append({"repository": repo, "checkout": True})
    except Exception:
        pass

    external_change: dict[str, Any] = {
        "provider": finding.provider,
        "from": finding.version_from,
        "to": finding.version_to,
        "summary": finding.summary,
        "guide_url": finding.guide_url,
        # Provenance of the change claim itself: manifest versions plus
        # registry metadata. The external change is INFERRED, never
        # observed at the vendor — reasoning must treat it as such.
        "evidence": "registry metadata (change not independently observed)",
    }
    if resolve_migration is not None:
        try:
            _from, _to, migration = resolve_migration(
                finding.provider, finding.version_from, finding.version_to)
            if migration is not None:
                external_change["migration"] = getattr(migration, "description", "") or ""
                external_change["changelog_url"] = getattr(migration, "changelog_url", "") or finding.guide_url
        except Exception:
            pass

    try:
        test_cmd = _detect_test_command(repo_dir) or ""
    except Exception:
        test_cmd = ""

    history: list[dict[str, Any]] = []
    try:
        hp = os.path.join(repo_dir, ".koyote", "history.json")
        if os.path.isfile(hp):
            with open(hp, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                history = [d for d in data if isinstance(d, dict)][-5:]
    except Exception:
        history = []

    memory: dict[str, Any] = {}
    try:
        entry = kb_lookup(repo_dir, finding.provider, finding.version_from, finding.version_to)
        if entry is not None:
            # Verified memory only: patterns with executable provenance and the
            # test recipe. Failed patterns are surfaced as avoid-list, never
            # as trusted knowledge.
            memory = {
                "verified_patterns": [
                    p.description for p in (entry.patterns or [])[:8] if p.description],
                "failed_patterns": [
                    f.get("description", "") for f in (entry.failed_patterns or [])[:5]],
                "test_recipe": (entry.test_recipe or {}).get("test_command", ""),
            }
    except Exception:
        memory = {}

    pr_state: dict[str, Any] = {}
    try:
        open_prs = _git(repo_dir, "branch", "-a", "--list", "*pull*")
        pr_state = {
            "branch": branch,
            "dirty": bool(status_raw),
            "dirty_files": status_raw.splitlines()[:20] if status_raw else [],
            "open_pull_refs": open_prs.splitlines()[:5] if open_prs else [],
            "github_repo": _github_repo_from_remote(repo_dir) or "",
        }
    except Exception:
        pr_state = {"branch": branch}

    policy: dict[str, Any] = {}
    try:
        from koyote.config import load_config  # lazy

        cfg_path = os.path.join(repo_dir, ".koyote", "config.yaml")
        cfg = load_config(cfg_path if os.path.isfile(cfg_path) else None)
        pol = cfg.pipeline_policy()
        policy = {
            "mode": getattr(pol, "mode", "work"),
            "pr_auto_fix": getattr(pol, "pr_auto_fix", False),
            "external_auto_fix": getattr(pol, "external_auto_fix", False),
        }
    except Exception:
        policy = {}

    return HuntContext(
        finding=finding,
        repository=os.path.basename(repo_dir),
        branch=branch,
        commit_sha=sha,
        recent_commits=log_raw.splitlines()[:8] if log_raw else [],
        recent_pushes=recent_pushes,
        active_work=active_work,
        relevant_files=relevant,
        symbols_callsites=symbols,
        dependency_edges=dep_edges,
        cross_repo=cross_repo,
        external_change=external_change,
        existing_tests=test_cmd,
        maintenance_history=history,
        verified_memory=memory,
        pr_state=pr_state,
        repo_policy=policy,
    )


# ── AI reasoning (decides; never edits) ───────────────────────────────────

@dataclass
class FileIntent:
    path: str
    why_inspected: str = ""
    why_affected: str = ""
    why_change: str = ""
    why_replacement: str = ""
    why_not_surrounding: str = ""
    intent_preserved: str = ""
    evidence: str = ""


@dataclass
class HuntReasoning:
    problem: str = ""
    current_behavior: str = ""
    cause: str = ""
    intended_behavior: str = ""
    affected_paths: list[str] = field(default_factory=list)
    affected_callsites: list[str] = field(default_factory=list)
    related_matter: list[str] = field(default_factory=list)
    explicitly_unaffected: list[str] = field(default_factory=list)
    assumptions: list[dict[str, str]] = field(default_factory=list)
    smallest_change: str = ""
    must_not_change: list[str] = field(default_factory=list)
    regression_risks: list[str] = field(default_factory=list)
    verification_plan: str = ""
    file_intents: list[FileIntent] = field(default_factory=list)
    raw: str = ""


REASONING_SYSTEM = (
    "You are Hunt, Koyote's autonomous software maintenance repair agent. "
    "Reason about a dependency/contract change before any code is touched. "
    "Distinguish observed fact from inferred relationship, AI hypothesis, "
    "intended behavior, proposed change, and verification evidence. "
    "Never invent identifiers, APIs, symbols, files, dependencies, behavior, "
    "or tests: reference only what exists in the provided context. "
    "Prefer the smallest semantically correct repair that restores intended "
    "behavior without unrelated changes. Reply as a single JSON object."
)

REASONING_QUESTIONS = (
    "Answer: 1 actual problem; 2 current behavior; 3 what changed/caused it; "
    "4 intended behavior; 5 exact affected code paths; 6 genuinely affected "
    "callsites; 7 related files/repos that matter; 8 explicitly unaffected "
    "code; 9 assumptions; 10 evidence for assumptions; 11 smallest correct "
    "semantic change; 12 what must not change; 13 regression risks; "
    "14 how correctness will be verified."
)


def _context_text(ctx: HuntContext) -> str:
    sections = [
        f"Finding: {ctx.finding.provider} {ctx.finding.version_from} -> {ctx.finding.version_to}",
        f"Summary: {ctx.finding.summary}",
        f"Repository: {ctx.repository} | branch {ctx.branch} | sha {ctx.commit_sha or 'unknown'}",
    ]
    if ctx.recent_commits:
        sections.append("Recent commits:\n" + "\n".join(f"- {c}" for c in ctx.recent_commits[:8]))
    if ctx.recent_pushes:
        sections.append("Recent pushes:\n" + "\n".join(
            f"- {p.get('candidate')} head={p.get('head')} ({p.get('pushes')} push(es))"
            for p in ctx.recent_pushes[:8]))
    if ctx.active_work:
        sections.append("Active developer work:\n" + "\n".join(
            f"- {w.get('branch')} @ {w.get('head')}" for w in ctx.active_work[:8]))
    if ctx.pr_state.get("dirty_files"):
        sections.append("Uncommitted work (do not overwrite):\n" + "\n".join(
            f"- {x}" for x in ctx.pr_state["dirty_files"][:20]))
    if ctx.relevant_files:
        sections.append("Relevant files:\n" + "\n".join(f"- {x}" for x in ctx.relevant_files[:40]))
    if ctx.symbols_callsites:
        sections.append("Symbols/callsites:\n" + "\n".join(f"- {x}" for x in ctx.symbols_callsites[:40]))
    if ctx.dependency_edges:
        sections.append("Dependency relationships:\n" + "\n".join(f"- {x}" for x in ctx.dependency_edges[:10]))
    if ctx.cross_repo:
        sections.append("Cross-repository relationships:\n" + "\n".join(
            f"- {x.get('repository')}" for x in ctx.cross_repo[:10]))
    ext = ctx.external_change
    if ext.get("migration"):
        sections.append(f"External change context: {ext['migration']}")
    if ext.get("changelog_url"):
        sections.append(f"Vendor guide: {ext['changelog_url']}")
    if ctx.existing_tests:
        sections.append(f"Existing tests: `{ctx.existing_tests}` must keep passing")
    if ctx.verified_memory.get("verified_patterns"):
        sections.append("Verified maintenance memory (trusted):\n" + "\n".join(
            f"- {x}" for x in ctx.verified_memory["verified_patterns"][:8]))
    if ctx.verified_memory.get("failed_patterns"):
        sections.append("Known-bad approaches (do NOT repeat):\n" + "\n".join(
            f"- {x}" for x in ctx.verified_memory["failed_patterns"][:5] if x))
    return "\n".join(sections)


def ai_reason(client: LLMClient, ctx: HuntContext,
              prior_evidence: str = "") -> HuntReasoning:
    """Run the pre-edit reasoning step. Returns structured repair state."""
    schema_hint = (
        '{"problem":"","current_behavior":"","cause":"","intended_behavior":"",'
        '"affected_paths":[],"affected_callsites":[],"related_matter":[],'
        '"explicitly_unaffected":[],"assumptions":[{"assumption":"","evidence":"","kind":""}],'
        '"smallest_change":"","must_not_change":[],"regression_risks":[],'
        '"verification_plan":"","file_intents":[{"path":"","why_inspected":"",'
        '"why_affected":"","why_change":"","why_replacement":"",'
        '"why_not_surrounding":"","intent_preserved":"","evidence":""}]}'
    )
    user_content = (
        _context_text(ctx)
        + f"\n\n{REASONING_QUESTIONS}\nRespond with exactly that JSON shape: {schema_hint}"
    )
    if prior_evidence:
        user_content += f"\n\nPrior attempt evidence to re-reason about:\n{prior_evidence[:3000]}"
    try:
        resp = client.complete(
            messages=[{"role": "user", "content": user_content}],
            system_prompt=REASONING_SYSTEM,
        )
        raw = resp.content or ""
    except Exception as exc:
        return HuntReasoning(problem=f"reasoning unavailable: {exc}", raw="")
    parsed: dict[str, Any] = {}
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(raw[start:end + 1])
    except Exception:
        parsed = {}
    intents: list[FileIntent] = []
    for item in (parsed.get("file_intents", []) or [])[:20]:
        if not isinstance(item, dict):
            continue
        intents.append(FileIntent(
            path=str(item.get("path", "")),
            why_inspected=str(item.get("why_inspected", "")),
            why_affected=str(item.get("why_affected", "")),
            why_change=str(item.get("why_change", "")),
            why_replacement=str(item.get("why_replacement", "")),
            why_not_surrounding=str(item.get("why_not_surrounding", "")),
            intent_preserved=str(item.get("intent_preserved", "")),
            evidence=str(item.get("evidence", "")),
        ))
    assumptions: list[dict[str, str]] = []
    for a in (parsed.get("assumptions", []) or [])[:10]:
        if isinstance(a, dict):
            kind = str(a.get("kind", "ai_hypothesis"))
            assumptions.append({
                "assumption": str(a.get("assumption", "")),
                "evidence": str(a.get("evidence", "")),
                "kind": kind if kind in _EVIDENCE_KINDS else "ai_hypothesis",
            })
    return HuntReasoning(
        problem=str(parsed.get("problem", "")),
        current_behavior=str(parsed.get("current_behavior", "")),
        cause=str(parsed.get("cause", "")),
        intended_behavior=str(parsed.get("intended_behavior", "")),
        affected_paths=[str(x) for x in (parsed.get("affected_paths", []) or [])[:20]],
        affected_callsites=[str(x) for x in (parsed.get("affected_callsites", []) or [])[:20]],
        related_matter=[str(x) for x in (parsed.get("related_matter", []) or [])[:20]],
        explicitly_unaffected=[str(x) for x in (parsed.get("explicitly_unaffected", []) or [])[:20]],
        assumptions=assumptions,
        smallest_change=str(parsed.get("smallest_change", "")),
        must_not_change=[str(x) for x in (parsed.get("must_not_change", []) or [])[:20]],
        regression_risks=[str(x) for x in (parsed.get("regression_risks", []) or [])[:10]],
        verification_plan=str(parsed.get("verification_plan", "")),
        file_intents=intents,
        raw=raw[:8000],
    )


# ── Sandbox execution (isolated worktree, real commands) ──────────────────

@dataclass
class SandboxResult:
    sandbox_dir: str
    worktree_created: bool
    commit_sha: str


def create_sandbox(repo_dir: str, sha: str = "") -> SandboxResult:
    """Create an isolated worktree at the exact revision. Evidence-grade."""
    repo_dir = os.path.abspath(repo_dir)
    parent = os.path.join(repo_dir, ".koyote", HUNT_DIRNAME, "sandboxes")
    os.makedirs(parent, exist_ok=True)
    sandbox = tempfile.mkdtemp(prefix="hunt-", dir=parent)
    target_ref = sha or _git(repo_dir, "rev-parse", "HEAD")
    if target_ref:
        proc = subprocess.run(
            ["git", "worktree", "add", "--detach", sandbox, target_ref],
            cwd=repo_dir, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return SandboxResult(sandbox_dir=sandbox, worktree_created=True,
                                 commit_sha=target_ref)
        shutil.rmtree(sandbox, ignore_errors=True)
        sandbox = tempfile.mkdtemp(prefix="hunt-", dir=parent)
    # Isolation without git: exact file copy of the tracked tree.
    try:
        tracked = _git(repo_dir, "ls-files").splitlines()
        for rel in tracked:
            src = os.path.join(repo_dir, rel)
            dst = os.path.join(sandbox, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
    except Exception:
        pass
    return SandboxResult(sandbox_dir=sandbox, worktree_created=False,
                         commit_sha=target_ref)


def destroy_sandbox(repo_dir: str, sandbox: SandboxResult) -> None:
    try:
        if sandbox.worktree_created:
            subprocess.run(["git", "worktree", "remove", "--force", sandbox.sandbox_dir],
                           cwd=repo_dir, capture_output=True, timeout=60)
        shutil.rmtree(sandbox.sandbox_dir, ignore_errors=True)
    except Exception:
        pass


@dataclass
class VerificationEvidence:
    command: str
    exit_code: int
    duration_ms: int
    output: str
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    build_ok: bool = True


def _hash_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def sandbox_changed_vs_repo(sandbox_dir: str, repo_dir: str,
                            candidates: list[str]) -> tuple[list[str], str]:
    """Changed files + unified diff of sandbox vs the untouched repo checkout.

    Git worktrees report via ``git diff``; plain-copy sandboxes fall back to
    content comparison of the candidate files. Either way this only measures
    what the AI-authored patch did — it never decides anything.
    """
    diff = _git(sandbox_dir, "diff", "--", ".")
    if diff:
        files = _git(sandbox_dir, "diff", "--name-only", "--", ".").splitlines()
        return [x for x in files if x.strip()], diff
    out_files: list[str] = []
    diffs: list[str] = []
    for abs_p in candidates:
        rel = os.path.relpath(abs_p, sandbox_dir)
        repo_p = os.path.join(repo_dir, rel)
        if not os.path.isfile(abs_p):
            continue
        if _hash_file(abs_p) == _hash_file(repo_p):
            continue
        out_files.append(rel)
        try:
            with open(repo_p, encoding="utf-8", errors="replace") as f:
                old = f.read().splitlines(keepends=True)
            with open(abs_p, encoding="utf-8", errors="replace") as f:
                new = f.read().splitlines(keepends=True)
            diffs.append("".join(difflib.unified_diff(
                old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")))
        except OSError:
            pass
    return out_files, "\n".join(diffs)[:20000]


def run_verification(sandbox_dir: str, timeout: int = 180) -> VerificationEvidence:
    """Run the project's real verification commands. Never fakes success."""
    cmd = ""
    try:
        cmd = _detect_test_command(sandbox_dir) or ""
    except Exception:
        cmd = ""
    if not cmd:
        return VerificationEvidence(command="", exit_code=-1, duration_ms=0,
                                    output="no test command detected", build_ok=False)
    try:
        _run_install(sandbox_dir, timeout=min(timeout, 120))
    except Exception:
        pass
    start = time.time()
    try:
        proc = _run_tests(sandbox_dir, cmd, timeout=timeout)
        exit_code = proc.returncode
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()[-6000:]
    except subprocess.TimeoutExpired:
        exit_code, output = 1, f"Test run timed out after {timeout}s"
    except Exception as exc:
        exit_code, output = 1, str(exc)[:2000]
    duration_ms = max(1, int((time.time() - start) * 1000))
    return VerificationEvidence(command=cmd, exit_code=exit_code,
                                duration_ms=duration_ms, output=output,
                                build_ok=(exit_code == 0))


# ── AI interpretation of evidence (decides; never executes) ───────────────

@dataclass
class HuntInterpretation:
    solved: bool
    unrelated_behavior: bool
    assumptions_false: list[str] = field(default_factory=list)
    cause: str = "uncertain"
    needs_more_investigation: bool = False
    rationale: str = ""


def ai_interpret(client: LLMClient, reasoning: HuntReasoning,
                 evidence: VerificationEvidence) -> HuntInterpretation:
    prompt = (
        "Interpret this repair verification evidence. Answer JSON "
        '{"solved":bool,"unrelated_behavior":bool,"assumptions_false":[],'
        '"cause":"hunt|pre_existing|uncertain","needs_more_investigation":bool,'
        '"rationale":""}. A passing test proves nothing unless it exercised '
        "the changed behavior; a failure may be pre-existing toolchain state, "
        "not the patch. Fail closed when uncertain.\n\n"
        f"Proposed change: {reasoning.smallest_change[:1500]}\n"
        f"Assumptions: {json.dumps(reasoning.assumptions)[:1500]}\n"
        f"Command: {evidence.command} exit={evidence.exit_code}\n"
        f"Changed files: {evidence.changed_files}\n"
        f"Output:\n{evidence.output[:3000]}"
    )
    try:
        resp = client.complete(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=(
                "You are Hunt verifying its own repair. Be skeptical: passing "
                "tests that did not exercise the change prove nothing."
            ),
        )
        raw = resp.content or ""
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start:end + 1]) if start != -1 and end > start else {}
    except Exception:
        return HuntInterpretation(solved=False, unrelated_behavior=False,
                                  cause="uncertain",
                                  needs_more_investigation=True,
                                  rationale="interpretation unavailable; failing closed")
    cause = str(parsed.get("cause", "uncertain"))
    return HuntInterpretation(
        solved=bool(parsed.get("solved", False)) and evidence.exit_code == 0,
        unrelated_behavior=bool(parsed.get("unrelated_behavior", False)),
        assumptions_false=[str(x) for x in (parsed.get("assumptions_false", []) or [])[:10]],
        cause=cause if cause in ("hunt", "pre_existing", "uncertain") else "uncertain",
        needs_more_investigation=bool(parsed.get("needs_more_investigation", False)),
        rationale=str(parsed.get("rationale", ""))[:2000],
    )


# ── Hunt report + audit ───────────────────────────────────────────────────

@dataclass
class HuntReport:
    success: bool
    finding_id: str
    provider: str
    version_from: str
    version_to: str
    repository_path: str
    commit_sha: str
    iterations: int
    files_modified: list[str] = field(default_factory=list)
    unified_diff: str = ""
    test_command: str = ""
    test_exit_code: int = -1
    trust_pr_body: str = ""
    pr_url: str | None = None
    audit_path: str = ""
    reason: str = ""
    evidence_summary: str = ""
    verified: VerifiedRepair | None = None
    """Sealed capability token. Set only on verified success; the only
    object decide_pr/publish will accept. Not settable into existence —
    only seal_verified_repair can mint it."""


def _audit_path(repo_dir: str, finding_id: str) -> str:
    path = os.path.join(os.path.abspath(repo_dir), ".koyote", HUNT_DIRNAME, finding_id, "audit.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def write_audit(repo_dir: str, finding_id: str, record: dict[str, Any]) -> str:
    path = _audit_path(repo_dir, finding_id)
    record = {"finding_id": finding_id, **record}
    clean, redacted = redact_record(record)
    if redacted:
        assert isinstance(clean, dict)
        clean["secrets_redacted"] = redacted
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    return path


# ── Port adapters (the loop talks to ports, never to modules) ─────────────

class _DefaultContext:
    """ContextProvider: read-only evidence assembly."""

    def gather(self, repo_dir: str, finding: HuntFinding) -> HuntContext:
        return gather_context(repo_dir, finding)


class _DefaultReasoner:
    """RepairReasoner: AI judgment over evidence. No repo access."""

    def __init__(self, client: LLMClient):
        self._client = client

    def reason(self, ctx: HuntContext, prior_evidence: str = "") -> HuntReasoning:
        return ai_reason(self._client, ctx, prior_evidence)


class AIPlannerAuthor:
    """PatchAuthor: the single channel that may emit patches.

    Receives the live context plus a RepairPlan (problem statement with
    guardrails, never an edit recipe) and seals every planner result
    with the live model's identity. This is the only place
    plan_and_apply may be called in Hunt's repair path — the core loop
    below never touches it.
    """

    def __init__(self, planner: Any):
        self._planner = planner

    @property
    def model(self) -> str:
        try:
            return str(getattr(getattr(self._planner, "client", None), "config", None).model or "unknown")
        except Exception:
            return "unknown"

    def author(
        self,
        *,
        ctx: HuntContext,
        plan: RepairPlan,
        sandbox_dir: str,
        candidate_files: list[str],
        reasoning_attempt: int,
    ) -> list[AIAuthoredPatch]:
        try:
            raw_results = self._planner.plan_and_apply(
                repo_dir=sandbox_dir,
                affected_files=candidate_files,
                provider_name=plan.provider,
                from_version=plan.version_from,
                to_version=plan.version_to,
                migration_details=_migration_details_for(plan),
                test_error=plan.test_error or None,
                dry_run=False,
                context=plan.base_context,
                changelog_url=plan.changelog_url,
            ) or []
        except Exception:
            return []
        sealed: list[AIAuthoredPatch] = []
        for raw in raw_results:
            patch = seal_ai_patch(raw, model=self.model, reasoning_attempt=reasoning_attempt)
            if patch is not None:
                sealed.append(patch)
        return sealed


def _migration_details_for(plan: RepairPlan) -> str:
    """Render the plan as planner briefing (context, not instructions)."""
    details = plan.summary
    if plan.smallest_change:
        details += f"\n\nHunt reasoning (smallest change): {plan.smallest_change}"
    if plan.must_not_change:
        details += "\nMust NOT change:\n" + "\n".join(f"- {x}" for x in plan.must_not_change[:15])
    return details


def build_repair_plan(
    finding: HuntFinding,
    reasoning: HuntReasoning,
    base_context: Any,
    test_error: str = "",
) -> RepairPlan:
    """Translate structured reasoning into the author's assignment.

    Frozen plan: what changed, what the model concluded, what is
    off-limits. Names no exact source lines — the author decides.
    """
    return RepairPlan(
        provider=finding.provider,
        version_from=finding.version_from,
        version_to=finding.version_to,
        summary=finding.summary,
        smallest_change=reasoning.smallest_change or "",
        must_not_change=tuple(reasoning.must_not_change or [])[:20],
        changelog_url=finding.guide_url or "",
        test_error=test_error or "",
        base_context=base_context,
    )


class _DefaultSandbox:
    """SandboxProvider: exact-revision isolation. Judges nothing."""

    def create(self, repo_dir: str, sha: str = "") -> SandboxResult:
        return create_sandbox(repo_dir, sha)

    def destroy(self, repo_dir: str, sandbox: SandboxResult) -> None:
        destroy_sandbox(repo_dir, sandbox)


class _DefaultVerifier:
    """Verifier: runs real commands, reports raw codes. Never forces success."""

    def verify(self, sandbox_dir: str, timeout: int = 180) -> VerificationEvidence:
        return run_verification(sandbox_dir, timeout)


class _DefaultInterpreter:
    """RepairInterpreter: skeptical AI judgment over evidence. Never executes."""

    def __init__(self, client: LLMClient):
        self._client = client

    def interpret(self, reasoning: HuntReasoning,
                  evidence: VerificationEvidence) -> HuntInterpretation:
        return ai_interpret(self._client, reasoning, evidence)


def render_repair_pr_body(repair: VerifiedRepair, ctx: HuntContext, repo_dir: str) -> str:
    """Developer Trust PR body for a sealed repair (presentation, not proof)."""
    try:
        from blake3 import blake3 as _blake3
        patch_hash = _blake3(repair.unified_diff.encode("utf-8")).hexdigest()
    except Exception:
        patch_hash = hashlib.sha256(repair.unified_diff.encode("utf-8")).hexdigest()
    meta = TrustPRMetadata(
        provider_name=repair.provider,
        from_version=repair.version_from,
        to_version=repair.version_to,
        changelog_url=ctx.external_change.get("changelog_url", "") or "",
        files_modified=len(repair.files),
        files_scanned=len(ctx.relevant_files) or len(repair.files),
        unintended_files_modified=0,
        quarantined_callsites_count=0,
        unified_diff=repair.unified_diff,
        test_command=repair.test_command,
        test_exit_code=repair.test_exit_code,
        test_duration_ms=repair.test_duration_ms,
        lockfile_hash=_compute_lockfile_hash(repo_dir),
        patch_hash=patch_hash,
        semantic_score=1.0,
        drift_reason=ctx.external_change.get("summary", "") or ctx.finding.summary,
        impacted_callsites=[
            {"file_path": f, "description": f"AI-authored repair for {repair.provider}"}
            for f in repair.files
        ],
    )
    return generate_trust_pr_markdown(meta)


class LocalGitHubPublisher:
    """PRPublisher: commits + opens PRs for sealed repairs only."""

    def publish(self, repair: VerifiedRepair, ctx: HuntContext,
                github_repo: str | None = None) -> str | None:
        if not isinstance(repair, VerifiedRepair):
            raise TypeError(
                "PRPublisher.publish requires a sealed VerifiedRepair. "
                "Raw reports and hand-built success flags are rejected."
            )
        repo = github_repo or (ctx.pr_state.get("github_repo", "") or "")
        if not repo:
            return None
        repo_dir = _repo_dir_for_ctx(ctx)
        if not repo_dir:
            return None
        branch = f"koyote/hunt-{repair.finding_id}"
        abs_files = [os.path.join(repo_dir, f) for f in repair.files]
        commit_msg = (
            f"fix: hunt repair {repair.provider} {repair.version_from} -> {repair.version_to}\n\n"
            f"Finding: {repair.finding_id}\n"
            f"Verified: `{repair.test_command}` exit 0\n"
            f"Audit: .koyote/hunt/{repair.finding_id}/audit.json\n\n"
            f"Agent-Origin: agent\n"
            f"Agent-Agent: koyote@1.1.3\n"
            f"Agent-Execution: hunt_{repair.finding_id}\n"
            f"Agent-Compartment: hunt\n"
            f"Agent-Sandbox: none\n"
            f"Execution-Isolation: git-worktree"
        )
        try:
            pushed = git_commit_and_push(repo_dir, abs_files, branch, commit_msg)
        except Exception:
            return None
        if not pushed:
            return None
        title = f"koyote(hunt): repair {repair.provider} {repair.version_from} -> {repair.version_to}"
        try:
            url = gh_create_pr(repo, branch, title,
                               render_repair_pr_body(repair, ctx, repo_dir))
            if url:
                return url
        except Exception:
            return None
        return f"pushed:{branch}"


def _repo_dir_for_ctx(ctx: HuntContext) -> str:
    """Recover the checkout path recorded on the context (set by run_hunt)."""
    path = getattr(ctx, "_repo_dir", "") or ""
    return str(path)


def default_ports(client: LLMClient, planner: Any) -> HuntPorts:
    """Wire the production seam set. Tests inject fakes instead."""
    return HuntPorts(
        context=_DefaultContext(),
        reasoner=_DefaultReasoner(client),
        author=AIPlannerAuthor(planner),
        sandbox=_DefaultSandbox(),
        verifier=_DefaultVerifier(),
        interpreter=_DefaultInterpreter(client),
        publisher=LocalGitHubPublisher(),
    )


# ── Main lifecycle ────────────────────────────────────────────────────────

def run_hunt(
    repo_dir: str,
    finding_ref: str,
    create_pr: bool = False,
    github_repo: str | None = None,
    auto_approve_pr: bool = False,
    max_iterations: int = MAX_ITERATIONS,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    ports: HuntPorts | None = None,
    lock_timeout_s: float = 120.0,
) -> HuntReport:
    """Reason -> patch (AI) -> sandbox -> verify -> interpret -> decide.

    AI-directed iteration only: compiler/test output is evidence the model
    reasons about. No deterministic loop ever authors code.

    The core loop talks ONLY to ``ports`` (default: production adapters).
    Tests inject fakes; future features implement protocols. The loop never
    imports a concrete repair module, so no deterministic edit path can be
    wired in without changing this function under review.
    """
    repo_dir = os.path.abspath(repo_dir)
    audit: dict[str, Any] = {"lifecycle": [], "decisions": []}
    audit["lifecycle"].append("finding_received")

    finding = resolve_finding(repo_dir, finding_ref)
    if finding is None:
        audit_path = write_audit(repo_dir, (finding_ref or "unknown").strip() or "unknown",
                                 {**audit, "final": "unknown_finding"})
        return HuntReport(success=False, finding_id=finding_ref, provider="",
                          version_from="", version_to="", repository_path=repo_dir,
                          commit_sha="", iterations=0, audit_path=audit_path,
                          reason=f"Unknown finding '{finding_ref}'. Run `koyote check` to list findings.")

    _refusal_sha = _git(repo_dir, "rev-parse", "HEAD")

    if not has_valid_credentials() and llm_api_key is None:
        audit_path = write_audit(repo_dir, finding.finding_id,
                                 {**audit, "final": "refused_no_credentials"})
        return HuntReport(success=False, finding_id=finding.finding_id,
                          provider=finding.provider, version_from=finding.version_from,
                          version_to=finding.version_to, repository_path=repo_dir,
                          commit_sha=_refusal_sha, iterations=0, audit_path=audit_path,
                          reason=("AI repair required but no AI provider is configured. "
                                  "Run `koyote auth` first."))

    cfg = resolve_llm_config(api_key=llm_api_key, model=llm_model, base_url=llm_base_url)
    if cfg is None or AIPatchPlanner is None:
        audit_path = write_audit(repo_dir, finding.finding_id,
                                 {**audit, "final": "refused_no_credentials"})
        return HuntReport(success=False, finding_id=finding.finding_id,
                          provider=finding.provider, version_from=finding.version_from,
                          version_to=finding.version_to, repository_path=repo_dir,
                          commit_sha=_refusal_sha, iterations=0, audit_path=audit_path,
                          reason=("AI repair required but no AI provider is configured. "
                                  "Run `koyote auth` first."))

    client = LLMClient(cfg)
    planner = AIPatchPlanner(client=client)
    active_ports = ports or default_ports(client, planner)

    lock = HuntLock(repo_dir)
    if not lock.acquire(timeout_s=lock_timeout_s):
        audit_path = write_audit(repo_dir, finding.finding_id,
                                 {**audit, "final": "refused_lock"})
        return HuntReport(success=False, finding_id=finding.finding_id,
                          provider=finding.provider, version_from=finding.version_from,
                          version_to=finding.version_to, repository_path=repo_dir,
                          commit_sha=_refusal_sha, iterations=0, audit_path=audit_path,
                          reason=("Another Hunt repair is already running on this "
                                  "repository. No PR was created."))
    if lock.degraded:
        audit["lifecycle"].append("lock_degraded_no_fcntl")

    ctx = active_ports.context.gather(repo_dir, finding)
    ctx._repo_dir = repo_dir  # sidecar: publisher port needs the checkout path
    audit["lifecycle"].append("context_gathered")
    audit["context"] = {
        "repository": ctx.repository, "branch": ctx.branch, "sha": ctx.commit_sha,
        "relevant_files": ctx.relevant_files, "callsites": ctx.symbols_callsites[:20],
        "tests": ctx.existing_tests, "policy": ctx.repo_policy,
        "dirty": ctx.pr_state.get("dirty", False),
    }

    sandbox = active_ports.sandbox.create(repo_dir, ctx.commit_sha)
    audit["lifecycle"].append(f"sandbox_created:{sandbox.sandbox_dir}")
    snapshotter = SnapshotManager(workdir=repo_dir,
                                  snapshot_dir=os.path.join(repo_dir, ".koyote",
                                                            HUNT_DIRNAME, finding.finding_id,
                                                            "snapshot_tmp"))
    try:
        snapshotter.snapshot()
    except Exception:
        pass
    baseline_dirty = _worktree_dirty_set(repo_dir)

    base_ctx = None
    if build_reasoning_context is not None:
        try:
            base_ctx = build_reasoning_context(
                repo_dir, finding.provider, finding.version_from, finding.version_to,
                finding.summary, finding.guide_url)
        except Exception:
            base_ctx = None

    prior_evidence = ""
    final_patches: list[AIAuthoredPatch] = []
    attempted_rel_files: list[str] = []
    final_evidence: VerificationEvidence | None = None
    final_reasoning: HuntReasoning | None = None
    final_interp: HuntInterpretation | None = None
    final_repair: VerifiedRepair | None = None
    iterations = 0

    try:
        for attempt in range(1, max(1, max_iterations) + 1):
            iterations = attempt
            reasoning = active_ports.reasoner.reason(ctx, prior_evidence)
            final_reasoning = reasoning
            audit["lifecycle"].append(f"reasoned:attempt_{attempt}")
            audit.setdefault("reasoning", []).append({
                "attempt": attempt,
                "problem": reasoning.problem,
                "smallest_change": reasoning.smallest_change,
                "affected_paths": reasoning.affected_paths,
                "must_not_change": reasoning.must_not_change,
                "assumptions": reasoning.assumptions,
                "file_intents": [asdict(x) for x in reasoning.file_intents],
            })

            declared = [x for x in (reasoning.affected_paths or []) if x]
            candidates = declared or list(ctx.relevant_files or []) or list(finding.affected_files or [])
            # Evidence candidates only: the model decides per file whether a
            # change is warranted (no-change yields no patch for that file).
            candidate_abs = []
            for rel in candidates[:30]:
                abs_p = rel if os.path.isabs(rel) else os.path.join(sandbox.sandbox_dir, rel)
                if os.path.isfile(abs_p):
                    candidate_abs.append(abs_p)
            if not candidate_abs:
                audit["decisions"].append({"attempt": attempt, "decision": "no_candidate_files"})
                prior_evidence = "No candidate files existed in the sandbox for the declared paths."
                continue

            plan = build_repair_plan(
                finding, reasoning, base_ctx,
                test_error=prior_evidence if prior_evidence else "")

            # Single authorized patch channel: the PatchAuthor port seals
            # every result with the live model's identity. Raw planner
            # output and deterministic fixer output cannot enter this
            # list — seal_ai_patch admits only successful, diff-bearing
            # results stamped author="ai".
            try:
                patches = active_ports.author.author(
                    ctx=ctx,
                    plan=plan,
                    sandbox_dir=sandbox.sandbox_dir,
                    candidate_files=candidate_abs,
                    reasoning_attempt=attempt,
                ) or []
            except Exception as exc:
                audit["decisions"].append({"attempt": attempt, "decision": f"patch_error:{exc}"})
                prior_evidence = f"Patch application errored: {exc}"
                continue
            audit["lifecycle"].append(f"patch_generated:attempt_{attempt}:{len(patches)}")
            if not patches:
                audit["decisions"].append({"attempt": attempt, "decision": "ai_produced_no_patch"})
                prior_evidence = "The model produced no applicable patch for the candidate files."
                continue
            attempted_rel_files = [
                os.path.relpath(p.file_path, sandbox.sandbox_dir)
                if str(p.file_path).startswith(sandbox.sandbox_dir) else str(p.file_path)
                for p in patches
            ]

            changed, diff = sandbox_changed_vs_repo(sandbox.sandbox_dir, repo_dir, candidate_abs)
            evidence = active_ports.verifier.verify(sandbox.sandbox_dir)
            evidence.changed_files = changed
            evidence.diff = diff[:20000]
            final_evidence = evidence
            audit["lifecycle"].append(
                f"sandbox_verified:attempt_{attempt}:exit_{evidence.exit_code}")
            audit.setdefault("verification", []).append({
                "attempt": attempt,
                "command": evidence.command,
                "exit_code": evidence.exit_code,
                "duration_ms": evidence.duration_ms,
                "output": evidence.output[:3000],
                "changed_files": changed,
            })

            interp = active_ports.interpreter.interpret(reasoning, evidence)
            final_interp = interp
            audit.setdefault("interpretation", []).append({
                **asdict(interp),
                "attempt": attempt,
                # Epistemic labels, not decorations: coverage relevance and
                # failure-cause attribution are AI judgments. The mechanism
                # proves tests ran green and scope held — nothing more.
                "coverage_provenance": "ai-judged (no independent coverage mapping)",
                "cause_provenance": "ai-attributed (no pre-patch baseline)",
            })

            scope_ok, scope_reason = evaluate_scope(
                reasoning.affected_paths, reasoning.must_not_change, changed)
            if not scope_ok:
                audit["decisions"].append({"attempt": attempt, "decision": f"scope_failed:{scope_reason}"})
                prior_evidence = (
                    f"Scope violation: {scope_reason}. Changed={changed}. "
                    f"Declared={reasoning.affected_paths}. Test output:\n{evidence.output[:2000]}")
                final_patches = []
                continue

            if evidence.exit_code != 0 or not evidence.command:
                cause = interp.cause if interp else "uncertain"
                audit["decisions"].append(
                    {"attempt": attempt, "decision": f"verification_failed:cause_{cause}"})
                diag = extract_structured_test_feedback(evidence.output, evidence.exit_code)
                prior_evidence = (
                    f"Verification {'has no test command (fail closed)' if not evidence.command else 'failed'}.\n"
                    f"{diag.format_for_model()}\n"
                    f"AI interpretation: cause={cause}, rationale={interp.rationale if interp else ''}."
                )
                final_patches = []
                continue

            if interp.needs_more_investigation or not interp.solved or interp.unrelated_behavior:
                audit["decisions"].append(
                    {"attempt": attempt, "decision": "interpretation_not_convinced"})
                prior_evidence = (
                    f"AI interpretation refused to accept the repair: solved={interp.solved} "
                    f"unrelated={interp.unrelated_behavior} rationale={interp.rationale}. "
                    f"Output:\n{evidence.output[:2000]}")
                final_patches = []
                continue

            # All green: mint the capability token. The sealer DERIVES
            # acceptance from the sealed patches, the measured evidence,
            # the interpretation object, and its own scope evaluation —
            # no caller-supplied booleans. None means fail closed.
            final_patches = patches
            final_repair = seal_verified_repair(
                finding_id=finding.finding_id,
                provider=finding.provider,
                version_from=finding.version_from,
                version_to=finding.version_to,
                patches=patches,
                sandbox_dir=sandbox.sandbox_dir,
                evidence=evidence,
                interpretation=interp,
                affected_paths=reasoning.affected_paths,
                must_not_change=reasoning.must_not_change,
                reasoning_attempt=attempt,
                model=patches[0].model if patches else "unknown",
            )
            if final_repair is None:
                audit["decisions"].append({"attempt": attempt, "decision": "seal_refused"})
                prior_evidence = "Repair seal refused despite green signals; failing closed."
                final_patches = []
                continue
            break

        if final_repair is None:
            try:
                snapshotter.restore()
            except Exception:
                pass
            # Every failed attempt lands on the avoid-list — including
            # attempts whose patches did not survive verification. Failed
            # repairs must be remembered as failures, never as patterns.
            blame_files = [
                os.path.relpath(p.file_path, sandbox.sandbox_dir)
                if str(p.file_path).startswith(sandbox.sandbox_dir)
                else str(p.file_path) for p in final_patches
            ] or attempted_rel_files
            try:
                record_failure(
                    repo_dir, finding.provider, finding.version_from, finding.version_to,
                    f"hunt verification failed ({(final_evidence.command if final_evidence else '') or 'no test command'}, "
                    f"exit {(final_evidence.exit_code if final_evidence else -1)})",
                    blame_files,
                )
            except Exception:
                pass
            audit_path = write_audit(repo_dir, finding.finding_id,
                                     {**audit, "final": "refused_unverified"})
            return HuntReport(
                success=False, finding_id=finding.finding_id, provider=finding.provider,
                version_from=finding.version_from, version_to=finding.version_to,
                repository_path=repo_dir, commit_sha=ctx.commit_sha, iterations=iterations,
                test_command=final_evidence.command if final_evidence else ctx.existing_tests,
                test_exit_code=final_evidence.exit_code if final_evidence else -1,
                audit_path=audit_path,
                reason="Hunt could not safely verify this repair. No PR was created.",
                evidence_summary=(final_evidence.output[:1500] if final_evidence else ""),
            )

        # Promote the sealed repair onto the real checkout. Binding is
        # verified first (same repo, same SHA, same worktree); promotion
        # accepts ONLY sealed patches and re-checks provenance plus path
        # containment at this boundary — raw paths can never arrive here.
        assert final_reasoning is not None and final_repair is not None
        binding_ok, binding_reason = verify_sandbox_binding(sandbox, repo_dir)
        if not binding_ok:
            try:
                snapshotter.restore()
            except Exception:
                pass
            audit["decisions"].append({"decision": f"binding_failed:{binding_reason}"})
            audit_path = write_audit(repo_dir, finding.finding_id,
                                     {**audit, "final": "refused_binding"})
            return HuntReport(
                success=False, finding_id=finding.finding_id, provider=finding.provider,
                version_from=finding.version_from, version_to=finding.version_to,
                repository_path=repo_dir, commit_sha=ctx.commit_sha, iterations=iterations,
                test_command=final_repair.test_command,
                test_exit_code=final_repair.test_exit_code,
                audit_path=audit_path,
                reason=(f"Hunt refused to promote: {binding_reason}. "
                        "No PR was created."),
                evidence_summary="",
            )
        try:
            promoted = _promote_sandbox(sandbox.sandbox_dir, repo_dir, final_patches)
        except (TypeError, ValueError, OSError) as exc:
            try:
                snapshotter.restore()
            except Exception:
                pass
            audit["decisions"].append({"decision": f"promotion_refused:{exc}"})
            audit_path = write_audit(repo_dir, finding.finding_id,
                                     {**audit, "final": "refused_promotion"})
            return HuntReport(
                success=False, finding_id=finding.finding_id, provider=finding.provider,
                version_from=finding.version_from, version_to=finding.version_to,
                repository_path=repo_dir, commit_sha=ctx.commit_sha, iterations=iterations,
                test_command=final_repair.test_command,
                test_exit_code=final_repair.test_exit_code,
                audit_path=audit_path,
                reason=(f"Hunt refused to promote: {exc}. "
                        "No PR was created."),
                evidence_summary="",
            )
        promo_ok, promo_reason = verify_promotion(
            sandbox.sandbox_dir, repo_dir, promoted, baseline_dirty)
        if not promo_ok:
            try:
                snapshotter.restore()
            except Exception:
                pass
            audit["decisions"].append({"decision": f"promotion_integrity_failed:{promo_reason}"})
            audit_path = write_audit(repo_dir, finding.finding_id,
                                     {**audit, "final": "refused_promotion_integrity"})
            return HuntReport(
                success=False, finding_id=finding.finding_id, provider=finding.provider,
                version_from=finding.version_from, version_to=finding.version_to,
                repository_path=repo_dir, commit_sha=ctx.commit_sha, iterations=iterations,
                test_command=final_repair.test_command,
                test_exit_code=final_repair.test_exit_code,
                audit_path=audit_path,
                reason=(f"Hunt refused to keep the promoted state: {promo_reason}. "
                        "No PR was created."),
                evidence_summary="",
            )
        unified = final_repair.unified_diff
        pr_body = render_repair_pr_body(final_repair, ctx, repo_dir)
        try:
            snapshotter.cleanup()
        except Exception:
            pass
        audit_path = write_audit(repo_dir, finding.finding_id, {
            **audit,
            "final": "verified",
            "reasoning_summary": final_reasoning.smallest_change,
            "final_interpretation": asdict(final_interp) if final_interp else {},
            "files": promoted,
            "test_command": final_repair.test_command,
            "test_exit_code": final_repair.test_exit_code,
            "model": final_repair.model,
            "drift_basis": finding.basis,
            "evidence_limits": [
                "coverage relevance: AI-judged, no independent test-target mapping",
                "failure attribution: not applicable on the green path; "
                "red-path causes are AI-attributed (no pre-patch baseline)",
                "minimality: AI-claimed; scope check enforces subset-of-declared only",
            ],
        })
        report = HuntReport(
            success=True, finding_id=finding.finding_id, provider=finding.provider,
            version_from=finding.version_from, version_to=finding.version_to,
            repository_path=repo_dir, commit_sha=ctx.commit_sha, iterations=iterations,
            files_modified=promoted, unified_diff=unified,
            test_command=final_repair.test_command, test_exit_code=final_repair.test_exit_code,
            trust_pr_body=pr_body, audit_path=audit_path,
            reason="Verified repair.",
            evidence_summary=f"{final_repair.test_command} exit 0 in {final_repair.test_duration_ms}ms",
            verified=final_repair,
        )
        pr_url = decide_pr(final_repair, ctx, github_repo=github_repo,
                           create_pr=create_pr, auto_approve=auto_approve_pr,
                           publisher=active_ports.publisher)
        report.pr_url = pr_url
        update_audit(audit_path, pr_url=pr_url,
                     pr_decision="created" if pr_url else "deferred")
        return report
    finally:
        active_ports.sandbox.destroy(repo_dir, sandbox)
        lock.release()


def verify_sandbox_binding(sandbox: SandboxResult, repo_dir: str) -> tuple[bool, str]:
    """Confirm the sandbox still belongs to this exact repository state.

    Refuses promotion when the sandbox directory is gone, when the
    recorded SHA is set but the checkout moved underneath the run, or
    when the worktree registration vanished. A sealed patch from an
    unrelated or uncontrolled directory can never be promoted.
    """
    if not os.path.isdir(sandbox.sandbox_dir):
        return False, "sandbox directory is gone"
    if not sandbox.commit_sha:
        return True, ""
    current = _git(repo_dir, "rev-parse", "HEAD")
    if not current:
        return False, "repository state unreadable; refusing promotion"
    if current != sandbox.commit_sha:
        return False, (
            f"repository moved during repair "
            f"({sandbox.commit_sha[:8]} -> {current[:8]}); refusing promotion"
        )
    if sandbox.worktree_created:
        registered = _git(repo_dir, "worktree", "list", "--porcelain")
        if registered and os.path.abspath(sandbox.sandbox_dir) not in registered:
            return False, "sandbox worktree is no longer registered"
    return True, ""


def update_audit(audit_path: str, **fields: Any) -> str:
    """Append fields to the audit record. The single audit-mutation point.

    Keeps all audit file writes in one place so the repair loop itself
    never opens files for writing — loop purity stays reviewable.
    """
    try:
        with open(audit_path, encoding="utf-8") as f:
            record = json.load(f)
    except Exception:
        record = {}
    record.update(fields)
    try:
        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
    except Exception:
        pass
    return audit_path


def _norm_repo_path(path: str) -> str:
    """Normalize a repo-relative path without mangling dotfiles."""
    path = (path or "").strip()
    if path.startswith("./"):
        path = path[2:]
    return path


def _worktree_dirty_set(repo_dir: str) -> set[str]:
    """Repo-relative names of worktree files differing from HEAD. Best-effort.

    Runs git directly instead of _git(): porcelain columns are
    position-sensitive and _git()'s output stripping destroys the
    leading status column (" M app.py" would parse as "pp.py").
    """
    out: set[str] = set()
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--", "."],
            cwd=repo_dir, capture_output=True, text=True, timeout=15)
        raw = proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return out
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename/copy entry: take the new path
            path = path.split(" -> ", 1)[1]
        path = _norm_repo_path(path.strip().strip('"'))
        if path:
            out.add(path)
    return out


class HuntLock:
    """Repo-level mutual exclusion for Hunt runs. Best-effort, fail-closed.

    Serializes snapshot → sandbox → promotion so two concurrent Hunts
    cannot silently clobber each other's verified repairs. Blocking
    acquire with a timeout; expiry refuses loudly instead of queueing
    forever. No fcntl on the platform degrades to an audit flag (the
    sandbox itself is POSIX-only too).
    """

    def __init__(self, repo_dir: str):
        self._path = os.path.join(os.path.abspath(repo_dir), ".koyote", "hunt.lock")
        self._fh: Any = None
        self.held = False
        self.degraded = False

    def acquire(self, timeout_s: float = 120.0) -> bool:
        if fcntl is None:
            self.degraded = True
            return True
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fh = open(self._path, "w")
        except OSError:
            return False
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                    return False
                time.sleep(0.25)

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self.held = False


def verify_promotion(
    sandbox_dir: str,
    repo_dir: str,
    promoted: list[str],
    baseline_dirty: set[str],
) -> tuple[bool, str]:
    """Confirm the promoted state is exactly what Hunt believes it created.

    Re-reads every promoted file (bytes must match the sandbox source)
    and requires the worktree diff to contain nothing beyond the
    pre-existing dirt plus Hunt's own bookkeeping and promoted files.
    Anything else means an outside writer interfered mid-run: fail closed.
    """
    base = os.path.abspath(sandbox_dir)
    for rel in promoted:
        src = os.path.join(base, rel)
        dst = os.path.join(repo_dir, rel)
        try:
            with open(src, "rb") as f:
                want = f.read()
            with open(dst, "rb") as f:
                got = f.read()
        except OSError:
            return False, f"promoted file unreadable after copy: {rel}"
        if want != got:
            return False, f"promoted file diverged after copy: {rel}"
    allowed = {_norm_repo_path(p) for p in promoted} | {_norm_repo_path(p) for p in baseline_dirty}
    unexpected = sorted(
        c for c in _worktree_dirty_set(repo_dir)
        if c not in allowed and not c.startswith(".koyote/")
    )
    if unexpected:
        return False, f"unexpected worktree changes during repair: {', '.join(unexpected)}"
    return True, ""


def _promote_sandbox(sandbox_dir: str, repo_dir: str,
                     patches: list[AIAuthoredPatch]) -> list[str]:
    """Copy the sealed repair onto the real checkout (execution, not authorship).

    Accepts ONLY sealed AIAuthoredPatch objects: isinstance plus author
    plus the private seal are re-checked at this boundary, so duck-typed
    lookalikes and forged provenance cannot pass. Sandbox containment is
    enforced per file. Raw path lists cannot arrive here — the signature
    forbids the entire class of accident where an unrelated file list
    gets promoted.
    """
    base = os.path.abspath(sandbox_dir)
    promoted: list[str] = []
    for patch in patches:
        if not is_sealed(patch):
            raise TypeError(
                "Promotion refused: unsealed patch at the sandbox boundary.")
        abs_p = os.path.abspath(patch.file_path)
        if not abs_p.startswith(base + os.sep):
            raise ValueError(
                f"Promotion refused: {patch.file_path} escapes the sandbox.")
        rel = os.path.relpath(abs_p, base)
        if not rel or rel.startswith(".koyote"):
            continue
        if os.path.islink(abs_p):
            raise ValueError(
                f"Promotion refused: {rel} is a symlink; refusing to copy linked content.")
        dst = os.path.join(repo_dir, rel)
        if os.path.islink(dst):
            raise ValueError(
                f"Promotion refused: {rel} would write through a symlink.")
        if os.path.isfile(abs_p):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(abs_p, dst)
            promoted.append(rel)
    return promoted


def decide_pr(repair: VerifiedRepair, ctx: HuntContext,
              github_repo: str | None = None,
              create_pr: bool = False,
              auto_approve: bool = False,
              publisher: PRPublisher | None = None) -> str | None:
    """Only a sealed verified repair may proceed toward a PR.

    The signature itself is the gate: anything that is not a
    VerifiedRepair raises TypeError. Hand-built reports, success flags,
    and exit-code claims cannot reach publication.
    """
    if not isinstance(repair, VerifiedRepair):
        raise TypeError(
            "decide_pr requires a sealed VerifiedRepair token. "
            "Unverified repairs can never become PRs — this is a type "
            "error by design, not a missing flag."
        )
    auto_enabled = bool(ctx.repo_policy.get("pr_auto_fix") or ctx.repo_policy.get("external_auto_fix"))
    should_create = bool(create_pr or auto_approve or auto_enabled)
    if not should_create:
        return None
    pub = publisher or LocalGitHubPublisher()
    return pub.publish(repair, ctx, github_repo)


def render_hunt_summary(report: HuntReport) -> str:
    if report.success:
        lines = [
            "Verified repair.",
            f"Finding: {report.finding_id} ({report.provider} "
            f"{report.version_from} -> {report.version_to})",
            f"Files: {', '.join(report.files_modified) or 'none'}",
            f"Tests: `{report.test_command}` exit 0",
            f"Audit: {report.audit_path}",
        ]
        if report.pr_url:
            lines.append(f"PR: {report.pr_url}")
        else:
            lines.append("Raise PR? [y/N] (pass --create-pr to open automatically)")
        return "\n".join(lines)
    return (
        "Hunt could not safely verify this repair.\n"
        "No PR was created.\n"
        f"Reason: {report.reason}\n"
        + (f"Evidence: {report.evidence_summary[:800]}\n" if report.evidence_summary else "")
        + f"Audit: {report.audit_path}"
    )


def render_diff_preview(diff: str, limit: int = 40) -> str:
    lines = (diff or "").splitlines()[:limit]
    return "\n".join(f"  {x}" for x in lines)


# Re-exported intentionally narrow surface: HuntBot delegates here.
__all__ = [
    "HuntFinding",
    "HuntContext",
    "HuntReasoning",
    "HuntReport",
    "FileIntent",
    "MAX_ITERATIONS",
    "finding_id_for",
    "list_findings",
    "persist_findings",
    "resolve_finding",
    "gather_context",
    "ai_reason",
    "ai_interpret",
    "create_sandbox",
    "destroy_sandbox",
    "sandbox_changed_vs_repo",
    "run_verification",
    "run_hunt",
    "decide_pr",
    "render_hunt_summary",
    "render_repair_pr_body",
    "default_ports",
    "build_repair_plan",
    "verify_sandbox_binding",
    "verify_promotion",
    "HuntLock",
    "update_audit",
    "AIPlannerAuthor",
    "LocalGitHubPublisher",
]
