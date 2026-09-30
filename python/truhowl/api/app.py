# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""FastAPI Application Entry Point for Truhowl Hosted Control Plane.

Exposes REST endpoints for:
- Control Plane SPA Web Dashboard
- Authentication & User Session
- Repository Inventory & Registration
- Upstream Changes & Registry Polling
- Migration Cases & Clean-Room Replay Verification Evidence
- Automation Policy Management (OBSERVE | PREPARE | DELIVER)
- Ask Truhowl Evidence QA
- GitHub Webhooks & App Integration
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import Body, Header, HTTPException, Request
from fastapi.applications import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from truhowl.agent import automation as agent_auto
from truhowl.agent import models as agent_models
from truhowl.agent import service as agent_svc
from truhowl.api.auth import validate_session
from truhowl.api.brand import avatar_svg, canonical_mascot_svg, favicon_svg, logo_svg
from truhowl.api.db import LocalAgentStoreAdapter, StorageAdapter
from truhowl.api.pilot import global_observability, global_pilot_metrics
from truhowl.api.ui import render_ui
from truhowl.api.webhooks import process_webhook_payload, verify_webhook_signature
from truhowl.changes.monitor import poll_and_watch

app = FastAPI(
    title="Truhowl Control Plane API",
    description="Backend API for Truhowl Autonomous Software Maintenance Agent",
    version="1.2.0",
)


def get_workspace(req: Request) -> str:
    path = req.headers.get("X-Truhowl-Workspace") or os.getcwd()
    return agent_svc.resolve_workspace(path)


def get_storage(workspace: str) -> StorageAdapter:
    return LocalAgentStoreAdapter(workspace)


@app.get("/", response_class=HTMLResponse)
def index_ui():
    """Serve the Truhowl Control Plane UI Dashboard."""
    return render_ui()


@app.get("/api/health")
def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "product": "Truhowl",
        "version": "1.2.0",
        "agent": "Phase 3 backend active",
    }


@app.get("/api/auth/session")
def session(req: Request):
    """Retrieve current authenticated user session."""
    token = req.cookies.get("truhowl_session")
    sess = validate_session(token)
    if not sess:
        raise HTTPException(status_code=401, detail="Unauthorized session")
    return sess


@app.get("/api/auth/github/login")
def github_oauth_login():
    """Redirect to GitHub OAuth authorization URL."""
    import secrets
    client_id = os.environ.get("TRUHOWL_GITHUB_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="TRUHOWL_GITHUB_CLIENT_ID is not configured")
    state = secrets.token_hex(16)
    redirect_uri = os.environ.get("TRUHOWL_OAUTH_REDIRECT_URI", "")
    url = f"https://github.com/login/oauth/authorize?client_id={client_id}&state={state}&scope=user:email"
    if redirect_uri:
        url += f"&redirect_uri={redirect_uri}"
    resp = RedirectResponse(url=url)
    resp.set_cookie("truhowl_oauth_state", state, httponly=True, max_age=600, samesite="lax")
    return resp


@app.get("/api/auth/github/callback")
def github_oauth_callback(req: Request, code: Optional[str] = None, state: Optional[str] = None):
    """Handle GitHub OAuth redirect callback."""
    if not code:
        raise HTTPException(status_code=400, detail="OAuth authorization code missing")
    cookie_state = req.cookies.get("truhowl_oauth_state")
    if cookie_state and state and cookie_state != state:
        raise HTTPException(status_code=400, detail="CSRF state verification failed")

    client_id = os.environ.get("TRUHOWL_GITHUB_CLIENT_ID")
    client_secret = os.environ.get("TRUHOWL_GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="GitHub OAuth credentials not configured")

    import json
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }).encode("utf-8")
    token_req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=data,
        headers={"Accept": "application/json", "User-Agent": "Truhowl-Control-Plane/1.2.0"},
    )
    token_data = {}
    try:
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
            access_token = token_data.get("access_token")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to exchange code for token: {exc}")

    if not access_token:
        error_msg = token_data.get("error_description") or "OAuth code exchange failed"
        raise HTTPException(status_code=400, detail=error_msg)

    user_req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Truhowl-Control-Plane/1.2.0"},
    )
    try:
        with urllib.request.urlopen(user_req, timeout=15) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch GitHub user identity: {exc}")

    from truhowl.api.auth import generate_session_token
    user_id = f"usr_gh_{user_data.get('id')}"
    username = user_data.get("login", "github-user")
    sess_token = generate_session_token(user_id=user_id, org_id="org_default", username=username)

    redirect_resp = RedirectResponse(url="/", status_code=302)
    redirect_resp.set_cookie(key="truhowl_session", value=sess_token, httponly=True, samesite="lax", max_age=86400 * 7)
    redirect_resp.delete_cookie("truhowl_oauth_state")
    return redirect_resp


@app.post("/api/auth/logout")
def logout(req: Request):
    """Revoke user session and clear authentication cookie."""
    import json
    from truhowl.api.auth import revoke_session
    token = req.cookies.get("truhowl_session")
    if token:
        revoke_session(token)
    resp = Response(content=json.dumps({"status": "logged_out"}), media_type="application/json")
    resp.delete_cookie("truhowl_session")
    return resp


@app.get("/api/repositories")
def list_repositories(req: Request):
    """List connected repositories and dependency status."""
    ws = get_workspace(req)
    storage = get_storage(ws)
    return {"repositories": storage.list_repositories("org_default")}


@app.post("/api/repositories")
def add_repository(req: Request, payload: Dict[str, Any] = Body(...)):
    """Register/connect a new repository to the Control Plane."""
    ws = get_workspace(req)
    path = payload.get("path") or ws
    key = payload.get("repo_key") or os.path.basename(path)
    storage = get_storage(ws)
    res = storage.add_repository("org_default", key, key, path, payload.get("github_repo"))
    return res


@app.get("/api/changes")
def list_changes(req: Request):
    """List detected upstream SDK/API changes."""
    ws = get_workspace(req)
    storage = get_storage(ws)
    return {"changes": storage.list_external_changes("org_default")}


@app.post("/api/changes/poll")
def poll_changes(req: Request, payload: Dict[str, Any] = Body(...)):
    """Trigger an upstream registry check for new releases."""
    ws = get_workspace(req)
    providers = payload.get("providers") or ["stripe", "openai", "anthropic"]
    repo_paths = payload.get("repo_paths") or [ws]
    out = poll_and_watch(ws, providers=providers, repo_paths=repo_paths)
    return out


@app.get("/api/cases")
def list_cases(req: Request):
    """List all migration cases and repository lifecycle states."""
    ws = get_workspace(req)
    storage = get_storage(ws)
    return {"cases": storage.list_cases("org_default")}


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, req: Request):
    """Get migration case detail including clean-room replay verification evidence."""
    ws = get_workspace(req)
    storage = get_storage(ws)
    case = storage.get_case("org_default", case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    explanation = agent_svc.explain_case(ws, case_id)
    return {"case": case, "explanation": explanation}


@app.post("/api/cases/{case_id}/run")
def run_case(case_id: str, req: Request, payload: Dict[str, Any] = Body(...)):
    """Execute migration repair & clean-room verification for a case."""
    ws = get_workspace(req)
    repo_key = payload.get("repo_key")
    confirmed = bool(payload.get("confirmed", False))
    create_pr = bool(payload.get("create_pr", False))
    github_repo = payload.get("github_repo")

    case = agent_svc.get_case(ws, case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    if not repo_key:
        repo_key = case["repos"][0]["repo_key"]

    result = agent_svc.run_repo(
        ws, case_id, repo_key,
        confirmed=confirmed,
        create_pr=create_pr,
        github_repo=github_repo,
    )
    return result


@app.get("/api/policy")
def get_policy(req: Request):
    """Get current persisted automation policy mode."""
    ws = get_workspace(req)
    policy = agent_auto.get_policy(ws)
    return policy


@app.post("/api/policy")
def set_policy(req: Request, payload: Dict[str, Any] = Body(...)):
    """Set persisted automation policy mode (observe | prepare | deliver)."""
    ws = get_workspace(req)
    mode = payload.get("mode", agent_auto.OBSERVE)
    try:
        policy = agent_auto.set_mode(ws, mode)
        return policy
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/ask")
def ask(req: Request, payload: Dict[str, Any] = Body(...)):
    """Ask Truhowl an evidence-grounded question about codebase migration state."""
    ws = get_workspace(req)
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question parameter is required")

    from truhowl.agent.service import explain_case, list_cases
    cases = list_cases(ws)

    q = question.lower()
    if "upstream" in q:
        store = agent_models.load_store(ws)
        up = store.org.get("upstream", {})
        if not up:
            return {"answer": "No upstream changes have been recorded yet. Run `truhowl agent watch --poll` to check package registries."}
        lines = [f"- {pkg}: v{info.get('version')} (checked at {info.get('checked_at')})" for pkg, info in up.items()]
        return {"answer": "Recorded upstream releases:\n" + "\n".join(lines)}

    if "pr" in q or "delivery" in q:
        store = agent_models.load_store(ws)
        if not store.delivery:
            return {"answer": "No PR delivery attempts recorded yet. Automation policy must be set to DELIVER with valid GitHub credentials."}
        lines = []
        for k, d in store.delivery.items():
            case_part = k.split("\x00")[0]
            lines.append(f"- Case {case_part}: status={d.get('status')} error={d.get('error')} url={d.get('pr_url')}")
        return {"answer": "Delivery status summary:\n" + "\n".join(lines)}

    if cases:
        case = cases[0]
        exp = explain_case(ws, case["case_id"])
        return {"answer": f"Evidence for case {case['case_id']}:\n\n{exp}"}

    return {"answer": f"No active migration cases found in workspace {ws}. Run `truhowl check` or `truhowl agent watch` to discover changes."}


@app.post("/api/webhooks/github")
async def github_webhook(
    req: Request,
    x_github_event: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),
):
    """GitHub App webhook endpoint verifying HMAC signatures."""
    body_bytes = await req.body()
    if not verify_webhook_signature(body_bytes, x_hub_signature_256 or ""):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    payload = await req.json() if body_bytes else {}
    ok, msg = process_webhook_payload(x_github_event or "ping", payload)
    return {"status": "success", "detail": msg}


@app.get("/api/brand/mascot.svg")
def brand_mascot():
    """Serve canonical mascot vector SVG."""
    return Response(content=canonical_mascot_svg(size=128), media_type="image/svg+xml")


@app.get("/api/brand/logo.svg")
def brand_logo():
    """Serve horizontal logo SVG."""
    return Response(content=logo_svg(height=36), media_type="image/svg+xml")


@app.get("/api/brand/favicon.svg")
def brand_favicon():
    """Serve high-contrast favicon SVG."""
    return Response(content=favicon_svg(), media_type="image/svg+xml")


@app.get("/api/brand/avatar.svg")
def brand_avatar():
    """Serve circular badge avatar SVG."""
    return Response(content=avatar_svg(size=120), media_type="image/svg+xml")


@app.get("/api/pilot/metrics")
def pilot_metrics():
    """Retrieve operational pilot metrics for default tenant."""
    return {"metrics": global_pilot_metrics.get_metrics("org_default")}


@app.get("/api/pilot/observability")
def pilot_observability():
    """Retrieve observability error and latency summary."""
    return {"observability": global_observability.get_summary()}


@app.post("/api/onboarding/complete")
def complete_onboarding(req: Request, payload: Dict[str, Any] = Body(...)):
    """Complete onboarding setup flow (B1)."""
    ws = get_workspace(req)
    mode = payload.get("mode", agent_auto.OBSERVE)
    policy = agent_auto.set_mode(ws, mode)
    repos = payload.get("repositories", [])
    test_cmd = payload.get("verification_cmd", "npm test")
    storage = get_storage(ws)
    for r in repos:
        storage.add_repository("org_default", r, r, ws, verification_cmd=test_cmd)
    return {"status": "completed", "policy": policy, "repositories": repos}

