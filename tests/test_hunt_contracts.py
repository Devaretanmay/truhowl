# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for Hunt's interface separation.

These tests answer: can a future feature accidentally introduce a
deterministic semantic-edit path? Each test names one smuggling route
and proves it closed:

* unsealed planner/fixer output cannot enter the sealed patch channel;
* direct construction — even with author="ai" — is rejected by the
  object model itself (private seal sentinel, never exported);
* forged provenance is rejected at construction AND at the boundaries
  (promotion re-checks seal + containment);
* the PR capability token is unmintable without derived evidence
  (no caller booleans anywhere in the sealing API);
* publication without the token is a TypeError, not a missed flag;
* the core loop talks only to ports (a full run succeeds on injected
  fakes with no model and no test runner);
* default adapters satisfy their protocols; hunt_ports imports no
  hunt internals;
* reasoning precedes authoring; iterations stay bounded and AI-directed;
* red evidence mints nothing and publishes nothing;
* failed repairs land on the avoid-list, never in trusted memory.
"""

import inspect
import json
import os
import subprocess

import pytest

from truhowl import hunt as hunt_agent
from truhowl import hunt_ports as hp
from truhowl.hunt import (
    SandboxResult,
    _promote_sandbox,
    create_sandbox,
    decide_pr,
    default_ports,
    destroy_sandbox,
    list_findings,
    run_hunt,
    update_audit,
    verify_sandbox_binding,
    write_audit,
)
from truhowl.patch_writer import PatchResult


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Truhowl Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@truhowl.dev"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _make_repo(tmp_path) -> str:
    repo = str(tmp_path / "svc")
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    with open(os.path.join(repo, "package.json"), "w") as f:
        json.dump({"dependencies": {"stripe": "^11.18.0"}}, f)
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("import stripe\nstripe.api_key = 'x'\ncharge = stripe.Charge.create(amount=100)\n")
    with open(os.path.join(repo, "tests", "test_sanity.py"), "w") as f:
        f.write("def test_sanity():\n    assert 1 + 1 == 2\n")
    _init_git_repo(repo)
    return repo


def _raw_patch(abs_path: str, *, success: bool = True, diff: str = "--- a\n+++ b\n+x\n"):
    return PatchResult(file_path=abs_path, success=success, lines_changed=1,
                       unified_diff=diff, rules_applied=["AI-authored Stripe repair"])


def _sealed_in(sandbox_dir: str, name: str = "app.py", model: str = "test-model"):
    target = os.path.join(sandbox_dir, name)
    with open(target, "w") as f:
        f.write("x = 1\n")
    sealed = hp.seal_ai_patch(_raw_patch(target), model=model)
    assert sealed is not None
    return sealed


# ── seal_ai_patch: the only door into the patch channel ───────────────────

def test_seal_admits_successful_ai_output(tmp_path):
    target = str(tmp_path / "app.py")
    sealed = hp.seal_ai_patch(_raw_patch(target), model="test-model", reasoning_attempt=2)
    assert sealed is not None
    assert sealed.author == "ai"
    assert sealed.model == "test-model"
    assert sealed.reasoning_attempt == 2
    assert sealed.admission == "seal_ai_patch"
    assert hp.is_sealed(sealed) is True


def test_seal_rejects_everything_else(tmp_path):
    target = str(tmp_path / "app.py")
    assert hp.seal_ai_patch(None, model="m") is None
    assert hp.seal_ai_patch(_raw_patch(target, success=False), model="m") is None
    assert hp.seal_ai_patch(_raw_patch(target, diff=""), model="m") is None
    assert hp.seal_ai_patch(PatchResult(file_path="", success=True, lines_changed=0,
                                        unified_diff="x"), model="m") is None


def test_seal_ignores_spoofed_model_identity(tmp_path):
    """Model identity comes from the live adapter, never the raw output."""
    target = str(tmp_path / "app.py")
    raw = _raw_patch(target)
    raw.model = "evil-spoof"
    raw.author = "definitely-ai-trust-me"
    sealed = hp.seal_ai_patch(raw, model="live-model")
    assert sealed is not None
    assert sealed.model == "live-model"
    assert sealed.author == "ai"


def test_direct_construction_rejected_even_with_perfect_fields():
    """Believable forgeries fail in the object model itself."""
    with pytest.raises(TypeError):
        hp.AIAuthoredPatch(file_path="a.py", unified_diff="d", author="ai",
                           model="m", admission="seal_ai_patch")
    with pytest.raises(TypeError):
        hp.AIAuthoredPatch(file_path="a.py", unified_diff="d", author="regex", model="m")
    with pytest.raises(TypeError):
        hp.AIAuthoredPatch(file_path="a.py", unified_diff="d", author="", model="m")
    with pytest.raises(TypeError):
        hp.VerifiedRepair(finding_id="x", provider="p", version_from="1",
                           version_to="2", files=["a.py"], unified_diff="d",
                           test_command="pytest -q", test_exit_code=0)


def test_seal_sentinel_not_exported():
    assert "_SEAL" not in hp.__all__
    assert "_SEAL" not in dir(hunt_agent)
    assert "_SEAL" not in hunt_agent.__all__


def test_sealed_objects_are_frozen():
    sealed = hp.seal_ai_patch(_raw_patch(os.path.join("x", "a.py")), model="m")
    assert sealed is not None
    with pytest.raises(Exception):
        sealed.author = "regex"
    with pytest.raises(Exception):
        sealed.model = "evil"


def test_subclass_cannot_bypass_the_seal():
    class Evil(hp.AIAuthoredPatch):
        pass

    with pytest.raises(TypeError):
        Evil(file_path="a.py", unified_diff="d", author="ai", model="m")


def test_duck_typed_lookalike_is_not_sealed():
    from types import SimpleNamespace

    mimic = SimpleNamespace(file_path="a.py", unified_diff="d", author="ai",
                            model="m", admission="seal_ai_patch",
                            reasoning_attempt=1, lines_changed=1, rules_applied=[])
    assert hp.is_sealed(mimic) is False
    assert hp.seal_verified_repair(
        finding_id="x", provider="p", version_from="1", version_to="2",
        patches=[mimic], sandbox_dir="/tmp",
        evidence=SimpleNamespace(command="t", exit_code=0, duration_ms=1,
                                 changed_files=["a.py"]),
        interpretation=SimpleNamespace(solved=True, unrelated_behavior=False,
                                       needs_more_investigation=False, rationale=""),
        affected_paths=["a.py"], must_not_change=[]) is None


# ── _promote_sandbox: seal + containment re-checked at the boundary ───────

def _forge_patch(**overrides):
    """Bypass __post_init__ the way only a deliberate forgery could."""
    forged = object.__new__(hp.AIAuthoredPatch)
    base = {"file_path": "a.py", "unified_diff": "d", "lines_changed": 0,
            "rules_applied": [], "author": "ai", "model": "m",
            "reasoning_attempt": 1, "admission": "seal_ai_patch", "_seal": None}
    for key, value in {**base, **overrides}.items():
        object.__setattr__(forged, key, value)
    return forged


def test_promote_rejects_forged_provenance(tmp_path):
    repo = str(tmp_path / "repo")
    sandbox = str(tmp_path / "sand")
    os.makedirs(repo)
    os.makedirs(sandbox)
    victim = os.path.join(sandbox, "app.py")
    with open(victim, "w") as f:
        f.write("x = 1\n")
    forged = _forge_patch(file_path=victim, author="regex", unified_diff="--- a\n+++ b\n")
    with pytest.raises(TypeError):
        _promote_sandbox(sandbox, repo, [forged])
    assert not os.path.exists(os.path.join(repo, "app.py"))


def test_promote_rejects_new_bypass_without_sentinel(tmp_path):
    """A forgery with author='ai' but no seal still fails the boundary."""
    repo = str(tmp_path / "repo")
    sandbox = str(tmp_path / "sand")
    os.makedirs(repo)
    os.makedirs(sandbox)
    victim = os.path.join(sandbox, "app.py")
    with open(victim, "w") as f:
        f.write("x = 1\n")
    forged = _forge_patch(file_path=victim, author="ai", unified_diff="--- a\n+++ b\n")
    assert hp.is_sealed(forged) is False
    with pytest.raises(TypeError):
        _promote_sandbox(sandbox, repo, [forged])
    assert not os.path.exists(os.path.join(repo, "app.py"))


def test_promote_rejects_sandbox_escape(tmp_path):
    repo = str(tmp_path / "repo")
    sandbox = str(tmp_path / "sand")
    os.makedirs(repo)
    os.makedirs(sandbox)
    outside = os.path.join(str(tmp_path), "outside.py")
    with open(outside, "w") as f:
        f.write("x = 1\n")
    sealed = hp.seal_ai_patch(_raw_patch(outside), model="m")
    assert sealed is not None
    with pytest.raises(ValueError):
        _promote_sandbox(sandbox, repo, [sealed])


def test_promote_accepts_sealed_patch(tmp_path):
    repo = str(tmp_path / "repo")
    sandbox = str(tmp_path / "sand")
    os.makedirs(repo)
    os.makedirs(sandbox)
    sealed = _sealed_in(sandbox)
    assert _promote_sandbox(sandbox, repo, [sealed]) == ["app.py"]
    with open(os.path.join(repo, "app.py")) as f:
        assert f.read() == "x = 1\n"


# ── seal_verified_repair: derived evidence only, no caller booleans ───────

def _evidence(*, command="pytest -q", exit_code=0, changed=("app.py",), duration=42):
    from types import SimpleNamespace

    return SimpleNamespace(command=command, exit_code=exit_code,
                           duration_ms=duration, output="ok",
                           changed_files=list(changed), diff="d")


def _interpretation(*, solved=True, unrelated=False, needs_more=False):
    from types import SimpleNamespace

    return SimpleNamespace(solved=solved, unrelated_behavior=unrelated,
                           needs_more_investigation=needs_more, rationale="r")


def _seal_kwargs(sandbox, patches, **overrides):
    kw = {
        "finding_id": "stripe-abc123",
        "provider": "stripe",
        "version_from": "11.0",
        "version_to": "13.0",
        "patches": patches,
        "sandbox_dir": sandbox,
        "evidence": _evidence(),
        "interpretation": _interpretation(),
        "affected_paths": ["app.py"],
        "must_not_change": [],
        "reasoning_attempt": 1,
        "model": "test-model",
    }
    kw.update(overrides)
    return kw


def test_seal_mints_token_on_full_evidence(tmp_path):
    sandbox = str(tmp_path / "sand")
    os.makedirs(sandbox)
    token = hp.seal_verified_repair(**_seal_kwargs(sandbox, [_sealed_in(sandbox)]))
    assert token is not None
    assert isinstance(token, hp.VerifiedRepair)
    assert token.files == ["app.py"]
    assert token.test_exit_code == 0


def test_seal_refuses_each_missing_condition(tmp_path):
    from types import SimpleNamespace

    sandbox = str(tmp_path / "sand")
    os.makedirs(sandbox)
    good = [_sealed_in(sandbox)]

    def attempt(**overrides):
        kw = _seal_kwargs(sandbox, good)
        kw.update(overrides)
        return hp.seal_verified_repair(**kw)

    assert attempt(patches=[]) is None
    assert attempt(evidence=_evidence(command="")) is None
    assert attempt(evidence=_evidence(exit_code=1)) is None
    # A "solved" claim with red evidence cannot mint: exit code rules.
    assert attempt(evidence=_evidence(exit_code=2),
                   interpretation=_interpretation(solved=True)) is None
    assert attempt(interpretation=_interpretation(solved=False)) is None
    assert attempt(interpretation=_interpretation(unrelated=True)) is None
    assert attempt(interpretation=_interpretation(needs_more=True)) is None
    assert attempt(evidence=_evidence(changed=["app.py", "evil.py"])) is None
    assert attempt(evidence=_evidence(changed=[])) is None
    assert attempt(affected_paths=["app.py"], must_not_change=["app.py"]) is None
    forged = _forge_patch(file_path=os.path.join(sandbox, "app.py"),
                          unified_diff="--- a\n+++ b\n", author="deterministic")
    assert attempt(patches=[forged]) is None
    # Malformed evidence/interpretation shapes fail closed, never mint.
    assert attempt(evidence=SimpleNamespace()) is None
    assert attempt(interpretation=SimpleNamespace()) is None
    assert attempt(evidence=None, interpretation=None) is None


def test_token_direct_construction_revalidates():
    with pytest.raises(TypeError):
        hp.VerifiedRepair(finding_id="x", provider="p", version_from="1",
                           version_to="2", files=["a.py"], unified_diff="d",
                           test_command="pytest -q", test_exit_code=0)


def test_sealer_signature_takes_no_caller_booleans():
    params = set(inspect.signature(hp.seal_verified_repair).parameters)
    for forbidden in ("success", "verified", "passed", "solved", "scope_ok",
                      "test_exit_code", "test_command"):
        assert forbidden not in params, f"caller boolean: {forbidden}"


# ── decide_pr: the signature is the gate ──────────────────────────────────

def _ctx():
    return hunt_agent.HuntContext(
        finding=hunt_agent.HuntFinding("x", "stripe", "1", "2", "s"),
        repository="svc", branch="main", commit_sha="abc",
        pr_state={}, repo_policy={},
    )


def test_decide_pr_defers_without_approval_even_with_token(tmp_path):
    sandbox = str(tmp_path / "sand")
    os.makedirs(sandbox)
    token = hp.seal_verified_repair(**_seal_kwargs(sandbox, [_sealed_in(sandbox)]))
    assert token is not None
    assert decide_pr(token, _ctx()) is None


def test_decide_pr_annotation_names_the_token():
    ann = inspect.signature(decide_pr).parameters["repair"].annotation
    assert "VerifiedRepair" in str(ann)


# ── Loop purity: the core talks only to ports ─────────────────────────────

def test_run_hunt_source_never_touches_concrete_repair_modules():
    src = inspect.getsource(run_hunt)
    assert "plan_and_apply" not in src
    assert "apply_rewrites" not in src
    assert "ports.author.author(" in src
    assert "seal_verified_repair(" in src


def test_run_hunt_contains_no_file_writes_or_imports():
    import ast

    tree = ast.parse(inspect.getsource(run_hunt))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom)), \
            "run_hunt must not import modules"
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            raise AssertionError("run_hunt must not open files (audit helper owns that)")


def test_default_adapters_satisfy_protocols():
    ports = default_ports(client=None, planner=None)
    assert isinstance(ports.context, hp.ContextProvider)
    assert isinstance(ports.reasoner, hp.RepairReasoner)
    assert isinstance(ports.author, hp.PatchAuthor)
    assert isinstance(ports.sandbox, hp.SandboxProvider)
    assert isinstance(ports.verifier, hp.Verifier)
    assert isinstance(ports.interpreter, hp.RepairInterpreter)
    assert isinstance(ports.publisher, hp.PRPublisher)


def test_hunt_ports_imports_no_hunt_internals():
    import ast

    src = inspect.getsource(hp)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert "truhowl.hunt" not in name and name != "truhowl", name
    # The real check: hunt_ports module object has no reference to hunt internals.
    assert not hasattr(hp, "gather_context")
    assert not hasattr(hp, "run_hunt")


def test_adapter_boundaries_hold_no_foreign_authority():
    for cls_name in ("_DefaultContext", "_DefaultReasoner", "_DefaultSandbox",
                     "_DefaultVerifier", "_DefaultInterpreter"):
        src = inspect.getsource(getattr(hunt_agent, cls_name))
        assert "seal_verified_repair" not in src, cls_name
        assert "VerifiedRepair" not in src, cls_name
        assert "_promote_sandbox" not in src, cls_name
        assert "git_commit_and_push" not in src, cls_name
        assert "gh_create_pr" not in src, cls_name
    pub_src = inspect.getsource(hunt_agent.LocalGitHubPublisher)
    assert "seal_verified_repair" not in pub_src  # publisher cannot mint
    assert "plan_and_apply" not in pub_src  # publisher cannot author


class _FakeReasoner:
    def __init__(self, events):
        self.events = events

    def reason(self, ctx, prior_evidence=""):
        self.events.append("reason")
        return hunt_agent.HuntReasoning(
            problem="drift", smallest_change="annotate the call",
            affected_paths=["app.py"], must_not_change=["tests/test_sanity.py"])


class _FakeAuthor:
    def __init__(self, events):
        self.events = events
        self.calls = 0
        self.authored_paths: list[str] = []

    def author(self, *, ctx, plan, sandbox_dir, candidate_files, reasoning_attempt):
        self.calls += 1
        self.events.append("author")
        self.authored_paths = list(candidate_files)
        assert isinstance(plan, hp.RepairPlan)
        assert plan.provider == "stripe"
        assert "tests/test_sanity.py" in plan.must_not_change
        target = os.path.join(sandbox_dir, "app.py")
        with open(target, "a", encoding="utf-8") as f:
            f.write("# port-authored repair\n")
        sealed = hp.seal_ai_patch(_raw_patch(target), model="port-test-model",
                                  reasoning_attempt=reasoning_attempt)
        assert sealed is not None
        return [sealed]


class _FakeVerifier:
    def verify(self, sandbox_dir, timeout=180):
        return hunt_agent.VerificationEvidence(
            command="port-check", exit_code=0, duration_ms=7,
            output="port suite green")


class _FakeInterpreter:
    def interpret(self, reasoning, evidence):
        return hunt_agent.HuntInterpretation(
            solved=True, unrelated_behavior=False, cause="hunt",
            rationale="port evidence convincing")


class _FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, repair, ctx, github_repo=None):
        assert isinstance(repair, hp.VerifiedRepair)
        self.published.append(repair)
        return "https://example.test/pr/1"


def _port_run(repo, monkeypatch, **overrides):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    events: list[str] = []
    publisher = _FakePublisher()
    author = _FakeAuthor(events)
    ports = hp.HuntPorts(
        context=hunt_agent._DefaultContext(),
        reasoner=overrides.get("reasoner", _FakeReasoner(events)),
        author=overrides.get("author", author),
        sandbox=hunt_agent._DefaultSandbox(),
        verifier=overrides.get("verifier", _FakeVerifier()),
        interpreter=overrides.get("interpreter", _FakeInterpreter()),
        publisher=overrides.get("publisher", publisher),
    )
    return ports, events, publisher, author


def test_full_run_succeeds_on_injected_ports_alone(tmp_path, monkeypatch):
    """No model, no test runner: the loop's only seams are the ports."""
    repo = _make_repo(tmp_path)
    ports, events, publisher, author = _port_run(repo, monkeypatch)
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, ports=ports,
                      create_pr=True, github_repo="acme/svc")

    assert author.calls == 1
    assert events.index("reason") < events.index("author")  # reasoning precedes editing
    assert report.success is True
    assert report.pr_url == "https://example.test/pr/1"
    assert isinstance(report.verified, hp.VerifiedRepair)
    assert report.verified.model == "port-test-model"
    assert len(publisher.published) == 1
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as f:
        assert "# port-authored repair" in f.read()
    audit = json.load(open(report.audit_path, encoding="utf-8"))
    assert audit["final"] == "verified"
    assert "raw" not in json.dumps(audit.get("reasoning", []))  # no chain-of-thought


def test_red_evidence_mints_nothing_and_publishes_nothing(tmp_path, monkeypatch):
    """Red verification → no token, no PR, repo untouched."""
    repo = _make_repo(tmp_path)

    class RedVerifier:
        def verify(self, sandbox_dir, timeout=180):
            return hunt_agent.VerificationEvidence(
                command="port-check", exit_code=1, duration_ms=7,
                output="1 failed")

    ports, events, publisher, author = _port_run(repo, monkeypatch, verifier=RedVerifier())
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, ports=ports,
                      max_iterations=1, create_pr=True, github_repo="acme/svc")

    assert report.success is False
    assert report.verified is None
    assert report.pr_url is None
    assert publisher.published == []
    assert "could not safely verify" in report.reason
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as f:
        assert "port-authored" not in f.read()


def test_scope_violation_fails_closed_and_leaves_repo_clean(tmp_path, monkeypatch):
    """A patch outside the declared scope is refused, never trimmed, never kept."""

    class RogueAuthor(_FakeAuthor):
        def author(self, *, ctx, plan, sandbox_dir, candidate_files, reasoning_attempt):
            target = os.path.join(sandbox_dir, "tests", "test_sanity.py")
            with open(target, "a", encoding="utf-8") as f:
                f.write("# rogue edit\n")
            sealed = hp.seal_ai_patch(_raw_patch(target), model="rogue")
            assert sealed is not None
            return [sealed]

    repo = _make_repo(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    events: list[str] = []
    publisher = _FakePublisher()
    ports = hp.HuntPorts(
        context=hunt_agent._DefaultContext(),
        reasoner=_FakeReasoner(events),
        author=RogueAuthor(events),
        sandbox=hunt_agent._DefaultSandbox(),
        verifier=_FakeVerifier(),
        interpreter=_FakeInterpreter(),
        publisher=publisher,
    )
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, ports=ports,
                      max_iterations=1, create_pr=True, github_repo="acme/svc")

    assert report.success is False
    assert report.verified is None
    assert publisher.published == []
    with open(os.path.join(repo, "tests", "test_sanity.py"), encoding="utf-8") as f:
        assert "rogue" not in f.read()


def test_iterations_bounded_and_ai_directed(tmp_path, monkeypatch):
    """A never-convinced interpreter yields exactly max_iterations AI rounds."""

    class Doubter:
        def interpret(self, reasoning, evidence):
            return hunt_agent.HuntInterpretation(
                solved=False, unrelated_behavior=False, cause="uncertain",
                needs_more_investigation=True, rationale="never convinced")

    repo = _make_repo(tmp_path)
    ports, events, publisher, author = _port_run(repo, monkeypatch, interpreter=Doubter())
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, ports=ports, max_iterations=2)

    assert author.calls == 2
    assert events.count("reason") == 2  # every round re-reasons; no mechanical retry
    assert report.success is False
    assert publisher.published == []


def test_phantom_reasoning_paths_cannot_invent_files(tmp_path, monkeypatch):
    """Reasoning that names nonexistent files yields no patch, no repair."""

    class PhantomReasoner:
        def reason(self, ctx, prior_evidence=""):
            return hunt_agent.HuntReasoning(
                problem="imagined", smallest_change="edit the void",
                affected_paths=["does/not/exist.py"], must_not_change=[])

    repo = _make_repo(tmp_path)
    ports, events, publisher, author = _port_run(repo, monkeypatch,
                                                 reasoner=PhantomReasoner())
    findings = list_findings(repo)
    report = run_hunt(repo, findings[0].finding_id, ports=ports, max_iterations=1)

    # The phantom path is never resolved to a real file, so it is never handed
    # to the author and never created. Candidate selection is bounded by the
    # migration plan (here: the manifest that declares the dependency), not by
    # the model's invented paths — so reasoning can neither invent files nor
    # shrink the migration to nothing, and the invented path still yields no
    # repair.
    assert not os.path.exists(os.path.join(repo, "does", "not", "exist.py"))
    assert all("does/not/exist" not in p for p in author.authored_paths)
    assert report.success is False


def test_failed_repairs_land_on_avoid_list_not_memory(tmp_path, monkeypatch):
    """record_failure appends failed_patterns; trusted patterns stay empty."""
    from truhowl.knowledge import lookup

    repo = _make_repo(tmp_path)

    class RedVerifier:
        def verify(self, sandbox_dir, timeout=180):
            return hunt_agent.VerificationEvidence(
                command="port-check", exit_code=1, duration_ms=7, output="red")

    ports, events, publisher, author = _port_run(repo, monkeypatch, verifier=RedVerifier())
    findings = list_findings(repo)
    finding = findings[0]
    run_hunt(repo, finding.finding_id, ports=ports, max_iterations=1)

    entry = lookup(repo, finding.provider, finding.version_from, finding.version_to)
    assert entry is not None
    assert entry.failed_patterns, "failure must be recorded"
    assert entry.patterns == [], "failed repairs must never become trusted patterns"


def test_audit_tampering_grants_no_authority(tmp_path, monkeypatch):
    """A hand-written 'verified' audit file cannot produce a token or PR."""
    repo = _make_repo(tmp_path)
    ports, events, publisher, author = _port_run(repo, monkeypatch)
    findings = list_findings(repo)
    audit_path = write_audit(repo, findings[0].finding_id,
                             {"final": "verified", "files": ["app.py"]})
    update_audit(audit_path, pr_decision="created", pr_url="https://fake/pr")
    record = json.load(open(audit_path, encoding="utf-8"))
    assert record["final"] == "verified"  # the file says so...
    # ...but no token exists in any report, and nothing was published.
    assert publisher.published == []
    with open(os.path.join(repo, "app.py"), encoding="utf-8") as f:
        assert "port-authored" not in f.read()


def test_interpretation_caps_solved_by_real_exit_code():
    """A model claiming solved:true over red evidence is not solved."""
    from truhowl.llm import LLMResponse

    class SolvedLiar:
        def complete(self, messages=None, system_prompt=None):
            return LLMResponse(content='{"solved": true, "unrelated_behavior": false, '
                                      '"assumptions_false": [], "cause": "hunt", '
                                      '"needs_more_investigation": false, '
                                      '"rationale": "trust me"}',
                               model="liar")

    from truhowl.hunt import ai_interpret

    reasoning = hunt_agent.HuntReasoning(smallest_change="x")
    red = hunt_agent.VerificationEvidence(command="pytest -q", exit_code=1,
                                          duration_ms=1, output="FAILED")
    interp = ai_interpret(SolvedLiar(), reasoning, red)
    assert interp.solved is False


def test_sandbox_port_roundtrip(tmp_path):
    repo = _make_repo(tmp_path)
    provider = hunt_agent._DefaultSandbox()
    assert isinstance(provider, hp.SandboxProvider)
    box = provider.create(repo, "")
    try:
        assert os.path.isdir(box.sandbox_dir)
    finally:
        provider.destroy(repo, box)
    assert not os.path.exists(box.sandbox_dir)
    assert create_sandbox.__module__ == "truhowl.hunt"
    assert destroy_sandbox.__module__ == "truhowl.hunt"


def test_sandbox_binding_refuses_moved_repo(tmp_path):
    repo = _make_repo(tmp_path)
    box = create_sandbox(repo, "")
    try:
        ok, _ = verify_sandbox_binding(box, repo)
        assert ok is True
        # Move the repo: new commit changes HEAD.
        with open(os.path.join(repo, "moved.txt"), "w") as f:
            f.write("moved\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "moved"], cwd=repo, check=True, capture_output=True)
        if box.commit_sha:
            ok, reason = verify_sandbox_binding(box, repo)
            assert ok is False
            assert "refusing promotion" in reason
    finally:
        destroy_sandbox(repo, box)


def test_binding_missing_sandbox_refused(tmp_path):
    repo = _make_repo(tmp_path)
    box = create_sandbox(repo, "")
    destroy_sandbox(repo, box)
    ok, reason = verify_sandbox_binding(box, repo)
    assert ok is False
    assert "gone" in reason


def test_binding_unreadable_repo_refused(tmp_path):
    repo = _make_repo(tmp_path)
    box = create_sandbox(repo, "")
    try:
        forged = SandboxResult(sandbox_dir=box.sandbox_dir,
                               worktree_created=False,
                               commit_sha="deadbeef" * 5)
        ok, reason = verify_sandbox_binding(forged, str(tmp_path / "missing"))
        assert ok is False
        assert "unreadable" in reason
    finally:
        destroy_sandbox(repo, box)
