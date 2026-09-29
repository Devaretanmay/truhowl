<div align="center">

# Koyote

### Your codebase has a second author: the outside world. Koyote reviews its pull requests.

![version](https://img.shields.io/badge/version-1.1.3-blue) ![license](https://img.shields.io/badge/license-Apache--2.0-green) ![python](https://img.shields.io/badge/python-3.10%2B-yellow) ![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

**APIs drift. SDKs break. Koyote detects it, repairs it, and proves it — before your CI goes red.**

```bash
pip install koyote
koyote check /path/to/your-repo   # read-only audit, no AI key needed
```

60 seconds to your first risk register. AI repair is opt-in (`koyote auth`).

[Quickstart](docs/QUICKSTART.md) | [CLI Reference](docs/CLI.md) | [Architecture](docs/ARCHITECTURE.md) | [Validation Guide](docs/VALIDATION_GUIDE.md) | [Join the beta](https://github.com/Devaretanmay/Koyote/issues)

</div>

---

## The Problem

Software changes in two ways:
1. **Internal changes**: Features and fixes written by your team (handled by code review and CI).
2. **External changes**: Upstream API contract drift, major SDK breaking bumps, deprecated endpoints, and security migrations.

Dependabot bumps version strings in lockfiles and leaves CI broken. Human engineers spend 20%+ of engineering cycles reading migration guides, mapping AST callsites, updating wrappers, and fixing broken tests.

**Koyote manages software changes originating outside the repository** — mapping external contracts to internal callsites, reasoning about impact with AI, generating verified repairs, and confirming zero blast radius with sandbox isolation.

---

## The Core Loop
 
```text
Any ChangeSource (Dependency release, vendor changelog, scheduled check, PR webhook)
        ↓
AST Evidence Scan + Semantic Pattern Memory (.koyote/knowledge/)
        ↓
Shared AI Reasoning Engine (Customer BYOK Provider)
AI reasons; native tools provide evidence and execute/verify
        ↓
┌─────────────────────────────────┬─────────────────────────────────┐
│ Consult (Howl Persona)          │ Work (Hunt Persona)             │
│ Find & explain problems.        │ Find, repair, verify & open PR. │
│ Deep AI impact analysis.        │ Kernel sandbox + real tests.    │
│ Files advisory GitHub Issue.    │ Delivers verified Trust PR.     │
│ Zero files touched.             │ Fails closed on test failure.   │
└─────────────────────────────────┴─────────────────────────────────┘
```

```bash
koyote auth              # Connect BYOK AI provider (Anthropic, OpenAI, Ollama)
koyote doctor            # Environment, credentials, test runner, repository health
koyote check .           # Read-only drift & affected usage scan
koyote hunt <finding>    # Plan, edit, build/test, repair, and prepare/open PR
koyote consult .         # Report-only: AI assessment as a GitHub Issue, modifies nothing
```

Canonical product workflow:
- **Check** (`koyote check`): Detects breaking SDK/API drift, maps affected callsites. Zero tokens, zero writes.
- **Hunt** (`koyote hunt <finding>`): Autonomous migration repair worker: plans the migration, edits files, executes tests in an isolated sandbox, repairs failures, and opens a verified PR.
- **Consult** (`koyote consult`): Deep AI reasoning, architectural impact diagnosis, files a GitHub Issue, modifies zero code.
See [GitHub App behavior](docs/GITHUB_APP.md).

Koyote also watches across connected repositories: a push in one repo is an
observation that can confirm into a Howl Issue on another repo's affected
work — never an automatic alert. See
[Cross-Repository Active-Work Impact](docs/CROSS_REPO_WORK_IMPACT.md).

## The Core Pipeline

```text
┌─────────────────────────┬─────────────────────────┬─────────────────────────┐
│ 1. Change Detection     │ 2. Dependency Graph     │ 3. Impact Analysis      │
│    Contract drift       │    Source → Callsite    │    ChangeSource-aware   │
├─────────────────────────┼─────────────────────────┼─────────────────────────┤
│ 4. AI-Guided Repair     │ 5. Controlled Execution │ 6. Developer Trust PR   │
│    Reasoned, then applied │    Sandboxed + Evidence │    Verified merge-ready │
└─────────────────────────┴─────────────────────────┴─────────────────────────┘
```

---

## 1. Day-0 Risk Register (`koyote check`)

When you run Koyote on any repository, it immediately answers:
- *What external APIs and SDKs does this codebase depend on?*
- *Which integrations are deprecated, behind, or at risk?*
- *Which breaking changes can Koyote already auto-repair?*

```bash
koyote check .
```

```text
================================================================================
         KOYOTE: EXTERNAL-CHANGE DEPENDENCY AUDIT & RISK REGISTER
================================================================================
Total External Providers Detected: 3
Total AST Callsites Mapped:        14
Auto-Repairable Callsites:         6
--------------------------------------------------------------------------------
[CRITICAL] AT RISK (Action Required):
  * Stripe (stripe@v21.0.0 -> v22.0.0)
    - Status: Breaking parameter mutation detected (amount: number -> string)
    - 4 callsites affected (4 auto-repairable by Koyote)

[WATCHLIST] UPCOMING DEPRECATION:
  * OpenAI (openai@v3.28.0)
    - Status: Deprecated client interface (v4 migration available)
    - 6 callsites affected

[HEALTHY] UP-TO-DATE INTEGRATIONS:
  * Anthropic (@anthropic-ai/sdk@v0.25.0)
    - Status: Up-to-date with active provider contract (4 callsites mapped)
================================================================================
```

Export directly to GitHub Issues or JSON:
```bash
koyote check . --format=github-issue   # Formatted markdown table for GitHub Issues
koyote check . --format=json           # Machine-readable risk register
```

---

## 2. External-Change Dependency Graph (`koyote graph`)

Koyote builds a unified dependency graph linking:
`Provider -> Version -> API Contract -> Manifest Dependency -> Wrapper Client -> AST Callsite -> Migration History`

```bash
koyote graph .
```

```text
================================================================================
                 KOYOTE: EXTERNAL-CHANGE DEPENDENCY GRAPH                     
================================================================================
Repository:              /path/to/my-repo
Providers Ingested:      3
Contracts Modeled:       6
Manifest Dependencies:   4
Wrapper Clients Found:   2
AST Callsites Mapped:    14
Active Graph Edges:      28
================================================================================
  [Wrapper] src/lib/stripe.ts -> wraps stripe
  [Callsite] src/billing.ts:12 -> stripe.charges.create
  [Callsite] src/checkout.ts:45 -> stripe.paymentIntents.create
================================================================================
```

---

## 3. Autonomous Repair (`koyote hunt`)

Every `koyote check` finding carries an ID. Hunt starts from that finding —
rebuilding live context (branch, exact SHA, active work, callsites, tests,
verified memory) — then reasons with AI, authors the patch with AI, verifies
it in an isolated sandbox worktree, and refuses loudly when correctness
cannot be established:

```bash
koyote check .                # read-only audit; note the finding ID
koyote hunt stripe-3a9c79     # full reasoning → repair → sandbox → verify cycle

# Provider-driven form (same engine, explicit target):
koyote work . --provider stripe
koyote work . --provider openai --from v3.28.0 --to v4.0.0 --create-pr --repo owner/repo
```

### What Hunt guarantees:
1. **AI-Authored Repair**: AI reasons about affected callsites, generates targeted source changes, and validates impact. No deterministic rewrite rules, templates, or regex fixers author code — ever.
2. **Isolated Verification**: Every repair executes in a sandbox worktree at the exact SHA with the project's real test command. No fake passes, no forced exits.
3. **Zero Blast Radius**: Verifies that 0 unintended files were modified; scope violations fail closed.
4. **Fail-Closed Refusals**: Unverified repairs produce no PR — "could not safely verify" with the evidence attached.
5. **Verified-Only PRs**: Only a sealed, green, scope-clean repair may proceed to a Developer Trust PR (explicit approval, or auto-PR where repository policy enables it).

---

## Proof, not promises

Koyote validates repairs against the repository's real test suite.
Refusals are loud and empty-handed: a repair that cannot be proven is a repair
not shipped. When verification fails, scope boundaries are breached, or no test
runner exists, Koyote rolls back changes in 2ms and refuses to open a PR.

Every commit is gated: **521 Rust + 483 Python tests**, lint-clean.
See the [Validation Guide](docs/VALIDATION_GUIDE.md)
for the full protocol.

---

## 4. Controlled Execution & Sandboxed Verification

Koyote provides **controlled, reproducible execution** across local kernel sandboxes (macOS Seatbelt, Linux Landlock) and Docker:
- **Zero-Exfiltration Isolation**: Credentials (`~/.ssh`, `~/.aws`, keychains) denied at the kernel boundary.
- **Execution-Evidence Compression**: Native Rust engines distill massive test outputs down to high-signal failure traces and stack traces for PR evidence.
- **2ms Instant Undo**: Pre-execution BLAKE3 hash snapshots enable physical rollback of modified and generated files in 2 milliseconds.

```bash
koyote init                          # Initialize workspace control plane
koyote diff                          # Inspect isolated execution change sets
koyote undo                          # Instant 2ms physical rollback
```

---

## Python SDK

```python
from koyote.graph import build_dependency_graph, audit_dependency_graph
from koyote.maintenance import run_maintenance_cycle

# 1. Audit repository external dependencies
summary = audit_dependency_graph(repo_root=".")
print(f"At Risk: {len(summary['at_risk'])}, Auto-Repairable: {summary['total_auto_repairable']}")

# 2. Run autonomous maintenance cycle
report = run_maintenance_cycle(
    repo_dir=".",
    provider_name="stripe",
    create_pr=False,
)
print(f"Maintenance Outcome: {'GREEN' if report.success else 'REFUSED'}")
print(report.unified_diff)
```

---

## Documentation

[Quickstart Guide](docs/QUICKSTART.md) · [CLI Reference](docs/CLI.md) · [Architecture](docs/ARCHITECTURE.md) · [API Reference](docs/API_REFERENCE.md) · [Validation Guide](docs/VALIDATION_GUIDE.md) · [Agent Governance & Trailers](SPEC.md)

The core abstraction is `ChangeSource` (external API, SDK, OpenAPI, GraphQL, protobuf, webhook,
MCP server, internal service): Koyote keeps software working when the systems around it change.
Vendor SDK migrations are the working wedge; other contract kinds are representable types with no
connectors yet — they fail closed to quarantine instead of guessing.

Under the hood, Koyote is an AI maintenance agent with deterministic tools: a code graph,
verified maintenance memory (trusted successes plus a failure avoid-list),
sandbox execution, and a fail-closed verifier.
Repeated work reuses verified knowledge instead of re-reasoning, so the system gets faster,
cheaper, and more precise the longer it watches a repository.

## Where Koyote Fits

Conventional AI reviewers start from a human pull request and ask whether the change
is correct. Koyote starts from the other end: a dependency or contract changed out
in the world, and it asks what that breaks in your repository. One AI reasons over
your codebase plus the change itself, backed by maintenance memory — past verified
repairs and quarantined failures. The output is not a review but a repair, proven
against your real test suite before it ever reaches a pull request.

Koyote is not a generic coding agent, a PR reviewer, a Dependabot clone, a
codebase Q&A tool, or vulnerability-management software. It is autonomous
maintenance for systems that change.

The old fable got it backwards: the village stopped believing because the boy
cried wolf over nothing. Most automation still does — vague green checks,
unverified badges, silent passes. Koyote only howls when there's actually
one in the fence: verified repairs, loud refusals, never a faked pass.

## Beta

Koyote is in private beta. The fastest way in: run `koyote check` on your
repo and [open an issue](https://github.com/Devaretanmay/Koyote/issues) with
what it found — misses and false alarms included. That feedback is the roadmap.

## License

Apache-2.0. Copyright 2026 Koyote Authors.

