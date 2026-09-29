# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Read-only dashboard: candidates, Issues, and alerts in one place."""

from __future__ import annotations

import html
import time
from typing import Any, Dict, List

from koyote import work_graph


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


_STATUS_COLORS = {
    "OBSERVED": "#6b7280",
    "POTENTIAL": "#d97706",
    "STABLE": "#2563eb",
    "CONFIRMED": "#c2410c",
    "NOTIFIED": "#15803d",
}


def _ago(ts: float, now: float) -> str:
    """Coarse relative time. Never fakes precision below the minute for old data."""
    delta = max(0, int(now - (ts or 0)))
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def collect_dashboard() -> Dict[str, Any]:
    """Snapshot work-graph state for display. Never mutates."""
    now = time.time()
    graph = work_graph.load_graph()
    candidates: List[Dict[str, Any]] = []
    for cid, cand in graph.get("candidates", {}).items():
        issues = []
        for n in cand.get("notified_issues") or []:
            issues.append({"target": n.get("target", ""),
                           "url": n.get("url", ""),
                           "state": n.get("state", "open"),
                           "head": (n.get("head") or "")[:8]})
        candidates.append({
            "id": cid,
            "repository": cand.get("repository", ""),
            "branch": cand.get("branch", ""),
            "status": cand.get("status", ""),
            "head": (cand.get("head_sha") or "")[:8],
            "full_head": cand.get("head_sha", ""),
            "pushes": len(cand.get("pushes", [])),
            "exact": bool(cand.get("exact_sha")),
            "updated": cand.get("updated_ts", 0),
            "updated_ago": _ago(cand.get("updated_ts", 0), now),
            "issues": issues,
        })
    candidates.sort(key=lambda c: c["updated"], reverse=True)
    branches = [{"repo": b.get("repository", ""), "branch": b.get("branch", ""),
                 "head": (b.get("head_sha") or "")[:8],
                 "full_head": b.get("head_sha", ""),
                 "pusher": b.get("pusher", ""),
                 "pr": b.get("open_pr")}
                for b in graph.get("branches", {}).values()]
    open_issues = sum(1 for c in candidates for i in c["issues"] if i["state"] == "open")
    needs_attention = sum(1 for c in candidates if c["status"] in ("POTENTIAL", "CONFIRMED"))
    return {"candidates": candidates, "branches": branches,
            "open_issues": open_issues, "needs_attention": needs_attention,
            "generated_at": now}


def _badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#6b7280")
    return (f'<span style="background:{color};color:#fff;border-radius:999px;'
            f'padding:.1em .7em;font-size:.85em;white-space:nowrap">{_esc(status)}</span>')


def _repo_link(repo: str) -> str:
    if "/" in repo:
        return f'<a href="https://github.com/{_esc(repo)}">{_esc(repo)}</a>'
    return _esc(repo)


def _commit_link(repo: str, sha: str, short: str) -> str:
    if "/" in repo and sha:
        return f'<a href="https://github.com/{_esc(repo)}/commit/{_esc(sha)}">{_esc(short)}</a>'
    return _esc(short)


def _pr_link(repo: str, pr: Any) -> str:
    if pr is not None and "/" in repo:
        return f'<a href="https://github.com/{_esc(repo)}/pull/{pr}">#{pr}</a>'
    if pr is not None:
        return f"#{pr}"
    return "—"


def render_dashboard() -> str:
    """Single HTML page. Stdlib only, no assets. Auto-refreshes every 20s."""
    data = collect_dashboard()
    rows = []
    for c in data["candidates"]:
        issues = "".join(
            f'<a href="{_esc(i["url"])}">{_esc(i["target"])}'
            f' ({_esc(i["state"])})</a><br>' if i["url"] else _esc(i["target"])
            for i in c["issues"]) or "<span class=dim>none yet — notifies only on CONFIRMED</span>"
        rows.append(
            f"<tr><td>{_repo_link(c['repository'])} / {_esc(c['branch'])}</td>"
            f"<td>{_badge(c['status'])}</td>"
            f"<td>{_commit_link(c['repository'], c['full_head'], c['head'])}"
            f"{' [exact]' if c['exact'] else ' ~'}</td>"
            f"<td>{c['pushes']}</td>"
            f"<td class=dim>{_esc(c['updated_ago'])}</td>"
            f"<td>{issues}</td></tr>")
    brows = "".join(
        f"<tr><td>{_repo_link(b['repo'])} / {_esc(b['branch'])}</td>"
        f"<td>{_commit_link(b['repo'], b['head'], b['head'])}</td>"
        f"<td>{_esc(b['pusher'])}</td>"
        f"<td>{_pr_link(b['repo'], b['pr'])}</td></tr>"
        for b in data["branches"])
    empty_candidates = (
        "<tr><td colspan=6>No pushes observed yet — this is normal before the "
        "first webhook arrives. Point your repository webhook at "
        "<code>/webhook</code> and push a branch; it will appear here as "
        "OBSERVED.</td></tr>") if not rows else ""
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content="20">
<title>Koyote Work Impact</title>
<style>body{{font-family:sans-serif;max-width:960px;margin:2em auto;padding:0 1em}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;
padding:.4em .6em;text-align:left}}.dim{{color:#888}}.kpi{{display:flex;gap:1em;
flex-wrap:wrap;margin:1em 0}}.card{{border:1px solid #ccc;border-radius:8px;
padding:.6em 1em;min-width:140px}}.card b{{font-size:1.4em;display:block}}</style>
</head><body>
<h1>Koyote: cross-repo work impact</h1>
<p class=dim>Pushes are observations. Only CONFIRMED impact notifies.
[exact] = exact pushed SHA inspected, ~ = fallback. Auto-refreshes every 20s.</p>
<div class=kpi>
<div class=card><b>{len(rows)}</b>candidates tracked</div>
<div class=card><b>{data['needs_attention']}</b>need attention</div>
<div class=card><b>{data['open_issues']}</b>open impact issues</div>
</div>
<h2>Candidates</h2>
<table><tr><th>Work</th><th>Status</th><th>Head</th><th>Pushes</th>
<th>Updated</th><th>Issues / alerts</th></tr>{''.join(rows)}{empty_candidates}</table>
<h2>Active branches ({len(data["branches"])})</h2>
<table><tr><th>Work</th><th>Head</th><th>By</th><th>PR</th></tr>{brows or
'<tr><td colspan=4 class=dim>Branches appear here after their first push.</td></tr>'}</table>
</body></html>"""
