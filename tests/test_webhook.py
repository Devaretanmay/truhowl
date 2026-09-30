# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for GitHub Webhook signature verification and event handling."""

import hashlib
import hmac
from truhowl.api.webhooks import process_webhook_payload, verify_webhook_signature


def test_verify_webhook_signature():
    secret = "test-webhook-secret"
    body = b'{"action": "created"}'
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, sig, secret=secret) is True
    assert verify_webhook_signature(body, "sha256=invalid", secret=secret) is False


def test_process_webhook_events():
    ok, msg = process_webhook_payload("ping", {})
    assert ok is True
    assert msg == "pong"

    ok, msg = process_webhook_payload("installation", {"action": "created", "installation": {"id": 12345, "account": {"login": "acme"}}})
    assert ok is True
    assert "acme" in msg
    assert "12345" in msg
