# Copyright 2026 Truhowl Authors
"""ChangeSource abstraction: kinds constructible, provider adapter round-trips."""

import pytest

from truhowl.change_source import (
    ChangeSource, Detection, KINDS,
    NO_IMPACT, IMPACT_AI, IMPACT_QUARANTINE,
)


def test_all_kinds_constructible():
    for kind in KINDS:
        s = ChangeSource(kind=kind, identity="x")
        assert s.kind == kind
        assert s.key().startswith(f"{kind}/x/")


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        ChangeSource(kind="blockchain", identity="x")


def test_sdk_adapter_round_trips_provider_paths():
    s = ChangeSource.sdk("Stripe", version_from="11.18.0", version_to="13.0.0")
    assert s.provider == "stripe"
    assert s.identity == "stripe"
    assert s.key() == "sdk/stripe/11.18.0__13.0.0"


def test_contract_hash_key_for_versionless_sources():
    s = ChangeSource(kind="protobuf", identity="billing.proto", contract_hash="abc123")
    assert s.key() == "protobuf/billing.proto/abc123"


def test_detection_outcomes_validated():
    s = ChangeSource.sdk("stripe")
    for outcome in (NO_IMPACT, IMPACT_AI, IMPACT_QUARANTINE):
        d = Detection(source=s, outcome=outcome)
        assert d.outcome == outcome
    with pytest.raises(ValueError):
        Detection(source=s, outcome="MAYBE")


def test_detection_defaults_fail_closed():
    s = ChangeSource(kind="mcp_server", identity="internal-tools")
    d = Detection(source=s, outcome=IMPACT_QUARANTINE, reason="no connector yet")
    assert d.confidence == 0.0
    assert d.affected_files == []
