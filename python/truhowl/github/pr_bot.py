# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

"""
Truhowl GitHub App PR bot.

This is the first product milestone: install the app, open a PR, and Truhowl
automatically posts a verification result on that PR.

The PR bot is not a separate code path from the maintenance engine. It is an
event handler that constructs a TriggerContext and passes it to the shared
MaintenancePipeline.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from typing import Any, Callable, Dict

from truhowl.ai_planner import AIPatchPlanner as AIPatchPlanner
from truhowl.github.client import GitHubAppClient
from truhowl.pipeline import (
    MaintenancePipeline,
    PipelinePolicy,
    TriggerContext,
)
from truhowl.repo_identity import STATE_ACTIVE, set_bot_state
from truhowl.audit import run_audit
from truhowl.github.howl_bot import HowlBot
from truhowl.github.hunt_bot import HuntBot
from truhowl.github.installations import (
    REPO_INDEXED, REPO_PENDING, REPO_READY, record_installation_event, set_repo_state,
)
from truhowl.github.provisioning import resolve_pr_workdir
from truhowl.graph import build_dependency_graph
from truhowl.drift import detect_drift
from truhowl import cross_repo, work_graph
from truhowl.github.push_events import parse_push_payload

_logger = logging.getLogger("truhowl.pr_bot")


# ── PR event handlers ──────────────────────────────────────────────────────

def handle_pull_request_event(
    payload: dict[str, Any],
    event_type: str,
    client: GitHubAppClient,
    policy: PipelinePolicy | None = None,
    workdir: str | None = None,
    exact_head: bool = True,
) -> dict[str, Any]:
    """
    Handle a pull_request.* webhook event.

    Supported trigger types:

    * pull_request.opened
    * pull_request.synchronize
    * pull_request.reopened
    """
    policy = policy or PipelinePolicy()

    ppr = payload.get("pull_request")
    if not ppr:
        return {"success": False, "error": "No pull_request in payload"}

    repo = payload.get("repository", {}).get("full_name")
    if not repo:
        return {"success": False, "error": "No repository in payload"}

    number = ppr.get("number")
    action = payload.get("action", "")
    full_event = f"pull_request.{action}"

    ref = ppr.get("head", {}).get("ref")
    sha = ppr.get("head", {}).get("sha")

    if not number or not ref or not sha:
        return {"success": False, "error": "Missing PR head info"}

    try:
        cross_repo.pr_fastpath(payload, client)
    except Exception as e:
        _logger.warning("cross-repo PR fast-path failed for %s: %s", repo, e)

    changed_files = _extract_changed_files(payload)
    pr_labels = [str(lb.get("name", "")) for lb in (ppr.get("labels") or []) if isinstance(lb, dict)]
    excluded = [lb for lb in pr_labels if lb.lower() in {e.lower() for e in (policy.exclude_labels or [])}]
    if excluded:
        return {"success": True, "event_type": full_event, "repository": repo,
                "pr_number": number, "skipped": True,
                "reason": f"excluded label(s): {', '.join(excluded)}"}
    if changed_files and policy.ignore_paths and all(
            any(fnmatch.fnmatch(f, pat) for pat in policy.ignore_paths) for f in changed_files):
        return {"success": True, "event_type": full_event, "repository": repo,
                "pr_number": number, "skipped": True,
                "reason": "all changed files match ignore_paths"}
    ctx = TriggerContext.from_pull_request_event(payload, workdir=workdir, changed_files=changed_files)
    ctx.metadata["exact_head"] = exact_head

    pipeline = MaintenancePipeline(client=client, policy=policy)
    result = pipeline.run(ctx)

    return {
        "success": True,
        "event_type": full_event,
        "repository": repo,
        "pr_number": number,
        "pipeline_status": result.status,
        "mergeable": result.mergeable,
        "check_state": result.check_state,
        "check_description": result.status_description,
        "comment_posted": bool(result.comment_body),
        "comment_preview": _safe_preview(result.comment_body),
        "exact_head": exact_head,
    }


def _extract_changed_files(payload: dict[str, Any]) -> list[str]:
    """Try to extract changed file list from webhook payload or metadata."""
    ppr = payload.get("pull_request") or {}
    if ppr.get("files"):
        return [f.get("filename") for f in ppr.get("files") if f.get("filename")]

    meta = payload.get("truhowl", {})
    if isinstance(meta, dict):
        return meta.get("changed_files", [])
    return []


def _safe_preview(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()[:6]
    return "\n".join(lines)


# ── External-change event handler ──────────────────────────────────────────

def handle_external_change_event(
    payload: dict[str, Any],
    client: GitHubAppClient,
    policy: PipelinePolicy | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """
    Handle an external-change event (provider version drift detected).

    The payload should contain:
        provider_name, from_version, to_version, repository, ref, sha, workdir
    """
    policy = policy or PipelinePolicy()

    repo = payload.get("repository")
    if not repo:
        return {"success": False, "error": "No repository in payload"}

    ctx = TriggerContext.from_external_change(
        provider_name=payload.get("provider_name", ""),
        from_version=payload.get("from_version", ""),
        to_version=payload.get("to_version", ""),
        repository=repo,
        ref=payload.get("ref", "main"),
        sha=payload.get("sha", ""),
        workdir=workdir,
        description=payload.get("description", ""),
    )

    pipeline = MaintenancePipeline(client=client, policy=policy)
    result = pipeline.run(ctx)

    return {
        "success": True,
        "event_type": "external.change.drift",
        "repository": repo,
        "pipeline_status": result.status,
        "mergeable": result.mergeable,
        "check_state": result.check_state,
        "comment_posted": bool(result.comment_body),
    }


# ── Local / CI run helpers ─────────────────────────────────────────────────

def run_on_pr_locally(
    repo: str,
    pr_number: int,
    workdir: str,
    base_branch: str = "main",
    client: GitHubAppClient | None = None,
    policy: PipelinePolicy | None = None,
) -> dict[str, Any]:
    """
    Run the PR bot against a local checkout of a PR.

    Useful for:
    * local development
    * CI check jobs that want Truhowl to post results
    """
    client = client or GitHubAppClient()
    changed_files = _diff_files_locally(workdir, base_branch)

    payload: dict[str, Any] = {
        "action": "synchronize",
        "pull_request": {
            "number": pr_number,
            "title": "Local PR run",
            "body": "Truhowl local run",
            "head": {"ref": "pr-branch", "sha": "local-sha"},
            "base": {"ref": base_branch},
            "files": [{"filename": f} for f in changed_files],
        },
        "repository": {"full_name": repo},
        "truhowl": {"changed_files": changed_files},
    }

    return handle_pull_request_event(
        payload,
        "pull_request.synchronize",
        client,
        policy,
        workdir=workdir,
    )


def _diff_files_locally(workdir: str, base_branch: str) -> list[str]:
    """Return files changed in the current checkout relative to base_branch."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_branch],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
            return files
    except Exception as e:
        _logger.warning("git diff failed: %s", e)

    return []


def render_day0_onboarding_issue(repo: str, workdir: str | None = None) -> str:
    """Render the Day-0 repository onboarding and contract inventory issue."""
    providers_text = "  - No external third-party SDK dependencies detected"
    total_files = 0
    callsites = 0

    if workdir and os.path.isdir(workdir):
        try:
            detected = detect_drift(workdir, None)
            if detected:
                providers_text = "\n".join(
                    f"  - {d.get('provider', 'API')} ({d.get('package_name', '')}) -> declared: {d.get('declared_version', 'unknown')}"
                    for d in detected
                )
            graph = build_dependency_graph(workdir)
            total_files = len(graph.get("nodes", []))
            callsites = sum(len(node.get("callsites", [])) for node in graph.get("nodes", []))
        except Exception as e:
            _logger.warning("failed to compute Day-0 stats for %s: %s", repo, e)

    lines = [
        "-----------------------------------------",
        "   TRUHOWL DAY-0 REPOSITORY ONBOARDING   ",
        "-----------------------------------------",
        "",
        f"Truhowl has mapped external API dependencies and contracts for `{repo}`.",
        "",
        "### External APIs & SDKs Monitored:",
        providers_text,
        "",
        "### Repository Architecture Graph:",
        f"  - Total indexed files: {total_files}",
        f"  - External call sites inspected: {callsites}",
        "",
        "### Continuous Guard Status:",
        "  - [OK] Automated PR Review: Active (Audit Mode)",
        "  - [OK] Auto-Fix Policy: Opt-in (Configurable via .truhowl/config.yaml)",
        "  - [OK] External Contract Drift: Watching upstream provider releases",
        "",
        "-----------------------------------------",
    ]
    return "\n".join(lines)


def handle_installation_event(
    payload: dict[str, Any],
    event_type: str,
    client: GitHubAppClient,
    workdir_fn: Callable[[str], str] | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Handle installation.* and installation_repositories.* webhook events.

    Persists the installation record, runs Day-0 indexing wherever a local
    checkout is available, and posts the onboarding issue. Install → record
    → index → READY, not just a comment.
    """
    repos_data = payload.get("repositories") or payload.get("repositories_added") or []
    onboarded: list[str] = []
    repo_states: dict[str, dict[str, Any]] = {}

    for repo_info in repos_data:
        repo_name = repo_info.get("full_name") if isinstance(repo_info, dict) else str(repo_info)
        if not repo_name:
            continue

        workdir = workdir_fn(repo_name) if workdir_fn else None
        state = REPO_PENDING
        if workdir and os.path.isdir(workdir):
            try:
                run_audit(repo_root=workdir, output_format="cli", write_graph=True)
                state = REPO_INDEXED
            except Exception as e:
                _logger.warning("day-0 index failed for %s: %s", repo_name, e)
        repo_states[repo_name] = {"state": state, "workdir": workdir}

        issue_body = render_day0_onboarding_issue(repo_name, workdir=workdir)
        try:
            client.create_issue(
                repo=repo_name,
                title="[TRUHOWL] Day-0 External Contract & API Dependency Map",
                body=issue_body,
                labels=["truhowl", "maintenance"],
            )
            onboarded.append(repo_name)
            # Indexed + surfaced to the user = READY for monitoring.
            if state == REPO_INDEXED:
                repo_states[repo_name]["state"] = REPO_READY
        except Exception as e:
            _logger.warning("failed to post Day-0 onboarding issue for %s: %s", repo_name, e)

    record = record_installation_event(payload, repo_states) if store else {}
    inst_id = record.get("installation_id") if record else None
    if store and inst_id and inst_id != "unknown":
        for repo_name, rs in repo_states.items():
            if rs["state"] == REPO_READY:
                set_repo_state(inst_id, repo_name, REPO_READY, workdir=rs["workdir"])
    return {
        "success": True,
        "event_type": event_type,
        "repositories_onboarded": onboarded,
        "repo_states": {r: s["state"] for r, s in repo_states.items()},
        "installation_id": inst_id,
    }


def handle_issue_comment_event(
    payload: dict[str, Any],
    event_type: str,
    client: GitHubAppClient,
    policy: PipelinePolicy | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Handle issue_comment.* events mentioning @truhowl.

    `@truhowl` alone re-runs the pipeline on the PR; `@truhowl explain`
    posts impact reasoning without touching code. Anything else is ignored.
    """
    policy = policy or PipelinePolicy()
    repo = payload.get("repository", {}).get("full_name", "")
    comment = payload.get("comment", {}) or {}
    body = str(comment.get("body", "") or "")
    sender = ((payload.get("sender") or {}).get("type", "") or "").lower()

    issue = payload.get("issue", {}) or {}
    number = issue.get("number")
    body_lower = body.lower()
    if not repo or not number or not body:
        return {"success": True, "event": event_type, "handled": False}
    if not any(trigger in body_lower for trigger in ("@truhowl", "@howl", "@hunt", "@consult", "@work")):
        return {"success": True, "event": event_type, "handled": False}
    if sender == "bot" or not issue.get("pull_request"):
        return {"success": True, "event": event_type, "handled": False,
                "note": "not a human comment on a PR"}

    try:
        pr = client.get_pull_request(repo, number)
    except Exception as e:
        return {"success": False, "error": f"could not fetch PR #{number}: {e}"}

    head = pr.get("head", {}) or {}
    payload_pr = {
        "action": "synchronize",
        "pull_request": {
            "number": number,
            "title": pr.get("title", ""),
            "body": pr.get("body", ""),
            "head": {"ref": (head.get("ref") or ""), "sha": (head.get("sha") or "")},
            "base": {"ref": ((pr.get("base") or {}).get("ref") or "")},
        },
        "repository": {"full_name": repo},
    }

    files = [f.get("filename", "") for f in client.get_pull_request_files(repo, number)
             if f.get("filename")]
    ctx = TriggerContext.from_pull_request_event(
        payload_pr, workdir=workdir, changed_files=files)

    # Route 1: Consult mode trigger (@howl, @consult, @truhowl explain)
    if any(t in body_lower for t in ("@howl", "@consult", "@truhowl explain")):
        set_bot_state(repo, "howl", STATE_ACTIVE)
        howl = HowlBot(client=client, policy=policy)
        res = howl.explain_pull_request(ctx, require_ai=True)
        if not res.get("success"):
            return res
        return {"success": True, "event_type": "issue_comment.howl",
                "repository": repo, "pr_number": number, "comment_posted": True, "result": res}

    # Route 2: Work mode trigger (@hunt, @work)
    if any(t in body_lower for t in ("@hunt", "@work")):
        set_bot_state(repo, "hunt", STATE_ACTIVE)
        hunt = HuntBot(client=client, policy=policy)
        res = hunt.execute_repair(ctx)
        return {"success": True, "event_type": "issue_comment.hunt",
                "repository": repo, "pr_number": number, "comment_posted": True, "result": res}

    pr_payload = dict(payload, **{"action": "synchronize", "pull_request": payload_pr["pull_request"]})
    return handle_pull_request_event(pr_payload, "pull_request.synchronize",
                                     client, policy, workdir=workdir)


def handle_push_event(
    payload: dict[str, Any],
    client: GitHubAppClient,
) -> dict[str, Any]:
    """Ingest a push as an observation. Never alerts, never repairs (Phase 1).

    Groups the push into its repo+branch candidate and records branch work
    state. Matching, AI reasoning, and notification arrive in later phases.
    """
    obs = parse_push_payload(payload)
    if obs is None:
        return {"success": True, "event_type": "push", "handled": False,
                "note": "no branch work to observe"}
    prior = work_graph.get_candidate(obs["repository"], obs["branch"])
    prior_status = (prior or {}).get("status")
    token = getattr(client, "token", None)
    exact = False
    try:
        resolved = cross_repo.resolve_push_checkout(obs, token=token)
        exact = bool(resolved.get("exact"))
    except Exception as e:
        _logger.warning("push SHA resolution failed for %s: %s",
                        obs["repository"], e)
    cand = work_graph.record_push(obs, exact_sha=exact)
    evaluation = cross_repo.evaluate_candidate(
        obs["repository"], obs["branch"],
        force=(prior_status == work_graph.NOTIFIED))
    if prior_status == work_graph.NOTIFIED and client is not None:
        evaluation["reconciliation"] = cross_repo.reconcile_prior_notification(
            obs["repository"], obs["branch"], client)
    notified: Any = False
    status = evaluation.get("status", cand["status"])
    if evaluation.get("fast_confirm") and client is not None:
        done = cross_repo.confirm_candidate(
            obs["repository"], obs["branch"], "high_confidence", client)
        notified = bool(done.get("notified"))
        if done.get("confirmed"):
            status = work_graph.NOTIFIED
    return {"success": True, "event_type": "push",
            "repository": obs["repository"], "branch": obs["branch"],
            "candidate_id": cand["candidate_id"], "status": status,
            "handled": True, "notified": notified,
            "exact_sha": exact}


def make_pr_bot_handler(
    client: GitHubAppClient | None = None,
    policy: PipelinePolicy | None = None,
    workdir_fn: Callable[[Dict[str, Any]], str] | None = None,
) -> Callable[[dict[str, Any], str], dict[str, Any]]:
    client = client or GitHubAppClient()
    policy = policy or PipelinePolicy()

    def handler(payload: dict[str, Any], event_type: str) -> dict[str, Any]:
        workdir = None
        if workdir_fn:
            workdir = workdir_fn(payload)

        if event_type.startswith("pull_request."):
            workdir, exact = resolve_pr_workdir(
                payload, token=getattr(client, "token", None),
                fallback_fn=(lambda p: workdir) if workdir else None,
            )
            return handle_pull_request_event(
                payload,
                event_type,
                client,
                policy,
                workdir=workdir,
                exact_head=exact,
            )

        if event_type.startswith("external.change"):
            return handle_external_change_event(
                payload,
                client,
                policy,
                workdir=workdir,
            )

        if event_type.startswith("issue_comment"):
            return handle_issue_comment_event(
                payload,
                event_type,
                client,
                policy,
                workdir=workdir,
            )

        if event_type.startswith("installation"):
            def repo_workdir_resolver(repo_name: str) -> str | None:
                if workdir_fn:
                    return workdir_fn({"repository": {"full_name": repo_name}})
                return None

            return handle_installation_event(
                payload,
                event_type,
                client,
                workdir_fn=repo_workdir_resolver,
            )

        if event_type == "push":
            return handle_push_event(payload, client)

        return {
            "success": True,
            "event": event_type,
            "handled": False,
            "note": "Event type not handled by PR bot",
        }

    return handler
