import json
import os
import sys
import tempfile

from truhowl.cli.main import main

OLD_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Payments API", "version": "2024-06-01"},
    "paths": {
        "/v1/charges": {
            "post": {
                "parameters": [
                    {"name": "amount", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "currency", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}

NEW_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Payments API", "version": "2026-02-15"},
    "paths": {
        "/v1/charges": {
            "post": {
                "parameters": [
                    {"name": "amount", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "currency", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def test_cli_diff_schema(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        old_file = os.path.join(tmpdir, "old.json")
        new_file = os.path.join(tmpdir, "new.json")
        with open(old_file, "w") as f:
            json.dump(OLD_SPEC, f)
        with open(new_file, "w") as f:
            json.dump(NEW_SPEC, f)

        sys.argv = ["truhowl", "diff-schema", old_file, new_file]
        main()
        captured = capsys.readouterr()
        assert "Schema Diff: Payments API" in captured.out
        assert "Breaking changes:" in captured.out


def test_cli_diff_schema_json(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        old_file = os.path.join(tmpdir, "old.json")
        new_file = os.path.join(tmpdir, "new.json")
        with open(old_file, "w") as f:
            json.dump(OLD_SPEC, f)
        with open(new_file, "w") as f:
            json.dump(NEW_SPEC, f)

        sys.argv = ["truhowl", "diff-schema", old_file, new_file, "--json"]
        main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["breaking_count"] >= 1


def test_cli_scan_api(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        src_file = os.path.join(tmpdir, "service.ts")
        with open(src_file, "w") as f:
            f.write("import Stripe from 'stripe';\nconst c = stripe.charges.create({ amount: 10 });\n")

        sys.argv = ["truhowl", "scan-api", "--root-dir", tmpdir, "--sdk", "stripe", "--method", "charges.create"]
        main()
        captured = capsys.readouterr()
        assert "API Callsite Scan:" in captured.out
        assert "service.ts" in captured.out


def test_cli_autopatch_generates_files(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        old_file = os.path.join(tmpdir, "old.json")
        new_file = os.path.join(tmpdir, "new.json")
        with open(old_file, "w") as f:
            json.dump(OLD_SPEC, f)
        with open(new_file, "w") as f:
            json.dump(NEW_SPEC, f)

        src_file = os.path.join(tmpdir, "app.ts")
        with open(src_file, "w") as f:
            f.write("import Stripe from 'stripe';\nconst c = stripe.charges.create({ amount: 10 });\n")

        out_dir = os.path.join(tmpdir, "autopatch_out")
        sys.argv = [
            "truhowl", "autopatch",
            "--old", old_file,
            "--new", new_file,
            "--root-dir", tmpdir,
            "--sdk", "stripe",
            "--method", "charges.create",
            "--lang", "typescript",
            "--out-dir", out_dir,
        ]
        main()
        captured = capsys.readouterr()
        assert "AutoPatch generated plan" in captured.out
        assert os.path.isfile(os.path.join(out_dir, "AUTOPATCH_REPORT.md"))
        assert os.path.isfile(os.path.join(out_dir, "test_contracts.ts"))


def test_cli_workflow_order(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        wf_file = os.path.join(tmpdir, "wf.json")
        wf = {
            "name": "maint-dag",
            "trigger": {"kind": "SchemaDrift"},
            "steps": [
                {"name": "b_patch", "kind": "Patch", "depends_on": ["a_scan"]},
                {"name": "a_scan", "kind": "ImpactAnalysis", "depends_on": []},
            ],
        }
        with open(wf_file, "w") as f:
            json.dump(wf, f)

        sys.argv = ["truhowl", "workflow-order", wf_file]
        main()
        captured = capsys.readouterr()
        assert "Execution Order" in captured.out
        assert "1. a_scan" in captured.out
        assert "2. b_patch" in captured.out


def test_cli_inventory(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        src_file = os.path.join(tmpdir, "app.ts")
        with open(src_file, "w") as f:
            f.write("import Stripe from 'stripe';\nconst c = stripe.charges.create({ amount: 10 });\n")

        sys.argv = ["truhowl", "inventory", "--root-dir", tmpdir]
        main()
        captured = capsys.readouterr()
        assert "External Dependency Inventory" in captured.out
        assert "Stripe" in captured.out





