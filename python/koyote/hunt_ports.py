# Copyright 2026 Koyote Authors
# SPDX-License-Identifier: Apache-2.0
"""Hunt interface contracts: ports and provenance-sealed types.

This module is the structural answer to Hunt's central rule — AI owns
semantic reasoning and semantic code changes. Convention ("please don't
call the regex fixer here") does not survive contact with future
features. Types and narrow channels do.

Authority layout::

    Evidence ports  (ContextProvider)  read-only by construction: they
                                       return data, expose no writes.
    Deciding ports  (RepairReasoner, RepairInterpreter) take evidence,
                                       return judgments. No repo access.
    Writing port    (PatchAuthor)      the ONLY channel that may produce
                                       patches, and it may only emit
                                       AIAuthoredPatch (author == "ai").
    Executing ports (SandboxProvider, Verifier) isolate and measure.
                                       They never judge or author.
    Publishing port (PRPublisher)      accepts ONLY a sealed
                                       VerifiedRepair token. A hand-built
                                       success flag is a TypeError, not
                                       a PR.

Sealing model::

    Both capability types carry a private ``_seal`` sentinel that only
    the sealing functions in THIS module can supply. Constructing
    ``AIAuthoredPatch(..., author="ai")`` by hand fails: the object
    model itself refuses believable forgeries. A future deterministic
    fixer produces unsealed output, and unsealed output cannot pass
    promotion (re-checks the seal), the verified-repair sealer
    (requires sealed patches plus derived evidence), or publication
    (requires the token). Forgery requires importing the private
    sentinel — a visibly hostile act, never an accident.

This module imports nothing from hunt.py at runtime (annotations only,
deferred), so hunt.py can import it freely without a cycle. It is
deliberately dependency-light: stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


_SEAL: Any = object()
"""Private admission sentinel. Never exported, never in __all__.

Only seal_ai_patch and seal_verified_repair close over this value.
Any other construction path — direct calls, subclasses, dataclasses
helpers — fails the identity check in __post_init__.
"""


# ── Repair plan (what the author receives) ─────────────────────────────────

@dataclass(frozen=True)
class RepairPlan:
    """The AI's repair assignment: live problem statement, not an edit recipe.

    Carries what changed, what the reasoning concluded, and what must be
    left alone. It deliberately names NO exact source lines to rewrite:
    deterministic analysis may narrow the candidate files, but the model
    decides what changes, why, and what remains untouched.
    """

    provider: str
    version_from: str
    version_to: str
    summary: str
    smallest_change: str = ""
    must_not_change: tuple[str, ...] = ()
    changelog_url: str = ""
    test_error: str = ""
    base_context: Any = None


# ── Provenance-sealed patch ───────────────────────────────────────────────

@dataclass(frozen=True)
class AIAuthoredPatch:
    """A semantic patch the model authored. Sealed at construction.

    Constructible ONLY via seal_ai_patch, which supplies the private
    ``_seal`` sentinel. Direct construction — even with ``author="ai"``,
    ``success=True``, or any other believable field — raises TypeError.
    The patch carries author, model identity, the actual diff, and the
    admission provenance (which sealer, which reasoning attempt).
    """

    file_path: str
    unified_diff: str
    lines_changed: int = 0
    rules_applied: list[str] = field(default_factory=list)
    author: str = "ai"
    model: str = "unknown"
    reasoning_attempt: int = 1
    admission: str = "seal_ai_patch"
    _seal: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError(
                "AIAuthoredPatch is sealed: construct it only via "
                "seal_ai_patch(). Direct construction — including with "
                "author='ai' — is rejected so deterministic output can "
                "never inhabit this type by accident."
            )
        if self.author != "ai":
            raise TypeError(
                f"AIAuthoredPatch requires author='ai', got {self.author!r}. "
                "Deterministic output cannot be promoted or published."
            )
        if not self.model:
            raise TypeError("AIAuthoredPatch requires a model identity.")
        if self.admission != "seal_ai_patch":
            raise TypeError(
                "AIAuthoredPatch carries unknown admission provenance."
            )
        if not self.file_path:
            raise TypeError("AIAuthoredPatch requires a file path.")
        if not self.unified_diff:
            raise TypeError("AIAuthoredPatch requires a unified diff.")


def seal_ai_patch(raw: Any, *, model: str, reasoning_attempt: int = 1) -> AIAuthoredPatch | None:
    """The single admission point from AI planner output into Hunt.

    Admits only successful, diff-bearing results and stamps them with
    the LIVE model's identity taken from the adapter — never from the
    raw output itself, so spoofed model identity on the input cannot
    propagate. Returns None for anything else. Only PatchAuthor
    adapters call this.
    """
    if raw is None or not bool(getattr(raw, "success", False)):
        return None
    path = str(getattr(raw, "file_path", "") or "")
    diff = str(getattr(raw, "unified_diff", "") or "")
    if not path or not diff:
        return None
    try:
        lines = int(getattr(raw, "lines_changed", 0) or 0)
    except (TypeError, ValueError):
        lines = 0
    rules = list(getattr(raw, "rules_applied", []) or [])
    return AIAuthoredPatch(
        file_path=path,
        unified_diff=diff[:20000],
        lines_changed=lines,
        rules_applied=[str(r) for r in rules][:10],
        author="ai",
        model=model or "unknown",
        reasoning_attempt=reasoning_attempt,
        admission="seal_ai_patch",
        _seal=_SEAL,
    )


def is_sealed(patch: Any) -> bool:
    """Boundary check: is this object a genuinely sealed AI patch?

    Verifies type, author, admission, AND the private sentinel identity.
    A duck-typed lookalike passes none of the structural checks; an
    object.__new__ forgery fails the sentinel check. Only objects that
    passed through seal_ai_patch return True.
    """
    return (
        isinstance(patch, AIAuthoredPatch)
        and getattr(patch, "author", None) == "ai"
        and getattr(patch, "admission", None) == "seal_ai_patch"
        and getattr(patch, "_seal", None) is _SEAL
    )


# ── Scope evaluation (pure; owned by the sealer, not the caller) ──────────

def evaluate_scope(
    affected_paths: list[str] | tuple[str, ...] | None,
    must_not_change: list[str] | tuple[str, ...] | None,
    changed: list[str] | tuple[str, ...] | None,
) -> tuple[bool, str]:
    """Decide whether measured changes fit the reasoned scope.

    Pure function of (declared intent, measured reality). The
    verified-repair sealer runs this itself — callers never supply a
    pre-computed boolean, so scope acceptance cannot be caller-forced.
    """
    changed = [c for c in (changed or []) if c]
    if not changed:
        return False, "no files changed"
    declared = {str(p).lstrip("./") for p in (affected_paths or []) if p}
    forbidden = {str(p).lstrip("./") for p in (must_not_change or []) if p}
    normalized = [str(c).lstrip("./") for c in changed]
    for path in normalized:
        if path in forbidden or any(path.endswith(m) or m.endswith(path) for m in forbidden if m):
            return False, f"{path} was declared must-not-change"
    if declared:
        extras = [c for c in normalized
                  if not any(c == d or c.endswith(d) or d.endswith(c) for d in declared)]
        if extras:
            return False, f"unrelated files modified: {', '.join(extras)}"
    return True, ""


# ── Verified-repair capability token ──────────────────────────────────────

@dataclass(frozen=True)
class VerifiedRepair:
    """Capability token: proof that a repair earned its PR. Sealed.

    Mintable ONLY via seal_verified_repair, which derives acceptance
    from trusted evidence instead of trusting caller booleans. Direct
    construction — even with exit_code=0 and files set — raises
    TypeError without the private sentinel. PRPublisher accepts nothing
    else, so an unverified repair is a type error at the publication
    boundary, not a missed boolean.
    """

    finding_id: str
    provider: str
    version_from: str
    version_to: str
    files: list[str] = field(default_factory=list)
    unified_diff: str = ""
    test_command: str = ""
    test_exit_code: int = -1
    test_duration_ms: int = 0
    reasoning_attempt: int = 1
    model: str = "unknown"
    interpretation_rationale: str = ""
    verification_tier: str = "behavioral_verified"
    _seal: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError(
                "VerifiedRepair is sealed: only seal_verified_repair() may "
                "mint it. A manually constructed success report — however "
                "believable its fields — is rejected here."
            )
        if not self.files:
            raise ValueError("VerifiedRepair requires at least one repaired file.")
        if not self.unified_diff:
            raise ValueError("VerifiedRepair requires evidence diff.")
        if not self.test_command:
            raise ValueError("VerifiedRepair requires a real test command.")
        if self.test_exit_code != 0:
            raise ValueError(
                f"VerifiedRepair requires exit 0, got {self.test_exit_code}. "
                "Unverified repairs can never become PRs."
            )


def seal_verified_repair(
    *,
    finding_id: str,
    provider: str,
    version_from: str,
    version_to: str,
    patches: list[AIAuthoredPatch],
    sandbox_dir: str,
    evidence: Any,
    interpretation: Any,
    affected_paths: list[str] | tuple[str, ...] | None,
    must_not_change: list[str] | tuple[str, ...] | None,
    reasoning_attempt: int = 1,
    model: str = "unknown",
) -> VerifiedRepair | None:
    """Mint the PR capability token from trusted evidence. Else None.

    Every acceptance input is DERIVED here, not trusted from the caller:

    * patch provenance — each patch must be a sealed AIAuthoredPatch
      (isinstance + author re-checked; duck-typed lookalikes rejected);
    * sandbox binding — every patch path must live inside sandbox_dir;
    * execution reality — evidence must carry a real command and exit 0;
    * interpretation — the interpretation OBJECT is inspected for
      solved / unrelated / needs-investigation (no caller booleans);
    * scope — evaluate_scope runs here over the measured changed files
      against the reasoned intent (no caller boolean).

    Any failure returns None. Callers fail closed; no token exists to
    publish.
    """
    import os as _os

    if not patches:
        return None
    for p in patches:
        if not is_sealed(p):
            return None
    test_command = str(getattr(evidence, "command", "") or "")
    try:
        test_exit = int(getattr(evidence, "exit_code", -1))
    except (TypeError, ValueError):
        return None
    try:
        test_duration = int(getattr(evidence, "duration_ms", 0) or 0)
    except (TypeError, ValueError):
        test_duration = 0
    if not test_command or test_exit != 0:
        return None
    solved = bool(getattr(interpretation, "solved", False))
    unrelated = bool(getattr(interpretation, "unrelated_behavior", True))
    needs_more = bool(getattr(interpretation, "needs_more_investigation", True))
    if not solved or unrelated or needs_more:
        return None
    changed = list(getattr(evidence, "changed_files", []) or [])
    scope_ok, _reason = evaluate_scope(affected_paths, must_not_change, changed)
    if not scope_ok:
        return None
    base = _os.path.abspath(sandbox_dir)
    files: list[str] = []
    diffs: list[str] = []
    for p in patches:
        rel = _os.path.relpath(p.file_path, base)
        if rel.startswith(".."):
            return None
        files.append(rel)
        diffs.append(p.unified_diff)
    unified = "\n".join(diffs)[:20000]
    if not files or not unified:
        return None
    rationale = str(getattr(interpretation, "rationale", "") or "")[:2000]
    cmd_lower = test_command.lower()
    is_compile_only = (
        any(k in cmd_lower for k in ("tsc", "type-check", "build", "mypy", "pyright"))
        and not any(k in cmd_lower for k in ("test", "jest", "pytest", "vitest", "mocha", "cargo test"))
    )
    output_text = str(getattr(evidence, "output", "") or "").lower()
    has_test_runner_output = any(k in output_text for k in ("passed", "test results:", "tests passed", "ok.", "failures:"))
    verification_tier = "behavioral_verified" if (not is_compile_only or has_test_runner_output) else "compile_verified"
    try:
        return VerifiedRepair(
            finding_id=finding_id,
            provider=provider,
            version_from=version_from,
            version_to=version_to,
            files=files,
            unified_diff=unified,
            test_command=test_command,
            test_exit_code=test_exit,
            test_duration_ms=test_duration,
            reasoning_attempt=reasoning_attempt,
            model=model or "unknown",
            interpretation_rationale=rationale,
            verification_tier=verification_tier,
            _seal=_SEAL,
        )
    except (TypeError, ValueError):
        return None


# ── Ports ─────────────────────────────────────────────────────────────────

@runtime_checkable
class ContextProvider(Protocol):
    """Read-only evidence source. Returns data; exposes no writes."""

    def gather(self, repo_dir: str, finding: Any) -> Any:
        ...


@runtime_checkable
class RepairReasoner(Protocol):
    """Decides what should change. No repo access, never edits."""

    def reason(self, ctx: Any, prior_evidence: str = "") -> Any:
        ...


@runtime_checkable
class PatchAuthor(Protocol):
    """The ONLY channel that may produce patches. Emits sealed AI patches.

    Receives the live context plus a RepairPlan (problem statement with
    guardrails, never an edit recipe) and returns sealed patch material.
    Deterministic analysis may narrow ``candidate_files``; the model
    decides what changes, why, and what remains untouched.
    """

    def author(
        self,
        *,
        ctx: Any,
        plan: RepairPlan,
        sandbox_dir: str,
        candidate_files: list[str],
        reasoning_attempt: int,
    ) -> list[AIAuthoredPatch]:
        ...


@runtime_checkable
class SandboxProvider(Protocol):
    """Isolates execution. Provides worktrees; judges nothing."""

    def create(self, repo_dir: str, sha: str = "") -> Any:
        ...

    def destroy(self, repo_dir: str, sandbox: Any) -> None:
        ...


@runtime_checkable
class Verifier(Protocol):
    """Measures reality. Returns raw evidence; never forces success."""

    def verify(self, sandbox_dir: str, timeout: int = 180) -> Any:
        ...


@runtime_checkable
class RepairInterpreter(Protocol):
    """Judges evidence skeptically. No execution, never edits."""

    def interpret(self, reasoning: Any, evidence: Any) -> Any:
        ...


@runtime_checkable
class PRPublisher(Protocol):
    """Publishes sealed repairs. Accepts ONLY a VerifiedRepair token."""

    def publish(self, repair: VerifiedRepair, ctx: Any,
                github_repo: str | None = None) -> str | None:
        ...


@dataclass
class HuntPorts:
    """The complete seam set for Hunt's core loop.

    Future features extend Hunt by implementing these protocols — new
    evidence sources, new model backends — without touching the loop,
    which never imports concrete modules itself.
    """

    context: ContextProvider
    reasoner: RepairReasoner
    author: PatchAuthor
    sandbox: SandboxProvider
    verifier: Verifier
    interpreter: RepairInterpreter
    publisher: PRPublisher


__all__ = [
    "AIAuthoredPatch",
    "VerifiedRepair",
    "RepairPlan",
    "seal_ai_patch",
    "seal_verified_repair",
    "is_sealed",
    "evaluate_scope",
    "ContextProvider",
    "RepairReasoner",
    "PatchAuthor",
    "SandboxProvider",
    "Verifier",
    "RepairInterpreter",
    "PRPublisher",
    "HuntPorts",
]
