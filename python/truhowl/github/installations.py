# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Persistent GitHub App installation records (stage-appropriate flat JSON).

No database: one file per installation under ~/.truhowl/installations/.
Tracks selected repositories, Day-0 index state, and provider association
so Install → Select → Index → READY survives restarts.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Any, Dict

REPO_PENDING = "PENDING"
REPO_INDEXED = "INDEXED"
REPO_READY = "READY"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def store_dir() -> str:
    base = os.environ.get("TRUHOWL_INSTALLATIONS_DIR",
                           os.path.join(os.path.expanduser("~/.truhowl"), "installations"))
    os.makedirs(base, exist_ok=True)
    return base


def _path(installation_id: str) -> str:
    return os.path.join(store_dir(), f"{installation_id}.json")


def load_installation(installation_id: str) -> Dict[str, Any] | None:
    p = _path(str(installation_id))
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_installation(record: dict[str, Any]) -> str:
    p = _path(str(record.get("installation_id", "unknown")))
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    return p


def record_installation_event(payload: dict[str, Any], repo_states: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Upsert an installation record from an installation.* webhook payload."""
    inst = payload.get("installation") or {}
    inst_id = str(inst.get("id") or payload.get("installation_id") or "unknown")
    account = (inst.get("account") or {}).get("login", "")
    record = load_installation(inst_id) or {
        "installation_id": inst_id, "account": account,
        "repos": {}, "created_utc": _now(),
    }
    record["account"] = account or record.get("account", "")
    record["event"] = payload.get("action", "")
    record["updated_utc"] = _now()
    for repo, state in (repo_states or {}).items():
        prev = record["repos"].get(repo, {})
        prev.update(state)
        record["repos"][repo] = prev
    save_installation(record)
    return record


def set_repo_state(installation_id: str, repo: str, state: str, **extra: Any) -> Dict[str, Any] | None:
    record = load_installation(str(installation_id))
    if record is None:
        return None
    entry = record.setdefault("repos", {}).setdefault(repo, {})
    entry["state"] = state
    entry["updated_utc"] = _now()
    entry.update(extra)
    save_installation(record)
    return record


def list_ready_repos(installation_id: str) -> list[str]:
    record = load_installation(str(installation_id)) or {}
    return [r for r, s in record.get("repos", {}).items() if s.get("state") == REPO_READY]
