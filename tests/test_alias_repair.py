# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Alias-aware repair: proven client aliases get precise rewrites, never loosened regex."""

import json
from unittest.mock import MagicMock

from truhowl.ai_planner import AIPatchPlanner
from truhowl.llm import LLMClient, LLMResponse
from truhowl.maintenance import run_maintenance_cycle
from truhowl.patch_writer import discover_aliases, instantiate_alias_rules
from truhowl.providers.registry import RewriteRule


def test_instantiate_alias_rules_exact_identifier_only():
    rules = [RewriteRule(pattern=r"stripe\.subscriptions\.del\(", replacement="stripe.subscriptions.cancel(",
                         file_extensions=[".ts"], description="del->cancel")]
    out = instantiate_alias_rules(rules, {"s": "stripe"})
    assert len(out) == 1
    assert out[0].pattern == r"s\.subscriptions\.del\("
    assert out[0].replacement == "s.subscriptions.cancel("
    assert "(alias s)" in out[0].description


def test_instantiate_alias_rules_preserves_non_rooted_replacement():
    rules = [RewriteRule(pattern=r"stripe\.x\(", replacement="make_x(",
                         file_extensions=[".ts"], description="d")]
    out = instantiate_alias_rules(rules, {"s": "stripe"})
    assert out[0].replacement == "make_x("


def test_instantiate_alias_rules_skips_non_identifiers_and_canonical():
    rules = [RewriteRule(pattern=r"stripe\.x\(", replacement="y(",
                         file_extensions=[".ts"], description="d")]
    assert instantiate_alias_rules(rules, {"stripe": "stripe"}) == []
    assert instantiate_alias_rules(rules, {"s-x": "stripe"}) == []
    assert instantiate_alias_rules(rules, {}) == []


def test_instantiate_alias_rules_skips_non_prefixed_patterns():
    rules = [RewriteRule(pattern=r"amount:\s*amount", replacement="amount: String(amount)",
                         file_extensions=[".ts"], description="d")]
    assert instantiate_alias_rules(rules, {"s": "stripe"}) == []


def test_discover_aliases_from_scan(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("import Stripe from 'stripe';\nconst s = new Stripe('x');\n"
                               "export const f = (id: string) => s.subscriptions.del(id);\n")
    with open(tmp_path / "package.json", "w") as f:
        f.write('{"dependencies": {"stripe": "^11.18.0"}}')
    assert discover_aliases(str(tmp_path), "stripe") == {"s": "stripe"}


def test_aliased_client_repaired_end_to_end(tmp_path, monkeypatch):
    """The exact shape that previously refused: const s = new Stripe() + s.subscriptions.del."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "test").mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "t", "dependencies": {"stripe": "^11.18.0"},
        "scripts": {"test": "node test/run.js"}}))
    (repo / "src" / "billing.ts").write_text(
        "import Stripe from 'stripe';\n"
        "const s = new Stripe(process.env.STRIPE_API_KEY || '');\n"
        "export const cancel = (id: string) => s.subscriptions.del(id);\n")
    (repo / "test" / "run.js").write_text(
        "const fs=require('fs'),p=require('path');"
        "const s=fs.readFileSync(p.join(__dirname,'../src/billing.ts'),'utf8');"
        "const ok=!s.includes('.del(')&&s.includes('.cancel(');"
        "console.log(ok?'PASS':'FAIL');process.exit(ok?0:1);\n")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    mock_client = MagicMock(spec=LLMClient)
    mock_client.complete.return_value = LLMResponse(
        content="<<<<<<< SEARCH\nexport const cancel = (id: string) => s.subscriptions.del(id);\n=======\nexport const cancel = (id: string) => s.subscriptions.cancel(id);\n>>>>>>> REPLACE",
        model="groq/llama-3.3-70b-versatile",
    )
    monkeypatch.setattr(
        "truhowl.maintenance.AIPatchPlanner.from_env",
        classmethod(lambda cls, **k: AIPatchPlanner(client=mock_client)),
    )
    report = run_maintenance_cycle(str(repo), "stripe", from_version="11.18.0", to_version="13.0.0")
    assert report.success, report.error
    assert report.repair_path == "ai-reasoning"
    content = (repo / "src" / "billing.ts").read_text()
    assert "s.subscriptions.cancel(id)" in content
    assert ".del(" not in content
