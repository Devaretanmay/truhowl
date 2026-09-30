"""Execution hooks for Truhowl.

Run code inside kernel-enforced compartments via :class:`SandboxRunner`.
"""

from __future__ import annotations

from .base import (
    DEFAULT_PERMISSIONS,
    VALID_PERMISSIONS,
    ExecutionResult,
    SandboxRunner,
    diff_trees,
    index_workdir,
    validate_permissions,
)

__all__ = [
    "VALID_PERMISSIONS",
    "DEFAULT_PERMISSIONS",
    "ExecutionResult",
    "SandboxRunner",
    "validate_permissions",
    "index_workdir",
    "diff_trees",
]
