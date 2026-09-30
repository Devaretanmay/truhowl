# Copyright 2026 Truhowl Authors
"""GitHub push webhook ingestion: observation, never alert.

Phase 1 only: normalize a push payload into a plain observation dict.
No AI, no notifications, no repair. Returns None for events that carry
no branch work (tag pushes, branch deletions).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_ZERO_SHA = "0000000000000000000000000000000000000000"


def parse_push_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a push event. None = nothing to observe."""
    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/"):
        return None
    branch = ref[len("refs/heads/"):]
    before = str(payload.get("before") or "")
    after = str(payload.get("after") or "")
    if after == _ZERO_SHA or not after:
        return None
    repo = ((payload.get("repository") or {}).get("full_name") or "")
    if not repo or not branch:
        return None
    commits: List[Dict[str, Any]] = []
    for c in payload.get("commits") or []:
        if not isinstance(c, dict):
            continue
        commits.append({
            "id": c.get("id") or c.get("sha") or "",
            "message": (c.get("message") or "")[:500],
            "author": ((c.get("author") or {}).get("name") or "") if isinstance(c.get("author"), dict) else "",
            "added": list(c.get("added") or []),
            "removed": list(c.get("removed") or []),
            "modified": list(c.get("modified") or []),
        })
    pusher = payload.get("pusher") or {}
    return {
        "repository": repo,
        "branch": branch,
        "before": before,
        "after": after,
        "created": bool(payload.get("created", before == _ZERO_SHA)),
        "forced": bool(payload.get("forced", False)),
        "pusher": pusher.get("name") or pusher.get("email") or "",
        "compare": payload.get("compare") or "",
        "commits": commits,
    }


def changed_files(obs: Dict[str, Any]) -> List[str]:
    """Deduplicated file paths touched by a push observation. Zero tokens."""
    seen: List[str] = []
    for c in obs.get("commits") or []:
        for key in ("added", "removed", "modified"):
            for f in c.get(key) or []:
                if f and f not in seen:
                    seen.append(f)
    return seen
