import http.server
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger("koyote.proxy")

# Hop-by-hop headers that must not be forwarded.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})


def _request_path(request_target: str) -> str:
    """Extract the path component from an origin-form or absolute-form request target.

    ``BaseHTTPRequestHandler.path`` is the raw HTTP/1.1 request target, which
    comes in two forms:

    * **Origin-form** (client pointed straight at the proxy):
      ``"/openai/v1/chat"``
    * **Absolute-form** (client using the proxy via ``HTTP_PROXY``):
      ``"http://api.example.com/openai/v1/chat"``

    Route matching must compare against the path component in both cases, so
    ``HTTP_PROXY``-configured clients get the same credential injection as
    direct clients. The query string is preserved (``?stream=true`` stays in
    the rewritten URL), since prefix matching only inspects the start of the
    path.
    """
    parsed = urllib.parse.urlsplit(request_target)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path


@dataclass
class RouteConfig:
    """Defines a single credential injection rule."""
    prefix: str
    upstream: str
    header: str = "Authorization"
    format: str = "Bearer {credential}"
    credential_source: str = ""

    def resolve_credential(self) -> str:
        if self.credential_source.startswith("env:"):
            var_name = self.credential_source[4:]
            val = os.environ.get(var_name, "")
            if not val:
                _logger.warning("Credential source '%s' is empty", self.credential_source)
            return val
        _logger.warning("Unknown credential_source: %s", self.credential_source)
        return ""

    def matches(self, path: str) -> bool:
        return path.startswith(self.prefix)

    def rewrite_path(self, path: str) -> str:
        """Strip the prefix and prepend upstream base."""
        relative = path[len(self.prefix):]
        if not relative.startswith("/"):
            relative = "/" + relative
        return f"{self.upstream.rstrip('/')}{relative}"


class _CredentialProxyHandler(http.server.BaseHTTPRequestHandler):
    """Request handler that proxies HTTP requests and injects credentials."""

    # Set by the factory to avoid passing args through the HTTPServer API.
    routes: list[RouteConfig] = []
    server: "CredentialProxy" = None  # type: ignore

    protocol_version = "HTTP/1.1"

    def do_GET(self):     self._proxy("GET")
    def do_POST(self):    self._proxy("POST")
    def do_PUT(self):     self._proxy("PUT")
    def do_DELETE(self):  self._proxy("DELETE")
    def do_PATCH(self):   self._proxy("PATCH")
    def do_HEAD(self):    self._proxy("HEAD")
    def do_OPTIONS(self): self._proxy("OPTIONS")

    def do_CONNECT(self):
        self.send_error(501, "CONNECT not supported - use HTTP to reach the proxy")

    def _proxy(self, method: str) -> None:
        # Match on the path so origin-form and absolute-form (HTTP_PROXY) targets get injected.
        request_path = _request_path(self.path)
        target_url = self.path
        route = self._match_route(request_path)
        if route is None:
            self.send_error(403, "No credential route matches this destination")
            return
        target_url = route.rewrite_path(request_path)

        body = self._read_body()
        headers = self._clean_headers(route)

        credential = route.resolve_credential()
        if credential:
            value = route.format.replace("{credential}", credential)
            headers[route.header] = value

        req = urllib.request.Request(
            target_url, data=body, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._respond(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as exc:
            self._respond(exc.code, exc.headers, exc.read())
        except urllib.error.URLError as exc:
            self.send_error(502, f"Proxy upstream error: {exc.reason}")
        except Exception as exc:
            _logger.exception("Proxy error for %s", target_url)
            self.send_error(500, f"Proxy internal error: {exc}")

    def _match_route(self, path: str) -> RouteConfig | None:
        for r in self.routes:
            if r.matches(path):
                return r
        return None

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return self.rfile.read(length)
        return None

    def _clean_headers(self, route: RouteConfig | None) -> dict[str, str]:
        headers = {}
        strip_host = route is not None  # let urllib set the correct Host
        for key, val in self.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            if strip_host and key.lower() == "host":
                continue
            headers[key] = val
        return headers

    def _respond(self, status: int, resp_headers: Any, body: bytes) -> None:
        self.send_response(status)
        for key, val in resp_headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            self.send_header(key, val)
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        _logger.debug("proxy: %s", fmt % args)


class CredentialProxy:
    """Local HTTP proxy for credential injection."""

    def __init__(
        self,
        routes: list[RouteConfig],
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.routes = list(routes)
        self.host = host
        self.port = port
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._saved_env: dict[str, str | None] = {}

    def start(self) -> None:
        if self._server is not None:
            return

        # Per-instance handler class so state is never shared between proxies.
        handler = type(
            "_CredentialProxyHandler",
            (_CredentialProxyHandler,),
            {"routes": self.routes, "proxy": self},
        )

        self._server = http.server.HTTPServer((self.host, 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        _logger.info("CredentialProxy started on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        _logger.info("CredentialProxy stopped")

    def set_env(self, set_https: bool = False) -> None:
        """Set HTTP_PROXY to route through this proxy.

        Note: HTTP CONNECT tunneling is unsupported; clients should direct HTTP
        requests to the proxy or use origin-form requests. HTTPS_PROXY is not
        set by default to prevent clients failing on unhandled CONNECT tunnels.
        """
        proxy_url = f"http://{self.host}:{self.port}"
        vars_to_set = ["HTTP_PROXY", "http_proxy"]
        if set_https:
            vars_to_set.extend(["HTTPS_PROXY", "https_proxy"])
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self._saved_env[var] = os.environ.get(var)
        for var in vars_to_set:
            os.environ[var] = proxy_url

    def restore_env(self) -> None:
        """Restore environment variables to pre-proxy state."""
        for var, val in self._saved_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)
        self._saved_env.clear()

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"
