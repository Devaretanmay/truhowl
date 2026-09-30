# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict

from truhowl.graph import audit_dependency_graph, build_dependency_graph


def _strip_strings_and_comments(line: str) -> str:
    """Remove string literals and line comments for evidence checks."""
    out: list[str] = []
    i, n = 0, len(line)
    quote: str | None = None
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def is_code_evidence(callsite: dict[str, Any]) -> bool:
    """True when a callsite match is real code, not prose inside strings.

    Structured AST kinds always count. Otherwise the matched pattern must
    still occur after stripping string literals and comments from the line.
    """
    if callsite.get("kind"):
        return True
    pattern = (callsite.get("matched_pattern") or "").strip()
    line = callsite.get("line_content") or ""
    if not pattern or not line:
        return False
    return pattern.lower() in _strip_strings_and_comments(line).lower()


def require_structural_evidence(summary: dict[str, Any], repo_root: str) -> dict[str, Any]:
    """Drop at-risk findings with no structural (AST-kind) callsites.

    The graph locator also reports bare name/URL substring hits (e.g. a URL
    constant or prose inside println!) with no AST kind. Those are not code
    callsites and must never present as CRITICAL breaking drift. Items with
    only such references move to healthy with an explicit note.
    """
    try:
        graph = build_dependency_graph(repo_root)
    except Exception:
        return summary
    structural_files: set[str] = set()
    for c in graph.get("callsites", []) or []:
        if c.get("file_path") and is_code_evidence(c):
            structural_files.add(os.path.abspath(c["file_path"]))
    kept = []
    for item in summary.get("at_risk", []):
        files = [f for f in item.get("affected_files", [])
                 if os.path.abspath(os.path.join(repo_root, f)) in structural_files]
        if files:
            item["affected_files"] = files
            item["callsites_count"] = len(files)
            kept.append(item)
            continue
        summary.setdefault("healthy", []).append({
            "provider_name": item.get("provider_name", "Unknown"),
            "package_name": item.get("package_name", ""),
            "current_version": item.get("current_version", "unknown"),
            "status_message": ("Name-only references (URLs/docs/strings); "
                               "no code callsites require migration."),
            "callsite_count": 0,
        })
    summary["at_risk"] = kept
    return summary


def render_audit_cli(summary: dict[str, Any]) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append("         TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT & RISK REGISTER")
    lines.append("=" * 80)
    lines.append(f"Total External Providers Detected: {summary.get('total_providers_detected', 0)}")
    lines.append(f"Total AST Callsites Mapped:        {summary.get('total_callsites_mapped', 0)}")
    lines.append(f"Auto-Repairable Callsites:         {summary.get('total_auto_repairable', 0)}")
    lines.append("-" * 80)

    at_risk = summary.get("at_risk", [])
    if at_risk:
        lines.append("[CRITICAL] BREAKING DRIFT (Immediate Action Required):")
        for item in at_risk:
            lines.append(f"  * Provider:         {item['provider_name']} ({item['package_name']} @ {item['current_version']} -> {item['target_version']})")
            lines.append(f"    Breaking Change:  {item['breaking_change']}")
            lines.append(f"    Active Callsites: {item['callsites_count']} callsites across {len(item['affected_files'])} files")
            lines.append(f"    Repair Status:    {'[MERGE_READY AUTO-PATCH]' if item.get('is_auto_repairable') else '[MANUAL REVIEW]'}")
            if item.get("migration_guide_url"):
                lines.append(f"    Vendor Guide:     {item['migration_guide_url']}")
            lines.append("")

    watchlist = summary.get("watchlist", [])
    if watchlist:
        lines.append("[WATCHLIST] UPCOMING DEPRECATION:")
        for item in watchlist:
            lines.append(f"  * Provider:         {item['provider_name']} ({item['method_pattern']})")
            lines.append(f"    Deadline:         {item['deprecation_deadline']} (~{item.get('days_remaining', 60)} days remaining)")
            lines.append(f"    Active Callsites: {item['callsite_count']} mapped")
            if item.get("documentation_url"):
                lines.append(f"    Vendor Notice:    {item['documentation_url']}")
            lines.append("")

    healthy = summary.get("healthy", [])
    if healthy:
        lines.append("[HEALTHY] UP-TO-DATE INTEGRATIONS:")
        for item in healthy:
            lines.append(f"  * Provider:         {item['provider_name']} ({item['package_name']} @ {item['current_version']})")
            lines.append(f"    Status:           {item['status_message']} ({item['callsite_count']} callsites)")
            lines.append("")

    lines.append("=" * 80)
    lines.append("Run `truhowl fix <path> --provider <name>` to execute autonomous migration.")
    lines.append("=" * 80)
    return "\n".join(lines)


def render_audit_github_issue(summary: dict[str, Any]) -> str:
    lines = []
    lines.append("# Truhowl: External Dependency Map & Risk Register\n")
    lines.append(f"Truhowl mapped **{summary.get('total_callsites_mapped', 0)} external API touchpoints** across **{summary.get('total_providers_detected', 0)} providers** in this repository.\n")

    at_risk = summary.get("at_risk", [])
    if at_risk:
        lines.append("### Breaking Drift (Immediate Action Required)")
        lines.append("| Provider | Detected Package | Current -> Target | Active Callsites | Breaking Drift | Autonomous Action |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for item in at_risk:
            lines.append(f"| **{item['provider_name']}** | `{item['package_name']}` | `{item['current_version']}` -> `{item['target_version']}` | **{item['callsites_count']} callsites** ({len(item['affected_files'])} files) | {item['breaking_change']} | `truhowl fix --provider {item['provider_name'].lower()}` [MERGE_READY] |")
        lines.append("")

    watchlist = summary.get("watchlist", [])
    if watchlist:
        lines.append("### Upcoming Deprecation Watchlist")
        lines.append("| Provider | Method / Pattern | Deprecation Deadline | Days Remaining | Documentation |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for item in watchlist:
            lines.append(f"| **{item['provider_name']}** | `{item['method_pattern']}` | **{item['deprecation_deadline']}** | ~{item.get('days_remaining', 60)} days | [Official Guide]({item['documentation_url']}) |")
        lines.append("")

    healthy = summary.get("healthy", [])
    if healthy:
        lines.append("### Healthy & Up-to-Date Integrations")
        for item in healthy:
            lines.append(f"- **{item['provider_name']}** (`{item['package_name']}@{item['current_version']}`): {item['status_message']} ({item['callsite_count']} callsites mapped)")
        lines.append("")

    lines.append("---")
    lines.append("> *Generated automatically by Truhowl External-Change Intelligence.*")
    return "\n".join(lines)


STATE_FILE = "index_state.json"

_MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "Cargo.toml",
              "go.mod", "Gemfile", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")


def _head_sha(repo_root: str) -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                              capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _changed_since(repo_root: str, state: dict[str, Any]) -> list[str]:
    """Files changed since the stored index state (git-aware, mtime fallback)."""
    changed: list[str] = []
    old_sha = state.get("commit_sha", "")
    new_sha = _head_sha(repo_root)
    if old_sha and new_sha and old_sha != new_sha:
        try:
            proc = subprocess.run(["git", "diff", "--name-only", old_sha, new_sha],
                                  cwd=repo_root, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                changed.extend(f for f in proc.stdout.splitlines() if f.strip())
        except Exception:
            pass
    old_mtimes = state.get("mtimes", {})
    for dirpath, dirnames, filenames in os.walk(repo_root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".next", "__pycache__", ".venv", "target", ".truhowl"}]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, repo_root)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            if rel not in old_mtimes:
                if old_mtimes:
                    changed.append(rel)
            elif old_mtimes[rel] != mt:
                changed.append(rel)
    return sorted(set(changed))


def read_index_state(repo_root: str = ".") -> Dict[str, Any] | None:
    p = os.path.join(repo_root, ".truhowl", STATE_FILE)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_index_state(repo_root: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Persist commit SHA + file mtimes + discovery counts for incremental re-indexing."""
    repo_root = os.path.abspath(repo_root)
    mtimes: dict[str, float] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".next", "__pycache__", ".venv", "target", ".truhowl"}]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, repo_root)
            try:
                mtimes[rel] = os.path.getmtime(fp)
            except OSError:
                continue
    state = {
        "version": 1,
        "commit_sha": _head_sha(repo_root),
        "indexed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": summary.get("total_providers_detected", 0),
        "callsites": summary.get("total_callsites_mapped", 0),
        "mtimes": mtimes,
    }
    os.makedirs(os.path.join(repo_root, ".truhowl"), exist_ok=True)
    with open(os.path.join(repo_root, ".truhowl", STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    return state


def changed_since_index(repo_root: str = ".") -> dict[str, Any]:
    """Describe what changed since the last persisted index. Read-only."""
    state = read_index_state(repo_root)
    if state is None:
        return {"indexed": False, "fresh": False, "changed_files": [], "manifests_changed": []}
    changed = _changed_since(os.path.abspath(repo_root), state)
    manifests = sorted({c for c in changed if os.path.basename(c) in _MANIFESTS})
    return {"indexed": True, "fresh": not changed, "changed_files": changed,
            "manifests_changed": manifests, "commit_sha": state.get("commit_sha", "")}


def run_audit(repo_root: str = ".", output_format: str = "cli", write_graph: bool = False) -> str:
    prev = read_index_state(repo_root) if write_graph else None
    summary = audit_dependency_graph(repo_root)
    summary = require_structural_evidence(summary, repo_root)

    if write_graph:
        graph = build_dependency_graph(repo_root)
        os.makedirs(os.path.join(repo_root, ".truhowl"), exist_ok=True)
        with open(os.path.join(repo_root, ".truhowl", "graph.json"), "w") as f:
            json.dump(graph, f, indent=2)
        write_index_state(repo_root, summary)
        if prev is not None:
            summary["_index_delta"] = {
                "providers_before": prev.get("providers", 0),
                "callsites_before": prev.get("callsites", 0),
                "providers_now": summary.get("total_providers_detected", 0),
                "callsites_now": summary.get("total_callsites_mapped", 0),
            }

    if output_format == "json":
        return json.dumps(summary, indent=2)
    elif output_format in ("github-issue", "issue", "markdown", "md"):
        return render_audit_github_issue(summary)
    else:
        return render_audit_cli(summary)
