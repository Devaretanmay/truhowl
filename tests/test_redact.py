# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Secret redaction: nothing secret leaves toward the provider or audit."""

import json
import urllib.request
from unittest.mock import patch

from truhowl.llm import LLMClient, LLMConfig
from truhowl.redact import redact_record, redact_secrets


def test_redacts_provider_key_shapes():
    cases = [
        "sk-ant-abcdefghij123456",
        "sk-proj-abcdefghij123456",
        "sk-abcdefghij1234567890",
        "gsk_abcdefghij1234567890",
        "ghp_abcdefghij1234567890",
        "github_pat_abcdefghij1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-12345-abcdef",
    ]
    for secret in cases:
        clean, n = redact_secrets(f"key = '{secret}'")
        assert n == 1, secret
        assert secret not in clean
        assert "[REDACTED:" in clean


def test_redacts_private_key_block():
    block = ("-----BEGIN RSA PRIVATE KEY-----\nMIIBogusKeyMaterial\n"
             "-----END RSA PRIVATE KEY-----")
    clean, n = redact_secrets(f"cert:\n{block}\nafter")
    assert n == 1
    assert "MIIBogusKeyMaterial" not in clean
    assert "after" in clean


def test_redacts_labeled_assignments_keeps_surrounding_code():
    clean, n = redact_secrets("stripe.api_key = 'sk-live-FAKEVALUE1234567890'\ncharge = 1\n")
    assert n >= 1
    assert "FAKEVALUE" not in clean
    assert "charge = 1" in clean


def test_leaves_ordinary_code_and_short_keys_alone():
    benign = [
        "sk-test",
        "gsk_test123",
        "charge = stripe.Charge.create(amount=100)",
        "password must be at least 8 characters",
        "the api_key parameter is required",
    ]
    for text in benign:
        clean, n = redact_secrets(text)
        assert n == 0, text
        assert clean == text


def test_complete_scrubs_body_but_keeps_auth_header():
    seen = {}

    class FakeResp:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self):
            return b'{"choices": [{"message": {"content": "ok"}}], "usage": {}}'

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    client = LLMClient(LLMConfig(provider="openai", api_key="sk-test",
                                 model="gpt-4o"))
    with patch.object(urllib.request, "urlopen", fake_urlopen):
        resp = client.complete(
            [{"role": "user", "content": "key = 'sk-live-FAKEVALUE1234567890'"}])
    assert resp.content == "ok"
    sent = json.dumps(seen["body"])
    assert "FAKEVALUE" not in sent
    assert seen["auth"] == "Bearer sk-test"


def test_redact_record_scrubs_nested_audit_data():
    record = {"output": "token gsk_abcdefghij1234567890 here",
              "nested": [{"k": "AKIAIOSFODNN7EXAMPLE"}],
              "exit_code": 1}
    clean, n = redact_record(record)
    assert n == 2
    blob = json.dumps(clean)
    assert "abcdefghij" not in blob and "AKIAIOSFODNN7EXAMPLE" not in blob
    assert clean["exit_code"] == 1


def test_write_audit_persists_no_secrets(tmp_path):
    from truhowl.hunt import write_audit

    path = write_audit(str(tmp_path), "f1", {
        "final": "refused_unverified",
        "evidence": "failing key sk-live-FAKEVALUE1234567890 end",
    })
    blob = open(path, encoding="utf-8").read()
    assert "FAKEVALUE" not in blob
    assert json.loads(blob)["secrets_redacted"] == 1
