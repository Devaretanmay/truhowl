# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""The single verification contract: one predicate behind every VERIFIED.

This module is deliberately dependency-free (no imports from the engine) so
that *every* path capable of minting a verified state can depend on it
without creating an import cycle. Two callers use it today:

* ``truhowl.verification.service`` — the service/agent path that the hosted
  product runs; it owns clean-room replay by restoring the baseline and
  re-applying the candidate.
* ``truhowl.hunt_ports.seal_verified_repair`` — the Hunt path, which replays
  inside a kernel sandbox.

Both consult ``check_verification_contract`` before a verified state can
exist, so "clean-room replay exists in Hunt but not in the service path"
cannot regress back into the codebase: there is no longer a lighter tier of
truth, only one predicate with four independent requirements.
"""

from __future__ import annotations

from dataclasses import dataclass

# Verification tiers, ordered weakest to strongest.
BEHAVIORAL = "behavioral_verified"
COMPILE_ONLY = "compile_verified"
UNVERIFIED = "unverified"

COMPILE_ONLY_TOKENS = ("tsc", "type-check", "build", "mypy", "pyright")
TEST_RUNNER_TOKENS = ("test", "jest", "pytest", "vitest", "mocha", "cargo test")
RUNNER_OUTPUT_TOKENS = ("passed", "test results:", "tests passed", "ok.", "failures:")


@dataclass(frozen=True)
class ContractVerdict:
    """Outcome of the shared verification predicate."""

    ok: bool
    reason: str = ""


def tier_for(test_command: str, output: str = "") -> str:
    """Classify a passing run as behavioral or compile-only.

    Shared by both verification paths so a repair cannot claim a stronger
    tier on one path than it would earn on the other.
    """
    cmd = (test_command or "").lower()
    compile_only = (
        any(k in cmd for k in COMPILE_ONLY_TOKENS)
        and not any(k in cmd for k in TEST_RUNNER_TOKENS)
    )
    out = (output or "").lower()
    runner_output = any(k in out for k in RUNNER_OUTPUT_TOKENS)
    return BEHAVIORAL if (not compile_only or runner_output) else COMPILE_ONLY


def check_verification_contract(
    *,
    test_command: str,
    test_exit_code: int,
    replay_command: str,
    replay_exit_code: int,
    patch_hash: str,
    scope_ok: bool,
    candidate_files: int | None = None,
) -> ContractVerdict:
    """The one predicate. Returns refusal reasons instead of raising.

    Every verified state — service path or Hunt path — must satisfy all of:

    1. a real test command actually ran,
    2. that run exited 0,
    3. a *separate* clean-room replay ran a real command,
    4. that replay exited 0,
    5. the candidate is content-addressed (hash present),
    6. the measured change set stayed inside the declared scope.
    """
    if not str(test_command or "").strip():
        return ContractVerdict(False, "no test command: cannot verify")
    try:
        if int(test_exit_code) != 0:
            return ContractVerdict(
                False, f"verification failed (exit {int(test_exit_code)})")
    except (TypeError, ValueError):
        return ContractVerdict(False, "verification exit code is not an integer")
    if not str(replay_command or "").strip():
        return ContractVerdict(False, "missing clean-room replay command")
    try:
        if int(replay_exit_code) != 0:
            return ContractVerdict(
                False, f"clean-room replay failed (exit {int(replay_exit_code)})")
    except (TypeError, ValueError):
        return ContractVerdict(False, "replay exit code is not an integer")
    if not str(patch_hash or "").strip():
        return ContractVerdict(False, "missing candidate hash")
    if not scope_ok:
        return ContractVerdict(False, "scope check failed")
    if candidate_files is not None and int(candidate_files) <= 0:
        return ContractVerdict(False, "no candidate files to verify")
    return ContractVerdict(True, "")


def require_contract(**kwargs: object):
    """Predicate variant that raises. Kept for callers that fail closed loudly."""
    verdict = check_verification_contract(**kwargs)  # type: ignore[arg-type]
    if not verdict.ok:
        raise ValueError(f"Refused: {verdict.reason}")
    return verdict


__all__ = [
    "BEHAVIORAL",
    "COMPILE_ONLY",
    "COMPILE_ONLY_TOKENS",
    "RUNNER_OUTPUT_TOKENS",
    "TEST_RUNNER_TOKENS",
    "UNVERIFIED",
    "ContractVerdict",
    "check_verification_contract",
    "require_contract",
    "tier_for",
]
