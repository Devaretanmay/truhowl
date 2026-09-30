
"""Continuous Autonomous API Maintenance Loop Engine."""

from dataclasses import dataclass, field
import json
import os
import subprocess
import time
from typing import Any

from truhowl.ai_planner import AIPatchPlanner, build_reasoning_context
from truhowl.drift import detect_drift  # noqa: F401
from truhowl.formatters import run_style_formatter
from truhowl.github.trust_pr import generate_trust_pr_markdown, TrustPRMetadata
from truhowl.git_ops import git_commit_and_push, gh_create_pr
from truhowl.maintenance_agents import analyze_impact
from truhowl.patch_writer import (
    discover_aliases, instantiate_alias_rules, PatchResult,
)
from truhowl.providers.registry import get_default_registry
from truhowl.sandbox.snapshot import SnapshotManager, _file_hash

from truhowl.intelligence import TruhowlIntelligence, resolve_migration
from truhowl.knowledge import direct_rewrites_for, record_failure, upsert_learned as kb_upsert
from truhowl.test_runner import (
    _detect_test_command,
    _compute_lockfile_hash,
    _run_install,
    _run_tests,
)
from blake3 import blake3

@dataclass
class MaintenanceRunReport:
    success: bool
    provider_name: str
    from_version: str
    to_version: str
    repository_path: str
    files_scanned: int
    files_modified: int
    unintended_files_modified: int
    blast_radius_verified: bool
    test_exit_code: int
    test_duration_ms: int
    unified_diff: str
    trust_pr_body: str
    patch_results: list[PatchResult] = field(default_factory=list)
    pr_url: str | None = None
    pr_number: int | None = None
    error: str | None = None
    repair_path: str = "none"




def record_migration_history(repo_dir: str, record: dict[str, Any]) -> None:
    """Record an auditable verified migration event into the repository history ledger."""
    history_dir = os.path.join(repo_dir, ".truhowl")
    os.makedirs(history_dir, exist_ok=True)
    history_file = os.path.join(history_dir, "history.json")

    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append(record)
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2)


def get_migration_history(repo_dir: str) -> list[dict[str, Any]]:
    """Retrieve verified migration history records from .truhowl/history.json."""
    history_file = os.path.join(repo_dir, ".truhowl", "history.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def run_maintenance_cycle(
    repo_dir: str,
    provider_name: str,
    from_version: str | None = None,
    to_version: str | None = None,
    create_pr: bool = False,
    github_repo: str | None = None,
    github_client: Any = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    **_ignored: Any,
) -> MaintenanceRunReport:
    """Execute full autonomous maintenance loop on a repository with AI-authored repairs."""
    repo_dir = os.path.abspath(repo_dir)
    registry = get_default_registry()
    p_spec = registry.get(provider_name)
    if not p_spec:
        return MaintenanceRunReport(
            success=False, provider_name=provider_name,
            from_version=from_version or "unknown", to_version=to_version or "unknown",
            repository_path=repo_dir, files_scanned=0, files_modified=0,
            unintended_files_modified=0, blast_radius_verified=False,
            test_exit_code=-1, test_duration_ms=0, unified_diff="",
            trust_pr_body="", error=f"Provider {provider_name} not found in registry",
        )

    actual_from, actual_to, migration = resolve_migration(provider_name, from_version, to_version)
    changelog_url = migration.changelog_url if migration else p_spec.docs_url
    rewrites = migration.rewrites if migration else []

    snapshot_dir = os.path.join(repo_dir, ".truhowl", "snapshot_tmp")
    snapshotter = SnapshotManager(workdir=repo_dir, snapshot_dir=snapshot_dir)
    files_scanned = snapshotter.snapshot()

    intel = TruhowlIntelligence()
    decision = intel.decide(repo_dir, provider_name, actual_from, actual_to, has_rewrites=bool(rewrites))

    patch_results: list[PatchResult] = []
    ai_planner = None
    quarantine_error: str | None = None
    applied_rewrites = list(rewrites)

    if decision.strategy == "AI":
        ai_planner = AIPatchPlanner.from_env(api_key=llm_api_key, model=llm_model, base_url=llm_base_url)
        if ai_planner is None:
            quarantine_error = (
                "AI repair required but no AI provider is configured. "
                "Run `truhowl auth` or set GROQ_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY."
            )
        else:
            impact = analyze_impact(repo_dir, provider_name)
            target_files = set(impact.affected_files)
            if not target_files:
                for dirpath, dirnames, filenames in os.walk(repo_dir):
                    dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".next", "__pycache__", ".truhowl"}]
                    for fn in filenames:
                        if fn.endswith((".ts", ".js", ".py", ".rs", ".go")):
                            fp = os.path.join(dirpath, fn)
                            try:
                                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                                    content = f.read()
                                if provider_name.lower() in content.lower():
                                    target_files.add(os.path.relpath(fp, repo_dir))
                            except Exception:
                                pass
            if target_files:
                migration_desc = migration.description if migration else f"Upgrade {provider_name} to {actual_to}"
                kb_rules = direct_rewrites_for(repo_dir, provider_name, actual_from, actual_to)
                base_rules = list(applied_rewrites or kb_rules)
                aliases = discover_aliases(repo_dir, provider_name)
                if aliases:
                    seen_p = {r.pattern for r in base_rules}
                    for ar in instantiate_alias_rules(base_rules, aliases):
                        if ar.pattern not in seen_p:
                            seen_p.add(ar.pattern)
                            base_rules.append(ar)
                if base_rules:
                    rules_summary = "\n".join(f"- {r.description}: {r.pattern} -> {r.replacement}" for r in base_rules)
                    migration_desc += f"\n\nContract pattern evidence (use as architectural guide):\n{rules_summary}"
                reason_ctx = build_reasoning_context(
                    repo_dir, provider_name, actual_from, actual_to,
                    migration_desc, changelog_url)
                ai_results = ai_planner.plan_and_apply(
                    repo_dir=repo_dir,
                    affected_files=sorted(list(target_files)),
                    provider_name=provider_name,
                    from_version=actual_from,
                    to_version=actual_to,
                    migration_details=migration_desc,
                    dry_run=False,
                    context=reason_ctx,
                    changelog_url=changelog_url,
                )
                if ai_results:
                    patch_results.extend(ai_results)
    else:
        quarantine_error = (
            f"No safe repair path for {provider_name} {actual_from}->{actual_to} ({decision.reason}). "
            "Run `truhowl auth` to enable AI repair, or add a verified migration to the registry."
        )

    modified_paths = [os.path.abspath(r.file_path) for r in patch_results if r.success]
    files_modified = len(modified_paths)
    unified_diff = "\n".join(r.unified_diff for r in patch_results if r.unified_diff)

    run_style_formatter(repo_dir, modified_paths)

    all_changed: set[str] = set()
    targeted: set[str] = set(modified_paths)
    for dirpath, dirnames, filenames in os.walk(repo_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".next", "__pycache__", ".truhowl"}]
        for fn in filenames:
            fp = os.path.abspath(os.path.join(dirpath, fn))
            try:
                snap_hash = snapshotter._snapshot_dir
                rel = os.path.relpath(fp, repo_dir)
                snap_copy = os.path.join(snap_hash, rel)
                if os.path.exists(snap_copy):
                    if _file_hash(fp) != _file_hash(snap_copy):
                        all_changed.add(fp)
            except Exception:
                pass

    unintended = all_changed - targeted
    unintended_count = len(unintended)
    blast_radius_verified = unintended_count == 0

    test_cmd = _detect_test_command(repo_dir)
    test_exit_code = -1
    test_duration_ms = 0
    raw_output = ""

    if files_modified > 0:
        if test_cmd:
            try:
                _run_install(repo_dir, timeout=120)
            except Exception:
                pass

            test_start = time.time()
            try:
                proc = _run_tests(repo_dir, test_cmd, timeout=120)
                test_exit_code = proc.returncode
                raw_output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
            except subprocess.TimeoutExpired:
                test_exit_code = 1
                raw_output = "Test run timed out after 120s"
            except Exception as exc:
                test_exit_code = 1
                raw_output = str(exc)
            test_duration_ms = max(1, int((time.time() - test_start) * 1000))

        if test_exit_code != 0 and ai_planner and modified_paths:
            retry_results = ai_planner.plan_and_apply(
                repo_dir=repo_dir,
                affected_files=modified_paths,
                provider_name=provider_name,
                from_version=actual_from,
                to_version=actual_to,
                migration_details=migration.description if migration else "",
                test_error=raw_output,
                dry_run=False,
                changelog_url=changelog_url,
            )
            if retry_results:
                decision.strategy = "AI"
                decision.reason = "ai_self_repair"
                decision.confidence = 0.85
                run_style_formatter(repo_dir, modified_paths)
                retry_proc = _run_tests(repo_dir, test_cmd, timeout=120)
                if retry_proc.returncode == 0:
                    test_exit_code = 0
                    patch_results = retry_results
                    unified_diff = "\n".join(r.unified_diff for r in patch_results if r.unified_diff)

    if test_exit_code != 0:
        snapshotter.restore()
        if patch_results:
            record_failure(
                repo_dir, provider_name, actual_from, actual_to,
                f"verification failed ({test_cmd or 'no test command'}, exit {test_exit_code})",
                [os.path.relpath(p, repo_dir) for p in modified_paths],
            )
        files_modified = 0
        unified_diff = ""

    snapshotter.cleanup()
    lockfile_hash = _compute_lockfile_hash(repo_dir)
    patch_hash = blake3(unified_diff.encode("utf-8")).hexdigest()

    all_rules = [desc for r in patch_results for desc in r.rules_applied]
    meta = TrustPRMetadata(
        provider_name=p_spec.display_name,
        from_version=actual_from,
        to_version=actual_to,
        changelog_url=changelog_url,
        files_modified=files_modified,
        files_scanned=files_scanned,
        unintended_files_modified=unintended_count,
        quarantined_callsites_count=0,
        unified_diff=unified_diff,
        test_command=test_cmd,
        test_exit_code=test_exit_code,
        test_duration_ms=test_duration_ms,
        lockfile_hash=lockfile_hash,
        patch_hash=patch_hash,
        semantic_score=1.0,
        impacted_callsites=[{"description": d} for d in all_rules],
    )
    pr_body = generate_trust_pr_markdown(meta)

    success = blast_radius_verified and test_exit_code == 0 and files_modified > 0 and quarantine_error is None

    if success:
        record_migration_history(repo_dir, {
            "migration_id": f"migration:{p_spec.name.lower()}:{actual_to}",
            "provider_name": p_spec.name,
            "from_version": actual_from,
            "to_version": actual_to,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "test_command": test_cmd,
            "test_exit_code": test_exit_code,
            "test_duration_ms": test_duration_ms,
            "patch_sha256": patch_hash,
            "blast_radius_zero": blast_radius_verified,
            "files_modified": modified_paths,
            "strategy": decision.strategy,
            "reason": decision.reason,
        })
        kb_upsert(
            repo_dir,
            provider_name,
            actual_from,
            actual_to,
            applied_rules=all_rules,
            patch_results=patch_results,
            test_command=test_cmd,
            evidence={"patch_hash": patch_hash, "lockfile_hash": lockfile_hash},
            rewrites=applied_rewrites,
        )
    elif quarantine_error and not patch_results:
        pr_body = (
            f"## Truhowl: repair quarantined\n\n{quarantine_error}\n\n"
            f"Provider: {p_spec.display_name} {actual_from} -> {actual_to}\n"
            f"Strategy: {decision.strategy} ({decision.reason})\n"
        )

    pr_url = None
    pr_number = None
    if create_pr and github_repo and files_modified > 0:
        branch_name = f"truhowl/{p_spec.name}-v{actual_to.replace('.', '-')}"
        commit_msg = (
            f"migrate: {p_spec.display_name} {actual_from} -> {actual_to}\n\n"
            f"Detected and patched by Truhowl autonomous maintenance engine.\n"
            f"Rules applied:\n" + "\n".join(f"- {d}" for d in all_rules)
        )
        pushed = git_commit_and_push(repo_dir, modified_paths, branch_name, commit_msg)

        if pushed:
            pr_title = f"truhowl: migrate {p_spec.display_name} {actual_from} -> {actual_to}"
            pr_url = gh_create_pr(github_repo, branch_name, pr_title, pr_body)
            if pr_url:
                try:
                    pr_number = int(pr_url.rstrip("/").split("/")[-1])
                except Exception:
                    pass

        if not pr_url and github_client:
            pr_resp = github_client.create_pull_request(
                repo=github_repo,
                title=f"fix(deps): upgrade {p_spec.display_name} to {actual_to}",
                body=pr_body,
                head_branch=branch_name,
                labels=["truhowl-maintenance", "verified-green"],
            )
            if pr_resp.get("html_url"):
                pr_url = pr_resp["html_url"]
                pr_number = pr_resp.get("number")

    return MaintenanceRunReport(
        success=success,
        provider_name=p_spec.name,
        from_version=actual_from,
        to_version=actual_to,
        repository_path=repo_dir,
        files_scanned=files_scanned,
        files_modified=files_modified,
        unintended_files_modified=unintended_count,
        blast_radius_verified=blast_radius_verified,
        test_exit_code=test_exit_code,
        test_duration_ms=test_duration_ms,
        unified_diff=unified_diff,
        trust_pr_body=pr_body,
        patch_results=patch_results,
        pr_url=pr_url,
        pr_number=pr_number,
        error=quarantine_error,
        repair_path="ai-reasoning" if decision.strategy == "AI" else "none",
    )
