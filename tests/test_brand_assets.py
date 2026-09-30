# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Truhowl Brand Assets & Mascot Specification (B11)."""

from fastapi.testclient import TestClient

from truhowl.api.app import app
from truhowl.api.brand import (
    avatar_svg,
    canonical_mascot_svg,
    empty_state_svg,
    favicon_svg,
    loading_state_svg,
    logo_svg,
)

client = TestClient(app)


def test_canonical_mascot_svg():
    svg = canonical_mascot_svg(size=128)
    assert "<svg" in svg
    assert 'width="128"' in svg
    assert "viewBox=\"0 0 100 100\"" in svg
    assert "url(#grad-canonical)" in svg
    assert "url(#glow-canonical)" in svg
    # Proportions: ears, tail, muzzle present
    assert "Left Ear" in svg
    assert "Right Ear" in svg
    assert "Tail" in svg


def test_brand_derivations():
    fav = favicon_svg()
    assert 'width="32"' in fav

    logo = logo_svg(height=36)
    assert 'height="36"' in logo
    assert "Truhowl" in logo

    avatar = avatar_svg(size=120)
    assert 'width="120"' in avatar
    assert "<circle" in avatar

    empty = empty_state_svg(size=140)
    assert 'width="140"' in empty

    loading = loading_state_svg(size=64)
    assert "howlPulse" in loading


def test_brand_api_endpoints():
    r_mascot = client.get("/api/brand/mascot.svg")
    assert r_mascot.status_code == 200
    assert r_mascot.headers["content-type"] == "image/svg+xml"
    assert "<svg" in r_mascot.text

    r_logo = client.get("/api/brand/logo.svg")
    assert r_logo.status_code == 200
    assert "Truhowl" in r_logo.text

    r_fav = client.get("/api/brand/favicon.svg")
    assert r_fav.status_code == 200

    r_avatar = client.get("/api/brand/avatar.svg")
    assert r_avatar.status_code == 200
