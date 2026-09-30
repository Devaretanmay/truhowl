"""CredentialProxy regression tests."""

import http.client
import http.server
import threading

import pytest

from truhowl.sandbox.proxy import CredentialProxy, RouteConfig, _request_path


class _CaptureUpstream(http.server.BaseHTTPRequestHandler):
    """Echo server that records the request path and Authorization header."""

    seen: list[dict] = []

    def do_GET(self):
        type(self).seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _CaptureUpstream.seen = []
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _CaptureUpstream)
    except PermissionError:
        pytest.skip("Local socket binding restricted by OS sandbox environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(upstream, monkeypatch):
    monkeypatch.setenv("PROXY_TEST_KEY", "sk-live-secret-999")
    p = CredentialProxy(routes=[
        RouteConfig(
            prefix="/openai",
            upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
            credential_source="env:PROXY_TEST_KEY",
        ),
    ])
    try:
        p.start()
    except PermissionError:
        pytest.skip("Local socket binding restricted by OS sandbox environment")
    yield p
    p.stop()


def _origin_form(proxy_port: int, path: str) -> int:
    """Send an origin-form GET to the proxy; return the HTTP status."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status
    except PermissionError:
        pytest.skip("Local socket connection restricted by OS sandbox environment")


def _absolute_form(proxy_port: int, target: str) -> int:
    """Send an absolute-form GET (as HTTP_PROXY clients do) to the proxy."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port)
        conn.request("GET", target)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status
    except PermissionError:
        pytest.skip("Local socket connection restricted by OS sandbox environment")


def test_origin_form_gets_rewritten_and_injected(proxy, upstream):
    assert _origin_form(proxy.port, "/openai/v1/chat") == 200
    assert _CaptureUpstream.seen == [{
        "path": "/v1/chat",
        "auth": "Bearer sk-live-secret-999",
    }]


def test_absolute_form_gets_rewritten_and_injected(proxy, upstream):
    port = upstream.server_address[1]
    assert _absolute_form(
        proxy.port, f"http://127.0.0.1:{port}/openai/v1/chat",
    ) == 200
    assert _CaptureUpstream.seen == [{
        "path": "/v1/chat",
        "auth": "Bearer sk-live-secret-999",
    }]


def test_no_route_match_is_rejected(proxy, upstream):
    port = upstream.server_address[1]
    assert _absolute_form(
        proxy.port, f"http://127.0.0.1:{port}/other/v1/chat",
    ) == 403
    assert _CaptureUpstream.seen == []


def test_query_string_preserved_on_rewrite(proxy, upstream):
    """Query params survive the rewrite (e.g. /openai/v1/chat?stream=true)."""
    assert _origin_form(proxy.port, "/openai/v1/chat?stream=true") == 200
    assert _CaptureUpstream.seen == [{
        "path": "/v1/chat?stream=true",
        "auth": "Bearer sk-live-secret-999",
    }]


def test_route_config_path_forms_match():
    """The path parser handles origin, absolute, and bare forms."""
    assert _request_path("/openai/v1/chat") == "/openai/v1/chat"
    assert _request_path("http://api.example.com/openai/v1/chat") == "/openai/v1/chat"
    assert _request_path("https://api.example.com/openai") == "/openai"
    assert _request_path("/openai/v1/chat?stream=true") == "/openai/v1/chat?stream=true"
    assert _request_path("http://host/openai?model=gpt") == "/openai?model=gpt"
    assert _request_path("/") == "/"
