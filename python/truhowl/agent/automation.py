# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automation policy: one persisted mode that decides how far the agent may go.

    OBSERVE  detect changes, open cases, never repair
    PREPARE  detect, repair, verify — never publish
    DELIVER  detect, repair, verify, publish only when verification passes

Default is OBSERVE. Publication is never reachable by a flag alone: the
policy is the ceiling, and ``--act``/``--yes`` only express operator intent
*beneath* that ceiling. An explicit confirmation can never escalate past the
persisted mode, so a compromised or careless caller cannot turn detection
into a public action.
"""

from __future__ import annotations

from typing import Any

from truhowl.agent.models import _now, load_store, save_store

OBSERVE = "observe"
PREPARE = "prepare"
DELIVER = "deliver"

MODES = (OBSERVE, PREPARE, DELIVER)
DEFAULT_MODE = OBSERVE

MODE_DESCRIPTIONS = {
    OBSERVE: "detect changes and open migration cases; never repair",
    PREPARE: "detect, repair and verify; never publish",
    DELIVER: "detect, repair, verify; publish only when verification passes",
}


def normalize_mode(value: str | None) -> str:
    """Accept a mode name (case/whitespace tolerant). Raises on nonsense."""
    mode = (value or "").strip().lower()
    if not mode:
        return DEFAULT_MODE
    if mode in MODES:
        return mode
    raise ValueError(
        f"Unknown automation mode {value!r}. Choose one of: {', '.join(MODES)}.")


def get_policy(workspace: str) -> dict[str, Any]:
    """Read the persisted policy, defaulting conservatively to OBSERVE."""
    store = load_store(workspace)
    raw = store.org.get("automation")
    if not isinstance(raw, dict):
        return {"mode": DEFAULT_MODE, "updated_at": ""}
    try:
        mode = normalize_mode(raw.get("mode"))
    except ValueError:
        mode = DEFAULT_MODE
    return {"mode": mode, "updated_at": str(raw.get("updated_at", "") or "")}


def get_mode(workspace: str) -> str:
    return str(get_policy(workspace)["mode"])


def set_mode(workspace: str, mode: str) -> dict[str, Any]:
    """Persist an automation mode. Explicit operator action, never implicit."""
    normalized = normalize_mode(mode)
    store = load_store(workspace)
    policy = {"mode": normalized, "updated_at": _now()}
    store.org["automation"] = policy
    save_store(workspace, store)
    return policy


def allows_repair(mode: str) -> bool:
    """PREPARE and DELIVER may repair. OBSERVE may not."""
    return normalize_mode(mode) in (PREPARE, DELIVER)


def allows_publish(mode: str) -> bool:
    """Only DELIVER may publish. This gate has no override."""
    return normalize_mode(mode) == DELIVER


def describe(mode: str) -> str:
    normalized = normalize_mode(mode)
    return MODE_DESCRIPTIONS[normalized]


def refusal_reason(mode: str, action: str = "repair") -> str:
    """A refusal an operator can act on, naming the exact fix."""
    normalized = normalize_mode(mode)
    return (
        f"Automation mode {normalized.upper()} does not permit {action}. "
        f"Run `truhowl agent policy {PREPARE}` (or {DELIVER}) to enable it, "
        f"or re-run with an explicit confirmation to act once."
    )


__all__ = [
    "DEFAULT_MODE",
    "DELIVER",
    "MODE_DESCRIPTIONS",
    "MODES",
    "OBSERVE",
    "PREPARE",
    "allows_publish",
    "allows_repair",
    "describe",
    "get_mode",
    "get_policy",
    "normalize_mode",
    "refusal_reason",
    "set_mode",
]
