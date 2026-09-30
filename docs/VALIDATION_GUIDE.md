# Product Validation Guide: Testing Truhowl Migrations

This guide provides a structured protocol for validating Truhowl as the autonomous SDK/API migration engine for your codebase.

---

## The Core Validation Model

```text
CONVENTIONAL DEPENDENCY BUMP
Dependabot / Renovate -> Version bumped in lockfile -> CI breaks -> Human reads migration docs

WITH TRUHOWL
Upstream API/SDK Drift -> Truhowl (Check callsites -> Hunt repairs -> Sandbox verifies) -> Developer Trust PR
```

---

## 1. Test Scenario 1: Day-0 External Dependency Audit & Risk Register

Audit your entire codebase for upstream breaking changes, deprecated API callsites, and auto-repairable integrations without sending code to an LLM or modifying files.

```bash
truhowl check .
truhowl check . --format=github-issue
```

### What You Observe:
- Fast callsite locator maps external SDK and API callsites across your repository.
- Categorizes dependencies into Critical (breaking drift), Watchlist (deprecations), and Healthy.
- Generates actionable finding IDs (e.g., `stripe-df9562`) and direct links to official vendor upgrade guides.
- Runs in under 2 seconds; consumes zero AI tokens; touches zero files.

---

## 2. Test Scenario 2: Advisory Consult Assessment (Howl)

Run deep diagnostic reasoning on detected contract drift without modifying source files.

```bash
truhowl consult .
```

### What You Observe:
- Analyzes affected callsites and breaking schema mutations.
- Uses your configured BYOK provider to formulate architectural impact analysis.
- Files a structured GitHub Issue (or renders markdown to stdout) explaining the migration path.
- Enforces strict read-only execution: modifies zero files in your repository.

---

## 3. Test Scenario 3: Autonomous Repair & Sandbox Verification (Hunt)

Execute a targeted repair starting from a specific finding ID, verified in an isolated workspace with your repository's real test suite.

```bash
truhowl check .                # Note the finding ID, e.g. stripe-df9562
truhowl hunt stripe-df9562     # Full reasoning -> repair -> sandbox -> verify cycle
```

### What You Observe:
- Gathers live context: exact commit SHA, active branch, callsites, and test command.
- Consults local semantic pattern memory (`.truhowl/knowledge/`) for verified patterns and quarantined failure shapes.
- AI planner formulates surgical SEARCH/REPLACE edits (deterministic fixers never author code).
- Provisions an isolated detached Git worktree (`.truhowl/hunt/sandboxes/hunt-*`).
- Executes repository build and test commands with strict timeouts.
- Evaluates scope boundaries: touching unapproved files fails closed immediately.
- On green tests (exit code 0), mints a sealed `VerifiedRepair` capability token, commits with RFC-5322 metadata trailers, and delivers a Developer Trust PR.

---

## 4. Test Scenario 4: Fail-Closed Refusal & 2ms Rollback

Verify that unproven repairs or broken test runs never open a PR or leave corrupt state behind.

### What You Observe:
- If tests fail or no test suite is configured, Truhowl refuses loudly: *"Hunt could not safely verify this repair. No PR was created."*
- Unverified changes are rolled back in 2ms using pre-execution BLAKE3 hash snapshots.
- Failure evidence is recorded in `.truhowl/knowledge/` avoid-lists to prevent repeating the failed repair pattern.
- A detailed tamper-evident audit log is preserved at `.truhowl/hunt/<id>/audit.json`.

---

## The Validation Question

After running these validation scenarios on your codebase:

> **"Would you merge a breaking SDK bump before Truhowl proves the migration works?"**
