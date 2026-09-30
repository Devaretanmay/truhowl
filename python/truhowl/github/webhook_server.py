"""GitHub Webhook Daemon & Event Ingestion Server for Truhowl."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import threading
from typing import Any, Callable, Dict

from .client import verify_webhook_signature

_logger = logging.getLogger("truhowl.github.webhook_server")


def handle_webhook_payload(
    payload_bytes: bytes,
    headers: dict[str, str],
    secret: str | None = None,
    handler_fn: Callable[[Dict[str, Any], str], Dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process incoming webhook payload with signature validation."""
    sig_header = headers.get("X-Hub-Signature-256") or headers.get("x-hub-signature-256")
    event_type = headers.get("X-GitHub-Event") or headers.get("x-github-event") or "push"

    if secret:
        if not verify_webhook_signature(payload_bytes, sig_header, secret):
            return {
                "success": False,
                "error": "Invalid HMAC-SHA256 webhook signature",
                "status_code": 401,
            }

    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        return {
            "success": False,
            "error": f"Malformed JSON: {e}",
            "status_code": 400,
        }

    repo_name = data.get("repository", {}).get("full_name", "unknown")
    result = {
        "success": True,
        "event": event_type,
        "repository": repo_name,
        "action": data.get("action", "triggered"),
    }

    if handler_fn:
        custom_res = handler_fn(data, event_type)
        result["handler_result"] = custom_res

    return result


class WebhookHTTPHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for GitHub Webhooks."""

    webhook_secret: str | None = None
    event_handler: Callable[[Dict[str, Any], str], Dict[str, Any]] | None = None

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "service": "truhowl-github-app"}).encode("utf-8"))
        elif self.path.split("?")[0].rstrip("/") == "/dashboard":
            from truhowl.github.dashboard import render_dashboard
            body = render_dashboard().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path in ("/webhook", "/api/webhook", "/webhook/howl", "/webhook/hunt", "/webhook/consult", "/webhook/work"):
            content_length = int(self.headers.get("Content-Length", 0))
            payload = self.rfile.read(content_length)

            headers_dict = {k: v for k, v in self.headers.items()}
            res = handle_webhook_payload(
                payload,
                headers_dict,
                secret=self.webhook_secret,
                handler_fn=self.event_handler,
            )

            status = 200 if res.get("success") else res.get("status_code", 400)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress verbose default access logging
        pass


class WebhookServer:
    """Background HTTP Server for GitHub Webhook events."""

    def __init__(
        self,
        port: int = 8080,
        host: str = "0.0.0.0",
        secret: str | None = None,
        handler: Callable[[Dict[str, Any], str], Dict[str, Any]] | None = None,
    ):
        self.port = port
        self.host = host
        self.secret = secret or os.environ.get("TRUHOWL_WEBHOOK_SECRET")
        self.handler = handler
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, blocking: bool = False):
        """Start the webhook listener server."""
        handler_cls = WebhookHTTPHandler
        handler_cls.webhook_secret = self.secret
        handler_cls.event_handler = (
            staticmethod(self.handler) if self.handler is not None else None
        )

        self._server = HTTPServer((self.host, self.port), handler_cls)
        if blocking:
            _logger.info("Listening for webhooks on http://%s:%s/webhook", self.host, self.port)
            self._server.serve_forever()
        else:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    def stop(self):
        """Stop the server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
