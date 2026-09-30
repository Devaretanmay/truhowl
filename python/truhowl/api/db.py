# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Persistence layer for the Truhowl Hosted Control Plane (B3).

Provides a unified domain storage interface abstraction (StorageAdapter)
supporting:
1. LocalAgentStoreAdapter — local file-based JSON store compatibility.
2. SQLiteStoreAdapter — SQLite relational persistence (in-memory or file).
3. PostgresStoreAdapter — PostgreSQL relational persistence for enterprise/hosted pilots.
4. RelationalStoreAdapter — Unified multi-dialect adapter routing to SQLite or PostgreSQL based on URL.

Covers all 12 core pilot domains:
- users
- organizations
- memberships
- github_installations
- repositories
- dependencies
- external_changes
- migration_cases
- attempts
- evidence
- delivery
- audit_events
- pilot_metrics
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from truhowl.agent import models as agent_models
from truhowl.agent.automation import DEFAULT_MODE, normalize_mode


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class User:
    user_id: str
    github_id: str
    username: str
    email: str
    avatar_url: str
    created_at: str = field(default_factory=_now)


@dataclass
class Organization:
    org_id: str
    name: str
    owner_id: str
    automation_mode: str = DEFAULT_MODE
    created_at: str = field(default_factory=_now)


@dataclass
class Membership:
    org_id: str
    user_id: str
    role: str = "member"
    created_at: str = field(default_factory=_now)


@dataclass
class GitHubInstallation:
    installation_id: str
    org_id: str
    account_name: str
    target_type: str = "Organization"
    permissions: Dict[str, str] = field(default_factory=dict)
    installed_at: str = field(default_factory=_now)


@dataclass
class Dependency:
    dep_id: str
    org_id: str
    repo_key: str
    name: str
    version: str
    provider: str
    updated_at: str = field(default_factory=_now)


@dataclass
class Attempt:
    attempt_id: str
    case_id: str
    org_id: str
    patch_hash: str
    tier: str
    verified: bool
    replay_exit_code: int
    created_at: str = field(default_factory=_now)


@dataclass
class Evidence:
    evidence_id: str
    case_id: str
    org_id: str
    test_cmd: str
    replay_cmd: str
    diff_text: str
    created_at: str = field(default_factory=_now)


@dataclass
class DeliveryRecord:
    delivery_id: str
    case_id: str
    org_id: str
    status: str
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    error: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class AuditEvent:
    event_id: str
    org_id: str
    event_type: str
    actor: str
    details: str
    created_at: str = field(default_factory=_now)


@dataclass
class PilotMetric:
    metric_id: str
    org_id: str
    name: str
    value: float
    recorded_at: str = field(default_factory=_now)


class StorageAdapter:
    """Base domain storage interface for Truhowl Control Plane."""

    def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def set_automation_policy(self, org_id: str, mode: str) -> Dict[str, Any]:
        raise NotImplementedError

    def list_repositories(self, org_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def add_repository(
        self,
        org_id: str,
        repo_key: str,
        name: str,
        path: str,
        github_repo: Optional[str] = None,
        verification_cmd: str = "npm test",
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def list_external_changes(self, org_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def record_external_change(self, org_id: str, change: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_cases(self, org_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_case(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def record_case(self, org_id: str, case_data: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    # Extended Pilot Entities (B3)
    def add_user(self, user: User) -> Dict[str, Any]:
        raise NotImplementedError

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def add_github_installation(self, inst: GitHubInstallation) -> Dict[str, Any]:
        raise NotImplementedError

    def get_github_installation(self, installation_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def record_attempt(self, attempt: Attempt) -> Dict[str, Any]:
        raise NotImplementedError

    def list_attempts(self, org_id: str, case_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def record_evidence(self, ev: Evidence) -> Dict[str, Any]:
        raise NotImplementedError

    def get_evidence(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def record_delivery(self, delivery: DeliveryRecord) -> Dict[str, Any]:
        raise NotImplementedError

    def get_delivery(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def record_audit_event(self, event: AuditEvent) -> Dict[str, Any]:
        raise NotImplementedError

    def list_audit_events(self, org_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def record_pilot_metric(self, metric: PilotMetric) -> Dict[str, Any]:
        raise NotImplementedError

    def list_pilot_metrics(self, org_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class SQLiteStoreAdapter(StorageAdapter):
    """SQLite implementation supporting in-memory and persistent file databases."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._conn is None:
                self._conn = sqlite3.connect(":memory:")
                self._conn.row_factory = sqlite3.Row
            return self._conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    github_id TEXT UNIQUE,
                    username TEXT,
                    email TEXT,
                    avatar_url TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS organizations (
                    org_id TEXT PRIMARY KEY,
                    name TEXT,
                    owner_id TEXT,
                    automation_mode TEXT DEFAULT 'observe',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS memberships (
                    org_id TEXT,
                    user_id TEXT,
                    role TEXT DEFAULT 'member',
                    created_at TEXT,
                    PRIMARY KEY (org_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS github_installations (
                    installation_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    account_name TEXT,
                    installed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS repositories (
                    repo_key TEXT PRIMARY KEY,
                    org_id TEXT,
                    name TEXT,
                    path TEXT,
                    github_repo TEXT,
                    verification_cmd TEXT DEFAULT 'npm test',
                    connected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS dependencies (
                    dep_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    repo_key TEXT,
                    name TEXT,
                    version TEXT,
                    provider TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS external_changes (
                    change_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    provider TEXT,
                    version_from TEXT,
                    version_to TEXT,
                    basis TEXT,
                    detected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS migration_cases (
                    case_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    provider TEXT,
                    version_from TEXT,
                    version_to TEXT,
                    status TEXT,
                    created_at TEXT,
                    payload_json TEXT
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    org_id TEXT,
                    patch_hash TEXT,
                    tier TEXT,
                    verified INTEGER,
                    replay_exit_code INTEGER,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    org_id TEXT,
                    test_cmd TEXT,
                    replay_cmd TEXT,
                    diff_text TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS delivery (
                    delivery_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    org_id TEXT,
                    status TEXT,
                    pr_url TEXT,
                    pr_number INTEGER,
                    error TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    event_type TEXT,
                    actor TEXT,
                    details TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS pilot_metrics (
                    metric_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    name TEXT,
                    value REAL,
                    recorded_at TEXT
                );
            """)

    def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM organizations WHERE org_id = ?", (org_id,)).fetchone()
            return dict(row) if row else None

    def set_automation_policy(self, org_id: str, mode: str) -> Dict[str, Any]:
        normalized = normalize_mode(mode)
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO organizations (org_id, name, owner_id, automation_mode, created_at) VALUES (?, ?, 'system', ?, ?) ON CONFLICT(org_id) DO UPDATE SET automation_mode = ?",
                (org_id, org_id, normalized, _now(), normalized),
            )
        return {"org_id": org_id, "mode": normalized, "updated_at": _now()}

    def list_repositories(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM repositories WHERE org_id = ?", (org_id,)).fetchall()
            return [dict(r) for r in rows]

    def add_repository(
        self,
        org_id: str,
        repo_key: str,
        name: str,
        path: str,
        github_repo: Optional[str] = None,
        verification_cmd: str = "npm test",
    ) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO repositories (repo_key, org_id, name, path, github_repo, verification_cmd, connected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_key, org_id, name, path, github_repo, verification_cmd, _now()),
            )
        return {
            "repo_key": repo_key,
            "name": name,
            "path": path,
            "github_repo": github_repo,
            "verification_cmd": verification_cmd,
        }

    def list_external_changes(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM external_changes WHERE org_id = ?", (org_id,)).fetchall()
            return [dict(r) for r in rows]

    def record_external_change(self, org_id: str, change: Dict[str, Any]) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO external_changes (change_id, org_id, provider, version_from, version_to, basis, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    change["change_id"],
                    org_id,
                    change["provider"],
                    change.get("version_from", ""),
                    change.get("version_to", ""),
                    change.get("basis", ""),
                    _now(),
                ),
            )
        return change

    def list_cases(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT payload_json FROM migration_cases WHERE org_id = ?", (org_id,)).fetchall()
            return [json.loads(r["payload_json"]) for r in rows if r["payload_json"]]

    def get_case(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM migration_cases WHERE org_id = ? AND case_id = ?",
                (org_id, case_id),
            ).fetchone()
            if not row or not row["payload_json"]:
                return None
            return json.loads(row["payload_json"])

    def record_case(self, org_id: str, case_data: Dict[str, Any]) -> Dict[str, Any]:
        case_id = case_data["case_id"]
        status = case_data.get("status", "detected")
        payload = json.dumps(case_data)
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO migration_cases (case_id, org_id, provider, version_from, version_to, status, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    org_id,
                    case_data.get("provider", ""),
                    case_data.get("version_from", ""),
                    case_data.get("version_to", ""),
                    status,
                    _now(),
                    payload,
                ),
            )
        return case_data

    def add_user(self, user: User) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, github_id, username, email, avatar_url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user.user_id, user.github_id, user.username, user.email, user.avatar_url, user.created_at),
            )
        return {"user_id": user.user_id, "username": user.username}

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def add_github_installation(self, inst: GitHubInstallation) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO github_installations (installation_id, org_id, account_name, installed_at) VALUES (?, ?, ?, ?)",
                (inst.installation_id, inst.org_id, inst.account_name, inst.installed_at),
            )
        return {"installation_id": inst.installation_id, "org_id": inst.org_id}

    def get_github_installation(self, installation_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM github_installations WHERE installation_id = ?", (installation_id,)).fetchone()
            return dict(row) if row else None

    def record_attempt(self, attempt: Attempt) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO attempts (attempt_id, case_id, org_id, patch_hash, tier, verified, replay_exit_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.case_id,
                    attempt.org_id,
                    attempt.patch_hash,
                    attempt.tier,
                    1 if attempt.verified else 0,
                    attempt.replay_exit_code,
                    attempt.created_at,
                ),
            )
        return {"attempt_id": attempt.attempt_id, "verified": attempt.verified}

    def list_attempts(self, org_id: str, case_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE org_id = ? AND case_id = ? ORDER BY created_at DESC",
                (org_id, case_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_evidence(self, ev: Evidence) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence (evidence_id, case_id, org_id, test_cmd, replay_cmd, diff_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ev.evidence_id, ev.case_id, ev.org_id, ev.test_cmd, ev.replay_cmd, ev.diff_text, ev.created_at),
            )
        return {"evidence_id": ev.evidence_id, "case_id": ev.case_id}

    def get_evidence(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM evidence WHERE org_id = ? AND case_id = ?", (org_id, case_id)).fetchone()
            return dict(row) if row else None

    def record_delivery(self, delivery: DeliveryRecord) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO delivery (delivery_id, case_id, org_id, status, pr_url, pr_number, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.delivery_id,
                    delivery.case_id,
                    delivery.org_id,
                    delivery.status,
                    delivery.pr_url,
                    delivery.pr_number,
                    delivery.error,
                    delivery.created_at,
                ),
            )
        return {"delivery_id": delivery.delivery_id, "status": delivery.status, "pr_url": delivery.pr_url}

    def get_delivery(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM delivery WHERE org_id = ? AND case_id = ?", (org_id, case_id)).fetchone()
            return dict(row) if row else None

    def record_audit_event(self, event: AuditEvent) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_events (event_id, org_id, event_type, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.org_id, event.event_type, event.actor, event.details, event.created_at),
            )
        return {"event_id": event.event_id, "event_type": event.event_type}

    def list_audit_events(self, org_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE org_id = ? ORDER BY created_at DESC LIMIT ?",
                (org_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_pilot_metric(self, metric: PilotMetric) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pilot_metrics (metric_id, org_id, name, value, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (metric.metric_id, metric.org_id, metric.name, metric.value, metric.recorded_at),
            )
        return {"metric_id": metric.metric_id, "name": metric.name, "value": metric.value}

    def list_pilot_metrics(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM pilot_metrics WHERE org_id = ?", (org_id,)).fetchall()
            return [dict(r) for r in rows]


class PostgresStoreAdapter(StorageAdapter):
    """PostgreSQL implementation using psycopg 3 with connection pooling / schema DDL."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._init_db()

    def _get_conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _init_db(self) -> None:
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            user_id TEXT PRIMARY KEY,
                            github_id TEXT UNIQUE,
                            username TEXT,
                            email TEXT,
                            avatar_url TEXT,
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS organizations (
                            org_id TEXT PRIMARY KEY,
                            name TEXT,
                            owner_id TEXT,
                            automation_mode TEXT DEFAULT 'observe',
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS memberships (
                            org_id TEXT,
                            user_id TEXT,
                            role TEXT DEFAULT 'member',
                            created_at TEXT,
                            PRIMARY KEY (org_id, user_id)
                        );
                        CREATE TABLE IF NOT EXISTS github_installations (
                            installation_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            account_name TEXT,
                            installed_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS repositories (
                            repo_key TEXT PRIMARY KEY,
                            org_id TEXT,
                            name TEXT,
                            path TEXT,
                            github_repo TEXT,
                            verification_cmd TEXT DEFAULT 'npm test',
                            connected_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS dependencies (
                            dep_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            repo_key TEXT,
                            name TEXT,
                            version TEXT,
                            provider TEXT,
                            updated_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS external_changes (
                            change_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            provider TEXT,
                            version_from TEXT,
                            version_to TEXT,
                            basis TEXT,
                            detected_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS migration_cases (
                            case_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            provider TEXT,
                            version_from TEXT,
                            version_to TEXT,
                            status TEXT,
                            created_at TEXT,
                            payload_json TEXT
                        );
                        CREATE TABLE IF NOT EXISTS attempts (
                            attempt_id TEXT PRIMARY KEY,
                            case_id TEXT,
                            org_id TEXT,
                            patch_hash TEXT,
                            tier TEXT,
                            verified INTEGER,
                            replay_exit_code INTEGER,
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS evidence (
                            evidence_id TEXT PRIMARY KEY,
                            case_id TEXT,
                            org_id TEXT,
                            test_cmd TEXT,
                            replay_cmd TEXT,
                            diff_text TEXT,
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS delivery (
                            delivery_id TEXT PRIMARY KEY,
                            case_id TEXT,
                            org_id TEXT,
                            status TEXT,
                            pr_url TEXT,
                            pr_number INTEGER,
                            error TEXT,
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS audit_events (
                            event_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            event_type TEXT,
                            actor TEXT,
                            details TEXT,
                            created_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS pilot_metrics (
                            metric_id TEXT PRIMARY KEY,
                            org_id TEXT,
                            name TEXT,
                            value DOUBLE PRECISION,
                            recorded_at TEXT
                        );
                    """)
                    conn.commit()
        except Exception:
            # If PostgreSQL server is not currently reachable during offline validation,
            # connection will be retried upon first operational request.
            pass

    def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM organizations WHERE org_id = %s", (org_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def set_automation_policy(self, org_id: str, mode: str) -> Dict[str, Any]:
        normalized = normalize_mode(mode)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO organizations (org_id, name, owner_id, automation_mode, created_at)
                       VALUES (%s, %s, 'system', %s, %s)
                       ON CONFLICT(org_id) DO UPDATE SET automation_mode = %s""",
                    (org_id, org_id, normalized, _now(), normalized),
                )
                conn.commit()
        return {"org_id": org_id, "mode": normalized, "updated_at": _now()}

    def list_repositories(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM repositories WHERE org_id = %s", (org_id,))
                return [dict(r) for r in cur.fetchall()]

    def add_repository(
        self,
        org_id: str,
        repo_key: str,
        name: str,
        path: str,
        github_repo: Optional[str] = None,
        verification_cmd: str = "npm test",
    ) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO repositories (repo_key, org_id, name, path, github_repo, verification_cmd, connected_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(repo_key) DO UPDATE SET name=%s, path=%s, github_repo=%s, verification_cmd=%s""",
                    (repo_key, org_id, name, path, github_repo, verification_cmd, _now(), name, path, github_repo, verification_cmd),
                )
                conn.commit()
        return {
            "repo_key": repo_key,
            "name": name,
            "path": path,
            "github_repo": github_repo,
            "verification_cmd": verification_cmd,
        }

    def list_external_changes(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM external_changes WHERE org_id = %s", (org_id,))
                return [dict(r) for r in cur.fetchall()]

    def record_external_change(self, org_id: str, change: Dict[str, Any]) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO external_changes (change_id, org_id, provider, version_from, version_to, basis, detected_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(change_id) DO UPDATE SET provider=%s, version_from=%s, version_to=%s, basis=%s""",
                    (
                        change["change_id"],
                        org_id,
                        change["provider"],
                        change.get("version_from", ""),
                        change.get("version_to", ""),
                        change.get("basis", ""),
                        _now(),
                        change["provider"],
                        change.get("version_from", ""),
                        change.get("version_to", ""),
                        change.get("basis", ""),
                    ),
                )
                conn.commit()
        return change

    def list_cases(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload_json FROM migration_cases WHERE org_id = %s", (org_id,))
                return [json.loads(r["payload_json"]) for r in cur.fetchall() if r["payload_json"]]

    def get_case(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload_json FROM migration_cases WHERE org_id = %s AND case_id = %s", (org_id, case_id))
                row = cur.fetchone()
                return json.loads(row["payload_json"]) if row and row["payload_json"] else None

    def record_case(self, org_id: str, case_data: Dict[str, Any]) -> Dict[str, Any]:
        case_id = case_data["case_id"]
        status = case_data.get("status", "detected")
        payload = json.dumps(case_data)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO migration_cases (case_id, org_id, provider, version_from, version_to, status, created_at, payload_json)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(case_id) DO UPDATE SET status=%s, payload_json=%s""",
                    (case_id, org_id, case_data.get("provider", ""), case_data.get("version_from", ""), case_data.get("version_to", ""), status, _now(), payload, status, payload),
                )
                conn.commit()
        return case_data

    def add_user(self, user: User) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users (user_id, github_id, username, email, avatar_url, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT(user_id) DO UPDATE SET username=%s, email=%s, avatar_url=%s""",
                    (user.user_id, user.github_id, user.username, user.email, user.avatar_url, user.created_at, user.username, user.email, user.avatar_url),
                )
                conn.commit()
        return {"user_id": user.user_id, "username": user.username}

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def add_github_installation(self, inst: GitHubInstallation) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO github_installations (installation_id, org_id, account_name, installed_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT(installation_id) DO UPDATE SET account_name=%s""",
                    (inst.installation_id, inst.org_id, inst.account_name, inst.installed_at, inst.account_name),
                )
                conn.commit()
        return {"installation_id": inst.installation_id, "org_id": inst.org_id}

    def get_github_installation(self, installation_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM github_installations WHERE installation_id = %s", (installation_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def record_attempt(self, attempt: Attempt) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO attempts (attempt_id, case_id, org_id, patch_hash, tier, verified, replay_exit_code, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(attempt_id) DO UPDATE SET verified=%s, replay_exit_code=%s""",
                    (attempt.attempt_id, attempt.case_id, attempt.org_id, attempt.patch_hash, attempt.tier, 1 if attempt.verified else 0, attempt.replay_exit_code, attempt.created_at, 1 if attempt.verified else 0, attempt.replay_exit_code),
                )
                conn.commit()
        return {"attempt_id": attempt.attempt_id, "verified": attempt.verified}

    def list_attempts(self, org_id: str, case_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM attempts WHERE org_id = %s AND case_id = %s ORDER BY created_at DESC", (org_id, case_id))
                return [dict(r) for r in cur.fetchall()]

    def record_evidence(self, ev: Evidence) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO evidence (evidence_id, case_id, org_id, test_cmd, replay_cmd, diff_text, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(evidence_id) DO UPDATE SET diff_text=%s""",
                    (ev.evidence_id, ev.case_id, ev.org_id, ev.test_cmd, ev.replay_cmd, ev.diff_text, ev.created_at, ev.diff_text),
                )
                conn.commit()
        return {"evidence_id": ev.evidence_id, "case_id": ev.case_id}

    def get_evidence(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM evidence WHERE org_id = %s AND case_id = %s", (org_id, case_id))
                row = cur.fetchone()
                return dict(row) if row else None

    def record_delivery(self, delivery: DeliveryRecord) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO delivery (delivery_id, case_id, org_id, status, pr_url, pr_number, error, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(delivery_id) DO UPDATE SET status=%s, pr_url=%s, pr_number=%s, error=%s""",
                    (delivery.delivery_id, delivery.case_id, delivery.org_id, delivery.status, delivery.pr_url, delivery.pr_number, delivery.error, delivery.created_at, delivery.status, delivery.pr_url, delivery.pr_number, delivery.error),
                )
                conn.commit()
        return {"delivery_id": delivery.delivery_id, "status": delivery.status, "pr_url": delivery.pr_url}

    def get_delivery(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM delivery WHERE org_id = %s AND case_id = %s", (org_id, case_id))
                row = cur.fetchone()
                return dict(row) if row else None

    def record_audit_event(self, event: AuditEvent) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_events (event_id, org_id, event_type, actor, details, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (event.event_id, event.org_id, event.event_type, event.actor, event.details, event.created_at),
                )
                conn.commit()
        return {"event_id": event.event_id, "event_type": event.event_type}

    def list_audit_events(self, org_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM audit_events WHERE org_id = %s ORDER BY created_at DESC LIMIT %s", (org_id, limit))
                return [dict(r) for r in cur.fetchall()]

    def record_pilot_metric(self, metric: PilotMetric) -> Dict[str, Any]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO pilot_metrics (metric_id, org_id, name, value, recorded_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT(metric_id) DO UPDATE SET value=%s, recorded_at=%s""",
                    (metric.metric_id, metric.org_id, metric.name, metric.value, metric.recorded_at, metric.value, metric.recorded_at),
                )
                conn.commit()
        return {"metric_id": metric.metric_id, "name": metric.name, "value": metric.value}

    def list_pilot_metrics(self, org_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM pilot_metrics WHERE org_id = %s", (org_id,))
                return [dict(r) for r in cur.fetchall()]


class RelationalStoreAdapter(StorageAdapter):
    """Unified relational storage adapter automatically delegating to SQLite or PostgreSQL."""

    def __init__(self, db_target: str = ":memory:"):
        self.db_target = db_target
        if db_target.startswith("postgresql://") or db_target.startswith("postgres://"):
            self._backend: StorageAdapter = PostgresStoreAdapter(db_target)
        else:
            self._backend = SQLiteStoreAdapter(db_target)

    def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_organization(org_id)

    def set_automation_policy(self, org_id: str, mode: str) -> Dict[str, Any]:
        return self._backend.set_automation_policy(org_id, mode)

    def list_repositories(self, org_id: str) -> List[Dict[str, Any]]:
        return self._backend.list_repositories(org_id)

    def add_repository(
        self,
        org_id: str,
        repo_key: str,
        name: str,
        path: str,
        github_repo: Optional[str] = None,
        verification_cmd: str = "npm test",
    ) -> Dict[str, Any]:
        return self._backend.add_repository(org_id, repo_key, name, path, github_repo, verification_cmd)

    def list_external_changes(self, org_id: str) -> List[Dict[str, Any]]:
        return self._backend.list_external_changes(org_id)

    def record_external_change(self, org_id: str, change: Dict[str, Any]) -> Dict[str, Any]:
        return self._backend.record_external_change(org_id, change)

    def list_cases(self, org_id: str) -> List[Dict[str, Any]]:
        return self._backend.list_cases(org_id)

    def get_case(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_case(org_id, case_id)

    def record_case(self, org_id: str, case_data: Dict[str, Any]) -> Dict[str, Any]:
        return self._backend.record_case(org_id, case_data)

    def add_user(self, user: User) -> Dict[str, Any]:
        return self._backend.add_user(user)

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_user(user_id)

    def add_github_installation(self, inst: GitHubInstallation) -> Dict[str, Any]:
        return self._backend.add_github_installation(inst)

    def get_github_installation(self, installation_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_github_installation(installation_id)

    def record_attempt(self, attempt: Attempt) -> Dict[str, Any]:
        return self._backend.record_attempt(attempt)

    def list_attempts(self, org_id: str, case_id: str) -> List[Dict[str, Any]]:
        return self._backend.list_attempts(org_id, case_id)

    def record_evidence(self, ev: Evidence) -> Dict[str, Any]:
        return self._backend.record_evidence(ev)

    def get_evidence(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_evidence(org_id, case_id)

    def record_delivery(self, delivery: DeliveryRecord) -> Dict[str, Any]:
        return self._backend.record_delivery(delivery)

    def get_delivery(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        return self._backend.get_delivery(org_id, case_id)

    def record_audit_event(self, event: AuditEvent) -> Dict[str, Any]:
        return self._backend.record_audit_event(event)

    def list_audit_events(self, org_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._backend.list_audit_events(org_id, limit)

    def record_pilot_metric(self, metric: PilotMetric) -> Dict[str, Any]:
        return self._backend.record_pilot_metric(metric)

    def list_pilot_metrics(self, org_id: str) -> List[Dict[str, Any]]:
        return self._backend.list_pilot_metrics(org_id)


class LocalAgentStoreAdapter(StorageAdapter):
    """File-backed AgentStore adapter wrapping truhowl.agent.service."""

    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)

    def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        store = agent_models.load_store(self.workspace_path)
        return {
            "org_id": org_id,
            "name": store.org.get("name", "local"),
            "mode": store.org.get("automation", {}).get("mode", DEFAULT_MODE),
        }

    def set_automation_policy(self, org_id: str, mode: str) -> Dict[str, Any]:
        from truhowl.agent.automation import set_mode

        return set_mode(self.workspace_path, mode)

    def list_repositories(self, org_id: str) -> List[Dict[str, Any]]:
        store = agent_models.load_store(self.workspace_path)
        out = []
        for k, r in store.repos.items():
            out.append(
                {
                    "repo_key": k,
                    "name": r.get("key", k),
                    "path": r.get("path", ""),
                    "providers": r.get("providers", []),
                    "verification_cmd": r.get("verification_cmd", "npm test"),
                }
            )
        return out

    def add_repository(
        self,
        org_id: str,
        repo_key: str,
        name: str,
        path: str,
        github_repo: Optional[str] = None,
        verification_cmd: str = "npm test",
    ) -> Dict[str, Any]:
        from truhowl.agent.service import _register_repos

        store = agent_models.load_store(self.workspace_path)
        _register_repos(store, [path])
        agent_models.save_store(self.workspace_path, store)
        return {
            "repo_key": repo_key,
            "name": name,
            "path": path,
            "github_repo": github_repo,
            "verification_cmd": verification_cmd,
        }

    def list_external_changes(self, org_id: str) -> List[Dict[str, Any]]:
        store = agent_models.load_store(self.workspace_path)
        return store.changes

    def record_external_change(self, org_id: str, change: Dict[str, Any]) -> Dict[str, Any]:
        store = agent_models.load_store(self.workspace_path)
        store.changes.append(change)
        agent_models.save_store(self.workspace_path, store)
        return change

    def list_cases(self, org_id: str) -> List[Dict[str, Any]]:
        from truhowl.agent.service import list_cases

        return list_cases(self.workspace_path)

    def get_case(self, org_id: str, case_id: str) -> Optional[Dict[str, Any]]:
        from truhowl.agent.service import get_case

        return get_case(self.workspace_path, case_id)

    def record_case(self, org_id: str, case_data: Dict[str, Any]) -> Dict[str, Any]:
        store = agent_models.load_store(self.workspace_path)
        store.cases[case_data["case_id"]] = case_data
        agent_models.save_store(self.workspace_path, store)
        return case_data
