"""Integration Engine & Preview Manager.

Combines changes from multiple Virtual Agent Lanes into an Integration Candidate,
previews diffs, checks for conflicts, and safely applies candidates to the target workspace.
"""

from __future__ import annotations

import json
import logging
import os
import time

from dataclasses import asdict, dataclass, field
from typing import Any
from .lane import LaneManager

_logger = logging.getLogger("truhowl.engine.integration")


@dataclass
class IntegrationCandidate:
    """Represents a combined set of changes from one or more lanes ready for review/apply."""
    candidate_id: str
    source_lanes: list[str]
    created_at: float = field(default_factory=time.time)
    changes: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntegrationEngine:
    """Manages integration candidates, previews, conflict detection, and applying lane diffs."""

    def __init__(self, workdir: str = ".") -> None:
        self.workdir = os.path.abspath(workdir)
        self.integration_dir = os.path.join(self.workdir, ".truhowl", "integration")
        os.makedirs(self.integration_dir, exist_ok=True)
        self.lane_mgr = LaneManager(workdir=self.workdir)

    def _candidate_file(self, candidate_id: str = "current_candidate") -> str:
        return os.path.join(self.integration_dir, f"{candidate_id}.json")

    def create_candidate(self, lane_ids: list[str]) -> IntegrationCandidate:
        """Create a candidate combining changes from specified lanes."""
        base = f"cand_{int(time.time() * 1000)}"
        cand_id = base
        n = 1
        while os.path.exists(self._candidate_file(cand_id)):
            cand_id = f"{base}_{n}"
            n += 1
        combined_changes: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        seen_paths: dict[str, str] = {}  # path -> lane_id

        for lid in lane_ids:
            lane = self.lane_mgr.get_lane(lid)
            if not lane:
                continue

            for chg in lane.changes:
                path = chg.get("path", "")
                if path in seen_paths:
                    conflicts.append({
                        "path": path,
                        "lane_a": seen_paths[path],
                        "lane_b": lid,
                        "conflict_type": "overlapping_modification",
                    })
                else:
                    seen_paths[path] = lid
                    chg_copy = dict(chg)
                    chg_copy["lane"] = lid
                    combined_changes.append(chg_copy)

        candidate = IntegrationCandidate(
            candidate_id=cand_id,
            source_lanes=lane_ids,
            changes=combined_changes,
            conflicts=conflicts,
        )
        self.save_candidate(candidate)
        return candidate

    def save_candidate(self, candidate: IntegrationCandidate) -> None:
        with open(self._candidate_file(), "w", encoding="utf-8") as f:
            json.dump(candidate.to_dict(), f, indent=2)

    def get_current_candidate(self) -> IntegrationCandidate | None:
        filepath = self._candidate_file()
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return IntegrationCandidate(**data)
        except Exception:
            return None

    def preview(self) -> str:
        """Render a clean summary preview of the current integration candidate."""
        cand = self.get_current_candidate()
        if not cand:
            return "No active integration candidate. Run 'truhowl integrate <lane1> <lane2>' first."

        lines = [
            "================================================================",
            f"            TRUHOWL INTEGRATION CANDIDATE #{cand.candidate_id}   ",
            "================================================================",
            f"Source Lanes: {', '.join(cand.source_lanes)}",
            f"Total Files : {len(cand.changes)}",
            f"Conflicts   : {len(cand.conflicts)}",
            "----------------------------------------------------------------",
        ]

        if cand.conflicts:
            lines.append("[CONFLICT] CONFLICTS DETECTED:")
            for conf in cand.conflicts:
                lines.append(f"  [CONFLICT] {conf['path']}: Modified by '{conf['lane_a']}' and '{conf['lane_b']}'")
            lines.append("----------------------------------------------------------------")

        lines.append("Proposed Combined Changes:")
        if not cand.changes:
            lines.append("  (No changes in candidate)")
        else:
            for chg in cand.changes:
                status = chg.get("status", "modified").upper()
                path = chg.get("path", "")
                lane = chg.get("lane", "")
                lines.append(f"  [{status}] {path} (from lane '{lane}')")

        lines.append("================================================================")
        return "\n".join(lines)

    def apply(self) -> bool:
        """Apply the integration candidate to the workspace."""
        cand = self.get_current_candidate()
        if not cand:
            return False

        if cand.conflicts:
            _logger.error("Cannot apply integration candidate with unresolved conflicts.")
            return False

        cand.applied = True
        self.save_candidate(cand)
        return True
