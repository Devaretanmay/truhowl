"""Truhowl agent orchestration service.

Capabilities (scanner, repair engine, verifier, GitHub) sit beneath the
agent. The service drives the lifecycle:

    discover external change -> calculate impact -> create migration case
    -> generate MigrationPlan -> run repair engine -> verify
    -> record evidence -> prepare action

All verification guarantees live in the repair engine
(`run_maintenance_cycle`); the service never weakens them, it only
orchestrates and records.
"""

from __future__ import annotations

import os
from typing import Any

from truhowl.agent.models import (
    ACTIONABLE_STATES,
    ANALYZING,
    DETECTED,
    NEEDS_ATTENTION,
    PLANNING,
    PR_READY,
    REFUSED,
    REPAIRING,
    VERIFIED,
    VERIFYING,
    AgentStore,
    CaseRepo,
    ExternalChange,
    MigrationCase,
    _now,
    case_id_for,
    load_store,
    save_store,
)
from truhowl.config import find_workspace_root


def resolve_workspace(path: str | None = None) -> str:
    return find_workspace_root(path) or os.path.abspath(path or ".")


def _slug_key(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (value or "").lower()).strip("-")


def _register_repos(store: AgentStore, repo_paths: list[str]) -> list[tuple[str, str]]:
    """Ensure store repos exist for paths. Returns [(repo_key, abs_path)]."""
    org = store.org
    if not org.get("name"):
        org["name"] = "local"
    out: list[tuple[str, str]] = []
    for p in repo_paths:
        abs_p = os.path.abspath(p)
        key = _slug_key(os.path.basename(abs_p)) or "repo"
        # Disambiguate duplicate basenames.
        base, i = key, 2
        existing_paths = {k: v.get("path") for k, v in store.repos.items()}
        while key in store.repos and existing_paths.get(key) != abs_p:
            key = f"{base}-{i}"
            i += 1
        store.repos[key] = {"key": key, "path": abs_p, "providers": []}
        if key not in org.setdefault("repo_keys", []):
            org["repo_keys"].append(key)
        out.append((key, abs_p))
    return out


def discover_external_change(
    workspace: str,
    provider: str,
    version_from: str,
    version_to: str,
    repo_paths: list[str] | None = None,
    basis: str = "registry metadata (change not independently observed)",
) -> dict[str, Any]:
    """Record an external change and calculate per-repository impact.

    Read-only against the repositories: no repairs, no writes outside the
    agent store.
    """
    from truhowl.intelligence import resolve_migration
    from truhowl.maintenance_agents import analyze_impact

    store = load_store(workspace)
    try:
        _from, _to, migration = resolve_migration(provider, version_from, version_to)
    except Exception:
        _from, _to, migration = version_from, version_to, None
    guide_url = getattr(migration, "changelog_url", "") or ""
    summary = getattr(migration, "description", "") or f"{provider} {version_from} -> {version_to}"

    paths = repo_paths if repo_paths is not None else [workspace]
    pairs = _register_repos(store, paths)

    change = ExternalChange(
        change_id=f"{_slug_key(provider)}-{version_from}-{version_to}",
        provider=provider,
        version_from=_from or version_from,
        version_to=_to or version_to,
        basis=basis,
        guide_url=guide_url,
        detected_at=_now(),
    )
    store.changes = [c for c in store.changes if c.get("change_id") != change.change_id]
    store.changes.append({
        "change_id": change.change_id, "provider": change.provider,
        "version_from": change.version_from, "version_to": change.version_to,
        "basis": change.basis, "guide_url": change.guide_url,
        "detected_at": change.detected_at,
    })

    usages: list[dict[str, Any]] = []
    for key, abs_p in pairs:
        try:
            impact = analyze_impact(abs_p, provider)
            files = list(impact.affected_files or [])
            count = int(impact.callsites_count or 0)
            wrappers = list(impact.wrapper_files or [])
        except Exception:
            files, count, wrappers = [], 0, []
        store.usages = [u for u in store.usages
                        if not (u.get("repo_key") == key
                                and str(u.get("provider", "")).lower() == provider.lower())]
        store.usages.append({"repo_key": key, "provider": provider, "files": files,
                             "callsites_count": count, "wrappers": wrappers})
        usages.append({"repo_key": key, "path": abs_p, "files": files,
                       "callsites_count": count, "wrappers": wrappers})
        repo = store.repos.get(key, {})
        provs = repo.get("providers", [])
        if provider not in provs:
            provs.append(provider)
        repo["providers"] = provs
        store.repos[key] = repo

    save_store(workspace, store)
    return {"change_id": change.change_id, "provider": provider,
            "version_from": change.version_from, "version_to": change.version_to,
            "guide_url": guide_url, "summary": summary, "usages": usages}


def create_case(workspace: str, change_id: str) -> MigrationCase:
    """Create a MigrationCase from a discovered external change."""
    store = load_store(workspace)
    change = next((c for c in store.changes if c.get("change_id") == change_id), None)
    if change is None:
        raise ValueError(f"Unknown external change: {change_id}")
    provider = change["provider"]
    case_id = case_id_for(provider, change["version_from"], change["version_to"])
    usages = [u for u in store.usages
              if str(u.get("provider", "")).lower() == provider.lower()]
    repos = []
    for u in usages:
        path = (store.repos.get(u["repo_key"], {}) or {}).get("path", "")
        repos.append(CaseRepo(repo_key=u["repo_key"], path=path, state=DETECTED, updated_at=_now()))
    case = MigrationCase(
        case_id=case_id, provider=provider,
        version_from=change["version_from"], version_to=change["version_to"],
        guide_url=change.get("guide_url", ""),
        summary=f"{provider} {change['version_from']} -> {change['version_to']}",
        repos=repos, created_at=_now(), updated_at=_now(),
    )
    store.cases[case_id] = {
        "case_id": case.case_id, "provider": case.provider,
        "version_from": case.version_from, "version_to": case.version_to,
        "guide_url": case.guide_url, "summary": case.summary,
        "repos": [{"repo_key": r.repo_key, "path": r.path, "state": r.state,
                   "attempts": r.attempts, "refusal_reason": r.refusal_reason,
                   "pr_url": r.pr_url, "pr_number": r.pr_number,
                   "updated_at": r.updated_at} for r in repos],
        "created_at": case.created_at, "updated_at": case.updated_at,
    }
    save_store(workspace, store)
    return case


def watch(workspace: str, provider: str, version_from: str, version_to: str,
          repo_paths: list[str] | None = None) -> MigrationCase:
    """Discover a change, calculate impact, and create its case. No repairs."""
    found = discover_external_change(workspace, provider, version_from, version_to, repo_paths)
    return create_case(workspace, found["change_id"])


def _load_case(workspace: str, case_id: str) -> tuple[AgentStore, dict[str, Any]]:
    store = load_store(workspace)
    raw = store.cases.get(case_id)
    if raw is None:
        raise ValueError(f"Unknown migration case: {case_id}")
    return store, raw


def _entry(raw_case: dict[str, Any], repo_key: str) -> dict[str, Any]:
    for r in raw_case.get("repos", []):
        if r.get("repo_key") == repo_key:
            return r
    raise ValueError(f"Repository {repo_key} not in case {raw_case.get('case_id')}")


def _set_state(entry: dict[str, Any], to_state: str) -> None:
    from truhowl.agent.models import _TRANSITIONS
    allowed = _TRANSITIONS.get(entry.get("state", DETECTED), frozenset())
    if to_state not in allowed:
        raise ValueError(f"Illegal transition {entry.get('state')} -> {to_state}")
    entry["state"] = to_state
    entry["updated_at"] = _now()


def run_repo(workspace: str, case_id: str, repo_key: str,
             create_pr: bool = False, github_repo: str | None = None,
             llm_api_key: str | None = None, llm_model: str | None = None,
             llm_base_url: str | None = None) -> dict[str, Any]:
    """Run the full lifecycle for one repository in a case.

    Planning uses the deterministic migration planner; repair and
    verification run through the unchanged `run_maintenance_cycle` engine,
    so every guarantee (sandbox, blast radius, fail-closed) holds.
    """
    from truhowl.maintenance import run_maintenance_cycle
    from truhowl.migration_plan import build_migration_plan
    from truhowl.test_runner import _detect_test_command

    store, raw = _load_case(workspace, case_id)
    entry = _entry(raw, repo_key)
    path = entry.get("path") or workspace
    provider = raw["provider"]
    version_from, version_to = raw["version_from"], raw["version_to"]

    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    _set_state(entry, ANALYZING)

    usage = next((u for u in store.usages
                  if u.get("repo_key") == repo_key
                  and str(u.get("provider", "")).lower() == provider.lower()), None)
    affected = list((usage or {}).get("files", []) or [])
    if not affected:
        entry["refusal_reason"] = "No affected usage detected — nothing to migrate."
        _set_state(entry, REFUSED)
        store.refusals.append({"case_id": case_id, "repo_key": repo_key,
                               "reason": entry["refusal_reason"],
                               "evidence_summary": "impact analysis found 0 files",
                               "recorded_at": _now()})
        raw["updated_at"] = _now()
        save_store(workspace, store)
        return {"repo_key": repo_key, "state": REFUSED, "reason": entry["refusal_reason"]}

    _set_state(entry, PLANNING)
    try:
        plan = build_migration_plan(path, provider, version_from, version_to,
                                    summary=raw.get("summary", ""),
                                    guide_url=raw.get("guide_url", ""),
                                    affected_files=affected)
        complete = bool(getattr(getattr(plan, "completeness", None), "complete", False))
        store.plans.append({"case_id": case_id, "repo_key": repo_key,
                            "plan": plan.to_dict(), "complete": complete,
                            "recorded_at": _now()})
    except Exception as exc:
        entry["refusal_reason"] = f"Planning failed: {exc}"
        _set_state(entry, REFUSED)
        raw["updated_at"] = _now()
        save_store(workspace, store)
        return {"repo_key": repo_key, "state": REFUSED, "reason": entry["refusal_reason"]}

    _set_state(entry, REPAIRING)
    store.attempts.append({"case_id": case_id, "repo_key": repo_key,
                           "attempt": entry["attempts"], "strategy": "maintenance-cycle",
                           "success": False, "test_exit_code": -1, "started_at": _now()})
    report = run_maintenance_cycle(
        repo_dir=path, provider_name=provider,
        from_version=version_from, to_version=version_to,
        create_pr=create_pr, github_repo=github_repo,
        llm_api_key=llm_api_key, llm_model=llm_model, llm_base_url=llm_base_url,
    )

    _set_state(entry, VERIFYING)
    try:
        test_command = _detect_test_command(path) or ""
    except Exception:
        test_command = ""
    store.attempts[-1].update({"success": bool(report.success),
                               "test_exit_code": int(report.test_exit_code)})
    store.evidence.append({
        "case_id": case_id, "repo_key": repo_key, "test_command": test_command,
        "test_exit_code": int(report.test_exit_code),
        "test_duration_ms": int(report.test_duration_ms),
        "files_modified": int(report.files_modified),
        "unintended_files_modified": int(report.unintended_files_modified),
        "blast_radius_zero": bool(report.blast_radius_verified),
        "diff_files": [os.path.relpath(p, path) if os.path.isabs(p) else p
                       for p in [r.file_path for r in (report.patch_results or []) if r.success]],
        "recorded_at": _now(),
    })

    if bool(report.success) and bool(report.blast_radius_verified) and int(report.test_exit_code) == 0:
        store.verified.append({"case_id": case_id, "repo_key": repo_key,
                               "files_modified": int(report.files_modified),
                               "test_exit_code": int(report.test_exit_code),
                               "verified_at": _now()})
        _set_state(entry, VERIFIED)
        if report.pr_url:
            entry["pr_url"] = report.pr_url
            entry["pr_number"] = report.pr_number
            store.pull_requests.append({"case_id": case_id, "repo_key": repo_key,
                                        "url": report.pr_url, "number": report.pr_number,
                                        "state": "open" if report.pr_number else "ready",
                                        "recorded_at": _now()})
            _set_state(entry, PR_READY)
    else:
        if not test_command:
            entry["refusal_reason"] = ("No test command detected — migration cannot be "
                                       "verified. Add tests, then retry.")
            _set_state(entry, NEEDS_ATTENTION)
        else:
            entry["refusal_reason"] = (report.error
                                       or f"Verification failed (exit {report.test_exit_code}).")
            _set_state(entry, REFUSED)
        store.refusals.append({"case_id": case_id, "repo_key": repo_key,
                               "reason": entry["refusal_reason"],
                               "evidence_summary": (f"tests exit {report.test_exit_code}, "
                                                    f"{report.files_modified} files modified, "
                                                    f"blast-radius-zero={report.blast_radius_verified}"),
                               "recorded_at": _now()})
    raw["updated_at"] = _now()
    save_store(workspace, store)
    return {"repo_key": repo_key, "state": entry["state"],
            "pr_url": entry.get("pr_url"), "reason": entry.get("refusal_reason", "")}


def run_case(workspace: str, case_id: str, repo_keys: list[str] | None = None,
             **kwargs: Any) -> list[dict[str, Any]]:
    """Run the lifecycle for every (or selected) repository in a case."""
    store = load_store(workspace)
    raw = store.cases.get(case_id)
    if raw is None:
        raise ValueError(f"Unknown migration case: {case_id}")
    targets = repo_keys or [r["repo_key"] for r in raw.get("repos", [])]
    return [run_repo(workspace, case_id, key, **kwargs) for key in targets]


def get_case(workspace: str, case_id: str) -> dict[str, Any] | None:
    return load_store(workspace).cases.get(case_id)


def list_cases(workspace: str, state: str | None = None) -> list[dict[str, Any]]:
    store = load_store(workspace)
    out = []
    for c in store.cases.values():
        states = [r.get("state") for r in c.get("repos", [])]
        if state and state not in states:
            continue
        out.append({"case_id": c["case_id"], "provider": c.get("provider"),
                    "version_from": c.get("version_from"),
                    "version_to": c.get("version_to"),
                    "repo_states": {r["repo_key"]: r.get("state") for r in c.get("repos", [])},
                    "updated_at": c.get("updated_at")})
    return out


def cases_needing_attention(workspace: str) -> list[dict[str, Any]]:
    store = load_store(workspace)
    out = []
    for c in store.cases.values():
        stuck = {r["repo_key"]: {"state": r.get("state"), "reason": r.get("refusal_reason", "")}
                 for r in c.get("repos", [])
                 if r.get("state") in ACTIONABLE_STATES}
        if stuck:
            out.append({"case_id": c["case_id"], "provider": c.get("provider"),
                        "version_from": c.get("version_from"),
                        "version_to": c.get("version_to"), "repos": stuck})
    return out


def explain_case(workspace: str, case_id: str) -> str:
    """Explain a case entirely from persisted evidence."""
    store = load_store(workspace)
    raw = store.cases.get(case_id)
    if raw is None:
        return f"Unknown migration case: {case_id}"
    lines = [f"{raw['provider']} {raw['version_from']} -> {raw['version_to']} (case {case_id})"]
    for r in raw.get("repos", []):
        lines.append(f"  [{r.get('state')}] {r.get('repo_key')}")
        ev = next((e for e in reversed(store.evidence)
                   if e.get("case_id") == case_id and e.get("repo_key") == r.get("repo_key")), None)
        if ev:
            lines.append(f"    tests: {ev.get('test_command') or 'none'} exit {ev.get('test_exit_code')}, "
                         f"{ev.get('files_modified')} files modified, "
                         f"blast-radius-zero={ev.get('blast_radius_zero')}")
        if r.get("refusal_reason"):
            lines.append(f"    reason: {r['refusal_reason']}")
        if r.get("pr_url"):
            lines.append(f"    PR: {r['pr_url']}")
    return "\n".join(lines)
