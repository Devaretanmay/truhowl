import json
import os
import tempfile

from truhowl import autopatch

OLD_SPEC = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Payments API", "version": "2024-06-01"},
    "paths": {
        "/v1/charges": {
            "post": {
                "parameters": [
                    {"name": "amount", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "currency", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "description", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "status": {"type": "string"},
                                        "amount": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        }
    },
})

NEW_SPEC = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Payments API", "version": "2026-02-15"},
    "paths": {
        "/v1/charges": {
            "post": {
                "parameters": [
                    {"name": "amount", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "currency", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "idempotency_key", "in": "header", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "status": {"type": "string"},
                                        "amount": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        }
    },
})


def test_diff_schemas_detects_breaking_changes():
    diff = autopatch.diff_schemas(OLD_SPEC, NEW_SPEC)
    assert diff["breaking_count"] >= 2
    assert diff["old_spec"]["version"] == "2024-06-01"
    assert diff["new_spec"]["version"] == "2026-02-15"
    assert len(diff["endpoint_changes"]) > 0


def test_scan_callsites_finds_matches():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, "billing.ts")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("import Stripe from 'stripe';\n")
            f.write("const charge = await stripe.charges.create({ amount: 100 });\n")

        cfg = autopatch.ScanConfig(
            sdk_names=["stripe"],
            method_patterns=["charges.create"],
            api_base_urls=["api.stripe.com"],
        )
        scan = autopatch.scan_callsites(tmpdir, cfg)
        assert scan["files_scanned"] == 1
        assert scan["files_with_hits"] == 1
        assert len(scan["callsites"]) >= 2


def test_generate_maintenance_plan_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, "billing.ts")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("import Stripe from 'stripe';\n")
            f.write("const charge = await stripe.charges.create({ amount: 100 });\n")

        cfg = autopatch.ScanConfig(
            sdk_names=["stripe"],
            method_patterns=["charges.create"],
        )
        plan = autopatch.generate_maintenance_plan(OLD_SPEC, NEW_SPEC, tmpdir, cfg)
        assert plan["status"] == "ActionRequired"
        assert plan["breaking_changes"] >= 2
        assert len(plan["patch_targets"]) > 0


def test_synthesize_contracts_typescript():
    specs = [{
        "endpoint": "/v1/charges",
        "method": "post",
        "fields_to_verify": ["parameters.amount", "response.amount"],
    }]
    ts_code = autopatch.synthesize_contracts("Payments", "2024-06-01", "2026-02-15", specs, language="typescript")
    assert "vitest" in ts_code or "describe" in ts_code
    assert "POST /v1/charges" in ts_code


def test_synthesize_contracts_python():
    specs = [{
        "endpoint": "/v1/charges",
        "method": "post",
        "fields_to_verify": ["parameters.amount", "response.amount"],
    }]
    py_code = autopatch.synthesize_contracts("Payments", "2024-06-01", "2026-02-15", specs, language="python")
    assert "pytest" in py_code
    assert "class Test" in py_code


def test_render_markdown_report():
    plan = {
        "status": "ActionRequired",
        "api_name": "Stripe API",
        "old_version": "2024-06-01",
        "new_version": "2026-02-15",
        "breaking_changes": 3,
        "total_affected_files": 2,
        "total_affected_callsites": 5,
        "impacted_endpoints": [],
        "patch_targets": [],
        "verification_specs": [],
    }
    md = autopatch.render_markdown_report(plan)
    assert "AutoPatch: Stripe API" in md
    assert "Breaking changes | 3" in md


def test_workflow_validate_and_order():
    wf = {
        "name": "api-upgrade",
        "trigger": {"kind": "SchemaDrift"},
        "steps": [
            {"name": "fetch", "kind": "SchemaRadar", "depends_on": []},
            {"name": "scan", "kind": "ImpactAnalysis", "depends_on": ["fetch"]},
            {"name": "patch", "kind": "Patch", "depends_on": ["scan"]},
        ],
    }
    errs = autopatch.validate_workflow(wf)
    assert len(errs) == 0

    order = autopatch.get_workflow_execution_order(wf)
    assert order == ["fetch", "scan", "patch"]





def test_no_deterministic_patcher_surface():
    """No AST patch engine may exist behind the SDK: repairs are AI-authored."""
    assert not hasattr(autopatch, "apply_patch")
    from truhowl import _core

    assert not hasattr(_core, "patch_apply")


def test_false_positive_regressions():
    """Verify that unrelated methods and imports are never flagged as affected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "checkout.ts")
        with open(test_file, "w") as f:
            f.write(
                "import Stripe from 'stripe';\n"
                "type StripeCharge = Stripe.Charge;\n"
                "const session = stripe.checkout.sessions.create({ line_items: [] });\n"
                "const portal = stripe.billingPortal.sessions.create({ customer: 'cus_123' });\n"
            )

        cfg = autopatch.ScanConfig(
            sdk_names=["stripe"],
            method_patterns=["charges.create", "checkout.sessions.create", "billingPortal.sessions.create"],
        )

        plan = autopatch.generate_maintenance_plan(OLD_SPEC, NEW_SPEC, tmpdir, cfg)

        # 1. checkout.sessions.create and billingPortal.sessions.create are NOT affected by /v1/charges
        assert plan["total_affected_callsites"] == 0
        assert len(plan["patch_targets"]) == 0
        assert plan["status"] == "NoImpact"

        # 2. Rendered trust report explicitly quarantines imports and rejects unaffected sibling callsites
        report = autopatch.render_trust_report(plan)
        assert "PROVABLY UNAFFECTED" in report or "No confirmed affected callsites" in report





