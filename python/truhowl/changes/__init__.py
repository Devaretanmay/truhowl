"""Upstream change monitoring: poll registries, open cases, never repair."""

from truhowl.changes.monitor import poll_and_watch, poll_upstream
from truhowl.changes.sources import (
    UpstreamCheck,
    UpstreamRelease,
    compare_versions,
    fetch_npm_latest,
    is_major_bump,
    is_valid_version,
)

__all__ = [
    "UpstreamCheck",
    "UpstreamRelease",
    "compare_versions",
    "fetch_npm_latest",
    "is_major_bump",
    "is_valid_version",
    "poll_and_watch",
    "poll_upstream",
]
