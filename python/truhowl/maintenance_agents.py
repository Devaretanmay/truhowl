from dataclasses import dataclass, field
from typing import Any

from truhowl.change_source import ChangeSource
from truhowl.graph import build_dependency_graph


@dataclass
class ImpactAnalysisResult:
    provider: str
    affected_files: list[str]
    callsites_count: int
    wrapper_files: list[str]
    callsites: list[dict[str, Any]] = field(default_factory=list)


def analyze_impact(repo_dir: str, provider_name: str) -> ImpactAnalysisResult:
    """Identity match against wrapper metadata, callsite patterns, and file paths."""
    source = ChangeSource.sdk(provider_name)
    identity = (getattr(source, "identity", "") or "").lower()
    provider_name = (getattr(source, "provider", "") or identity) or ""
    graph = build_dependency_graph(repo_dir)
    affected_files = set()
    wrappers = []
    matched_callsites = []

    for w in graph.get("wrappers", []):
        hay = f"{w.get('wrapper_file', '')} {w.get('wraps_provider', '')}".lower()
        if identity and identity in hay:
            wrappers.append(w.get("wrapper_file"))
            affected_files.add(w.get("wrapper_file"))

    for c in graph.get("callsites", []):
        hay = f"{c.get('function_name', '')} {c.get('matched_pattern', '')} {c.get('file_path', '')}".lower()
        if identity and identity in hay:
            matched_callsites.append(c)
            affected_files.add(c.get("file_path"))

    return ImpactAnalysisResult(
        provider=provider_name,
        affected_files=sorted(list(affected_files)),
        callsites_count=len(matched_callsites),
        wrapper_files=wrappers,
        callsites=matched_callsites,
    )
