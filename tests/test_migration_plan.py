# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Migration intelligence: plan completeness, residual drift, knowledge provenance.

Invariants under test:

1. A candidate whose changed files do not parse is never verified (syntax gate).
2. A candidate that leaves a migration unit half-done is never sealed, and the
   residual report names exactly what is left.
3. Residual drift never fires on untouched files, on string-literal data (test
   harnesses comparing source text), or on symbols the plan never observed — a
   completeness gate that refuses correct repairs is worse than useless.
4. Scope is the planned file set, and a file outside both the plan and the
   model's declaration still fails the scope check.
5. Migration knowledge records where each claim came from, and says so when the
   only evidence is a development fixture rather than a vendor document.
"""

import json
import os
import subprocess

from truhowl import hunt as hunt_agent
from truhowl.hunt import run_verification
from truhowl.migration_knowledge import (
    DEVELOPMENT_FIXTURE_CONTRACT,
    OFFICIAL_MIGRATION_GUIDE,
    SOURCE_AUTHORITY,
    knowledge_for,
    source_authority_rank,
)
from truhowl.migration_plan import (
    NonDetectable,
    build_migration_plan,
    detection_from_change,
    residual_hits,
)
from truhowl.providers.registry import SymbolChange, get_default_registry
from truhowl.test_runner import check_candidate_syntax


# ── Helpers ───────────────────────────────────────────────────────────────

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "K"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "k@k.dev"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


_OPENAI_V3_CLIENT = """import { Configuration, OpenAIApi } from 'openai';

export class ChatOpenAI {
  private client: OpenAIApi;

  constructor(apiKey: string) {
    const config = new Configuration({ apiKey });
    this.client = new OpenAIApi(config);
  }

  async call(messages: Array<{ role: string; content: string }>) {
    const response = await this.client.createChatCompletion({
      model: 'gpt-3.5-turbo',
      messages,
    });
    return response.data.choices[0].message;
  }
}
"""

_OPENAI_V4_CLIENT = """import OpenAI from 'openai';

export class ChatOpenAI {
  private client: OpenAI;

  constructor(apiKey: string) {
    this.client = new OpenAI({ apiKey });
  }

  async call(messages: Array<{ role: string; content: string }>) {
    const response = await this.client.chat.completions.create({
      model: 'gpt-3.5-turbo',
      messages,
    });
    return response.choices[0].message;
  }
}
"""


def _openai_repo(tmp_path) -> str:
    repo = str(tmp_path / "svc")
    _write(os.path.join(repo, "package.json"),
           json.dumps({"name": "svc", "dependencies": {"openai": "^3.3.0"}}))
    _write(os.path.join(repo, "src", "client.ts"), _OPENAI_V3_CLIENT)
    _write(os.path.join(repo, "tsconfig.json"), json.dumps({"compilerOptions": {}}))
    _git_repo(repo)
    return repo


# ── 1. Syntax gate: unparseable candidates are never verified ─────────────

def test_syntax_gate_detects_python_syntax_error(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(os.path.join(repo, "tests"))
    _write(os.path.join(repo, "consumer.py"), "def broken(:\n")
    _write(os.path.join(repo, "tests", "test_api.py"), "def test_api():\n    assert True\n")

    code, command, output = check_candidate_syntax(repo, ["consumer.py"])
    assert code != 0
    assert command == "syntax-gate"
    assert "consumer.py" in output
    assert "SyntaxError" in output


def test_syntax_gate_accepts_parseable_files(tmp_path):
    repo = str(tmp_path / "r")
    _write(os.path.join(repo, "a.py"), "x = 1\n")
    _write(os.path.join(repo, "b.json"), '{"ok": true}\n')
    code, _cmd, output = check_candidate_syntax(repo, ["a.py", "b.json"])
    assert code == 0, output


def test_syntax_gate_ignores_non_source_and_vendored_paths(tmp_path):
    repo = str(tmp_path / "r")
    _write(os.path.join(repo, "notes.md"), "# not source\n")
    _write(os.path.join(repo, "node_modules", "dep", "broken.py"), "def broken(:\n")
    code, _cmd, _out = check_candidate_syntax(
        repo, ["notes.md", "node_modules/dep/broken.py"])
    assert code == 0


def test_syntax_gate_discovery_finds_untracked_files(tmp_path):
    """A newly added file with a syntax error must be caught by discovery."""
    repo = str(tmp_path / "r")
    _write(os.path.join(repo, "app.py"), "x = 1\n")
    _git_repo(repo)
    _write(os.path.join(repo, "fresh.py"), "def broken(:\n")

    code, _cmd, output = check_candidate_syntax(repo, None)
    assert code != 0
    assert "fresh.py" in output


def test_run_verification_refuses_unparseable_candidate_even_when_tests_pass(tmp_path, monkeypatch):
    """The repository's test command only proves what it loads.

    Here the test suite passes and the changed file is unparseable: verification
    must still refuse, otherwise an unparseable repair would be sealed.
    """
    repo = str(tmp_path / "r")
    os.makedirs(os.path.join(repo, "tests"))
    _write(os.path.join(repo, "consumer.py"), "def broken(:\n")
    _write(os.path.join(repo, "app.py"), "x = 1\n")
    _write(os.path.join(repo, "tests", "test_api.py"), "def test_api():\n    assert 1 + 1 == 2\n")
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    evidence = run_verification(repo, timeout=60, changed_files=["consumer.py"])
    assert evidence.exit_code != 0
    assert "SYNTAX GATE FAILED" in evidence.output
    assert evidence.build_ok is False


def test_run_verification_accepts_when_tests_pass_and_files_parse(tmp_path, monkeypatch):
    repo = str(tmp_path / "r")
    os.makedirs(os.path.join(repo, "tests"))
    _write(os.path.join(repo, "app.py"), "x = 1\n")
    _write(os.path.join(repo, "tests", "test_api.py"), "def test_api():\n    assert 1 + 1 == 2\n")
    monkeypatch.setattr(hunt_agent, "_run_install", lambda *a, **k: None)

    evidence = run_verification(repo, timeout=120, changed_files=["app.py"])
    assert evidence.exit_code == 0
    assert evidence.command == "pytest -q"


# ── 2. Detection derivation: only assert what is safe to assert ───────────

def test_call_shape_change_is_not_asserted():
    """``Stripe('key') -> new Stripe('key')`` keeps the identifier on purpose."""
    change = SymbolChange(old_symbol="Stripe('key')", new_symbol="new Stripe('key')",
                          change_type="replaced", unit="initialization")
    derived = detection_from_change(change)
    assert isinstance(derived, NonDetectable)
    assert "call-shape" in derived.reason


def test_generic_tokens_are_not_asserted():
    change = SymbolChange(old_symbol=".data", new_symbol="", change_type="removed",
                          unit="response_shape")
    derived = detection_from_change(change)
    assert isinstance(derived, NonDetectable)


def test_module_change_is_asserted_and_guarded():
    detectable = detection_from_change(SymbolChange(
        old_symbol="require('aws-sdk')", new_symbol="require('@aws-sdk/client-s3')",
        change_type="moved", unit="module_system"))
    assert not isinstance(detectable, NonDetectable)
    assert detectable.token == "aws-sdk" and detectable.mode == "module"

    guarded = detection_from_change(SymbolChange(
        old_symbol="require('stripe').default", new_symbol="require('stripe')",
        change_type="replaced", unit="module_system"))
    assert isinstance(guarded, NonDetectable)


def test_class_like_and_member_path_symbols_are_asserted():
    for old, new, token in [
        ("Configuration", "OpenAI", "Configuration"),
        ("OpenAIApi", "OpenAI", "OpenAIApi"),
        ("auth.user", "auth.getUser", "auth.user"),
        ("createChatCompletion", "chat.completions.create", "createChatCompletion"),
        ("getCurrentHub().getClient()", "getClient()", "getCurrentHub"),
    ]:
        derived = detection_from_change(SymbolChange(
            old_symbol=old, new_symbol=new, change_type="renamed", unit="method_call"))
        assert not isinstance(derived, NonDetectable), old
        assert derived.token == token, (old, derived.token)


# ── 3. Knowledge provenance ───────────────────────────────────────────────

def test_openai_knowledge_is_vendor_sourced_and_covers_all_units():
    knowledge = knowledge_for("openai", "^3.3.0", "4.0.0")
    assert knowledge is not None
    assert set(knowledge.units()) >= {"import", "initialization", "method_call", "response_shape"}
    kinds = {src.kind for src in knowledge.sources}
    assert OFFICIAL_MIGRATION_GUIDE in kinds
    assert all(src.url for src in knowledge.sources if src.kind == OFFICIAL_MIGRATION_GUIDE)


def test_fixture_contracts_are_labelled_not_dressed_up_as_vendor_guidance():
    knowledge = knowledge_for("stripe", "^11.18.0", "22.0.0")
    assert knowledge is not None
    fixture_claims = [c for c in knowledge.symbol_changes if c.old_symbol == "amount: amount"]
    assert fixture_claims, "the fixture string-amount contract should be recorded"
    assert any(src.kind == DEVELOPMENT_FIXTURE_CONTRACT for src in knowledge.sources)
    # The fixture contract must not cite a vendor URL as its authority.
    assert not fixture_claims[0].source_url


def test_authority_ladder_orders_vendor_docs_above_fixture_contracts():
    assert source_authority_rank(OFFICIAL_MIGRATION_GUIDE) < source_authority_rank(
        DEVELOPMENT_FIXTURE_CONTRACT)
    assert len(SOURCE_AUTHORITY) == len(set(SOURCE_AUTHORITY))


def test_registry_attaches_knowledge_to_every_development_case_provider():
    registry = get_default_registry()
    for provider, frm, to in [
        ("openai", "^3.3.0", "4.0.0"), ("stripe", "^11.18.0", "13.0.0"),
        ("stripe", "^11.18.0", "22.0.0"), ("supabase", "^1.35.0", "2.0.0"),
        ("clerk", "^4.29.0", "5.0.0"), ("sentry", "^7.114.0", "8.0.0"),
        ("aws-sdk", "^2.1400.0", "3.0.0"), ("octokit", "^16.43.0", "17.0.0"),
        ("anthropic", "^0.10.0", "0.5.0"), ("twilio", "^3.85.0", "5.0.0"),
    ]:
        knowledge = registry.migration_knowledge(provider, frm, to)
        assert knowledge is not None, (provider, frm, to)
        assert knowledge.symbol_changes, (provider, frm, to)


# ── 4. Plan construction ──────────────────────────────────────────────────

def test_plan_observes_multi_file_migration_units(tmp_path):
    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts", "package.json"])

    assert plan.completeness is not None and plan.completeness.complete
    units = {u.id for u in plan.units}
    assert {"import", "method_call", "dependency_metadata"} <= units
    assert "src/client.ts" in plan.files

    by_id = {u.id: u for u in plan.units}
    observed_tokens = {h.token for h in by_id["import"].observed}
    assert {"Configuration", "OpenAIApi"} <= observed_tokens
    assert "createChatCompletion" in {h.token for h in by_id["method_call"].observed}
    # dependency metadata is planned even though no callsite proves it
    assert by_id["dependency_metadata"].required_changes


def test_plan_records_verification_commands_from_the_repository(tmp_path):
    repo = _openai_repo(tmp_path)
    _write(os.path.join(repo, "package.json"), json.dumps({
        "name": "svc", "dependencies": {"openai": "^3.3.0"},
        "scripts": {"type-check": "tsc --noEmit", "test": "vitest run"},
    }))
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0")
    assert plan.verification["compile"] == "npm run type-check"
    assert plan.verification["behavioral"] == "npm test"


def test_plan_reports_no_knowledge_instead_of_inventing_it(tmp_path):
    repo = str(tmp_path / "unknown")
    _write(os.path.join(repo, "package.json"), json.dumps({"dependencies": {"leftpad": "^1.0.0"}}))
    plan = build_migration_plan(repo, "leftpad", "^1.0.0", "2.0.0")
    assert plan.knowledge_available is False
    # No symbol claims are invented: the only planned unit is the dependency
    # metadata move, which needs no vendor knowledge to be observed.
    assert [u.id for u in plan.units] == ["dependency_metadata"]
    assert plan.completeness is not None and plan.completeness.complete
    assert any("no authoritative symbol-level knowledge" in n for n in plan.completeness.notes)


# ── 5. Residual drift: the completeness gate ──────────────────────────────

def test_residual_blocks_a_partial_migration(tmp_path):
    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts"])
    assert residual_hits(repo, plan, ["src/client.ts"]), "baseline is un-migrated"

    # Import fixed, constructor/callsite/type left behind: the classic partial repair.
    _write(os.path.join(repo, "src", "client.ts"), _OPENAI_V3_CLIENT.replace(
        "import { Configuration, OpenAIApi } from 'openai';", "import OpenAI from 'openai';"))
    hits = residual_hits(repo, plan, ["src/client.ts"])
    assert hits, "a partial migration must be caught"
    assert {"OpenAIApi", "Configuration", "createChatCompletion"} <= {h.old_symbol for h in hits}


def test_residual_is_empty_for_a_complete_migration(tmp_path):
    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts"])
    _write(os.path.join(repo, "src", "client.ts"), _OPENAI_V4_CLIENT)
    assert residual_hits(repo, plan, ["src/client.ts"]) == []


def test_residual_ignores_string_literals_and_test_harness_data(tmp_path):
    """A harness that compares source text mentions old symbols as data."""
    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts"])
    _write(os.path.join(repo, "src", "client.ts"), _OPENAI_V4_CLIENT)
    harness = os.path.join(repo, "test", "run.js")
    _write(harness, "const src = read('src/client.ts');\n"
                    "if (src.includes('createChatCompletion')) { fail(); }\n")

    hits = residual_hits(repo, plan, ["src/client.ts", "test/run.js"])
    assert hits == [], f"string-literal mentions must not assert: {hits}"


def test_residual_ignores_untouched_files_and_unobserved_symbols(tmp_path):
    repo = _openai_repo(tmp_path)
    _write(os.path.join(repo, "src", "other.ts"), "const x = new Configuration({});\n")
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts", "src/other.ts"])
    # Only src/client.ts is the candidate's own change set.
    _write(os.path.join(repo, "src", "client.ts"), _OPENAI_V4_CLIENT)
    assert residual_hits(repo, plan, ["src/client.ts"]) == []


def test_residual_does_not_fire_when_plan_observed_nothing(tmp_path):
    repo = str(tmp_path / "clean")
    _write(os.path.join(repo, "package.json"), json.dumps({"dependencies": {"openai": "^3.3.0"}}))
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0")
    assert plan.observed == {}
    assert residual_hits(repo, plan, ["package.json"]) == []


# ── 6. Scope stays closed ─────────────────────────────────────────────────

def test_scope_still_refuses_files_outside_plan_and_declaration():
    from truhowl.hunt_ports import evaluate_scope

    plan_paths = ["src/client.ts"]
    declared = ["src/client.ts"]
    scope = list(dict.fromkeys(declared + plan_paths))

    ok, _ = evaluate_scope(scope, [], ["src/client.ts"])
    assert ok
    ok, reason = evaluate_scope(scope, [], ["src/unrelated.ts"])
    assert not ok and "unrelated" in reason


def test_plan_render_names_units_files_and_invariants(tmp_path):
    from truhowl.migration_plan import render_plan_briefing

    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts", "package.json"])
    text = render_plan_briefing(plan)
    assert "MIGRATION PLAN" in text
    assert "createChatCompletion" in text
    assert "src/client.ts" in text
    assert "Invariants:" in text
    assert "@ts-ignore" in text


def test_residual_briefing_tells_the_model_to_restart_from_baseline(tmp_path):
    from truhowl.migration_plan import render_residual_briefing

    repo = _openai_repo(tmp_path)
    plan = build_migration_plan(repo, "openai", "^3.3.0", "4.0.0",
                                affected_files=["src/client.ts"])
    hits = residual_hits(repo, plan, ["src/client.ts"])
    text = render_residual_briefing(hits, plan)
    assert "ORIGINAL migration baseline" in text
    assert "COMPLETE replacement candidate" in text
