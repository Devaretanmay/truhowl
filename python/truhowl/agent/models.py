"""Truhowl agent domain model: persistent state for the maintenance agent.

The agent watches external SDK/API changes, tracks which repositories are
affected, repairs them, proves the repair, and delivers the result. Every
object below persists to the workspace agent store
(`.truhowl/agent/store.json`) as plain JSON so the agent can explain its
state entirely from persisted evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

STORE_SCHEMA = "truhowl.agent.store.v1"
STORE_RELPATH = os.path.join(".truhowl", "agent", "store.json")

# ── Per-repository lifecycle inside a MigrationCase ──────────────────────────

DETECTED = "detected"
ANALYZING = "analyzing"
PLANNING = "planning"
REPAIRING = "repairing"
VERIFYING = "verifying"
VERIFIED = "verified"
PR_READY = "pr-ready"
REFUSED = "refused"
NEEDS_ATTENTION = "needs-attention"

TERMINAL_STATES = frozenset({VERIFIED, PR_READY, REFUSED, NEEDS_ATTENTION})
ACTIONABLE_STATES = frozenset({REFUSED, NEEDS_ATTENTION})

_TRANSITIONS: dict[str, frozenset[str]] = {
    DETECTED: frozenset({ANALYZING}),
    ANALYZING: frozenset({PLANNING, REFUSED}),
    PLANNING: frozenset({REPAIRING, REFUSED, NEEDS_ATTENTION}),
    REPAIRING: frozenset({VERIFYING, REFUSED}),
    VERIFYING: frozenset({VERIFIED, REFUSED, NEEDS_ATTENTION}),
    VERIFIED: frozenset({PR_READY}),
    PR_READY: frozenset(),
    REFUSED: frozenset({ANALYZING}),
    NEEDS_ATTENTION: frozenset({ANALYZING}),
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def case_id_for(provider: str, version_from: str, version_to: str) -> str:
    seed = "|".join([(provider or "").lower(), version_from or "", version_to or ""])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
    safe = "".join(c if c.isalnum() else "-" for c in (provider or "change").lower())[:24] or "change"
    return f"{safe}-{digest}"


# ── Domain objects ───────────────────────────────────────────────────────────

@dataclass
class Organization:
    name: str = ""
    repo_keys: list[str] = field(default_factory=list)


@dataclass
class Repository:
    key: str = ""
    path: str = ""
    providers: list[str] = field(default_factory=list)


@dataclass
class Dependency:
    repo_key: str = ""
    provider: str = ""
    current_version: str = ""


@dataclass
class ExternalChange:
    change_id: str = ""
    provider: str = ""
    version_from: str = ""
    version_to: str = ""
    basis: str = "registry metadata (change not independently observed)"
    guide_url: str = ""
    detected_at: str = ""


@dataclass
class AffectedUsage:
    repo_key: str = ""
    provider: str = ""
    files: list[str] = field(default_factory=list)
    callsites_count: int = 0
    wrappers: list[str] = field(default_factory=list)


@dataclass
class CaseRepo:
    """Lifecycle state of one repository inside a MigrationCase."""

    repo_key: str = ""
    path: str = ""
    state: str = DETECTED
    attempts: int = 0
    refusal_reason: str = ""
    pr_url: str | None = None
    pr_number: int | None = None
    updated_at: str = ""

    def transition(self, to_state: str) -> None:
        allowed = _TRANSITIONS.get(self.state, frozenset())
        if to_state not in allowed:
            raise ValueError(f"Illegal transition {self.state} -> {to_state}")
        self.state = to_state
        self.updated_at = _now()


@dataclass
class MigrationCase:
    case_id: str = ""
    provider: str = ""
    version_from: str = ""
    version_to: str = ""
    guide_url: str = ""
    summary: str = ""
    repos: list[CaseRepo] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def repo(self, repo_key: str) -> CaseRepo | None:
        for r in self.repos:
            if r.repo_key == repo_key:
                return r
        return None


@dataclass
class MigrationPlanRecord:
    case_id: str = ""
    repo_key: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    complete: bool = False
    recorded_at: str = ""


@dataclass
class RepairAttempt:
    case_id: str = ""
    repo_key: str = ""
    attempt: int = 0
    strategy: str = ""
    success: bool = False
    test_exit_code: int = -1
    started_at: str = ""


@dataclass
class VerificationEvidence:
    case_id: str = ""
    repo_key: str = ""
    test_command: str = ""
    test_exit_code: int = -1
    test_duration_ms: int = 0
    files_modified: int = 0
    unintended_files_modified: int = 0
    blast_radius_zero: bool = False
    diff_files: list[str] = field(default_factory=list)
    recorded_at: str = ""


@dataclass
class VerifiedMigration:
    case_id: str = ""
    repo_key: str = ""
    files_modified: int = 0
    test_exit_code: int = 0
    verified_at: str = ""


@dataclass
class Refusal:
    case_id: str = ""
    repo_key: str = ""
    reason: str = ""
    evidence_summary: str = ""
    recorded_at: str = ""


@dataclass
class PullRequest:
    case_id: str = ""
    repo_key: str = ""
    url: str = ""
    number: int | None = None
    state: str = "ready"
    recorded_at: str = ""


# ── Store ────────────────────────────────────────────────────────────────────

@dataclass
class AgentStore:
    schema: str = STORE_SCHEMA
    org: dict[str, Any] = field(default_factory=dict)
    repos: dict[str, Any] = field(default_factory=dict)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    usages: list[dict[str, Any]] = field(default_factory=list)
    cases: dict[str, Any] = field(default_factory=dict)
    plans: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verified: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    pull_requests: list[dict[str, Any]] = field(default_factory=list)


def store_path(workspace_root: str) -> str:
    return os.path.join(os.path.abspath(workspace_root), STORE_RELPATH)


def load_store(workspace_root: str) -> AgentStore:
    path = store_path(workspace_root)
    if not os.path.isfile(path):
        return AgentStore()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return AgentStore()
    if raw.get("schema") != STORE_SCHEMA:
        return AgentStore()
    store = AgentStore()
    for k in ("org", "repos", "dependencies", "changes", "usages", "cases",
              "plans", "attempts", "evidence", "verified", "refusals", "pull_requests"):
        if k in raw:
            setattr(store, k, raw[k])
    return store


def save_store(workspace_root: str, store: AgentStore) -> str:
    path = store_path(workspace_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(store), f, indent=2)
        f.write("\n")
    return path
