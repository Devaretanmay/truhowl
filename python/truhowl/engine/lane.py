"""Lane Primitive & Virtual Agent Workspace Engine.

Implements the GitButler-equivalent Virtual Agent Lane abstraction for Truhowl.
- Lane: isolated unit of agent work owning identity, agent session, compartment execution boundary, filesystem diff tree, and lifecycle.
"""

from __future__ import annotations

import json
import os
import time

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, List


class LaneStatus(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    INTEGRATED = "INTEGRATED"
    FAILED = "FAILED"


@dataclass
class Lane:
    """First-class domain primitive representing a Virtual Agent Lane."""
    lane_id: str
    name: str
    workspace_id: str = "default_workspace"
    agent_id: str = "Claude Code"
    status: str = LaneStatus.CREATED
    session_id: str | None = None
    compartment_id: str = "AgentTask"
    permissions: list[str] = field(default_factory=lambda: ["fs_read", "fs_write", "fs_exec"])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    changes: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LaneManager:
    """Manages Virtual Agent Lanes under .truhowl/lanes/."""

    def __init__(self, workdir: str = ".") -> None:
        self.workdir = os.path.abspath(workdir)
        self.lanes_dir = os.path.join(self.workdir, ".truhowl", "lanes")
        os.makedirs(self.lanes_dir, exist_ok=True)

    def _lane_file(self, lane_id: str) -> str:
        return os.path.join(self.lanes_dir, f"{lane_id}.json")

    def _lane_worktree(self, lane_id: str) -> str:
        d = os.path.join(self.lanes_dir, lane_id, "worktree")
        os.makedirs(d, exist_ok=True)
        return d

    def create_lane(
        self,
        name: str,
        agent_id: str = "Claude Code",
        permissions: List[str] | None = None,
    ) -> Lane:
        """Create and persist a new Virtual Agent Lane."""
        lane_id = name.lower().replace(" ", "-")
        lane = Lane(
            lane_id=lane_id,
            name=name,
            workspace_id=os.path.basename(self.workdir),
            agent_id=agent_id,
            permissions=permissions or ["fs_read", "fs_write", "fs_exec"],
            status=LaneStatus.CREATED
        )
        self._lane_worktree(lane_id)
        self.save_lane(lane)
        return lane

    def get_lane(self, lane_id: str) -> Lane | None:
        """Load a lane by ID."""
        filepath = self._lane_file(lane_id)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Lane(**data)
        except Exception:
            return None

    def list_lanes(self) -> list[Lane]:
        """List all virtual agent lanes in workspace."""
        lanes: list[Lane] = []
        if not os.path.exists(self.lanes_dir):
            return lanes
        for filename in sorted(os.listdir(self.lanes_dir)):
            if filename.endswith(".json"):
                lid = filename[:-5]
                lane = self.get_lane(lid)
                if lane:
                    lanes.append(lane)
        return lanes

    def save_lane(self, lane: Lane) -> None:
        """Save lane metadata to JSON."""
        lane.updated_at = time.time()
        filepath = self._lane_file(lane.lane_id)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(lane.to_dict(), f, indent=2)

    def record_diff(self, lane_id: str, diffs: list[dict[str, Any]]) -> None:
        """Record file changes made by the lane's session."""
        lane = self.get_lane(lane_id)
        if not lane:
            return
        lane.changes = diffs
        lane.status = LaneStatus.COMPLETED
        self.save_lane(lane)
