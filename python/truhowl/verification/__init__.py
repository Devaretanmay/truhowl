"""Canonical verification service: one contract behind every VERIFIED."""

from truhowl.verification.contract import (
    BEHAVIORAL,
    COMPILE_ONLY,
    UNVERIFIED,
    ContractVerdict,
    check_verification_contract,
    tier_for,
)
from truhowl.verification.service import (
    VerificationResult,
    VerificationSession,
    begin,
    require_verified,
    seal,
    verify_candidate,
)

__all__ = [
    "BEHAVIORAL",
    "COMPILE_ONLY",
    "UNVERIFIED",
    "ContractVerdict",
    "VerificationResult",
    "VerificationSession",
    "begin",
    "check_verification_contract",
    "require_verified",
    "seal",
    "tier_for",
    "verify_candidate",
]
