import hashlib
import hmac
import json
import urllib.request

from truhowl.github.client import verify_webhook_signature
from truhowl.github.webhook_server import WebhookServer, handle_webhook_payload
from truhowl.github.trust_pr import generate_trust_pr_markdown, TrustPRMetadata


def test_webhook_signature_verification():
    secret = "test_truhowl_secret_key_123"
    payload = b'{"action": "push", "repository": {"full_name": "owner/repo"}}'
    
    mac = hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256)
    valid_sig = f"sha256={mac.hexdigest()}"
    invalid_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

    assert verify_webhook_signature(payload, valid_sig, secret) is True
    assert verify_webhook_signature(payload, invalid_sig, secret) is False
    assert verify_webhook_signature(payload, None, secret) is False
    assert verify_webhook_signature(payload, "invalid_prefix", secret) is False


def test_handle_webhook_payload_dispatch():
    secret = "my_secret"
    payload = json.dumps({"action": "opened", "repository": {"full_name": "calcom/cal.com"}}).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256)
    sig = f"sha256={mac.hexdigest()}"

    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "pull_request",
    }

    def mock_handler(data, event):
        return {"processed": True, "event": event}

    res = handle_webhook_payload(payload, headers, secret=secret, handler_fn=mock_handler)
    assert res["success"] is True
    assert res["event"] == "pull_request"
    assert res["repository"] == "calcom/cal.com"
    assert res["handler_result"]["processed"] is True


def test_trust_pr_markdown_generation():
    meta = TrustPRMetadata(
        provider_name="Stripe Node SDK",
        from_version="11.18.0",
        to_version="22.0.0",
        changelog_url="https://docs.stripe.com/changelog",
        files_modified=1,
        files_scanned=4,
        unintended_files_modified=0,
        quarantined_callsites_count=0,
        unified_diff="--- a/src/billing.ts\n+++ b/src/billing.ts\n@@ -7,1 +7,1 @@\n-    amount: amount,\n+    amount: String(amount),",
        test_command="npm test",
        test_exit_code=0,
        test_duration_ms=42,
        lockfile_hash="af1349b9f5f9a1a6a0404dea36dcc949",
        patch_hash="3559d0c1e5a59574f260d31cd680f471",
        semantic_score=1.0,
    )

    markdown = generate_trust_pr_markdown(meta)
    assert "[VERIFIED] Autonomous Maintenance: Upgrade `Stripe Node SDK`" in markdown
    assert "Blast Radius Containment Receipt" in markdown
    assert "100% Contained" in markdown
    assert "amount: String(amount)" in markdown
    assert "SUCCESS (GREEN)" in markdown
    assert "42ms" in markdown
    assert "— Hunt, Work bot" in markdown


def test_http_server_delivers_to_handler_unbound(tmp_path):
    """Regression: handler stored on the handler class must not bind self
    over HTTP (TypeError: takes 2 args but 3 given). Exercises the real
    socket path, not handle_webhook_payload directly."""
    seen = []

    def handler(data, event_type):
        seen.append((data.get("repository", {}).get("full_name"), event_type))
        return {"ok": True}

    server = WebhookServer(port=0, secret=None, handler=handler)
    server.start(blocking=False)
    try:
        port = server._server.server_address[1]
        body = json.dumps({"ref": "refs/heads/main",
                           "repository": {"full_name": "acme/demo"}}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/webhook", data=body,
            headers={"Content-Type": "application/json",
                     "X-GitHub-Event": "push"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.load(resp)
        assert res["success"] is True
        assert res["handler_result"] == {"ok": True}
        assert seen == [("acme/demo", "push")]
    finally:
        server.stop()


def test_dashboard_renders_state(tmp_path, monkeypatch):
    from truhowl import work_graph
    from truhowl.github.dashboard import collect_dashboard, render_dashboard
    from truhowl.github.push_events import parse_push_payload
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg.json"))
    work_graph.record_push(parse_push_payload({
        "ref": "refs/heads/feature/payments", "before": "0" * 40, "after": "abc123",
        "repository": {"full_name": "acme/api-service"}, "pusher": {"name": "dev1"},
        "commits": []}), exact_sha=True)
    data = collect_dashboard()
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["status"] == "OBSERVED"
    page = render_dashboard()
    assert "acme/api-service" in page and "OBSERVED" in page


def test_dashboard_ux_elements(tmp_path, monkeypatch):
    from truhowl import work_graph
    from truhowl.github.dashboard import render_dashboard
    from truhowl.github.push_events import parse_push_payload
    monkeypatch.setenv("TRUHOWL_WORK_GRAPH_FILE", str(tmp_path / "wg2.json"))
    work_graph.record_push(parse_push_payload({
        "ref": "refs/heads/feature/payments", "before": "0" * 40, "after": "abc123",
        "repository": {"full_name": "acme/api-service"}, "pusher": {"name": "dev1"},
        "commits": []}))
    page = render_dashboard()
    assert 'http-equiv=refresh content="20"' in page  # honest auto-refresh
    assert "candidates tracked" in page and "need attention" in page
    assert "border-radius:999px" in page and "OBSERVED" in page  # text badge, not color alone
    assert "ago" in page or "just now" in page  # freshness signal
    assert "https://github.com/acme/api-service" in page  # drill link
    assert "viewport" in page
