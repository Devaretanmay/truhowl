"""Upstream release monitoring: make "Truhowl watches" honest.

Polls authoritative package registries (npm for the current provider set),
compares against persisted per-package state, and records one ExternalChange
per genuinely new release. No scraping, no guessing.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

NPM_REGISTRY = "https://registry.npmjs.org"
REQUEST_TIMEOUT = 15


@dataclass
class UpstreamRelease:
    ecosystem: str = "npm"
    package: str = ""
    version: str = ""
    published_at: str = ""
    source_url: str = ""


@dataclass
class UpstreamCheck:
    package: str = ""
    ok: bool = False
    release: UpstreamRelease | None = None
    error: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fetch_npm_latest(package: str, timeout: int = REQUEST_TIMEOUT) -> UpstreamCheck:
    """Fetch the latest published version from the npm registry."""
    url = f"{NPM_REGISTRY}/{package.strip()}/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "truhowl-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return UpstreamCheck(package=package, ok=False, error=f"npm HTTP {exc.code}")
    except Exception as exc:
        return UpstreamCheck(package=package, ok=False,
                             error=f"{type(exc).__name__}: {exc}")
    version = str(data.get("version", "") or "").strip()
    if not version:
        return UpstreamCheck(package=package, ok=False, error="malformed version: empty")
    return UpstreamCheck(package=package, ok=True, release=UpstreamRelease(
        ecosystem="npm", package=package, version=version,
        published_at=_now(), source_url=url))


def _numeric_parts(version: str) -> list[int] | None:
    cleaned = (version or "").strip().lstrip("v=^~<> ").split("+")[0].split("-")[0]
    parts = cleaned.split(".")
    out: list[int] = []
    for p in parts:
        digits = "".join(c for c in p if c.isdigit())
        if not digits and p:
            return None
        out.append(int(digits) if digits else 0)
    if not out:
        return None
    return out


def is_valid_version(version: str) -> bool:
    """True when the version parses into numeric components."""
    return _numeric_parts(version) is not None


def compare_versions(old: str, new: str) -> int | None:
    """-1 if new is older, 0 if equal, 1 if newer. None if unparseable."""
    a, b = _numeric_parts(old), _numeric_parts(new)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    if b == a:
        return 0
    return 1 if b > a else -1


def is_major_bump(old: str, new: str) -> bool:
    a, b = _numeric_parts(old), _numeric_parts(new)
    if not a or not b:
        return False
    return b[0] != a[0]
