"""Output compression for compartment results via the Rust engine."""

import json
import logging
from typing import Any

from truhowl._core import route_and_compress as _compress

_logger = logging.getLogger("truhowl.compression")


class OutputCompressor:
    """Compresses compartment outputs above the size threshold."""

    def __init__(self) -> None:
        self._stats: dict[str, dict] = {}

    def record(self, name: str, result: Any) -> Any:
        if _compress is None:
            return None
        raw = result
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = json.dumps(raw, default=str, separators=(",", ":"))
        if not isinstance(raw, str) or len(raw) < 512:
            return None
        try:
            compressed = _compress(raw)
        except Exception as exc:
            _logger.debug("Compression failed for '%s': %s", name, exc)
            return None
        if compressed == raw:
            return None
        ratio = (1 - len(compressed) / len(raw)) * 100
        if ratio < 1.0:
            return None
        self._stats[name] = {
            "compressed": compressed,
            "original_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "ratio": ratio,
        }
        _logger.info(
            "Compressed '%s': %d -> %d bytes (%.1f%% saved)",
            name, len(raw), len(compressed), ratio,
        )
        return compressed

    def log_totals(self) -> None:
        if self._stats:
            total_in = sum(s["original_bytes"] for s in self._stats.values())
            total_out = sum(len(s["compressed"]) for s in self._stats.values())
            if total_in > 0:
                ratio = (1 - total_out / total_in) * 100
                _logger.info(
                    "Compression total: %.1f%% saved across %d compartment(s)",
                    ratio, len(self._stats),
                )
        self._stats.clear()

    @property
    def compressed_outputs(self) -> dict[str, str]:
        return {k: v["compressed"] for k, v in self._stats.items()}
