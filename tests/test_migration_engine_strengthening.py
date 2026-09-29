# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for hardened migration reasoning, windowed callsites, and structured test feedback."""

from unittest.mock import MagicMock

from koyote.ai_planner import (
    AIPatchPlanner,
    bound_file_content,
)
from koyote.llm import LLMClient, LLMResponse
from koyote.test_feedback import extract_structured_test_feedback


def test_bound_file_content_small_file():
    content = "import os\nprint('hello')\n"
    text, truncated = bound_file_content(content, limit=1000)
    assert text == content
    assert truncated is False


def test_bound_file_content_windowed_preserves_headers_and_callsites():
    # Construct a large 500-line file
    lines = [f"# line {i}: setup boilerplate" for i in range(1, 501)]
    lines[2] = "import stripe"
    lines[3] = "from stripe import StripeClient"
    # Target callsite at line 350
    lines[349] = "client = StripeClient('sk_test')"
    lines[350] = "charge = client.charges.create(amount=1000)"
    full_content = "\n".join(lines) + "\n"

    # Call with callsite at line 351 and small limit
    text, truncated = bound_file_content(
        full_content, limit=6000, callsite_lines=[351], context_radius=10
    )

    assert truncated is True
    assert len(text) <= 6000
    # Must preserve imports / header
    assert "import stripe" in text
    assert "from stripe import StripeClient" in text
    # Must preserve the callsite despite being past line 300
    assert "client.charges.create" in text
    # Must include gap markers
    assert "omitted for brevity" in text


def test_extract_structured_test_feedback_python():
    sample_pytest_output = """
============================= FAILURES =============================
___________________________ test_charge ____________________________

    def test_charge():
>       charge = client.charges.create(amount=100)
E       AttributeError: module 'stripe' has no attribute 'charges'

tests/test_billing.py:14: AttributeError
===================== short test summary info ======================
FAILED tests/test_billing.py::test_charge - AttributeError: module 'stripe' has no attribute 'charges'
1 failed in 0.05s
"""
    diag = extract_structured_test_feedback(sample_pytest_output, exit_code=1)
    assert any("test_billing.py" in f for f in diag.failing_files)
    assert "AttributeError" in diag.error_types
    assert "AttributeError: module 'stripe' has no attribute 'charges'" in diag.primary_message
    formatted = diag.format_for_model()
    assert "--- STRUCTURED VERIFICATION / TEST FAILURE ---" in formatted
    assert "AttributeError" in formatted
    assert "test_billing.py" in formatted


def test_extract_structured_test_feedback_typescript():
    sample_tsc_output = """
src/billing/client.ts(42,15): error TS2339: Property 'charges' does not exist on type 'Stripe'.
src/billing/client.ts(88,9): error TS2322: Type 'string' is not assignable to type 'number'.
"""
    diag = extract_structured_test_feedback(sample_tsc_output, exit_code=2)
    assert any("client.ts" in f for f in diag.failing_files)
    assert "TS2339" in diag.error_types or "TS2322" in diag.error_types
    assert "TS2339" in diag.primary_message or "TS2322" in diag.primary_message
    formatted = diag.format_for_model()
    assert "Failing Target(s):" in formatted
    assert "client.ts" in formatted


def test_multi_file_plan_coordination(tmp_path):
    f1 = tmp_path / "client.py"
    f2 = tmp_path / "test_client.py"
    f1.write_text("import stripe\ncharge = stripe.Charge.create()\n", encoding="utf-8")
    f2.write_text("from client import charge\nassert charge\n", encoding="utf-8")

    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(content="no-op", model="test")

    planner = AIPatchPlanner(client=mock_client)
    planner.plan_and_apply(
        repo_dir=str(tmp_path),
        affected_files=["client.py", "test_client.py"],
        provider_name="stripe",
        from_version="11.0.0",
        to_version="13.0.0",
    )

    # Check that mock_client was called with multi_file_plan context
    call_args = mock_client.complete.call_args_list
    assert len(call_args) >= 1
    prompt_text = call_args[0][1]["messages"][0]["content"]
    assert "Multi-file coordinated migration across 2 files" in prompt_text


def test_authoritative_migration_material_in_prompt(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("import openai\n", encoding="utf-8")

    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(content="no-op", model="test")

    context = {
        "migration_guide_content": "Replace openai.ChatCompletion with client.chat.completions.create",
        "openapi_diff": "- /v1/completions removed in favor of /v1/chat/completions",
    }

    planner = AIPatchPlanner(client=mock_client)
    planner.plan_and_apply(
        repo_dir=str(tmp_path),
        affected_files=["app.py"],
        provider_name="openai",
        from_version="0.28.0",
        to_version="1.0.0",
        context=context,
    )

    prompt_text = mock_client.complete.call_args[1]["messages"][0]["content"]
    assert "Authoritative Vendor Migration Guide:" in prompt_text
    assert "Replace openai.ChatCompletion" in prompt_text
    assert "OpenAPI Schema Breaking Changes:" in prompt_text
