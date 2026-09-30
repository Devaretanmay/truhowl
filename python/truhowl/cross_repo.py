# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Cross-repo work-impact evaluation (Phase 2: silent, no notifications).

Pipeline per push:
    cheap change extraction -> installed-repo matching -> active-work
    filtering -> AI only for plausible pairs -> POTENTIAL (or retract).

Consumption evidence reuses the existing repository graph machinery
(scan_callsites on the source short-name). This module never duplicates
dependency state and never notifies.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time as _time
from typing import Any, Dict, List, Optional

from truhowl import work_graph
from truhowl.ai_planner import AIPatchPlanner, build_reasoning_context
from truhowl.audit import is_code_evidence
from truhowl.autopatch import ScanConfig, scan_callsites
from truhowl.git_ops import git_commit_and_push
from truhowl.github.installations import store_dir as installations_store_dir
from truhowl.github.provisioning import cached_path, ensure_branch_checkout
from truhowl.knowledge import record_failure
from truhowl.repo_identity import STATE_ACTIVE, get_repository
from truhowl.sandbox.snapshot import SnapshotManager
from truhowl.test_runner import _detect_test_command, _run_tests

_logger = logging.getLogger("truhowl.cross_repo")

DEFAULT_QUIET_S = 600


def installed_repos(exclude: str = "") -> List[str]:
    """Repos in any installation record. v1 scope: installed only."""
    found: List[str] = []
    try:
        idir = installations_store_dir()
        files = [os.path.join(idir, f) for f in os.listdir(idir) if f.endswith(".json")]
    except Exception:
        return []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        for repo in (record.get("repos") or {}):
            if repo and repo.lower() != exclude.lower() and repo not in found:
                found.append(repo)
    return found


def consumer_checkout(repo: str) -> Optional[str]:
    """Local checkout for matching, or None (skip honestly, never guess)."""
    path = cached_path(repo)
    if os.path.isdir(os.path.join(path, ".git")):
        return path
    return None


def _sha_present(checkout: str, sha: str) -> bool:
    try:
        proc = subprocess.run(["git", "cat-file", "-e", sha], cwd=checkout,
                              capture_output=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


def resolve_push_checkout(obs: Dict[str, Any], token: Any = None) -> Dict[str, Any]:
    """Obtain the exact pushed revision when possible, else say so honestly.

    Free path first (cached checkout already contains the SHA); network fetch
    only with credentials or an explicit remote override, so unit contexts
    never hang on unreachable remotes. Never claims an uninspected SHA.
    """
    repo, branch, sha = obs["repository"], obs["branch"], obs.get("after", "")
    checkout = consumer_checkout(repo)
    if checkout and sha and _sha_present(checkout, sha):
        return {"workdir": checkout, "exact": True}
    slug = repo.replace("/", "__").upper()
    may_fetch = bool(token) or bool(os.environ.get(f"TRUHOWL_REPO_REMOTE_{slug}"))
    if may_fetch and sha:
        try:
            path, exact = ensure_branch_checkout(repo, branch, sha, token=token)
        except Exception as e:
            _logger.warning("exact push checkout failed for %s: %s", repo, e)
            return {"workdir": None, "exact": False}
        if exact:
            return {"workdir": path, "exact": True}
    return {"workdir": checkout, "exact": False}


def short_name(repo: str) -> str:
    return repo.split("/")[-1].lower()


def consumes_source(consumer_checkout_dir: str, source_repo: str) -> List[Dict[str, Any]]:
    """Cheap consumption evidence via the existing AST locator. Zero AI tokens.

    Only structurally-evidenced callsites count: bare name/URL substring
    hits without an AST kind are strings/docs, not consumption.
    """
    try:
        res = scan_callsites(consumer_checkout_dir, ScanConfig(sdk_names=[short_name(source_repo)]))
    except Exception as e:
        _logger.warning("cross-repo scan failed for %s: %s", consumer_checkout_dir, e)
        return []
    return [c for c in (res.get("callsites", []) or []) if is_code_evidence(c)]


def find_plausible_targets(source_repo: str, source_branch: str) -> List[Dict[str, Any]]:
    """Installed repos with active work that reference the source. No AI."""
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None:
        return []
    graph = work_graph.load_graph()
    targets: List[Dict[str, Any]] = []
    for repo in installed_repos(exclude=source_repo):
        checkout = consumer_checkout(repo)
        if checkout is None:
            continue
        callsites = consumes_source(checkout, source_repo)
        if not callsites:
            continue
        for key, entry in graph.get("branches", {}).items():
            if entry.get("repository", "").lower() != repo.lower():
                continue
            branch = entry.get("branch", "")
            if not branch or not work_graph.is_active_work(repo, branch):
                continue
            targets.append({
                "repository": repo, "branch": branch,
                "checkout": checkout,
                "evidence_callsites": len(callsites),
            })
    return targets


def active_work_context(source_repo: str, source_branch: str, cand: Dict[str, Any],
                        target: Dict[str, Any]) -> str:
    """Active Work Context: the fourth context block for AI reasoning."""
    files = work_graph.candidate_changed_files(cand)
    pushes = cand.get("pushes", [])
    latest = pushes[-1] if pushes else {}
    latest_files = ", ".join(latest.get("files", [])[:20]) or "unknown"
    branch_entry = work_graph.load_graph()["branches"].get(
        f"{source_repo}#{source_branch}", {})
    exact = "exact pushed revision inspected" if cand.get("exact_sha") else (
        "fallback — pushed SHA unavailable; analyzed payload file lists and "
        "known checkout state, not the exact pushed revision")
    lines = [
        f"Source work: {source_repo}/{source_branch} @ {cand.get('head_sha', '')[:8]}",
        f"Revision evidence: {exact}",
        f"Pushed by: {branch_entry.get('pusher', '') or 'unknown'} "
        f"({len(cand.get('pushes', []))} push(es) in this candidate)",
        f"Affected work: {target['repository']}/{target['branch']}",
        f"Changed files upstream ({len(files)}): " + (", ".join(files[:20]) or "unknown"),
        f"Latest push {(latest.get('after', '') or '')[:8]}: {latest_files}",
        f"Consumption evidence: {target.get('evidence_callsites', 0)} callsite(s) "
        f"reference '{short_name(source_repo)}' in the affected checkout",
    ]
    return "\n".join(lines)


def _stamp_evaluated(source_repo: str, source_branch: str, head: str, now: float) -> None:
    graph = work_graph.load_graph()
    cid = work_graph.candidate_id(source_repo, source_branch)
    cand = graph["candidates"].get(cid)
    if cand is None:
        return
    cand["last_evaluated_head"] = head
    cand["last_evaluated_ts"] = now
    cand["eval_pending"] = False
    graph["candidates"][cid] = cand
    work_graph.save_graph(graph)


def evaluate_candidate(source_repo: str, source_branch: str,
                       planner: Optional[Any] = None, force: bool = False,
                       quiet_s: int = DEFAULT_QUIET_S,
                       now: Optional[float] = None) -> Dict[str, Any]:
    """Evaluate one candidate. Sets POTENTIAL on AI-confirmed impact, else retracts to OBSERVED.

    Cost bound: after an evaluation, later pushes coalesce until the quiet
    window passes or a fast path forces re-evaluation of the latest head.
    """
    now = now if now is not None else _time.time()
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None:
        return {"evaluated": False, "reason": "no_candidate"}
    last_ts = cand.get("last_evaluated_ts") or 0
    if not force and last_ts and now - last_ts < quiet_s:
        return {"evaluated": False, "reason": "coalesced",
                "status": cand.get("status", work_graph.OBSERVED)}
    if planner is None:
        planner = AIPatchPlanner.from_env()
    if planner is None:
        return {"evaluated": False, "reason": "no_credentials_for_ai"}
    targets = find_plausible_targets(source_repo, source_branch)
    _stamp_evaluated(source_repo, source_branch, cand.get("head_sha", ""), now)
    if not targets:
        work_graph.set_candidate_status(source_repo, source_branch, work_graph.OBSERVED)
        return {"evaluated": True, "status": work_graph.OBSERVED, "targets": []}
    confirmed: List[Dict[str, Any]] = []
    for target in targets:
        details = (
            f"Cross-repo change: {source_repo}/{source_branch} pushed "
            f"{cand.get('head_sha', '')[:8]}.\n\n"
            f"Active Work Context:\n{active_work_context(source_repo, source_branch, cand, target)}"
        )
        context = build_reasoning_context(
            target["checkout"], source_repo,
            (cand.get("pushes", [{}])[0].get("before", "")[:8] or "unknown"),
            (cand.get("head_sha", "")[:8] or "unknown"),
            details, "")
        try:
            assessment = planner.assess(
                repo_dir=target["checkout"], provider_name=source_repo,
                from_version=cand.get("pushes", [{}])[0].get("before", "")[:8] or "unknown",
                to_version=cand.get("head_sha", "")[:8] or "unknown",
                migration_details=details, changelog_url="",
                affected_files=[], context=context)
        except Exception as e:
            _logger.warning("cross-repo assess failed for %s: %s", target["repository"], e)
            continue
        if assessment.get("body"):
            confirmed.append({**target, "confidence": assessment.get("confidence", "unknown")})
    if confirmed:
        work_graph.set_candidate_status(source_repo, source_branch, work_graph.POTENTIAL)
        graph = work_graph.load_graph()
        cid = work_graph.candidate_id(source_repo, source_branch)
        graph["candidates"][cid]["potential_targets"] = [
            {k: t[k] for k in ("repository", "branch", "confidence") if k in t} for t in confirmed
        ]
        work_graph.save_graph(graph)
        targets = graph["candidates"][cid]["potential_targets"]
        fast_confirm = any(t.get("confidence") == "high" for t in targets)
        return {"evaluated": True, "status": work_graph.POTENTIAL,
                "targets": targets, "fast_confirm": fast_confirm}
    work_graph.set_candidate_status(source_repo, source_branch, work_graph.OBSERVED)
    return {"evaluated": True, "status": work_graph.OBSERVED, "targets": []}


def _issue_title(source_repo: str, source_branch: str, target: Dict[str, Any]) -> str:
    return (f"[Truhowl] `{target['repository']}/{target['branch']}` may be affected "
            f"by `{source_repo}/{source_branch}`")


def _issue_body(source_repo: str, source_branch: str, cand: Dict[str, Any],
                target: Dict[str, Any]) -> str:
    files = work_graph.candidate_changed_files(cand)
    lines = [
        f"Your current work in `{target['repository']}/{target['branch']}` depends on a "
        f"contract that changed in `{source_repo}/{source_branch}`.",
        "",
        f"Changed upstream: `{source_repo}/{source_branch}` @ `{cand.get('head_sha', '')[:8]}`",
        f"Changed files ({len(files)}): " + (", ".join(files[:20]) or "unknown"),
    ]
    if cand.get("compare"):
        lines.append(f"Compare: {cand['compare']}")
    lines += [
        f"Confidence: {target.get('confidence', 'unknown')}",
        "",
        "No code was modified. Hunt repair is available on request, not automatic.",
    ]
    return "\n".join(lines)


def _issue_number(url: str) -> Optional[int]:
    try:
        return int((url or "").rstrip("/").split("/")[-1])
    except Exception:
        return None


def _update_body(source_repo: str, source_branch: str, cand: Dict[str, Any]) -> str:
    return (f"Howl update: `{source_repo}/{source_branch}` is still affected "
            f"at `{cand.get('head_sha', '')[:8]}`. Prior impact assessment stands.")


def _retract_body(source_repo: str, source_branch: str) -> str:
    return (f"Howl correction: the change in `{source_repo}/{source_branch}` no longer "
            f"appears to affect this work. This warning is retracted; no action needed.")


def notify_confirmed(source_repo: str, source_branch: str, client: Any) -> Dict[str, Any]:
    """One durable Issue per affected work; re-confirmation updates, never duplicates."""
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None or cand.get("status") != work_graph.CONFIRMED:
        return {"notified": False, "reason": "not_confirmed"}
    graph = work_graph.load_graph()
    cid = work_graph.candidate_id(source_repo, source_branch)
    fresh = graph["candidates"].get(cid, {})
    notified = fresh.get("notified_issues") or []
    issues: List[Dict[str, Any]] = []
    updated: List[Dict[str, Any]] = []
    for target in cand.get("potential_targets") or []:
        key = f"{target['repository']}#{target['branch']}"
        prior = next((n for n in notified
                      if n.get("target") == key and n.get("state", "open") == "open"), None)
        if prior is not None:
            if prior.get("head") == cand.get("head_sha"):
                continue  # same head already notified: stay silent
            num = _issue_number(prior.get("url", ""))
            if num is not None:
                try:
                    client.post_pr_comment(target["repository"], num,
                                           _update_body(source_repo, source_branch, cand))
                    prior["head"] = cand.get("head_sha")
                    updated.append({"target": key, "url": prior.get("url")})
                    continue
                except Exception as e:
                    _logger.warning("work-impact update failed for %s: %s", key, e)
        try:
            res = client.create_issue(
                repo=target["repository"],
                title=_issue_title(source_repo, source_branch, target),
                body=_issue_body(source_repo, source_branch, cand, target),
                labels=["truhowl", "work-impact"],
            )
        except Exception as e:
            _logger.warning("work-impact issue failed for %s: %s", key, e)
            continue
        url = res.get("html_url") if isinstance(res, dict) else None
        issues.append({"target": key, "url": url})
        notified.append({"target": key, "url": url, "head": cand.get("head_sha"),
                         "state": "open"})
        branch_entry = graph["branches"].get(key, {})
        if branch_entry.get("open_pr") is not None and url:
            try:
                client.post_pr_comment(
                    target["repository"], branch_entry["open_pr"],
                    f"Howl: possible upstream impact — see {url}")
            except Exception as e:
                _logger.warning("work-impact PR link failed for %s: %s", key, e)
    fresh["notified_issues"] = notified
    fresh["notified_head"] = cand.get("head_sha")
    fresh["status"] = work_graph.NOTIFIED
    fresh["updated_ts"] = work_graph._now()
    graph["candidates"][cid] = fresh
    work_graph.save_graph(graph)
    return {"notified": True, "issues": issues, "updated": updated}


def reconcile_prior_notification(source_repo: str, source_branch: str,
                                 client: Any) -> Dict[str, Any]:
    """Correct an already-notified candidate after re-evaluation.

    Still affected -> update stands (handled by notify path). No longer
    affected -> retract with comment + close, so stale warnings never pose
    as current truth.
    """
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None or cand.get("status") != work_graph.OBSERVED:
        return {"reconciled": False}
    graph = work_graph.load_graph()
    cid = work_graph.candidate_id(source_repo, source_branch)
    fresh = graph["candidates"].get(cid, {})
    notified = fresh.get("notified_issues") or []
    retracted: List[Dict[str, Any]] = []
    for n in notified:
        if n.get("state", "open") != "open":
            continue
        num = _issue_number(n.get("url", ""))
        if num is None:
            continue
        try:
            client.post_pr_comment(n["target"].split("#")[0], num,
                                   _retract_body(source_repo, source_branch))
            client.close_issue(n["target"].split("#")[0], num)
        except Exception as e:
            _logger.warning("work-impact retraction failed for %s: %s",
                            n.get("target"), e)
            continue
        n["state"] = "closed"
        retracted.append({"target": n.get("target")})
    if retracted:
        fresh["notified_issues"] = notified
        graph["candidates"][cid] = fresh
        work_graph.save_graph(graph)
    return {"reconciled": bool(retracted), "retracted": retracted}


def confirm_candidate(source_repo: str, source_branch: str, reason: str,
                      client: Any) -> Dict[str, Any]:
    """CONFIRMED (cheap recheck still plausible) -> notify. Else retract silent."""
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None or cand.get("status") not in (work_graph.POTENTIAL, work_graph.STABLE):
        return {"confirmed": False, "reason": "no_confirmable_candidate"}
    if not find_plausible_targets(source_repo, source_branch):
        work_graph.set_candidate_status(source_repo, source_branch, work_graph.OBSERVED)
        return {"confirmed": False, "reason": "impact_retracted"}
    work_graph.set_candidate_status(source_repo, source_branch, work_graph.CONFIRMED)
    result = notify_confirmed(source_repo, source_branch, client)
    return {"confirmed": True, "reason": reason, **result}


def sweep_and_notify(client: Any, quiet_s: int = DEFAULT_QUIET_S,
                     now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Quiet POTENTIAL -> STABLE -> evaluate latest -> confirm+notify (or silent retract)."""
    now = now if now is not None else _time.time()
    outcomes: List[Dict[str, Any]] = []
    for cand in work_graph.sweep_quiet(window_s=quiet_s, now=now):
        evaluate_candidate(cand["repository"], cand["branch"], force=True,
                           quiet_s=quiet_s, now=now)
        res = confirm_candidate(cand["repository"], cand["branch"], "stable_quiet_period", client)
        outcomes.append({"repository": cand["repository"], "branch": cand["branch"], **res})
    return outcomes


def pr_fastpath(payload: Dict[str, Any], client: Any) -> Dict[str, Any]:
    """PR open/merge on a source branch: re-evaluate, then confirm fast-path."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    ppr = payload.get("pull_request") or {}
    ref = (ppr.get("head") or {}).get("ref", "")
    sha = (ppr.get("head") or {}).get("sha", "")
    number = ppr.get("number")
    action = payload.get("action", "")
    merged = bool(ppr.get("merged", False))
    if not repo or not ref:
        return {"fastpath": False}
    if work_graph.get_candidate(repo, ref) is None:
        return {"fastpath": False}  # no observed work: store untouched
    work_graph.record_branch_activity(repo, ref, sha, open_pr=number)
    if action not in ("opened", "reopened") and not (action == "closed" and merged):
        return {"fastpath": True, "action": "tracked"}
    evaluation = evaluate_candidate(repo, ref, force=True)
    if evaluation.get("status") != work_graph.POTENTIAL:
        return {"fastpath": True, "action": "evaluated", "confirmed": False}
    reason = "merged" if merged else "pr_opened"
    return {"fastpath": True, "action": "evaluated",
            **confirm_candidate(repo, ref, reason, client)}


def request_hunt_repair(source_repo: str, source_branch: str, target_repo: str,
                        target_branch: str, client: Any,
                        planner: Optional[Any] = None) -> Dict[str, Any]:
    """Explicit opt-in Hunt repair for notified work. Never called automatically.

    Gate: Hunt must be ACTIVE for the affected repo. AI authors the patch,
    real tests must pass, else the checkout is restored and no PR is opened.
    """
    cand = work_graph.get_candidate(source_repo, source_branch)
    if cand is None or cand.get("status") != work_graph.NOTIFIED:
        return {"hunt": False, "reason": "no_notified_impact"}
    record = get_repository(target_repo)
    if record is None or record.get("hunt_state") != STATE_ACTIVE:
        return {"hunt": False, "reason": "hunt_not_enabled_for_repo"}
    checkout = consumer_checkout(target_repo)
    if checkout is None:
        return {"hunt": False, "reason": "no_checkout"}
    callsites = consumes_source(checkout, source_repo)
    files = sorted({c.get("file_path", "") for c in callsites if c.get("file_path")})
    if not files:
        return {"hunt": False, "reason": "impact_retracted"}
    if planner is None:
        planner = AIPatchPlanner.from_env()
    if planner is None:
        return {"hunt": False, "reason": "no_credentials_for_ai"}

    snapshotter = SnapshotManager(
        workdir=checkout, snapshot_dir=os.path.join(checkout, ".truhowl", "snapshot_tmp_hunt"))
    snapshotter.snapshot()
    try:
        issue_url = ""
        for n in cand.get("notified_issues") or []:
            if n.get("target") == f"{target_repo}#{target_branch}" and n.get("url"):
                issue_url = n["url"]
        work_ctx = active_work_context(
            source_repo, source_branch, cand,
            {"repository": target_repo, "branch": target_branch,
             "evidence_callsites": len(callsites)})
        details = (
            f"Confirmed cross-repo impact: adapt `{target_repo}/{target_branch}` "
            f"to `{source_repo}/{source_branch}` @ {cand.get('head_sha', '')[:8]}.\n\n"
            f"Active Work Context:\n{work_ctx}"
            + (f"\nHowl issue: {issue_url}" if issue_url else "")
        )
        context = build_reasoning_context(
            checkout, source_repo,
            (cand.get("pushes", [{}])[0].get("before", "")[:8] or "unknown"),
            (cand.get("head_sha", "")[:8] or "unknown"), details, "")
        results = planner.plan_and_apply(
            repo_dir=checkout, affected_files=files, provider_name=source_repo,
            from_version=cand.get("pushes", [{}])[0].get("before", "")[:8] or "unknown",
            to_version=cand.get("head_sha", "")[:8] or "unknown",
            migration_details=details, dry_run=False, context=context, changelog_url="")
        modified = [r.file_path for r in (results or []) if r.success]
        if not modified:
            return {"hunt": False, "reason": "ai_produced_no_patch"}

        test_cmd = _detect_test_command(checkout)
        if not test_cmd:
            snapshotter.restore()
            return {"hunt": False, "reason": "no_test_command_fail_closed"}
        try:
            proc = _run_tests(checkout, test_cmd, timeout=120)
            exit_code = proc.returncode
        except Exception:
            exit_code = 1
        if exit_code != 0:
            snapshotter.restore()
            record_failure(
                checkout, source_repo,
                cand.get("pushes", [{}])[0].get("before", "")[:8] or "unknown",
                cand.get("head_sha", "")[:8] or "unknown",
                f"hunt verification failed (exit {exit_code})",
                [os.path.relpath(p, checkout) if os.path.isabs(p) else p for p in modified])
            return {"hunt": False, "reason": "verification_failed"}

        branch = f"truhowl/work-impact-{short_name(source_repo)}"
        pushed = git_commit_and_push(
            checkout, modified, branch,
            f"adapt {target_branch} to {source_repo}/{source_branch}\n\n"
            f"Truhowl-Verified: true")
        if not pushed:
            snapshotter.restore()
            return {"hunt": False, "reason": "push_failed"}
        body = (f"Hunt repair for confirmed work impact of `{source_repo}/{source_branch}` "
                f"on `{target_repo}/{target_branch}`.\nVerified: `{test_cmd}` exit 0."
                + (f"\nHowl issue: {issue_url}" if issue_url else ""))
        pr = client.create_pull_request(
            repo=target_repo,
            title=f"[Truhowl] adapt {target_branch} to {source_repo}/{source_branch}",
            body=body, head_branch=branch, base_branch=target_branch,
            labels=["truhowl", "work-impact"])
        pr_url = pr.get("html_url") if isinstance(pr, dict) else None
        return {"hunt": True, "pr_url": pr_url, "branch": branch,
                "files": len(modified), "test_command": test_cmd}
    finally:
        try:
            snapshotter.cleanup()
        except Exception:
            pass
