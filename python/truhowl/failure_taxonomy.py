# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Migration failure taxonomy and classifier.

Turns a Hunt audit record into a machine-readable root-cause class, so repair
failures can be counted by cause instead of guessed from final stderr text.
Classification is evidence-driven: it reads what the loop actually recorded
(which migration unit survived, what the compiler said, whether the provider
was reachable) rather than inferring from the last line of output.

The taxonomy is deliberately diagnostic, not a gate: nothing here decides
whether a repair is verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── Categories ────────────────────────────────────────────────────────────
#
# One class per way a migration stops short. A run may show several; the
# primary class is the earliest blocker in the pipeline order below.

IMPORT_NOT_MIGRATED = "IMPORT_NOT_MIGRATED"
CLIENT_INIT_NOT_MIGRATED = "CLIENT_INIT_NOT_MIGRATED"
CALLSITE_NOT_MIGRATED = "CALLSITE_NOT_MIGRATED"
REQUEST_SHAPE_NOT_MIGRATED = "REQUEST_SHAPE_NOT_MIGRATED"
RESPONSE_SHAPE_NOT_MIGRATED = "RESPONSE_SHAPE_NOT_MIGRATED"
TYPE_NOT_MIGRATED = "TYPE_NOT_MIGRATED"
MODULE_SYSTEM_MISMATCH = "MODULE_SYSTEM_MISMATCH"
DEPENDENCY_METADATA_NOT_MIGRATED = "DEPENDENCY_METADATA_NOT_MIGRATED"
MULTI_FILE_INCOMPLETE = "MULTI_FILE_INCOMPLETE"
SYNTAX_GATE_FAILED = "SYNTAX_GATE_FAILED"
PATCH_NOT_APPLICABLE = "PATCH_NOT_APPLICABLE"
AUTHOR_NO_PATCH = "AUTHOR_NO_PATCH"
MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
NO_VERIFICATION_COMMAND = "NO_VERIFICATION_COMMAND"
COMPILER_UNKNOWN = "COMPILER_UNKNOWN"
TEST_FAILURE = "TEST_FAILURE"
OTHER = "OTHER"

CATEGORIES: tuple[str, ...] = (
    IMPORT_NOT_MIGRATED,
    CLIENT_INIT_NOT_MIGRATED,
    CALLSITE_NOT_MIGRATED,
    REQUEST_SHAPE_NOT_MIGRATED,
    RESPONSE_SHAPE_NOT_MIGRATED,
    TYPE_NOT_MIGRATED,
    MODULE_SYSTEM_MISMATCH,
    DEPENDENCY_METADATA_NOT_MIGRATED,
    MULTI_FILE_INCOMPLETE,
    SYNTAX_GATE_FAILED,
    PATCH_NOT_APPLICABLE,
    AUTHOR_NO_PATCH,
    MODEL_RATE_LIMIT,
    NO_VERIFICATION_COMMAND,
    COMPILER_UNKNOWN,
    TEST_FAILURE,
    OTHER,
)

# Migration unit -> the taxonomy class its residue represents.
UNIT_TO_CATEGORY: dict[str, str] = {
    "import": IMPORT_NOT_MIGRATED,
    "initialization": CLIENT_INIT_NOT_MIGRATED,
    "method_call": CALLSITE_NOT_MIGRATED,
    "request_shape": REQUEST_SHAPE_NOT_MIGRATED,
    "response_shape": RESPONSE_SHAPE_NOT_MIGRATED,
    "type": TYPE_NOT_MIGRATED,
    "module_system": MODULE_SYSTEM_MISMATCH,
    "dependency_metadata": DEPENDENCY_METADATA_NOT_MIGRATED,
}

# TypeScript diagnostics -> class. Symbol-removal codes dominate migrations.
_TS_CODE_CATEGORY: dict[str, str] = {
    "TS2305": IMPORT_NOT_MIGRATED,   # module has no exported member
    "TS2307": IMPORT_NOT_MIGRATED,   # cannot find module
    "TS2614": IMPORT_NOT_MIGRATED,   # no default export
    "TS1192": IMPORT_NOT_MIGRATED,   # no default export (esModuleInterop)
    "TS2339": CALLSITE_NOT_MIGRATED, # property does not exist
    "TS2551": CALLSITE_NOT_MIGRATED, # property does not exist, did you mean
    "TS2554": REQUEST_SHAPE_NOT_MIGRATED,
    "TS2345": REQUEST_SHAPE_NOT_MIGRATED,
    "TS2322": TYPE_NOT_MIGRATED,
    "TS2741": TYPE_NOT_MIGRATED,
    "TS2551_TYPE": TYPE_NOT_MIGRATED,
    "TS2351": MODULE_SYSTEM_MISMATCH,  # not constructable (import interop)
    "TS1259": MODULE_SYSTEM_MISMATCH,  # can only be default-imported with esModuleInterop
    "TS1479": MODULE_SYSTEM_MISMATCH,  # CJS/ESM interop
}

_RATE_LIMIT_RE = re.compile(r"\b429\b|too many requests|rate limit|LLMTransientError", re.IGNORECASE)
_NO_TEST_RE = re.compile(r"no test command detected", re.IGNORECASE)
_SYNTAX_RE = re.compile(r"SYNTAX GATE FAILED", re.IGNORECASE)
_TS_ERR_RE = re.compile(r"\b(TS\d{4})\b")


@dataclass
class FailureClassification:
    """Why a repair stopped, with the evidence that supports the claim."""

    category: str
    detail: str = ""
    secondary: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "detail": self.detail,
            "secondary": self.secondary,
            "evidence": self.evidence[:12],
        }


def _text_of(evidence: Any) -> str:
    parts = []
    for attr in ("output", "diff", "command"):
        value = getattr(evidence, attr, "")
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def classify_from_audit(audit: dict[str, Any]) -> FailureClassification:
    """Classify one audit record into a primary category plus secondary classes.

    Order of precedence follows the pipeline: a missing provider explains more
    than a test failure; an incomplete migration explains more than the
    compiler error it produced.
    """
    secondary: list[str] = []

    if audit.get("final") == "verified":
        return FailureClassification(category="VERIFIED", detail="sealed verified repair")

    if audit.get("final") == "refused_no_credentials":
        return FailureClassification(category=OTHER, detail="no AI provider configured",
                                     evidence=["final=refused_no_credentials"])

    # 1. Provider reachability dominates: nothing else was attempted.
    decisions = audit.get("decisions", []) or []
    for decision in decisions:
        err = str(decision.get("error", ""))
        if decision.get("decision") == "author_provider_error":
            if _RATE_LIMIT_RE.search(err):
                return FailureClassification(
                    category=MODEL_RATE_LIMIT,
                    detail=err[:200] or "provider throttled the authoring call",
                    evidence=[f"attempt={decision.get('attempt')}"],
                )
            return FailureClassification(
                category=AUTHOR_NO_PATCH, detail=f"provider error: {err[:200]}",
                evidence=[f"attempt={decision.get('attempt')}"],
            )
    provider_errors = re.findall(r"LLM call failed: [^\n]{0,160}", json.dumps(decisions))
    if any(_RATE_LIMIT_RE.search(e) for e in provider_errors):
        return FailureClassification(
            category=MODEL_RATE_LIMIT,
            detail=next(e for e in provider_errors if _RATE_LIMIT_RE.search(e))[:200],
            evidence=provider_errors[:3],
        )

    # 2. Authoring never produced a change.
    if not audit.get("lifecycle"):
        return FailureClassification(category=OTHER, detail="empty audit record")
    if not any(str(x).startswith("patch_generated") for x in audit["lifecycle"]):
        return FailureClassification(
            category=AUTHOR_NO_PATCH,
            detail="no candidate produced before verification",
            evidence=[str(x) for x in audit["lifecycle"]][:6],
        )

    # 3. Residual migration drift: a partial migration, named by unit.
    residual = audit.get("residual_drift", []) or []
    if residual:
        units = [h.get("unit", "") for entry in residual for h in (entry.get("hits") or [])]
        primary = UNIT_TO_CATEGORY.get(units[0] if units else "", OTHER)
        for unit in units:
            category = UNIT_TO_CATEGORY.get(unit, OTHER)
            if category != primary and category not in secondary:
                secondary.append(category)
        if len({h.get("file") for entry in residual for h in (entry.get("hits") or [])}) > 1:
            secondary.append(MULTI_FILE_INCOMPLETE)
        return FailureClassification(
            category=primary,
            detail=f"{sum(len(e.get('hits') or []) for e in residual)} residual legacy usage(s)",
            secondary=secondary,
            evidence=[
                f"{h.get('file')}:{h.get('line')} {h.get('old_symbol')} -> {h.get('new_symbol')}"
                for entry in residual for h in (entry.get("hits") or [])
            ],
        )

    # 4. Scope refusal: the candidate changed something it was not allowed to.
    for decision in decisions:
        if str(decision.get("decision", "")).startswith("scope_failed"):
            return FailureClassification(
                category=PATCH_NOT_APPLICABLE, detail=str(decision.get("decision"))[:200],
                evidence=[str(decision.get("decision"))[:200]],
            )

    # 5. Verification evidence.
    verifications = audit.get("verification", []) or []
    replays = audit.get("replay_verification", []) or []
    last = (replays or verifications)
    if not last:
        return FailureClassification(category=OTHER, detail="no verification was recorded")
    tail = last[-1]
    output = str(tail.get("output", ""))
    command = str(tail.get("command", ""))
    exit_code = tail.get("exit_code")

    if _NO_TEST_RE.search(output) or (not command and exit_code in (-1, None)):
        return FailureClassification(
            category=NO_VERIFICATION_COMMAND,
            detail="repository declares no verification command; failing closed",
            evidence=[output[:200]],
        )
    if _SYNTAX_RE.search(output):
        return FailureClassification(
            category=SYNTAX_GATE_FAILED,
            detail="changed files did not parse; verification refused before the test run",
            evidence=[line for line in output.splitlines() if line.startswith("---")][:6],
        )
    codes = list(dict.fromkeys(_TS_ERR_RE.findall(output)))
    if codes:
        mapped = [_TS_CODE_CATEGORY.get(code, COMPILER_UNKNOWN) for code in codes]
        primary = mapped[0]
        for category in mapped:
            if category != primary and category not in secondary:
                secondary.append(category)
        return FailureClassification(
            category=primary, detail=f"compiler diagnostics: {', '.join(codes[:6])}",
            secondary=secondary, evidence=codes[:8],
        )
    if exit_code not in (0, None):
        return FailureClassification(
            category=TEST_FAILURE, detail=f"`{command}` exited {exit_code}",
            evidence=[output[-400:]],
        )
    return FailureClassification(category=OTHER, detail="unclassified refusal",
                                 evidence=[output[-300:]])


def classify_report(report: Any, audit: dict[str, Any] | None = None) -> FailureClassification:
    """Classify a finished Hunt run using its audit when available."""
    if report is not None and getattr(report, "success", False):
        tier = getattr(getattr(report, "verified", None), "verification_tier", "")
        return FailureClassification(category="VERIFIED", detail=tier or "verified")
    if audit:
        return classify_from_audit(audit)
    if report is None:
        return FailureClassification(category=OTHER, detail="no report")
    reason = str(getattr(report, "reason", ""))
    if "no AI provider" in reason:
        return FailureClassification(category=OTHER, detail=reason[:200])
    return FailureClassification(
        category=TEST_FAILURE if getattr(report, "test_exit_code", -1) not in (0, -1) else OTHER,
        detail=reason[:200],
    )


def read_audit(path: str) -> dict[str, Any]:
    """Load an audit record, tolerating a missing or malformed file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_failure_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case classifications into the migration_failures report.

    ``records`` entries: {case, provider, classification: {...}, outcome: str}.
    """
    counts = {category: 0 for category in CATEGORIES}
    counts["VERIFIED"] = 0
    per_unit: dict[str, int] = {unit: 0 for unit in UNIT_TO_CATEGORY}
    for record in records:
        classification = record.get("classification") or {}
        category = str(classification.get("category", OTHER))
        counts[category] = counts.get(category, 0) + 1
        for entry in classification.get("evidence", []) or []:
            for unit, label in UNIT_TO_CATEGORY.items():
                if label in str(classification.get("category", "")):
                    per_unit[unit] += 1
                    break
    verified = counts.get("VERIFIED", 0)
    return {
        "schema": "truhowl.migration_failures.v1",
        "cases": len(records),
        "verified": verified,
        "refused": len(records) - verified,
        "false_verified": 0,
        "counts": {k: v for k, v in counts.items() if v},
        "by_migration_unit": {k: v for k, v in per_unit.items() if v},
        "records": records,
    }


__all__ = [
    "CATEGORIES",
    "UNIT_TO_CATEGORY",
    "FailureClassification",
    "classify_from_audit",
    "classify_report",
    "read_audit",
    "build_failure_report",
] + list(CATEGORIES)
