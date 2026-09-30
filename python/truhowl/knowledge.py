# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Persistent Repository Knowledge Base (.truhowl/knowledge/).

Blueprint box 3 + 8: accumulated verified patch patterns that close the
flywheel. Every verified fix upserts a pattern; every future fix checks
the KB first for a 0-token Direct path.

Ponytail: single purpose, tiny surface, explicit file format.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

from truhowl.providers.registry import RewriteRule

import blake3


def _pattern_hash(pattern: str) -> str:
    return blake3.blake3(pattern.encode("utf-8")).hexdigest()[:16]


def _norm_version(v: str) -> str:
    return v.strip().lstrip("^~>=< ")


def _kb_dir(repo_dir: str) -> str:
    return os.path.join(os.path.abspath(repo_dir), ".truhowl", "knowledge")


def _entry_path(repo_dir: str, provider: str, from_version: str, to_version: str, kind: str = "sdk") -> str:
    """Namespaced entry path. Legacy provider-only layout is preserved as fallback read."""
    d = os.path.join(_kb_dir(repo_dir), kind, provider.lower())
    fname = f"{_norm_version(from_version)}__{_norm_version(to_version)}.json"
    return os.path.join(d, fname)


def _legacy_entry_path(repo_dir: str, provider: str, from_version: str, to_version: str) -> str:
    d = os.path.join(_kb_dir(repo_dir), provider.lower())
    fname = f"{_norm_version(from_version)}__{_norm_version(to_version)}.json"
    return os.path.join(d, fname)


@dataclass
class KBPattern:
    pattern: str
    replacement: str
    file_extensions: list[str] = field(default_factory=list)
    description: str = ""
    source: str = "learned"
    pattern_hash: str = ""
    verified_count: int = 1
    last_verified_utc: str = ""

    def __post_init__(self):
        if not self.pattern_hash:
            self.pattern_hash = _pattern_hash(self.pattern)


@dataclass
class KBEntry:
    migration_id: str
    provider: str
    from_version: str
    to_version: str
    patterns: list[KBPattern] = field(default_factory=list)
    test_recipe: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    created_utc: str = ""
    updated_utc: str = ""
    kind: str = "sdk"
    identity: str = ""
    repo_id: str = ""
    language: str = ""
    failed_patterns: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    source_commit: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_rule(pattern: str) -> str:
    """Canonical form for dedup: strip, collapse whitespace."""
    return re.sub(r"\s+", " ", pattern.strip())


def _parse_entry(raw: dict[str, Any], provider: str, from_version: str, to_version: str) -> KBEntry:
    pats = []
    for pp in raw.get("patterns", []):
        try:
            pats.append(KBPattern(**pp))
        except Exception:
            continue
    return KBEntry(
        migration_id=raw.get("migration_id", ""),
        provider=raw.get("provider", provider),
        from_version=raw.get("from_version", from_version),
        to_version=raw.get("to_version", to_version),
        patterns=pats,
        test_recipe=raw.get("test_recipe", {}),
        evidence=raw.get("evidence", {}),
        created_utc=raw.get("created_utc", ""),
        updated_utc=raw.get("updated_utc", ""),
        kind=raw.get("kind", "sdk"),
        identity=raw.get("identity", ""),
        repo_id=raw.get("repo_id", ""),
        language=raw.get("language", ""),
        failed_patterns=raw.get("failed_patterns", []),
        confidence=raw.get("confidence", 1.0),
        source_commit=raw.get("source_commit", ""),
    )


def load_entry(repo_dir: str, provider: str, from_version: str, to_version: str, kind: str = "sdk") -> KBEntry | None:
    for p in (_entry_path(repo_dir, provider, from_version, to_version, kind),
              _legacy_entry_path(repo_dir, provider, from_version, to_version)):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                return _parse_entry(json.load(f), provider, from_version, to_version)
        except Exception:
            return None
    return None


def lookup(repo_dir: str, provider: str, from_version: str, to_version: str, kind: str = "sdk") -> KBEntry | None:
    return load_entry(repo_dir, provider, from_version, to_version, kind)


def _is_executable(p: "KBPattern") -> bool:
    return bool(p.pattern) and bool(p.file_extensions)


def to_rewrite_rules(entry: KBEntry | None) -> list[Any]:
    """Convert executable KB patterns into RewriteRule objects. Skips description-only notes."""
    if not entry:
        return []
    rules = []
    for p in entry.patterns:
        if not _is_executable(p):
            continue
        rules.append(RewriteRule(
            pattern=p.pattern,
            replacement=p.replacement,
            file_extensions=list(p.file_extensions),
            description=p.description or p.pattern,
            is_regex=True,
        ))
    return rules


def direct_rewrites_for(repo_dir: str, provider: str, from_version: str, to_version: str) -> list[Any]:
    return to_rewrite_rules(lookup(repo_dir, provider, from_version, to_version))


def ensure_test_recipe(repo_dir: str, provider: str, from_version: str, to_version: str, test_command: str) -> KBEntry | None:
    """Seed or refresh the test_recipe for a migration without requiring patterns yet."""
    if not test_command:
        return lookup(repo_dir, provider, from_version, to_version)
    repo_dir = os.path.abspath(repo_dir)
    entry = load_entry(repo_dir, provider, from_version, to_version)
    now = _now()
    if entry is None:
        entry = KBEntry(
            migration_id=f"{provider.lower()}:{_norm_version(from_version)}->{_norm_version(to_version)}",
            provider=provider.lower(),
            from_version=_norm_version(from_version),
            to_version=_norm_version(to_version),
            patterns=[],
            test_recipe={"test_command": test_command, "last_exit": 0, "last_verified_utc": now},
            evidence={},
            created_utc=now,
            updated_utc=now,
        )
    else:
        entry.test_recipe = {"test_command": test_command, "last_exit": 0, "last_verified_utc": now}
        entry.updated_utc = now
    return _save_entry(repo_dir, entry)


def _save_entry(repo_dir: str, entry: KBEntry, kind: str = "sdk") -> KBEntry:
    p = _entry_path(repo_dir, entry.provider, entry.from_version, entry.to_version, kind)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    raw = {
        "migration_id": entry.migration_id,
        "provider": entry.provider,
        "from_version": entry.from_version,
        "to_version": entry.to_version,
        "patterns": [asdict(pp) for pp in entry.patterns],
        "test_recipe": entry.test_recipe,
        "evidence": entry.evidence,
        "created_utc": entry.created_utc,
        "updated_utc": entry.updated_utc,
        "kind": entry.kind,
        "identity": entry.identity,
        "repo_id": entry.repo_id,
        "language": entry.language,
        "failed_patterns": entry.failed_patterns,
        "confidence": entry.confidence,
        "source_commit": entry.source_commit,
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    return entry


def record_failure(
    repo_dir: str,
    provider: str,
    from_version: str,
    to_version: str,
    description: str,
    affected_paths: List[str] | None = None,
    kind: str = "sdk",
) -> KBEntry | None:
    """Record an unverified/failed repair attempt. Never promotes guesses to trusted patterns."""
    repo_dir = os.path.abspath(repo_dir)
    entry = load_entry(repo_dir, provider, from_version, to_version, kind)
    now = _now()
    if entry is None:
        entry = KBEntry(
            migration_id=f"{provider.lower()}:{_norm_version(from_version)}->{_norm_version(to_version)}",
            provider=provider.lower(),
            from_version=_norm_version(from_version),
            to_version=_norm_version(to_version),
            kind=kind,
            created_utc=now,
        )
    entry.failed_patterns.append({
        "description": description,
        "affected_paths": affected_paths or [],
        "recorded_utc": now,
    })
    entry.updated_utc = now
    return _save_entry(repo_dir, entry, kind)


def upsert_learned(
    repo_dir: str,
    provider: str,
    from_version: str,
    to_version: str,
    applied_rules: List[str] | None = None,
    patch_results: List[Any] | None = None,
    test_command: str = "",
    evidence: Dict[str, Any] | None = None,
    rewrites: List[Any] | None = None,
    kind: str = "sdk",
) -> KBEntry:
    """Create or update KB entry after verified fix. Stores executable rewrites, not just descriptions."""
    repo_dir = os.path.abspath(repo_dir)
    entry = load_entry(repo_dir, provider, from_version, to_version, kind)
    now = _now()
    if entry is None:
        entry = KBEntry(
            migration_id=f"{provider.lower()}:{_norm_version(from_version)}->{_norm_version(to_version)}",
            provider=provider.lower(),
            from_version=_norm_version(from_version),
            to_version=_norm_version(to_version),
            patterns=[],
            test_recipe={"test_command": test_command, "last_exit": 0} if test_command else {},
            evidence=evidence or {},
            created_utc=now,
            updated_utc=now,
            kind=kind,
        )
    seen = {normalize_rule(p.pattern): p for p in entry.patterns if _is_executable(p)}
    for r in rewrites or []:
        key = normalize_rule(getattr(r, "pattern", ""))
        if not key:
            continue
        if key not in seen:
            pat = KBPattern(
                pattern=getattr(r, "pattern", ""),
                replacement=getattr(r, "replacement", ""),
                file_extensions=list(getattr(r, "file_extensions", []) or []),
                description=getattr(r, "description", "") or key,
                source="registry",
                verified_count=1,
                last_verified_utc=now,
            )
            entry.patterns.append(pat)
            seen[key] = pat
        else:
            seen[key].verified_count += 1
            seen[key].last_verified_utc = now
            if getattr(r, "replacement", None) is not None:
                seen[key].replacement = getattr(r, "replacement", "")
            if getattr(r, "file_extensions", None):
                seen[key].file_extensions = list(getattr(r, "file_extensions", []))
    if patch_results:
        for r in patch_results:
            for desc in getattr(r, "rules_applied", []) or []:
                key = normalize_rule(desc)
                if key not in seen and key not in {normalize_rule(p.pattern) for p in entry.patterns}:
                    continue

    if test_command:
        entry.test_recipe = {"test_command": test_command, "last_exit": 0, "last_verified_utc": now}
    if evidence:
        entry.evidence.update(evidence)
    entry.updated_utc = now
    entry.confidence = 1.0

    return _save_entry(repo_dir, entry, kind)
