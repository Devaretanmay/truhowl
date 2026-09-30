<div align="center">

# Truhowl

### Your codebase has a second author: the outside world. Truhowl reviews its pull requests.

![version](https://img.shields.io/badge/version-1.2.0-blue) ![license](https://img.shields.io/badge/license-Apache--2.0-green) ![python](https://img.shields.io/badge/python-3.10%2B-yellow) ![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

**APIs drift. SDKs break. Truhowl detects it, repairs it, and proves it — before your CI goes red.**

```bash
pip install truhowl
truhowl check /path/to/your-repo   # read-only audit, no AI key needed
```

60 seconds to your first risk register. AI repair is opt-in (`truhowl auth`).

[Quickstart](docs/QUICKSTART.md) | [CLI Reference](docs/CLI.md) | [Architecture](docs/ARCHITECTURE.md) | [Validation Guide](docs/VALIDATION_GUIDE.md) | [Join the beta](https://github.com/Devaretanmay/truhowl/issues)

</div>

---

## The Problem

Software changes in two ways:
1. **Internal changes**: Features and fixes written by your team (handled by code review and CI).
2. **External changes**: Upstream API contract drift, major SDK breaking bumps, deprecated endpoints, and security migrations.

Dependabot bumps version strings in lockfiles and leaves CI broken. Human engineers spend 20%+ of engineering cycles reading migration guides, mapping AST callsites, updating wrappers, and fixing broken tests.

**Truhowl manages software changes originating outside the repository** — mapping external contracts to internal callsites, reasoning about impact with AI, generating verified repairs, and confirming zero blast radius with sandbox isolation.

---

## The Core Loop
 
```text
Any ChangeSource (Dependency release, vendor changelog, scheduled check, PR webhook)
        ↓
AST Evidence Scan + Semantic Pattern Memory (.truhowl/knowledge/)
        ↓
Shared AI Reasoning Engine (Customer BYOK Provider)
AI reasons; native tools provide evidence and execute/verify
        ↓
┌─────────────────────────────────┬─────────────────────────────────┐
│ Consult (advisory)              │ Migrate (repair)                │
│ Find & explain problems.        │ Find, repair, verify & open PR. │
│ Deep AI impact analysis.        │ Kernel sandbox + real tests.    │
│ Files advisory GitHub Issue.    │ Delivers verified Trust PR.     │
│ Zero files touched.             │ Fails closed on test failure.   │
└─────────────────────────────────┴─────────────────────────────────┘
```

```bash
truhowl login             # Connect BYOK AI provider (Anthropic, OpenAI, Ollama)
truhowl doctor            # Environment, credentials, test runner, repository health
truhowl check .           # Read-only drift & affected usage scan
truhowl ask "what breaks if we upgrade stripe?"  # Read-only impact answer
truhowl migrate <finding> # Plan, edit, build/test, repair, and prepare/open PR
truhowl consult .         # Report-only: AI assessment as a GitHub Issue, modifies nothing
```

### The product workflow

The primary path is the agent, not the CLI. The CLI is the local interface to
the same services.

```text
Connect          truhowl connect                  link repositories (GitHub App or local paths)
   ↓
Watch            truhowl agent watch --poll       poll upstream registries, open Migration Cases
   ↓
Migration Case   truhowl agent cases              a published release meets affected usage
   ↓
Repair           truhowl agent run <case>         plan → edit → test
   ↓
Verify           (automatic)                      clean-room replay + scope/hash check
   ↓
Deliver          truhowl agent run <case> --create-pr   PR only for a verified migration
```

Every step above is explainable from persisted evidence: `truhowl agent show <case>`
and `truhowl ask "why wasn't the PR opened?"` answer from the store, not from prose.

Automation is bounded by one persisted policy, default `OBSERVE`:

| Mode | Detect | Repair | Verify | Publish |
|------|--------|--------|--------|---------|
| `observe` (default) | yes | no | no | no |
| `prepare` | yes | yes | yes | no |
| `deliver` | yes | yes | yes | yes |

```bash
truhowl agent policy prepare    # repair and prove, never publish
truhowl agent policy deliver    # also publish verified migrations
```

CLI surface for the same services (all optional):
- **Check** (`truhowl check`): Detects breaking SDK/API drift, maps affected callsites. Zero tokens, zero writes.
- **Ask** (`truhowl ask`): Answers impact, verification and delivery questions from local evidence. Read-only, no AI key needed.
- **Migrate** (`truhowl migrate <finding>`): The repair entry point for a single finding ID, using the same verification and delivery services as the agent.
- **Consult** (`truhowl consult`): Deep AI reasoning, architectural impact diagnosis, files a GitHub Issue, modifies zero code.

Agent loop (no manual finding IDs): `truhowl agent watch --poll` polls
upstream registries (npm today, for the supported provider set), turns a new
release into a Migration Case when connected repositories show affected usage,
and `truhowl agent run` drives it end-to-end; `truhowl agent show` explains
from persisted evidence. See [GitHub App behavior](docs/GITHUB_APP.md).

Truhowl also watches across connected repositories: a push in one repo is an
observation that can confirm into an advisory Issue on another repo's affected
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

## 1. Day-0 Risk Register (`truhowl check`)

When you run Truhowl on any repository, it immediately answers:
- *What external APIs and SDKs does this codebase depend on?*
- *Which integrations are deprecated, behind, or at risk?*
- *Which breaking changes can Truhowl already auto-repair?*

```bash
truhowl check .
```

```text
================================================================================
         TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT & RISK REGISTER
================================================================================
Total External Providers Detected: 3
Total AST Callsites Mapped:        14
Auto-Repairable Callsites:         6
--------------------------------------------------------------------------------
[CRITICAL] AT RISK (Action Required):
  * Stripe (stripe@v21.0.0 -> v22.0.0)
    - Status: Breaking parameter mutation detected (amount: number -> string)
    - 4 callsites affected (4 auto-repairable by Truhowl)

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
truhowl check . --format=github-issue   # Formatted markdown table for GitHub Issues
truhowl check . --format=json           # Machine-readable risk register
```

---

## 2. External-Change Dependency Graph (`truhowl graph`)

Truhowl builds a unified dependency graph linking:
`Provider -> Version -> API Contract -> Manifest Dependency -> Wrapper Client -> AST Callsite -> Migration History`

```bash
truhowl graph .
```

```text
================================================================================
                 TRUHOWL: EXTERNAL-CHANGE DEPENDENCY GRAPH                     
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

## 3. Autonomous Repair (`truhowl migrate`)

Every `truhowl check` finding carries an ID. `truhowl migrate` starts from
that finding — rebuilding live context (branch, exact SHA, affected
callsites, tests, verified memory) — then reasons with AI, authors the
patch with AI, verifies it in an isolated sandbox worktree, and refuses
loudly when correctness cannot be established:

```bash
truhowl check .                # read-only audit; note the finding ID
truhowl migrate stripe-3a9c79     # full reasoning → repair → sandbox → verify cycle

# Provider-driven form (same engine, explicit target):
truhowl migrate . --provider stripe
truhowl migrate . --provider openai --from v3.28.0 --to v4.0.0 --create-pr --repo owner/repo
```

### What migrate guarantees:
1. **AI-Authored Repair**: AI reasons about affected callsites, generates targeted source changes, and validates impact. No deterministic rewrite rules, templates, or regex fixers author code — ever.
2. **Isolated Verification**: Every repair executes in a sandbox worktree at the exact SHA with the project's real test command. No fake passes, no forced exits.
3. **Zero Blast Radius**: Verifies that 0 unintended files were modified; scope violations fail closed.
4. **Fail-Closed Refusals**: Unverified repairs produce no PR — "could not safely verify" with the evidence attached.
5. **Verified-Only PRs**: Only a sealed, green, scope-clean repair may proceed to a Developer Trust PR (explicit approval, or auto-PR where repository policy enables it).

---

## Proof, not promises

Truhowl validates repairs against the repository's real test suite.
Refusals are loud and empty-handed: a repair that cannot be proven is a repair
not shipped. When verification fails, scope boundaries are breached, or no test
runner exists, Truhowl restores the baseline from pre-execution BLAKE3
snapshots and refuses to open a PR.

Every verification path runs the same contract: deterministic verification,
then a fresh clean-room replay that restores the baseline, re-applies the
candidate exactly, re-verifies, and checks scope and candidate hash. There is
no lighter "verified" tier, and no verified state can be minted without replay
evidence. Every commit is gated: **{RUST_TESTS} Rust + {PYTHON_TESTS} Python
tests**, lint-clean.
See the [Validation Guide](docs/VALIDATION_GUIDE.md)
for the full protocol.

---

## 4. Controlled Execution & Sandboxed Verification

Truhowl provides **controlled, reproducible execution** across local kernel sandboxes (macOS Seatbelt, Linux Landlock) and Docker:
- **Zero-Exfiltration Isolation**: Credentials (`~/.ssh`, `~/.aws`, keychains) denied at the kernel boundary.
- **Execution-Evidence Compression**: Native Rust engines distill massive test outputs down to high-signal failure traces and stack traces for PR evidence.
- **Hash-Verified Undo**: Pre-execution BLAKE3 snapshots make rollback of modified and generated files a deterministic restore rather than a best-effort reverse patch.

```bash
truhowl init                          # Initialize workspace control plane
truhowl diff                          # Inspect isolated execution change sets
truhowl undo                          # Restore from the pre-execution snapshot
```

---

## Python SDK

```python
from truhowl.graph import build_dependency_graph, audit_dependency_graph
from truhowl.maintenance import run_maintenance_cycle

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
MCP server, internal service): Truhowl keeps software working when the systems around it change.
Vendor SDK migrations are the working wedge; other contract kinds are representable types with no
connectors yet — they fail closed to quarantine instead of guessing.

Under the hood, Truhowl is an AI maintenance agent with deterministic tools: a code graph,
verified maintenance memory (trusted successes plus a failure avoid-list),
sandbox execution, and a fail-closed verifier.
Repeated work reuses verified knowledge instead of re-reasoning, so the system gets faster,
cheaper, and more precise the longer it watches a repository.

## Where Truhowl Fits

Conventional AI reviewers start from a human pull request and ask whether the change
is correct. Truhowl starts from the other end: a dependency or contract changed out
in the world, and it asks what that breaks in your repository. One AI reasons over
your codebase plus the change itself, backed by maintenance memory — past verified
repairs and quarantined failures. The output is not a review but a repair, proven
against your real test suite before it ever reaches a pull request.

Truhowl is not a generic coding agent, a PR reviewer, a Dependabot clone, a
codebase Q&A tool, or vulnerability-management software. It is autonomous
maintenance for systems that change.

The old fable got it backwards: the village stopped believing because the boy
cried wolf over nothing. Most automation still does — vague green checks,
unverified badges, silent passes. Truhowl's verified-repair path only speaks
when a repair has been proven against your own test suite, and every other
outcome is a loud, evidence-backed refusal. Claims outside that path are
scoped to what the code actually does, not to what would sound best.

## Beta

Truhowl is in private beta. The fastest way in: run `truhowl check` on your
repo and [open an issue](https://github.com/Devaretanmay/truhowl/issues) with
what it found — misses and false alarms included. That feedback is the roadmap.

## License

Apache-2.0. Copyright 2026 Truhowl Authors.

