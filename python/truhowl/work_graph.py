# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Active Work Graph: developer-work state layered on the existing repo graph.

Owns WORK state only (branches, pushes, candidates). Dependency /
consumer relationships are read live from the existing repository graph
(detect_drift / build_dependency_graph), never duplicated here.

Lifecycle (Phase 1 persists OBSERVED + re-evaluation; later phases
advance POTENTIAL -> STABLE -> CONFIRMED -> NOTIFIED):
    OBSERVED -> POTENTIAL -> STABLE -> CONFIRMED -> NOTIFIED

Active work (v1): branch with recent pushes and/or an open PR.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Any, Dict, List, Optional

OBSERVED = "OBSERVED"
POTENTIAL = "POTENTIAL"
STABLE = "STABLE"
CONFIRMED = "CONFIRMED"
NOTIFIED = "NOTIFIED"

DEFAULT_ACTIVE_WINDOW_S = 14 * 24 * 3600



def _now() -> float:
    return time.time()


def store_path() -> str:
    base = os.environ.get("TRUHOWL_WORK_GRAPH_FILE",
                           os.path.join(os.path.expanduser("~/.truhowl"), "work_graph.json"))
    return base


def load_graph() -> Dict[str, Any]:
    p = store_path()
    if not os.path.isfile(p):
        return {"branches": {}, "candidates": {}}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"branches": {}, "candidates": {}}
        data.setdefault("branches", {})
        data.setdefault("candidates", {})
        return data
    except Exception:
        return {"branches": {}, "candidates": {}}


def save_graph(graph: Dict[str, Any]) -> str:
    p = store_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
        f.write("\n")
    return p


def candidate_id(repo: str, branch: str) -> str:
    return f"{repo.strip().lower()}#{branch.strip().lower()}"


def _push_files(obs: Dict[str, Any]) -> List[str]:
    seen: List[str] = []
    for c in obs.get("commits") or []:
        if not isinstance(c, dict):
            continue
        for key in ("added", "removed", "modified"):
            for f in c.get(key) or []:
                if f and f not in seen:
                    seen.append(f)
    return seen


def candidate_changed_files(cand: Dict[str, Any]) -> List[str]:
    """Union of files across the candidate's pushes. Zero tokens."""
    seen: List[str] = []
    for p in cand.get("pushes") or []:
        for f in p.get("files") or []:
            if f not in seen:
                seen.append(f)
    return seen


def record_push(obs: Dict[str, Any], open_pr: Optional[int] = None,
                exact_sha: Optional[bool] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Group a push observation into its repo+branch candidate. Re-evaluates."""
    graph = load_graph()
    repo, branch = obs["repository"], obs["branch"]
    key = f"{repo}#{branch}"
    cid = candidate_id(repo, branch)
    now = now if now is not None else _now()

    entry = graph["branches"].get(key, {})
    entry.update({
        "repository": repo, "branch": branch,
        "head_sha": obs["after"], "last_push_ts": now,
        "pusher": obs.get("pusher", ""),
    })
    if open_pr is not None:
        entry["open_pr"] = open_pr
    graph["branches"][key] = entry

    cand = graph["candidates"].get(cid, {
        "candidate_id": cid, "repository": repo, "branch": branch,
        "status": OBSERVED, "pushes": [], "created_ts": now,
    })
    cand["pushes"].append({
        "before": obs.get("before", ""), "after": obs.get("after", ""),
        "forced": bool(obs.get("forced", False)), "ts": now,
        "files": _push_files(obs),
        "exact_sha": exact_sha if exact_sha is not None else False,
    })
    cand["head_sha"] = obs["after"]
    cand["exact_sha"] = exact_sha if exact_sha is not None else cand.get("exact_sha", False)
    cand["eval_pending"] = True
    cand["updated_ts"] = now
    if obs.get("compare"):
        cand["compare"] = obs["compare"]
    if cand.get("status") in (STABLE, CONFIRMED):
        cand["status"] = POTENTIAL
    graph["candidates"][cid] = cand
    save_graph(graph)
    return cand


def is_active_work(repo: str, branch: str, open_prs: Optional[List[int]] = None,
                   window_s: int = DEFAULT_ACTIVE_WINDOW_S, now: Optional[float] = None) -> bool:
    """v1 definition: recent push and/or open PR."""
    graph = load_graph()
    entry = graph["branches"].get(f"{repo}#{branch}")
    if entry is None:
        return False
    if open_prs:
        return True
    if entry.get("open_pr") is not None:
        return True
    ts = entry.get("last_push_ts") or 0
    return (now if now is not None else _now()) - ts <= window_s


def record_branch_activity(repo: str, branch: str, sha: str,
                           open_pr: Optional[int] = None) -> None:
    """Track branch head / open-PR linkage from PR events. Work state only."""
    graph = load_graph()
    key = f"{repo}#{branch}"
    entry = graph["branches"].get(key, {"repository": repo, "branch": branch})
    entry["head_sha"] = sha
    if open_pr is not None:
        entry["open_pr"] = open_pr
    graph["branches"][key] = entry
    save_graph(graph)


def sweep_quiet(window_s: int = 600, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """POTENTIAL candidates with no new push inside the window become STABLE."""
    now = now if now is not None else _now()
    graph = load_graph()
    moved: List[Dict[str, Any]] = []
    for cid, cand in graph.get("candidates", {}).items():
        if cand.get("status") != POTENTIAL:
            continue
        if now - (cand.get("updated_ts") or 0) >= window_s:
            cand["status"] = STABLE
            cand["updated_ts"] = now
            moved.append(cand)
    if moved:
        save_graph(graph)
    return moved


def set_candidate_status(repo: str, branch: str, status: str) -> Optional[Dict[str, Any]]:
    graph = load_graph()
    cid = candidate_id(repo, branch)
    cand = graph["candidates"].get(cid)
    if cand is None:
        return None
    cand["status"] = status
    cand["updated_ts"] = _now()
    graph["candidates"][cid] = cand
    save_graph(graph)
    return cand


def get_candidate(repo: str, branch: str) -> Optional[Dict[str, Any]]:
    return load_graph()["candidates"].get(candidate_id(repo, branch))
