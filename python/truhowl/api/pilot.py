# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Pilot Controls, Observability, and Metrics Tracker for Truhowl (B13, B14, B18).

Implements:
1. Pilot limits & organization allowlisting (PILOT_MODE=true).
2. Observability for web, job, migration, model, GitHub, and verification errors.
3. Domain metrics tracking with the foundational reliability invariant: FALSE_VERIFIED = 0.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PilotLimitsConfig:
    enabled: bool = False
    allowed_orgs: List[str] = field(default_factory=lambda: ["org_default", "pilot_partner_1", "pilot_partner_2"])
    max_orgs: int = 5
    max_repos_per_org: int = 10
    max_daily_migrations: int = 20
    max_concurrent_repairs: int = 2


def get_pilot_config() -> PilotLimitsConfig:
    enabled = os.environ.get("PILOT_MODE", "false").lower() in ("true", "1", "yes")
    allowed = os.environ.get("PILOT_ALLOWED_ORGS", "").split(",")
    allowed_list = [o.strip() for o in allowed if o.strip()] or ["org_default", "pilot_partner_1", "pilot_partner_2"]
    return PilotLimitsConfig(enabled=enabled, allowed_orgs=allowed_list)


class PilotLimitsEnforcer:
    """Enforces pilot quotas and partner organization allowlists."""

    def __init__(self, config: Optional[PilotLimitsConfig] = None):
        self.config = config or get_pilot_config()
        self._daily_migration_counts: Dict[str, int] = defaultdict(int)
        self._active_repair_jobs: Dict[str, int] = defaultdict(int)

    def check_organization_access(self, org_id: str) -> Tuple[bool, str]:
        if not self.config.enabled:
            return True, ""
        if org_id not in self.config.allowed_orgs:
            return False, f"Organization '{org_id}' is not in the design-partner pilot allowlist."
        return True, ""

    def check_can_add_repo(self, org_id: str, current_repo_count: int) -> Tuple[bool, str]:
        if not self.config.enabled:
            return True, ""
        ok, reason = self.check_organization_access(org_id)
        if not ok:
            return False, reason
        if current_repo_count >= self.config.max_repos_per_org:
            return False, f"Pilot repository limit reached ({self.config.max_repos_per_org} max)."
        return True, ""

    def check_can_start_migration(self, org_id: str) -> Tuple[bool, str]:
        if not self.config.enabled:
            return True, ""
        ok, reason = self.check_organization_access(org_id)
        if not ok:
            return False, reason
        if self._daily_migration_counts[org_id] >= self.config.max_daily_migrations:
            return False, f"Daily pilot migration quota reached ({self.config.max_daily_migrations}/day)."
        if self._active_repair_jobs[org_id] >= self.config.max_concurrent_repairs:
            return False, f"Max concurrent repairs reached ({self.config.max_concurrent_repairs} active)."
        return True, ""

    def record_migration_started(self, org_id: str) -> None:
        self._daily_migration_counts[org_id] += 1
        self._active_repair_jobs[org_id] += 1

    def record_migration_finished(self, org_id: str) -> None:
        if self._active_repair_jobs[org_id] > 0:
            self._active_repair_jobs[org_id] -= 1


class ObservabilityTracker:
    """Lightweight operational observability for errors, latencies, and durations."""

    def __init__(self):
        self._error_counts: Dict[str, int] = defaultdict(int)
        self._durations: Dict[str, List[float]] = defaultdict(list)
        self._recent_errors: List[Dict[str, Any]] = []

    def record_error(self, category: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Categories: web, job, migration, model, github, verification."""
        self._error_counts[category] += 1
        record = {
            "category": category,
            "message": message,
            "context": context or {},
            "timestamp": time.time(),
        }
        self._recent_errors.append(record)
        if len(self._recent_errors) > 100:
            self._recent_errors.pop(0)

    def record_duration(self, operation: str, duration_seconds: float) -> None:
        self._durations[operation].append(duration_seconds)
        if len(self._durations[operation]) > 200:
            self._durations[operation].pop(0)

    def get_summary(self) -> Dict[str, Any]:
        avg_latencies = {}
        for op, durs in self._durations.items():
            avg_latencies[op] = round(sum(durs) / len(durs), 3) if durs else 0.0

        return {
            "error_counts": dict(self._error_counts),
            "average_durations_sec": avg_latencies,
            "recent_errors": self._recent_errors[-10:],
        }


class PilotMetricsTracker:
    """Design-partner pilot metrics tracker.

    Strict reliability invariant: false_verified is always 0.
    """

    def __init__(self):
        self._metrics: Dict[str, Dict[str, float]] = defaultdict(lambda: {
            "changes_detected": 0,
            "repos_affected": 0,
            "migration_attempts": 0,
            "verified_migrations": 0,
            "behavioral_verified": 0,
            "compile_verified": 0,
            "refused": 0,
            "false_verified": 0,
            "prs_opened": 0,
            "prs_merged": 0,
            "human_interventions": 0,
            "time_to_verified_pr_seconds": 0.0,
            "model_cost_usd": 0.0,
            "tokens_consumed": 0,
        })

    def increment(self, org_id: str, metric: str, amount: float = 1.0) -> None:
        self._metrics[org_id][metric] += amount

    def set_metric(self, org_id: str, metric: str, value: float) -> None:
        self._metrics[org_id][metric] = value

    def record_verification_observation(
        self,
        org_id: str,
        claimed_verified: bool,
        independent_replay_exit_code: int,
        scope_ok: bool,
    ) -> bool:
        """Observational metric check: audits if independent replay contradicts verification claim."""
        is_false_verified = claimed_verified and (independent_replay_exit_code != 0 or not scope_ok)
        if is_false_verified:
            self._metrics[org_id]["false_verified"] += 1.0
            return False

        if claimed_verified:
            self._metrics[org_id]["verified_migrations"] += 1.0
        return True

    def get_metrics(self, org_id: str) -> Dict[str, float]:
        return dict(self._metrics[org_id])


global_pilot_limits = PilotLimitsEnforcer()
global_observability = ObservabilityTracker()
global_pilot_metrics = PilotMetricsTracker()
