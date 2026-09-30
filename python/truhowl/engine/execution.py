"""Unified Execution domain primitive.

Every workload Truhowl governs - interactive agents, workflows, scripts,
MCP servers - is represented as an Execution.  The kernel does not care
which kind it is; the compartment policy is the same abstraction.

Hierarchy
---------
::

    Execution
    ├── kind: INTERACTIVE   claude / codex / opencode (PTY-attached)
    ├── kind: WORKFLOW      LangGraph / CrewAI / custom Python
    ├── kind: PROCESS       pytest / arbitrary shell command
    └── kind: SERVICE       MCP server / long-running daemon
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

_logger = logging.getLogger("truhowl.engine.execution")

# Agent binaries recognized for Agent-Origin provenance classification
# (mirrors cli._KNOWN_AGENTS; kept local to avoid a cli import cycle).
_AGENT_BINARIES = frozenset({"claude", "codex", "opencode", "cursor", "aider"})


class ExecutionKind:
    INTERACTIVE = "INTERACTIVE"
    WORKFLOW = "WORKFLOW"
    PROCESS = "PROCESS"
    SERVICE = "SERVICE"


class ExecutionStatus:
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"


@dataclass
class Execution:
    """First-class domain object representing one governed workload."""

    execution_id: str
    kind: str
    command: list[str]
    workspace_id: str = "default"
    compartment_id: str = "default"
    lane_id: str = "default"
    session_id: str | None = None
    pid: int | None = None
    status: str = ExecutionStatus.CREATED
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    snapshot_dir: str | None = None
    policy: dict[str, Any] = field(default_factory=lambda: {"permissions": ["fs_read", "fs_write", "fs_exec"]})
    events: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)

    def emit(self, name: str, payload: Dict[str, Any] | None = None) -> None:
        """Append a structured lifecycle event."""
        self.events.append({"timestamp": time.time(), "name": name, "payload": payload or {}})

    def start(self, pid: int | None = None) -> None:
        self.status = ExecutionStatus.RUNNING
        self.started_at = time.time()
        self.pid = pid
        self.emit("execution.started", {"command": self.command, "pid": pid})

    def complete(self, returncode: int, changes: List[Dict[str, Any]] | None = None) -> None:
        self.returncode = returncode
        self.finished_at = time.time()
        self.status = ExecutionStatus.COMPLETED if returncode == 0 else ExecutionStatus.FAILED
        if changes:
            self.changes = changes
        self.emit("execution.completed", {"returncode": returncode, "change_count": len(self.changes)})

    def apply(self) -> None:
        """Promote this execution's change set into the workspace baseline."""
        self.status = ExecutionStatus.APPLIED
        self.emit("execution.applied", {"change_count": len(self.changes)})

    def git_trailers(self) -> str:
        """Format Git trailers per the Agent Provenance Trailers spec (SPEC.md).

        Emits Agent-* field names. Pre-rename releases wrote legacy
        Truhowl-*, Volf-*, Sheepdog-*, or Compart-* names; readers should accept
        all variants (see SPEC.md, "Compatibility").
        """
        # Determine sandbox provenance truthfully per SPEC.md
        # SPEC.md: "clean, blocked (policy violations occurred), or none (unsandboxed)"
        sandboxed = self.policy.get("sandbox", True) if isinstance(self.policy, dict) else True
        blocked_count = sum(
            1 for ev in self.events
            if "blocked" in ev.get("name", "").lower() or "denied" in ev.get("name", "").lower()
        )
        if not sandboxed:
            sandbox_status = "none"
        elif blocked_count > 0:
            sandbox_status = "blocked"
        else:
            sandbox_status = "clean"

        origin = (
            "agent"
            if self.command and self.command[0] in _AGENT_BINARIES
            else "agent-assisted"
        )

        lines = [
            f"Agent-Origin: {origin}",
            f"Agent-Agent: {self.agent_name}",
            f"Agent-Execution: {self.execution_id}",
            f"Agent-Compartment: {self.compartment_id}",
            f"Agent-Sandbox: {sandbox_status}",
        ]
        if isinstance(self.policy, dict) and self.policy.get("isolation"):
            lines.append(f"Execution-Isolation: {self.policy['isolation']}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    @property
    def agent_name(self) -> str:
        """Human-readable agent name derived from the command."""
        return self.command[0] if self.command else "unknown"


class ExecutionManager:
    """Persists and retrieves Execution domain objects under .truhowl/executions/."""

    def __init__(self, workdir: str = ".") -> None:
        self.workdir = os.path.abspath(workdir)
        self.executions_dir = os.path.join(self.workdir, ".truhowl", "executions")
        os.makedirs(self.executions_dir, exist_ok=True)

    def _path(self, execution_id: str) -> str:
        safe_id = os.path.basename(execution_id)
        if safe_id.endswith(".json"):
            safe_id = safe_id[:-5]
        return os.path.join(self.executions_dir, f"{safe_id}.json")

    def _next_id(self, prefix: str) -> str:
        """Millisecond-timestamp id, disambiguated when created in the same ms."""
        base = f"{prefix}_{int(time.time() * 1000)}"
        candidate = base
        n = 1
        while os.path.exists(self._path(candidate)):
            candidate = f"{base}_{n}"
            n += 1
        return candidate

    def create(
        self,
        kind: str,
        command: list[str],
        compartment_id: str = "default",
        lane_id: str = "default",
        policy: Dict[str, Any] | None = None,
        extra_env: Dict[str, str] | None = None,
    ) -> Execution:
        """Create and persist a new Execution."""
        eid = self._next_id("exec")
        exec_ = Execution(
            execution_id=eid,
            kind=kind,
            command=list(command),
            workspace_id=os.path.basename(self.workdir),
            compartment_id=compartment_id,
            lane_id=lane_id,
            policy=policy or {"permissions": ["fs_read", "fs_write", "fs_exec"]},
            extra_env=extra_env or {},
        )
        exec_.emit("execution.created", {"kind": kind, "command": command})
        self.save(exec_)
        return exec_

    def save(self, execution: Execution) -> None:
        with open(self._path(execution.execution_id), "w", encoding="utf-8") as f:
            json.dump(execution.to_dict(), f, indent=2)

    def get(self, execution_id: str) -> Execution | None:
        path = self._path(execution_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            valid = Execution.__dataclass_fields__.keys()
            return Execution(**{k: v for k, v in data.items() if k in valid})
        except Exception as exc:
            _logger.warning("Failed to load execution %s: %s", execution_id, exc)
            return None

    def list_all(self, status_filter: str | None = None) -> list[Execution]:
        results: list[Execution] = []
        if not os.path.exists(self.executions_dir):
            return results
        for fname in sorted(os.listdir(self.executions_dir), reverse=True):
            if not fname.endswith(".json"):
                continue
            ex = self.get(fname[:-5])
            if ex and (status_filter is None or ex.status == status_filter):
                results.append(ex)
        return results

    def list_running(self) -> list[Execution]:
        return self.list_all(status_filter=ExecutionStatus.RUNNING)
