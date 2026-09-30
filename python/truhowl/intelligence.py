# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Truhowl Intelligence — invisible smart routing (blueprint box 5).

Single brain. User never picks --ai vs deterministic.
AI is the exclusive patch author. When valid credentials are present,
routes to AI; otherwise fails closed into QUARANTINE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from truhowl.change_source import ChangeSource
from truhowl.credentials import has_valid_credentials
from truhowl.knowledge import _norm_version, direct_rewrites_for
from truhowl.providers.registry import find_migration_for, get_default_registry


@dataclass
class Decision:
    strategy: str
    reason: str
    elapsed_ms: int = 0
    provider: str = ""
    from_version: str = ""
    to_version: str = ""
    confidence: float = 0.0


def resolve_migration(provider: str, from_version: str | None = None, to_version: str | None = None):
    """Pick the registry migration matching the requested versions, else first.

    Returns (from, to, migration|None); matched versions come back canonical,
    unmatched fall back to caller raw (legacy parity).
    """
    migration = find_migration_for(ChangeSource.sdk(
        provider or "", version_from=from_version or "unknown", version_to=to_version or "unknown"))
    if migration is None:
        return (from_version or "unknown", to_version or "unknown", None)
    actual_from = migration.from_version \
        if from_version and _norm_version(from_version) == _norm_version(migration.from_version) \
        else (from_version or migration.from_version)
    actual_to = migration.to_version \
        if to_version and _norm_version(to_version) == _norm_version(migration.to_version) \
        else (to_version or migration.to_version)
    return (actual_from, actual_to, migration)


class TruhowlIntelligence:
    """Reason over change + repo knowledge base, pick cheapest safe path."""

    def decide(
        self,
        repo_dir: str,
        provider: str,
        from_version: str | None = None,
        to_version: str | None = None,
        has_rewrites: bool = False,
        kb_hit: bool | None = None,
    ) -> Decision:
        t0 = time.time()
        registry = get_default_registry()
        p_spec = registry.get(provider) if provider else None
        if not p_spec:
            return Decision(strategy="QUARANTINE", reason="unknown provider", elapsed_ms=int((time.time()-t0)*1000), provider=provider or "", from_version=from_version or "", to_version=to_version or "")

        actual_from, actual_to, _mig = resolve_migration(provider, from_version, to_version)

        hit = kb_hit
        if hit is None:
            hit = len(direct_rewrites_for(repo_dir, provider, actual_from, actual_to)) > 0

        if has_valid_credentials():
            elapsed = int((time.time() - t0) * 1000)
            reason = "ai_authored_with_pattern_evidence" if (hit or has_rewrites) else "ai_authored_novel_drift"
            return Decision(
                strategy="AI",
                reason=reason,
                elapsed_ms=elapsed,
                provider=provider,
                from_version=actual_from,
                to_version=actual_to,
                confidence=0.95 if (hit or has_rewrites) else 0.8,
            )

        elapsed = int((time.time() - t0) * 1000)
        return Decision(
            strategy="QUARANTINE",
            reason="no_credentials_for_ai",
            elapsed_ms=elapsed,
            provider=provider,
            from_version=actual_from,
            to_version=actual_to,
            confidence=0.0,
        )

    def decide_for_source(self, repo_dir: str, source: ChangeSource) -> Decision:
        """Generalized entry point: route any change source, not just vendor SDKs."""
        t0 = time.time()
        migration = find_migration_for(source)
        if migration is None:
            if has_valid_credentials():
                return Decision(
                    strategy="AI",
                    reason=f"ai_authored_novel_{source.kind}",
                    elapsed_ms=int((time.time() - t0) * 1000),
                    provider=source.provider,
                    from_version=source.version_from,
                    to_version=source.version_to,
                    confidence=0.8,
                )
            return Decision(
                strategy="QUARANTINE",
                reason=f"no_connector_for_{source.kind}",
                elapsed_ms=int((time.time() - t0) * 1000),
                provider=source.provider,
                from_version=source.version_from,
                to_version=source.version_to,
                confidence=0.0,
            )
        return self.decide(
            repo_dir,
            source.provider,
            source.version_from,
            source.version_to,
            has_rewrites=bool(migration.rewrites),
        )
