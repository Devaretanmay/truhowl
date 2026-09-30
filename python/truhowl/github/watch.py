# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Background monitoring: poll READY repos for new contract drift.

watch_once() is the testable unit: refresh checkouts, detect changes,
compare against the last recorded outcome, and run the maintenance
pipeline only on genuinely new findings. The serve loop just calls it
on an interval. Respects pipeline policy (fail-closed by default).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from truhowl import cross_repo
from truhowl.config import PipelinePolicy
from truhowl.drift import detect_changes
from truhowl.github.client import GitHubAppClient
from truhowl.github.installations import REPO_INDEXED, REPO_READY, save_installation, store_dir
from truhowl.github.provisioning import ensure_repo_checkout
from truhowl.pipeline import MaintenancePipeline, TriggerContext

_logger = logging.getLogger("truhowl.watch")


def _install_files() -> list[str]:
    try:
        idir = store_dir()
        return [os.path.join(idir, f) for f in os.listdir(idir) if f.endswith(".json")]
    except Exception:
        return []


def watch_once(client: Any = None, policy: Any = None) -> list[dict[str, Any]]:
    """Poll every INDEXED/READY repo once. Returns an outcome per watched repo."""
    if client is None:
        client = GitHubAppClient()
    if policy is None:
        policy = PipelinePolicy()

    outcomes: list[dict[str, Any]] = []
    for path in _install_files():
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        token = getattr(client, "token", None)
        dirty = False
        for repo, state in record.get("repos", {}).items():
            if state.get("state") not in (REPO_INDEXED, REPO_READY):
                continue
            workdir = ensure_repo_checkout(repo, token=token)
            if not workdir:
                outcomes.append({"repository": repo, "watched": False, "reason": "no checkout"})
                continue
            try:
                detections = detect_changes(workdir)
            except Exception as e:
                outcomes.append({"repository": repo, "watched": False, "reason": str(e)})
                continue
            sig = sorted([d.source.provider, d.outcome, d.callsite_count] for d in detections)
            if sig == state.get("last_watch"):
                outcomes.append({"repository": repo, "watched": True, "new_findings": 0})
                continue
            state["last_watch"] = sig
            dirty = True
            fired = 0
            for d in detections:
                if d.outcome == "NO_IMPACT":
                    continue
                ctx = TriggerContext.from_external_change(
                    provider_name=d.source.provider,
                    from_version=d.source.version_from,
                    to_version=d.source.version_to,
                    repository=repo, workdir=workdir,
                    description=d.reason,
                )
                try:
                    result = MaintenancePipeline(client=client, policy=policy).run(ctx)
                    fired += 1
                    outcomes.append({"repository": repo, "watched": True, "new_findings": 1,
                                     "provider": d.source.provider,
                                     "pipeline_status": result.status})
                except Exception as e:
                    _logger.warning("watch pipeline failed for %s: %s", repo, e)
            if fired == 0:
                outcomes.append({"repository": repo, "watched": True, "new_findings": 0})
        if dirty:
            try:
                save_installation(record)
            except Exception as e:
                _logger.warning("watch state save failed: %s", e)
    try:
        for res in cross_repo.sweep_and_notify(client):
            outcomes.append({"sweep": True, **res})
    except Exception as e:
        _logger.warning("cross-repo sweep failed: %s", e)
    return outcomes
