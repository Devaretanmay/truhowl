# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""GitHub Webhook integration for Truhowl Hosted Control Plane.

Receives and processes GitHub App events:
- installation (created, deleted)
- installation_repositories (added, removed)
- push
- ping

Verifies HMAC-SHA256 signatures before processing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Tuple


def verify_webhook_signature(body_bytes: bytes, signature_header: str, secret: str | None = None) -> bool:
    """Verify GitHub HMAC-SHA256 signature header (X-Hub-Signature-256)."""
    secret_key = secret or os.environ.get("TRUHOWL_GITHUB_WEBHOOK_SECRET", "")
    if not secret_key:
        # If no secret configured, accept in dev/mock environment
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret_key.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def process_webhook_payload(event: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Process incoming GitHub event payload and route to appropriate service."""
    if event == "ping":
        return True, "pong"

    if event == "installation":
        action = payload.get("action")
        installation_id = str(payload.get("installation", {}).get("id", ""))
        account = payload.get("installation", {}).get("account", {}).get("login", "")
        if action == "created":
            return True, f"GitHub App installed for {account} (ID: {installation_id})"
        elif action == "deleted":
            return True, f"GitHub App uninstalled for {account} (ID: {installation_id})"
        return True, f"Installation action {action} received"

    if event == "installation_repositories":
        action = payload.get("action")
        added = payload.get("repositories_added", [])
        removed = payload.get("repositories_removed", [])
        return True, f"Repositories updated: +{len(added)}, -{len(removed)}"

    if event == "push":
        ref = payload.get("ref", "")
        repo_name = payload.get("repository", {}).get("full_name", "")
        return True, f"Push received for {repo_name} branch {ref}"

    return True, f"Event {event} acknowledged"
