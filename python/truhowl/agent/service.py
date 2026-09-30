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


def _redact(text: str) -> str:
    """Central scrubber for anything headed into persisted state."""
    from truhowl.redact import redact_secrets
    clean, _ = redact_secrets(str(text or ""))
    return clean


def _latest_evidence(store: AgentStore, case_id: str, repo_key: str) -> dict[str, Any] | None:
    return next((e for e in reversed(store.evidence)
                 if e.get("case_id") == case_id and e.get("repo_key") == repo_key), None)


def _verified_evidence_ok(evidence: dict[str, Any]) -> bool:
    """The one contract, applied to persisted evidence."""
    from truhowl.verification.contract import check_verification_contract

    return check_verification_contract(
        test_command=str(evidence.get("test_command") or ""),
        test_exit_code=int(evidence.get("test_exit_code", -1)),
        replay_command=str(evidence.get("replay_command") or ""),
        replay_exit_code=int(evidence.get("replay_exit_code", -1)),
        patch_hash=str(evidence.get("patch_hash") or ""),
        scope_ok=bool(evidence.get("blast_radius_zero")),
        candidate_files=len(evidence.get("diff_files") or []) or None,
    ).ok


def deliver_verified(workspace: str, case_id: str, repo_key: str,
                     github_repo: str | None = None) -> dict[str, Any]:
    """Publish an already-verified migration. Never re-repairs, never downgrades.

    This is the re-entry point for delivery: a verified migration whose PR
    was blocked by missing credentials can be published later without
    repeating (or re-litigating) the verification. It refuses when the
    persisted evidence does not satisfy the same verification contract.
    """
    from truhowl.delivery.service import PUBLISHED, publish_verified

    store, raw = _load_case(workspace, case_id)
    entry = _entry(raw, repo_key)
    if entry.get("state") not in (VERIFIED, PR_READY):
        raise ValueError(
            f"Repository {repo_key} is {entry.get('state')}; only a verified "
            "migration can be delivered.")
    evidence = _latest_evidence(store, case_id, repo_key)
    if evidence is None or not _verified_evidence_ok(evidence):
        entry["refusal_reason"] = ("Delivery refused: persisted verification evidence "
                                   "does not satisfy the verification contract.")
        if entry.get("state") == VERIFIED:
            _set_state(entry, NEEDS_ATTENTION)
        store.refusals.append({"case_id": case_id, "repo_key": repo_key,
                               "reason": entry["refusal_reason"],
                               "evidence_summary": "delivery contract check failed",
                               "recorded_at": _now()})
        save_store(workspace, store)
        return {"repo_key": repo_key, "state": entry.get("state"),
                "reason": entry["refusal_reason"], "delivery": {}}

    files = [f for f in (evidence.get("diff_files") or []) if f]
    repo_dir = entry.get("path") or workspace
    if github_repo and not github_repo.strip():
        github_repo = None
    if not github_repo:
        github_repo = str(raw.get("github_repo") or "") or None
    if not files:
        entry["refusal_reason"] = "Delivery refused: no repaired files recorded."
        save_store(workspace, store)
        return {"repo_key": repo_key, "state": entry.get("state"),
                "reason": entry["refusal_reason"], "delivery": {}}

    from truhowl.github.trust_pr import TrustPRMetadata, generate_trust_pr_markdown

    test_command = str(evidence.get("test_command") or "")
    meta = TrustPRMetadata(
        provider_name=str(raw.get("provider") or ""),
        from_version=str(raw.get("version_from") or ""),
        to_version=str(raw.get("version_to") or ""),
        changelog_url=str(raw.get("guide_url") or ""),
        files_modified=int(evidence.get("files_modified") or len(files)),
        files_scanned=int(evidence.get("files_scanned") or 0),
        unintended_files_modified=int(evidence.get("unintended_files_modified") or 0),
        quarantined_callsites_count=0,
        unified_diff=str(evidence.get("unified_diff") or ""),
        test_command=test_command,
        test_exit_code=int(evidence.get("test_exit_code") or 0),
        test_duration_ms=int(evidence.get("test_duration_ms") or 0),
        lockfile_hash="",
        patch_hash=str(evidence.get("patch_hash") or ""),
        semantic_score=1.0,
        drift_reason=str(raw.get("summary") or ""),
        impacted_callsites=[{"file_path": f, "description": "verified migration"}
                            for f in files],
    )
    result = publish_verified(
        repo_dir=repo_dir, provider_display=str(raw.get("provider") or ""),
        version_from=str(raw.get("version_from") or ""),
        version_to=str(raw.get("version_to") or ""),
        modified_paths=[os.path.join(repo_dir, f) for f in files],
        rules=["verified migration (evidence-backed delivery retry)"],
        trust_pr_body=generate_trust_pr_markdown(meta),
        github_repo=github_repo, github_client=None,
    )
    store.delivery[f"{case_id}\x00{repo_key}"] = {
        "status": result.status, "error": result.error,
        "pr_url": result.pr_url, "pr_number": result.pr_number,
        "updated_at": _now()}
    if result.status == PUBLISHED and result.pr_url:
        entry["pr_url"] = result.pr_url
        entry["pr_number"] = result.pr_number
        store.pull_requests.append({"case_id": case_id, "repo_key": repo_key,
                                    "url": result.pr_url, "number": result.pr_number,
                                    "state": "open" if result.pr_number else "ready",
                                    "recorded_at": _now()})
        if entry.get("state") == VERIFIED:
            _set_state(entry, PR_READY)
    raw["updated_at"] = _now()
    save_store(workspace, store)
    return {"repo_key": repo_key, "state": entry.get("state"),
            "pr_url": entry.get("pr_url"), "reason": entry.get("refusal_reason", ""),
            "delivery": store.delivery[f"{case_id}\x00{repo_key}"]}


def run_repo(workspace: str, case_id: str, repo_key: str,
             create_pr: bool = False, github_repo: str | None = None,
             llm_api_key: str | None = None, llm_model: str | None = None,
             llm_base_url: str | None = None,
             confirmed: bool = False) -> dict[str, Any]:
    """Run the full lifecycle for one repository in a case.

    Planning uses the deterministic migration planner; repair and
    verification run through the unchanged `run_maintenance_cycle` engine,
    so every guarantee (sandbox, blast radius, replay, fail-closed) holds.

    Automation policy is the ceiling:
      * OBSERVE refuses to repair unless the operator confirms this one run;
      * PREPARE repairs and verifies but can never publish;
      * DELIVER publishes only after the same verification passes.
    """
    from truhowl.agent import automation
    from truhowl.maintenance import run_maintenance_cycle
    from truhowl.migration_plan import build_migration_plan
    from truhowl.test_runner import _detect_test_command

    store, raw = _load_case(workspace, case_id)
    entry = _entry(raw, repo_key)
    path = entry.get("path") or workspace
    provider = raw["provider"]
    version_from, version_to = raw["version_from"], raw["version_to"]

    # Already verified: requesting delivery must re-attempt delivery only.
    # Re-running the repair would re-litigate a settled verification.
    if entry.get("state") in (VERIFIED, PR_READY) and create_pr:
        mode_ceiling = automation.get_mode(workspace)
        if not automation.allows_publish(mode_ceiling):
            return {"repo_key": repo_key, "state": entry["state"],
                    "reason": automation.refusal_reason(mode_ceiling, "publication"),
                    "delivery": store.delivery.get(f"{case_id}\x00{repo_key}", {})}
        return deliver_verified(workspace, case_id, repo_key, github_repo)

    mode = automation.get_mode(workspace)
    if not automation.allows_repair(mode) and not confirmed:
        _set_state(entry, ANALYZING)
        entry["refusal_reason"] = automation.refusal_reason(mode, "repair")
        _set_state(entry, NEEDS_ATTENTION)
        store.refusals.append({"case_id": case_id, "repo_key": repo_key,
                               "reason": entry["refusal_reason"],
                               "evidence_summary": f"automation mode {mode}",
                               "recorded_at": _now()})
        raw["updated_at"] = _now()
        save_store(workspace, store)
        return {"repo_key": repo_key, "state": NEEDS_ATTENTION,
                "reason": entry["refusal_reason"], "delivery": {}}
    if not automation.allows_publish(mode):
        # The policy ceiling outranks the caller's flag. Requesting a PR
        # under PREPARE/OBSERVE is not an error; it is simply not granted.
        create_pr = False

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
    replay_ok = (bool(report.replay_command)
                 and int(report.replay_exit_code) == 0
                 and bool(report.patch_hash))
    store.evidence.append({
        "case_id": case_id, "repo_key": repo_key, "test_command": test_command,
        "test_exit_code": int(report.test_exit_code),
        "test_duration_ms": int(report.test_duration_ms),
        "files_modified": int(report.files_modified),
        "unintended_files_modified": int(report.unintended_files_modified),
        "blast_radius_zero": bool(report.blast_radius_verified),
        "replay_exit_code": int(report.replay_exit_code),
        "replay_command": report.replay_command,
        "patch_hash": report.patch_hash,
        "verification_tier": report.verification_tier,
        "diff_files": [os.path.relpath(p, path) if os.path.isabs(p) else p
                       for p in [r.file_path for r in (report.patch_results or []) if r.success]],
        # Redacted at persistence: the store is an audit surface, and a secret
        # in repair context must not survive into it.
        "unified_diff": _redact(report.unified_diff or ""),
        "files_scanned": int(getattr(report, "files_scanned", 0) or 0),
        "recorded_at": _now(),
    })

    # No VERIFIED without canonical replay evidence — a fabricated or
    # bypassed report can never mint verified state.
    if (bool(report.success) and bool(report.blast_radius_verified)
            and int(report.test_exit_code) == 0 and replay_ok):
        store.verified.append({"case_id": case_id, "repo_key": repo_key,
                               "files_modified": int(report.files_modified),
                               "test_exit_code": int(report.test_exit_code),
                               "verified_at": _now()})
        _set_state(entry, VERIFIED)
        delivery_key = f"{case_id}\x00{repo_key}"
        delivery = {"status": report.delivery_status or "not-requested",
                    "error": report.delivery_error or "",
                    "pr_url": report.pr_url, "pr_number": report.pr_number,
                    "updated_at": _now()}
        store.delivery[delivery_key] = delivery
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
        # Reasons are persisted and later surfaced by `ask` / `agent show`.
        # Scrub centrally so an upstream error body cannot carry a secret
        # into the store or into an explanation.
        from truhowl.redact import redact_secrets
        entry["refusal_reason"], _ = redact_secrets(str(entry["refusal_reason"]))
        store.refusals.append({"case_id": case_id, "repo_key": repo_key,
                               "reason": entry["refusal_reason"],
                               "evidence_summary": (f"tests exit {report.test_exit_code}, "
                                                    f"{report.files_modified} files modified, "
                                                    f"blast-radius-zero={report.blast_radius_verified}"),
                               "recorded_at": _now()})
    raw["updated_at"] = _now()
    save_store(workspace, store)
    return {"repo_key": repo_key, "state": entry["state"],
            "pr_url": entry.get("pr_url"), "reason": entry.get("refusal_reason", ""),
            "delivery": store.delivery.get(f"{case_id}\x00{repo_key}", {})}


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
        # A verified migration whose delivery is blocked is still verified —
        # it is never downgraded — but it is waiting on an operator.
        for r in c.get("repos", []):
            delivery = store.delivery.get(f"{c['case_id']}\x00{r.get('repo_key')}", {})
            if delivery.get("status") in ("blocked-auth", "failed"):
                stuck[r["repo_key"]] = {
                    "state": r.get("state"),
                    "reason": f"delivery {delivery['status']}: {delivery.get('error', '')}",
                    "delivery": delivery.get("status"),
                }
        if stuck:
            out.append({"case_id": c["case_id"], "provider": c.get("provider"),
                        "version_from": c.get("version_from"),
                        "version_to": c.get("version_to"), "repos": stuck})
    return out


def explain_case(workspace: str, case_id: str) -> str:
    """Explain a case entirely from persisted evidence.

    Output is redacted before it reaches a terminal, log, GitHub issue or
    LLM prompt: persisted state can contain upstream failure text, and text
    is untrusted until it has been through the central scrubber.
    """
    from truhowl.redact import redact_secrets

    store = load_store(workspace)
    raw = store.cases.get(case_id)
    if raw is None:
        return f"Unknown migration case: {case_id}"
    lines = [f"{raw['provider']} {raw['version_from']} -> {raw['version_to']} (case {case_id})"]
    for r in raw.get("repos", []):
        lines.append(f"  [{r.get('state')}] {r.get('repo_key')}")
        delivery = store.delivery.get(f"{case_id}\x00{r.get('repo_key')}")
        if delivery and delivery.get("status") not in ("not-requested",):
            dline = f"    delivery: {delivery['status']}"
            if delivery.get("error"):
                dline += f": {delivery['error'][:200]}"
            elif delivery.get("pr_url"):
                dline += f": {delivery['pr_url']}"
            lines.append(dline)
        ev = next((e for e in reversed(store.evidence)
                   if e.get("case_id") == case_id and e.get("repo_key") == r.get("repo_key")), None)
        if ev:
            lines.append(f"    tests: {ev.get('test_command') or 'none'} exit {ev.get('test_exit_code')}, "
                         f"{ev.get('files_modified')} files modified, "
                         f"blast-radius-zero={ev.get('blast_radius_zero')}")
            replay_cmd = ev.get("replay_command") or ""
            replay_exit = ev.get("replay_exit_code", -1)
            if replay_cmd:
                state = "passed" if int(replay_exit) == 0 else f"failed (exit {replay_exit})"
                lines.append(f"    clean-room replay: {state} [{replay_cmd}]")
            else:
                lines.append("    clean-room replay: NOT RUN (cannot be verified without it)")
            lines.append(f"    verification tier: {ev.get('verification_tier') or 'unverified'}"
                         f"  candidate hash: {(ev.get('patch_hash') or '')[:12] or 'none'}")
        if r.get("refusal_reason"):
            lines.append(f"    reason: {r['refusal_reason']}")
        if r.get("pr_url"):
            lines.append(f"    PR: {r['pr_url']}")
    text = "\n".join(lines)
    clean, _ = redact_secrets(text)
    return clean
