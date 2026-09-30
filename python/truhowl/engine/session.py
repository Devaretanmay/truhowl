"""AgentSession Domain Primitive & Persistent Engine.

Represents an agent execution session as a first-class domain object with invariants:
- 1 AgentSession -> 1 Agent
- 1 AgentSession -> 1 Lane
- 1 Lane -> 1 Compartment execution boundary
- 1 Workspace -> Many Lanes & Sessions
"""

from __future__ import annotations

import json
import logging
import os
import time

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List
from ..sandbox.snapshot import SnapshotManager

_logger = logging.getLogger("truhowl.session")


class SessionStatus:
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class SessionEvent:
    """Structured lifecycle event in the AgentSession stream."""
    timestamp: float
    name: str  # e.g., "session.created", "tool.started", "permission.allowed", "permission.denied", "file.changed"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentSession:
    """First-class persistent domain primitive representing an agent session."""
    session_id: str
    workspace_id: str = "default_workspace"
    lane_id: str = "default_lane"
    agent: str = "Claude Code"
    command: str = ""
    task: str = ""
    status: str = SessionStatus.CREATED
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    parent_session: str | None = None
    compartment_id: str = "AgentTask"
    policy: dict[str, Any] = field(default_factory=lambda: {"permissions": ["fs_read", "fs_exec"]})
    events: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    
    # Backwards compatibility attributes
    actions: list[dict[str, Any]] = field(default_factory=list)
    diffs: list[dict[str, str]] = field(default_factory=list)
    returncode: int = 0

    @property
    def agent_name(self) -> str:
        return self.agent

    @property
    def compartment_name(self) -> str:
        return self.compartment_id

    def emit_event(self, event_name: str, payload: Dict[str, Any] | None = None) -> None:
        """Append a structured event to the session stream."""
        event = SessionEvent(
            timestamp=time.time(),
            name=event_name,
            payload=payload or {}
        )
        self.events.append(event.to_dict())

    def log_action(self, action_type: str, target: str, status: str = "OK", details: str = "") -> None:
        """Legacy helper mapping log actions to structured events."""
        event_name = "permission.allowed" if status == "OK" else "permission.denied"
        self.emit_event(event_name, {"action_type": action_type, "target": target, "details": details})
        self.actions.append({
            "timestamp": time.time(),
            "action_type": action_type,
            "target": target,
            "status": status,
            "details": details,
        })

    def create_checkpoint(self, name: str, snapshot_manifest: str | None = None) -> dict[str, Any]:
        """Record a time-travel checkpoint for this session."""
        cp = {
            "checkpoint_id": f"cp_{int(time.time() * 1000)}",
            "name": name,
            "timestamp": time.time(),
            "snapshot_manifest": snapshot_manifest,
        }
        self.checkpoints.append(cp)
        self.emit_event("checkpoint.created", cp)
        return cp

    def start(self) -> None:
        self.status = SessionStatus.RUNNING
        self.started_at = time.time()
        self.emit_event("session.started", {"agent": self.agent, "task": self.task})

    def complete(self, returncode: int = 0, diffs: List[Dict[str, str]] | None = None) -> None:
        """Mark session as finished cleanly."""
        self.returncode = returncode
        self.finished_at = time.time()
        self.status = SessionStatus.COMPLETED if returncode == 0 else SessionStatus.FAILED
        if diffs is not None:
            self.diffs = diffs
            self.changes = diffs
        self.result = {"returncode": returncode, "diff_count": len(self.diffs)}
        event_name = "session.completed" if returncode == 0 else "session.failed"
        self.emit_event(event_name, self.result)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Ensure agent_name backwards compatibility for legacy callers
        d["agent_name"] = self.agent
        d["compartment_name"] = self.compartment_id
        d["permissions"] = self.policy.get("permissions", ["fs_read", "fs_exec"])
        d["ended_at"] = self.finished_at
        return d

    def format_ascii_view(self) -> str:
        """Render session details as a structured ASCII view."""
        duration = round((self.finished_at or time.time()) - self.started_at, 2)
        lines = [
            "================================================================",
            f"              TRUHOWL AGENT SESSION #{self.session_id}          ",
            "================================================================",
            f"Workspace   : {self.workspace_id}",
            f"Lane        : {self.lane_id}",
            f"Agent       : {self.agent}",
            f"Task        : {self.task}",
            f"Compartment : {self.compartment_id}",
            f"Permissions : {self.policy.get('permissions', [])}",
            f"Status      : {self.status} (Exit code: {self.returncode})",
            f"Duration    : {duration}s",
            "----------------------------------------------------------------",
            f"Event Stream ({len(self.events)} events):",
        ]

        if not self.events:
            lines.append("  (No events recorded)")
        else:
            for ev in self.events[-10:]:  # show latest 10
                name = ev.get("name", "")
                payload = ev.get("payload", {})
                lines.append(f"  [{name}] {payload}")

        lines.append("----------------------------------------------------------------")
        lines.append(f"Changes ({len(self.changes)} file(s)):")
        if not self.changes:
            lines.append("  (No file changes detected)")
        else:
            for d in self.changes:
                lines.append(f"  {d.get('status', 'modified').upper()}: {d.get('path', '')}")
        lines.append("================================================================")
        return "\n".join(lines)


class SessionManager:
    """Manages persistence and operations for AgentSessions under .truhowl/sessions/."""

    def __init__(self, workdir: str = ".") -> None:
        self.workdir = os.path.abspath(workdir)
        self.sessions_dir = os.path.join(self.workdir, ".truhowl", "sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _session_file(self, session_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_id}.json")

    def _next_id(self, prefix: str) -> str:
        """Millisecond-timestamp id, disambiguated when created in the same ms."""
        base = f"{prefix}_{int(time.time() * 1000)}"
        candidate = base
        n = 1
        while os.path.exists(self._session_file(candidate)):
            candidate = f"{base}_{n}"
            n += 1
        return candidate

    def create_session(
        self,
        agent_name: str = "Claude Code",
        task: str = "Agent Task",
        compartment_name: str = "AgentTask",
        permissions: List[str] | None = None,
        lane_id: str = "default_lane",
    ) -> AgentSession:
        """Create and persist a new AgentSession."""
        session_id = self._next_id("sess")
        perms = permissions or ["fs_read", "fs_exec"]
        session = AgentSession(
            session_id=session_id,
            workspace_id=os.path.basename(self.workdir),
            lane_id=lane_id,
            agent=agent_name,
            task=task,
            compartment_id=compartment_name,
            policy={"permissions": perms},
            status=SessionStatus.CREATED
        )
        session.emit_event("session.created", {"agent": agent_name, "lane_id": lane_id})
        self.save_session(session)
        return session

    def save_session(self, session: AgentSession) -> None:
        """Save session object to JSON file."""
        filepath = self._session_file(session.session_id)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, indent=2)

    def get_session(self, session_id: str) -> AgentSession | None:
        """Load session object by ID."""
        filepath = self._session_file(session_id)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Map legacy field names if necessary
            if "agent_name" in data and "agent" not in data:
                data["agent"] = data.pop("agent_name")
            if "compartment_name" in data and "compartment_id" not in data:
                data["compartment_id"] = data.pop("compartment_name")
            if "ended_at" in data and "finished_at" not in data:
                data["finished_at"] = data.pop("ended_at")
            
            # Filter unknown fields for AgentSession
            valid_fields = AgentSession.__dataclass_fields__.keys()
            filtered = {k: v for k, v in data.items() if k in valid_fields}
            return AgentSession(**filtered)
        except Exception:
            return None

    def list_sessions(self) -> list[AgentSession]:
        """List all recorded sessions in reverse chronological order."""
        sessions: list[AgentSession] = []
        if not os.path.exists(self.sessions_dir):
            return sessions

        for filename in sorted(os.listdir(self.sessions_dir), reverse=True):
            if filename.endswith(".json"):
                sid = filename[:-5]
                sess = self.get_session(sid)
                if sess:
                    sessions.append(sess)
        return sessions

    def rollback_session(self, session_id: str) -> bool:
        """Roll back the workspace to the session's pre-execution snapshot.

        Uses the most recent checkpoint that carries a ``snapshot_manifest``
        (taken by the CLI before the session ran).  Returns False when the
        session is unknown or no snapshot checkpoint exists - never a silent
        no-op.
        """
        session = self.get_session(session_id)
        if not session:
            return False

        snap_dir = None
        for cp in reversed(session.checkpoints):
            manifest = cp.get("snapshot_manifest")
            if manifest and os.path.isdir(manifest):
                snap_dir = manifest
                break
        if not snap_dir:
            _logger.warning(
                "No snapshot checkpoint for session %s; nothing to restore",
                session_id,
            )
            return False

        snap = SnapshotManager(workdir=self.workdir, snapshot_dir=snap_dir)
        snap.restore()
        session.status = SessionStatus.ROLLED_BACK
        session.emit_event("session.rolled_back", {"session_id": session_id})
        self.save_session(session)
        return True
