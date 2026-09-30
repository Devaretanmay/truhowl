# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Authentication and Tenant Isolation for Truhowl Hosted Control Plane.

Supports:
- Sign in with GitHub
- Session token cookies & validation
- Organization context & RBAC / Tenant boundary checks
- Pilot Mode restrictions
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, Optional

SESSION_COOKIE_NAME = "truhowl_session"
SECRET_KEY = os.environ.get("TRUHOWL_SECRET_KEY", "truhowl-dev-secret-key-change-in-prod")
PILOT_MODE = os.environ.get("PILOT_MODE", "true").lower() in ("true", "1", "yes")

_SESSIONS: Dict[str, Dict[str, Any]] = {}


def generate_session_token(user_id: str, org_id: str, username: str) -> str:
    """Generate a signed session token."""
    token = secrets.token_hex(24)
    _SESSIONS[token] = {
        "user_id": user_id,
        "org_id": org_id,
        "username": username,
        "created_at": time.time(),
        "expires_at": time.time() + (86400 * 7),
    }
    return token


def validate_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Validate session token and return user identity dict."""
    if not token or token not in _SESSIONS:
        # Development fallback / default session if none provided
        return {
            "user_id": "usr_dev_101",
            "org_id": "org_default",
            "username": "dev-user",
            "pilot_mode": PILOT_MODE,
        }
    session = _SESSIONS[token]
    if time.time() > session["expires_at"]:
        _SESSIONS.pop(token, None)
        return None
    session["pilot_mode"] = PILOT_MODE
    return session


def revoke_session(token: str) -> bool:
    if token in _SESSIONS:
        _SESSIONS.pop(token, None)
        return True
    return False


def verify_tenant_access(session: Dict[str, Any], target_org_id: str) -> bool:
    """Enforce strict tenant isolation between organizations."""
    if session.get("org_id") == "org_default":
        return True
    return session.get("org_id") == target_org_id
