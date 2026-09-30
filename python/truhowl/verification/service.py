"""Canonical verification service: the single contract behind VERIFIED.

Every repair path that can produce a verified / PR-ready state must satisfy
the same contract, enforced here rather than by caller booleans:

    baseline -> repair attempt -> deterministic verification
    -> candidate artifact -> fresh clean-room replay (restore to baseline,
       re-apply candidate exactly, re-verify) -> scope/hash check
    -> VerifiedMigration or refusal

No VerifiedMigration may be created without valid replay evidence.
No PR-ready state may be reached without the same evidence.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any  # noqa: F401

from truhowl.test_runner import _run_tests
from truhowl.verification.contract import (
    BEHAVIORAL as _BEHAVIORAL,
    COMPILE_ONLY as _COMPILE_ONLY,
    UNVERIFIED as _UNVERIFIED,
    check_verification_contract,
    tier_for as _tier_for_shared,
)

BEHAVIORAL = _BEHAVIORAL
COMPILE_ONLY = _COMPILE_ONLY
UNVERIFIED = _UNVERIFIED


@dataclass
class VerificationResult:
    verified: bool = False
    tier: str = UNVERIFIED
    test_command: str = ""
    test_exit_code: int = -1
    test_duration_ms: int = 0
    replay_exit_code: int = -1
    replay_command: str = ""
    patch_hash: str = ""
    files: list[str] = field(default_factory=list)
    scope_ok: bool = False
    reason: str = ""


@dataclass
class VerificationSession:
    repo_dir: str = ""
    files: list[str] = field(default_factory=list)
    baseline: dict[str, str] = field(default_factory=dict)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _tier_for(test_command: str, output: str) -> str:
    """Delegates to the shared contract so both paths grade identically."""
    return _tier_for_shared(test_command, output)


def _hash_candidate(candidate: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(candidate):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(candidate[rel].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _rel_key(repo_dir: str, rel: str) -> str | None:
    """Normalize a candidate path to a repo-relative POSIX key, or None.

    Callers hand us absolute paths (the repair engine works in absolutes)
    and relative paths (CLI flags, plans) interchangeably. Normalizing here
    is what makes the scope check comparable: a candidate keyed
    ``/abs/repo/src/a.ts`` can never match a declared scope of ``src/a.ts``,
    which would refuse every legitimate repair.
    """
    text = str(rel or "").strip()
    if not text:
        return None
    if os.path.isabs(text):
        text = os.path.relpath(text, repo_dir)
    text = text.replace(os.sep, "/")
    if text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("..") or text.startswith("/"):
        return None
    return text


def begin(repo_dir: str, rel_paths: list[str]) -> VerificationSession:
    """Capture the pristine baseline for candidate files. No writes."""
    repo_dir = os.path.abspath(repo_dir)
    baseline: dict[str, str] = {}
    files: list[str] = []
    for rel in rel_paths or []:
        norm = _rel_key(repo_dir, rel)
        if norm is None:
            continue
        content = _read(os.path.join(repo_dir, norm))
        if content is None:
            continue
        baseline[norm] = content
        files.append(norm)
    return VerificationSession(repo_dir=repo_dir, files=files, baseline=baseline)


def _run(repo_dir: str, test_command: str, timeout: int) -> tuple[int, int, str]:
    start = time.time()
    try:
        proc = _run_tests(repo_dir, test_command, timeout=timeout)
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        return int(proc.returncode), max(1, int((time.time() - start) * 1000)), out
    except Exception as exc:
        return 1, max(1, int((time.time() - start) * 1000)), str(exc)


def seal(session: VerificationSession, test_command: str,
         scope_allow: list[str] | None = None, timeout: int = 120) -> VerificationResult:
    """Verify the current tree state, then replay it from baseline.

    Captures the candidate, restores baseline, re-applies the candidate
    exactly, and re-verifies. Any mismatch refuses and leaves the baseline
    restored. The candidate remains applied only on full success.
    """
    repo = session.repo_dir
    if not test_command:
        return VerificationResult(verified=False, reason="no test command: cannot verify")

    # Imported lazily: hunt_ports consumes this contract module, so a
    # module-level import here would close an import cycle.
    from truhowl.hunt_ports import evaluate_scope

    candidate: dict[str, str] = {}
    for rel in session.files:
        content = _read(os.path.join(repo, rel))
        if content is None:
            _restore(session)
            return VerificationResult(verified=False,
                                      reason=f"candidate file vanished: {rel}")
        if content != session.baseline.get(rel):
            candidate[rel] = content
    if not candidate:
        return VerificationResult(verified=False, reason="no changes to verify")

    exit_code, duration_ms, output = _run(repo, test_command, timeout)
    if exit_code != 0:
        _restore(session)
        return VerificationResult(verified=False, test_command=test_command,
                                  test_exit_code=exit_code, test_duration_ms=duration_ms,
                                  reason=f"verification failed (exit {exit_code})")

    # Clean-room replay: pristine baseline, exact re-application, re-verify.
    _restore(session)
    for rel, content in candidate.items():
        _write(os.path.join(repo, rel), content)
    replay_exit, _, replay_out = _run(repo, test_command, timeout)
    if replay_exit != 0:
        _restore(session)
        return VerificationResult(verified=False, test_command=test_command,
                                  test_exit_code=exit_code, test_duration_ms=duration_ms,
                                  replay_exit_code=replay_exit, replay_command=test_command,
                                  reason=f"clean-room replay failed (exit {replay_exit})")

    scope_ok, scope_reason = evaluate_scope(scope_allow, None, list(candidate))
    if not scope_ok:
        _restore(session)
        return VerificationResult(verified=False, test_command=test_command,
                                  test_exit_code=exit_code, test_duration_ms=duration_ms,
                                  replay_exit_code=replay_exit, replay_command=test_command,
                                  reason=f"scope mismatch: {scope_reason}")

    return VerificationResult(
        verified=True,
        tier=_tier_for(test_command, output + replay_out),
        test_command=test_command,
        test_exit_code=exit_code,
        test_duration_ms=duration_ms,
        replay_exit_code=replay_exit,
        replay_command=test_command,
        patch_hash=_hash_candidate(candidate),
        files=sorted(candidate),
        scope_ok=True,
    )


def _restore(session: VerificationSession) -> None:
    for rel, content in session.baseline.items():
        try:
            _write(os.path.join(session.repo_dir, rel), content)
        except OSError:
            pass


def verify_candidate(repo_dir: str, candidate: dict[str, str],
                     scope_allow: list[str] | None = None,
                     test_command: str = "", timeout: int = 120) -> VerificationResult:
    """Capture baseline, apply candidate, and seal. Convenience wrapper."""
    repo_dir = os.path.abspath(repo_dir)
    rels = [_rel_key(repo_dir, r) for r in candidate]
    rels = [r for r in rels if r is not None]
    session = begin(repo_dir, rels)
    for rel, content in candidate.items():
        norm = _rel_key(repo_dir, rel)
        if norm is None:
            _restore(session)
            return VerificationResult(verified=False, reason=f"candidate escapes repo: {rel}")
        _write(os.path.join(repo_dir, norm), content)
    return seal(session, test_command, scope_allow or session.files, timeout)


def require_verified(result: VerificationResult) -> VerificationResult:
    """Enforce the contract: verified + replay evidence, else refuse loudly."""
    verdict = check_verification_contract(
        test_command=result.test_command,
        test_exit_code=result.test_exit_code,
        replay_command=result.replay_command,
        replay_exit_code=result.replay_exit_code,
        patch_hash=result.patch_hash,
        scope_ok=result.scope_ok,
    )
    if not verdict.ok:
        raise ValueError(f"Refused: {verdict.reason}")
    return result
