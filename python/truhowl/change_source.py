# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Change-source abstraction: the system/contract a repository depends upon.

Thin value types covering vendor SDKs today and versioned contracts
(OpenAPI, GraphQL, protobuf, webhooks, MCP, internal services) as
fail-closed kinds. No connectors live here — just the seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


KINDS = (
    "external_api",
    "sdk",
    "openapi",
    "graphql",
    "protobuf",
    "webhook",
    "mcp_server",
    "internal_service",
)

NO_IMPACT = "NO_IMPACT"
IMPACT_AI = "IMPACT_AI"
IMPACT_QUARANTINE = "IMPACT_QUARANTINE"


@dataclass
class ChangeSource:
    """A system/contract the repository depends upon, at a detected change."""

    kind: str
    identity: str
    version_from: str = "unknown"
    version_to: str = "unknown"
    contract_hash: str = ""
    origin: str = "registry"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"Unknown change-source kind: {self.kind!r}")

    @property
    def provider(self) -> str:
        """Back-compat alias: vendor-SDK code paths key on provider name."""
        return self.metadata.get("provider", self.identity)

    @classmethod
    def sdk(cls, provider: str, version_from: str = "unknown", version_to: str = "unknown",
            origin: str = "registry", **meta: Any) -> "ChangeSource":
        return cls(kind="sdk", identity=provider.lower(), version_from=version_from,
                   version_to=version_to, origin=origin,
                   metadata={"provider": provider.lower(), **meta})

    @classmethod
    def external_api(cls, provider: str, version_from: str = "unknown", version_to: str = "unknown",
                     origin: str = "registry", **meta: Any) -> "ChangeSource":
        return cls(kind="external_api", identity=provider.lower(), version_from=version_from,
                   version_to=version_to, origin=origin,
                   metadata={"provider": provider.lower(), **meta})

    def key(self) -> str:
        base = self.contract_hash or f"{self.version_from}__{self.version_to}"
        return f"{self.kind}/{self.identity}/{base}"


@dataclass
class Detection:
    """Read-only outcome of understanding one change against one repository."""

    source: ChangeSource
    outcome: str
    reason: str = ""
    affected_files: list[str] = field(default_factory=list)
    callsite_count: int = 0
    ai_dependent: bool = False
    confidence: float = 0.0

    def __post_init__(self):
        valid = (NO_IMPACT, IMPACT_AI, IMPACT_QUARANTINE)
        if self.outcome not in valid:
            raise ValueError(f"Unknown detection outcome: {self.outcome!r}")
