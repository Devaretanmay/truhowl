
from __future__ import annotations

import json
from typing import Any

from truhowl._core import dependency_graph_build, dependency_graph_audit


def build_dependency_graph(repo_root: str = ".") -> dict[str, Any]:
    """Build the complete External-Change Dependency Graph for a repository."""
    if dependency_graph_build is None:
        return {"providers": [], "callsites": [], "edges": []}
    raw = dependency_graph_build(repo_root)
    return json.loads(raw)


def audit_dependency_graph(repo_root: str = ".") -> dict[str, Any]:
    """Run an audit over the External-Change Dependency Graph and return risk summary."""
    if dependency_graph_audit is None:
        return {
            "total_providers_detected": 0,
            "total_callsites_mapped": 0,
            "at_risk": [],
            "watchlist": [],
            "healthy": [],
            "total_auto_repairable": 0,
        }
    raw = dependency_graph_audit(repo_root)
    return json.loads(raw)
