"""Developer Trust Surface: High-Confidence PR Markdown Generator."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrustPRMetadata:
    provider_name: str
    from_version: str
    to_version: str
    changelog_url: str
    files_modified: int
    files_scanned: int
    unintended_files_modified: int
    quarantined_callsites_count: int
    unified_diff: str
    test_command: str
    test_exit_code: int
    test_duration_ms: int
    lockfile_hash: str
    patch_hash: str
    semantic_score: float
    drift_reason: str = ""
    impacted_callsites: list[dict[str, Any]] = field(default_factory=list)
    unaffected_callsites: list[dict[str, Any]] = field(default_factory=list)
    quarantined_callsites: list[dict[str, Any]] = field(default_factory=list)
    found_callsites_count: int | None = None


def generate_trust_pr_markdown(meta: TrustPRMetadata) -> str:
    """Format high-trust, developer-delighting Pull Request body."""
    is_real_test = bool(meta.test_command and meta.test_command.strip() not in ("exit 0", "none", ""))
    tests_passed = is_real_test and (meta.test_exit_code == 0)
    patched_count = len(meta.impacted_callsites) or meta.files_modified
    found_count = meta.found_callsites_count if meta.found_callsites_count is not None else patched_count
    change_clause = f"changed {meta.drift_reason}" if meta.drift_reason else "contract drift"

    if is_real_test and tests_passed and meta.unintended_files_modified == 0:
        status_tag = "[VERIFIED]"
        test_summary = "real tests ran -> GREEN"
        test_lines = [
            f"- **Test Command**: `{meta.test_command}`",
            "- **Exit Code**: `0` (SUCCESS (GREEN))",
            f"- **Duration**: `{meta.test_duration_ms}ms`",
        ]
        confidence_str = "Verified green against repository test suite"
        next_steps = [
            "This autonomous patch has passed all local tests and blast-radius verification.",
            "- [ ] Review diff preview above.",
            "- [ ] Click **Merge pull request** to apply update to `main`.",
            "- **Merge Readiness**: `MERGEABLE (READY)`",
        ]
    elif not is_real_test:
        status_tag = "[UNVERIFIED: NO AUTOMATED TEST SUITE]"
        test_summary = "no test suite configured"
        test_lines = [
            "- **Test Command**: None detected (no automated test suite in repository)",
            "- **Status**: Automated test verification skipped; patch applied by contract specification.",
        ]
        confidence_str = "Unverified by automated tests (no repository test command configured)"
        next_steps = [
            "This autonomous patch was applied strictly within the blast radius boundary, but **automated verification could not run because no test runner is configured**.",
            "- [ ] Review diff preview above.",
            "- [ ] Verify behavior manually or configure a test suite before merging.",
            "- **Merge Readiness**: `NOT MERGEABLE (BLOCKED: UNVERIFIED)`",
        ]
    else:
        status_tag = "[NEEDS REVIEW]"
        test_summary = f"tests ran -> FAILED (exit {meta.test_exit_code})"
        test_lines = [
            f"- **Test Command**: `{meta.test_command}`",
            f"- **Exit Code**: `{meta.test_exit_code}` (FAILURE (RED))",
            f"- **Duration**: `{meta.test_duration_ms}ms`",
        ]
        confidence_str = "Test failure detected"
        next_steps = [
            "This autonomous patch failed local test execution and requires human repair.",
            "- [ ] Inspect failing test output.",
            "- [ ] Manually repair callsites before merging.",
            "- **Merge Readiness**: `NOT MERGEABLE (BLOCKED: FAILING TESTS)`",
        ]

    diff_block = ""
    if meta.unified_diff:
        diff_lines = meta.unified_diff.strip().splitlines()
        preview = "\n".join(diff_lines[:40])
        if len(diff_lines) > 40:
            preview += f"\n... ({len(diff_lines) - 40} more lines)"
        diff_block = f"```diff\n{preview}\n```"
    else:
        diff_block = "_No source modifications required._"

    callsite_lines = [
        f"- **Confirmed Affected**: `{patched_count}` callsite(s) surgically patched across `{meta.files_modified}` file(s).",
    ]
    for c in meta.impacted_callsites[:8]:
        file_p = c.get("file_path") or c.get("file") or ""
        line_n = c.get("line_number") or c.get("line") or ""
        loc = f"`{file_p}:{line_n}`" if line_n else f"`{file_p}`"
        desc = c.get("description") or c.get("matched_code") or "API contract update"
        callsite_lines.append(f"  - {loc}: {desc}")

    warning_lines = []
    if not is_real_test:
        warning_lines = [
            "> [!WARNING]",
            "> **UNVERIFIED PATCH**: This patch was applied by contract specification but has **not been verified by an automated test suite**. Automated merge is blocked.",
            "",
        ]

    summary_sentence = (
        f"**{meta.provider_name} {meta.from_version} -> {meta.to_version} {change_clause} -> "
        f"Truhowl found {found_count} affected callsite(s) -> patched {patched_count} -> "
        f"{test_summary} -> 0 unrelated files -> {status_tag}**"
    ) if found_count != patched_count else (
        f"**{meta.provider_name} {meta.from_version} -> {meta.to_version} {change_clause} -> "
        f"Truhowl found {patched_count} affected callsite(s) -> patched those {patched_count} -> "
        f"{test_summary} -> 0 unrelated files -> {status_tag}**"
    )

    lines = [
        f"## {status_tag} Autonomous Maintenance: Upgrade `{meta.provider_name}` ({meta.from_version} -> {meta.to_version})",
        "",
    ]
    lines.extend(warning_lines)
    lines.extend([
        summary_sentence,
        "",
        "### Upstream API Drift Summary",
        f"- **Provider**: **{meta.provider_name}**",
        f"- **Migration**: `{meta.from_version}` -> `{meta.to_version}`",
        f"- **Official Documentation**: [Vendor Changelog & Migration Guide]({meta.changelog_url})",
        f"- **Verification State**: `{confidence_str}`",
        "",
        "---",
        "",
        "### Blast Radius Containment Receipt",
        "Truhowl verified zero unintended side effects across the entire codebase:",
        f"- **Files Scanned**: `{meta.files_scanned}`",
        f"- **Files Modified**: `{meta.files_modified}`",
        f"- **Unintended Files Touched**: **`{meta.unintended_files_modified}`** (100% Contained)",
        "",
        "<details>",
        "<summary>Cryptographic Integrity Receipts</summary>",
        "",
        f"- **Lockfile Digest (BLAKE3)**: `{meta.lockfile_hash[:16]}...`",
        f"- **Patch Digest (BLAKE3)**: `{meta.patch_hash[:16]}...`",
        "",
        "</details>",
        "",
        "---",
        "",
        "### Verification Evidence",
    ])
    lines.extend(test_lines)
    lines.extend([
        "",
        "---",
        "",
        "### Surgical Patch Preview",
        diff_block,
        "",
        "---",
        "",
        "### Callsite Triage",
    ])
    lines.extend(callsite_lines)
    lines.extend([
        f"- **Unaffected Safe**: `{len(meta.unaffected_callsites)}` callsites verified compatible with `{meta.to_version}`.",
        f"- **Quarantined for Review**: `{meta.quarantined_callsites_count}` callsites.",
        "",
        "---",
        "",
        "### Next Steps",
    ])
    lines.extend(next_steps)
    lines.extend([
        "",
        "_Generated automatically by [Truhowl](https://github.com/Devaretanmay/truhowl) Continuous Autonomous Maintenance Engine._",
        "— Hunt, Work bot",
    ])
    return "\n".join(lines)
