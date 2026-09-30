"""Upstream watch loop: poll -> compare -> discover -> impact -> case.

Conservative by design: polling only creates MigrationCases, never repairs.
One ExternalChange per genuinely new release; failures are recorded, never
raised, so one broken source cannot crash the whole watch cycle.
"""

from __future__ import annotations

from typing import Any, Callable

from truhowl.agent.models import load_store, save_store
from truhowl.changes.sources import (
    UpstreamCheck,
    compare_versions,
    fetch_npm_latest,
    is_major_bump,
    is_valid_version,
    _now,
)


def _provider_packages() -> list[tuple[str, str]]:
    from truhowl.providers.registry import get_default_registry
    out = []
    for spec in get_default_registry().list_providers():
        if spec.package_name:
            out.append((spec.name, spec.package_name))
    return out


def _scrub(text: str) -> str:
    """Registry errors are persisted and later surfaced by `ask`."""
    from truhowl.redact import redact_secrets
    clean, _ = redact_secrets(str(text or ""))
    return clean


def poll_upstream(workspace: str, providers: list[str] | None = None,
                  timeout: int = 15,
                  fetcher: Callable[[str, int], UpstreamCheck] | None = None
                  ) -> dict[str, Any]:
    """Check registries for new releases. Returns releases, failures, errors."""
    fetch = fetcher or fetch_npm_latest
    store = load_store(workspace)
    upstream = store.org.setdefault("upstream", {})
    wanted = {p.lower() for p in (providers or [])}
    releases: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for name, package in _provider_packages():
        if wanted and name.lower() not in wanted and package.lower() not in wanted:
            continue
        state = upstream.setdefault(package, {})
        try:
            check = fetch(package, timeout)
        except Exception as exc:
            check = UpstreamCheck(package=package, ok=False,
                                  error=f"{type(exc).__name__}: {exc}")
        state["checked_at"] = _now()
        if not check.ok or not check.release:
            state["error"] = _scrub(check.error)
            failures.append({"package": package, "provider": name, "error": state["error"]})
            continue
        state.pop("error", None)
        latest = check.release.version
        if not is_valid_version(latest):
            state["error"] = _scrub(f"malformed upstream version: {latest!r}")
            failures.append({"package": package, "provider": name, "error": state["error"]})
            continue
        known = str(state.get("version", "") or "")
        cmp = compare_versions(known, latest) if known else 1
        if cmp is None:
            state["error"] = _scrub(f"malformed version: known={known!r} latest={latest!r}")
            failures.append({"package": package, "provider": name, "error": state["error"]})
            continue
        state["version"] = check.release.version
        state["source"] = check.release.source_url
        if cmp == 1:
            releases.append({"provider": name, "package": package,
                             "previous_version": known,
                             "current_version": check.release.version,
                             "detected_at": check.release.published_at,
                             "source": check.release.source_url,
                             "breaking_candidate": is_major_bump(known, check.release.version) if known else True})
    save_store(workspace, store)
    return {"releases": releases, "failures": failures}


def poll_and_watch(workspace: str, providers: list[str] | None = None,
                   repo_paths: list[str] | None = None, timeout: int = 15,
                   fetcher: Callable[[str, int], UpstreamCheck] | None = None
                   ) -> dict[str, Any]:
    """Poll upstream, then open cases for newly released providers.

    Only creates cases where repositories show affected usage. A release with
    no affected usage records the ExternalChange but opens no case.
    Duplicate releases (change already recorded) open no new case.
    """
    from truhowl.agent import service as agent_svc

    poll = poll_upstream(workspace, providers, timeout, fetcher)
    cases: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rel in poll["releases"]:
        store = agent_svc.load_store(workspace)
        if _case_open(store, rel):
            skipped.append({"provider": rel["provider"], "reason": "case already open"})
            continue
        found = agent_svc.discover_external_change(
            workspace, rel["provider"],
            rel["previous_version"] or rel["current_version"],
            rel["current_version"], repo_paths,
            basis=f"upstream registry ({rel['source']})")
        store = agent_svc.load_store(workspace)
        case = agent_svc.create_case(workspace, found["change_id"])
        affected = [r for r in case.repos
                    if any(u.get("repo_key") == r.repo_key and u.get("files")
                           for u in store.usages)]
        if not affected:
            skipped.append({"provider": rel["provider"], "reason": "no affected usage"})
            continue
        cases.append({"case_id": case.case_id, "provider": rel["provider"],
                      "version_from": case.version_from, "version_to": case.version_to,
                      "repos": [r.repo_key for r in case.repos]})
    return {"releases": poll["releases"], "failures": poll["failures"],
            "cases": cases, "skipped": skipped}


def _case_open(store: Any, rel: dict[str, Any]) -> bool:
    for c in store.cases.values():
        if (str(c.get("provider", "")).lower() == str(rel["provider"]).lower()
                and c.get("version_to") == rel["current_version"]):
            return True
    return False
