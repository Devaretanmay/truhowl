# Changelog

All notable changes to Truhowl are documented here.

## [1.1.3] - 2026-09-11

### Added
- **Hunt autonomous repair agent (`truhowl/hunt.py`, `truhowl hunt <id>`)**: finding-driven lifecycle — live context reconstruction, structured 14-question AI reasoning with per-file intent, AI-authored patches, isolated exact-SHA sandbox worktrees, real verification, skeptical AI interpretation, bounded AI-directed iteration, fail-closed refusals, verified-only PRs, and a full JSON audit trail per finding.
- **Capability ports + sealed provenance (`truhowl/hunt_ports.py`)**: `ContextProvider / RepairReasoner / PatchAuthor / SandboxProvider / Verifier / RepairInterpreter / PRPublisher` protocols; frozen `AIAuthoredPatch` (sealable only via `seal_ai_patch`) and `VerifiedRepair` capability token (mintable only via `seal_verified_repair`, which derives acceptance from evidence instead of trusting caller flags).
- **Concurrency + promotion integrity**: repo-level `HuntLock` serializes runs (loud refusal on timeout); post-promotion verification re-reads promoted files and rejects unexpected worktree changes; sandbox binding (repo/SHA/worktree) checked before promotion; symlinks refused at the promotion boundary.
- **Secret redaction (`truhowl/redact.py`)**: provider keys, tokens, and private-key blocks are scrubbed before LLM submission and before audit persistence.
- **Registry-basis labeling**: findings carry explicit `basis`/evidence labels (registry-inferred, never observed at the vendor); audits record `evidence_limits` marking coverage relevance, failure attribution, and minimality as AI-judged.

### Changed
- **Finding IDs are repo-relative**: same checkout yields the same ID across symlink/relative/absolute path spellings.
- **Work-family CLI commands share one parser** (`aliases=["fix", "maintain", "update", "hunt", "@hunt"]`); consult family likewise (`aliases=["howl", "@howl"]`).

### Removed
- **Framework hook adapters** (`hooks/langchain.py`, `hooks/crewai.py`, `hooks/autogen.py`, `hooks/data_agent.py`): zero in-repo consumers.
- **Surgical AST patcher engine** (`engines/autopatch/patcher.rs`, its binding, and the `autopatch.apply_patch` wrapper): no product caller; a contract test now pins the deterministic patch surface as absent.
- **CI runner** (`truhowl/ci/`): no callers.
- **Bespoke test gates** in `scripts/run_all_tests.py` (comment-density, inline-import, demo-file checks): `ruff` + `cargo test` + `pytest` remain.

## [1.1.1] - 2026-09-09

### Added
- **Automated CI Workflow**: Gated pull requests and main branch on `ruff`, `cargo test --lib`, and `pytest`.
- **Runtime Dependencies**: Added `pyjwt[crypto]>=2.8.0` for full GitHub App authentication support.
- **PEP 621 Manifest Parsing**: Support for standard `pyproject.toml` dependencies array and `Cargo.toml` inline tables in `drift.py`.

### Changed
- **Consult & Work Mode Architecture**: Formalized the Consult (Howl persona) vs Work (Hunt persona) mode split. Consult diagnoses maintenance drift and opens GitHub Issues without modifying code; Work repairs code, validates in sandbox, and delivers verified PRs.
- **Security & Credential Protection**: Ephemeral tokens are dynamically passed via `http.extraHeader` instead of being persisted into `.git/config`.
- **Eliminated Circular Import Cycle**: Cleaned module boundaries between `truhowl.pipeline` and `truhowl.github`.
- **Naming Reconciliation**: Canonical Truhowl naming applied across code, documentation, test suites, and configurations.


### Changed
- **Rename Volf → Truhowl across product, code, and docs.** Package `truhowl` 1.1.0 (CLI, imports, crate `truhowl-core`, `TRUHOWL_*` env, `.truhowl/` state dirs, MCP tools, `@truhowl` trigger, trailers, SDK names). Hard cut with no aliases. KB keeps pinned legacy read paths for `.volf/`, `.sheepdog/`, and `.compart/` trees.
- **Howl warns, Hunt repairs.** Consult Issues sign as Howl, verified Trust PRs as Hunt — two voices, one reasoning engine.
- **Rename Sheepdog → Volf across product, code, and docs.** Package `volf` 1.1.0 (CLI, imports, crate `volf-core`, `VOLF_*` env, `.volf/` state dirs, MCP tools, `@volf` trigger, trailers, SDK names). Hard cut with no aliases. KB keeps pinned legacy read paths for `.sheepdog/` and `.compart/` trees.
- **Howl, the hunt voice.** The watch loop and monitoring surface speak as Howl — `Howl hunting` in serve/doctor output. Howl advises, Hunt repairs; one reasoning engine behind both.

### Added
- **Alias-aware callsite analysis and repair.** The AST locator resolves proven client bindings (`const s = new Stripe()`, `require('stripe')`, `import stripe as s`) and reports `alias`-tagged callsites. DIRECT rewrites instantiate exact-identifier variants with the receiver preserved — regexes are never loosened, exotic bindings fail closed.

### Added
- **External-Change Dependency Graph (`truhowl graph`).** Native Rust graph engine mapping external providers, versions, OpenAPI contracts, manifest dependencies, wrapper clients, and AST callsites.
- **Day-0 Risk Register (`truhowl check`).** Instant audit command scanning codebases for at-risk, deprecated, and auto-repairable external API callsites with ANSI and GitHub Issue markdown exports.
- **Autonomous Continuous Maintenance (`truhowl work`).** Closed-loop maintenance engine detecting upstream breaking changes, synthesizing surgical AST patches, running local formatters (`prettier`, `ruff`), and opening verified Developer Trust PRs.
- **Provider Contract Registry (`truhowl providers`).** Pre-indexed breaking-change contract catalog for Stripe, OpenAI, Anthropic, Clerk, Sentry, Supabase, Twilio, Octokit, and AWS SDK.
- **Time-Machine Replay Protocol (`truhowl reproduce`).** Historical benchmark engine evaluating verified ground-truth migrations against real open-source repositories with zero blast radius.
- **GitHub App & Webhook Server (`truhowl app`).** Continuous webhook daemon for automated PR drift detection and verification.
- **Change-source abstraction (`truhowl.change_source`).** Thin `ChangeSource`/`Detection` types generalizing the pipeline beyond vendor SDKs (OpenAPI, GraphQL, protobuf, webhooks, MCP, internal services as representable, fail-closed kinds).
- **Invisible decision engine (`TruhowlIntelligence`).** Internal DIRECT/AI/HYBRID/QUARANTINE routing with confidence, token estimates, and blast-radius metadata. No `--ai`/`--direct` user flags.
- **Repository knowledge flywheel (`.truhowl/knowledge/`).** Namespaced verified-pattern cache with legacy fallback reads, failure quarantine, and test-recipe seeding on index.
- **Incremental indexing (`index_state.json`).** Commit-SHA + mtime tracking; `changed_since_index()` reports freshness and discovery deltas.
- **Installation persistence (`truhowl.github.installations`).** Flat-JSON install records with PENDING → INDEXED → READY lifecycle and Day-0 indexing on install events.
- **Scoped BYOK credentials.** Env → per-installation → global resolution (0600); secrets never enter repo state, logs, or knowledge.
- **`truhowl doctor`.** Six-line product readiness: GitHub, AI provider, index, knowledge, test command, monitoring.
- **Fail-closed webhook serving.** Missing secret is a hard error (`--no-secret` is local-debug only).
- **Rename Compart → Truhowl.** Package, CLI, crate, env vars (`TRUHOWL_*`), state dirs (`.truhowl/`), and docs. Hard cut: no `compart` aliases. KB entries under old `.compart/` trees are still read via legacy fallback; re-run `truhowl auth` once to recreate credentials.

### Changed
- **Relicensed Apache-2.0.** The project moves from Elastic License 2.0 to the
  Apache License 2.0. The Truhowl name and logo remain trademarks of Compart
  Labs (see NOTICE). Contributors are covered by CLA.md.
- **Agent Provenance Trailers (spec v0.1).** `truhowl commit` now emits the
  open `Agent-*` trailer names defined in SPEC.md (`Agent-Origin`,
  `Agent-Agent`, `Agent-Execution`, `Agent-Compartment`, `Agent-Sandbox`),
  adding `Agent-Origin` classification and collapsing security detail to the
  spec's `clean`/`blocked` enum so trailers stay grep-queryable. Releases
  prior to 1.1 wrote legacy `Volf-*` names; readers should accept both.
- **SPEC.md.** Open specification for Agent Provenance Trailers - plain git,
  neutral naming, CC0 license text, legacy compatibility mapping.
- **CLA.md.** Contributor license agreement keeping future dual-licensing open.

## [1.0.4] - 2026-08-19

### Added
- **Frozen Public CLI Contract.** Clean grouped CLI surface (`init`, `status`, `inspect`, `claude/opencode/codex/cursor/aider`, `exec`, `-w`, `step`, `--run`, `diff`, `apply`, `commit`, `undo`, `restore`). Suppressed internal plumbing commands (`wrap`, `lanes`, `sessions`, `integrate`) from public `--help`.
- **Primary `--run <workflow>` DAG Command.** Execute multi-step workflow DAGs directly with `compart --run <name>` across isolated compartments (`research`, `builder`, `tester`, `reviewer`).
- **Dual Workflow Discovery.** Discovers workflow YAMLs in both `workflows/<name>.yaml` and `.compart/workflows/<name>.yaml`.
- **Hostile Concurrency Test Suite.** Added canonical "Two Agents, Two Realities" integration tests proving simultaneous multi-agent policy isolation, process tree inheritance, and snapshot collision immunity.

## [1.0.3] - 2026-08-18

### Added
- **Interactive PTY Supervision.** Raw terminal PTY supervisor supporting true native TUI fidelity, ANSI color, alternate screen, and window resize events.
- **Git Provenance Trailers.** `compart commit` embeds structured RFC-5322 metadata trailers (`Compart-Execution`, `Compart-Agent`, `Compart-Compartment`, `Compart-Security: clean`).
- **Instant Physical Rollback.** `compart undo` restores pre-execution BLAKE3 hash snapshots in ~2 milliseconds.

### Fixed

- **Version drift.** `compart.__version__` is now read from installed
  package metadata instead of a hardcoded constant that lagged behind
  releases.

## [0.9.3] - 2026-08-02

### Removed

- **Dead code culled.** Dropped the unused C ABI (`src/c_api.rs`,
  `include/compart.h`), the credential proxy's unused header-injection
  machinery, the `profile` config knob, and a re-export shim module.
  No public Python/TS API change.

## [0.9.2] - 2026-08-01

### Removed

- **Go SDK removed.** The `sdk/go/` module was dropped; Compart now ships
  Python and TypeScript SDKs only. `Cargo.toml` no longer emits a
  `staticlib` archive, `include/compart.h` no longer references the Go
  bindings, and all Go code samples were stripped from the docs.

### Changed

- **Product rebrand.** The README is now a product landing page rather than a
  technical writeup: a one-line hero promise ("Sandbox any AI agent in
  seconds"), a "Why Compart" section, benefit-led features, real use cases
  (coding agents, builds/tests, deploys, pipelines), and an honest security
  model. SDK, CLI, and package metadata descriptions now carry the same
  product voice.
- **CLI branding.** `compart --version` and `--help` now surface the
  tagline, and `compart run`/`why` print a brand banner on interactive
  terminals. Non-TTY output stays machine-friendly for scripts and CI.
- **TypeScript SDK package metadata.** `@compart/sdk` descriptions updated;
  the publish layout is trimmed to the shipped macOS platform packages
  (`darwin-arm64`, `darwin-x64`) at version `0.9.2`.
- **Version bumped to `0.9.2`** across the Rust core, Python package, and
  TypeScript SDK to publish the rebranded build to PyPI.

## [0.9.1] - 2026-07-31

### Fixed

- **CLI: `compart run` now prints command output.** The compartment
  function returned a `subprocess.CompletedProcess` while the CLI only
  printed dict results, so `stdout`/`stderr` were silently swallowed. The
  CLI now captures and prints `Stdout:`/`Stderr:` blocks after the run
  summary.
- **CLI: non-zero command exit codes now surface.** `compart run "exit 3"`
  previously reported `Status: success`; the CLI now exits with status `1`
  when the shell command fails.
- **Credential proxy: absolute-form (`HTTP_PROXY`) requests now get
  credential-injected.** Route matching now uses the path component of the
  request target, so both origin-form (`/openai/v1/chat`) and absolute-form
  (`http://host/openai/v1/chat`, as sent by `HTTP_PROXY` clients) are
  matched and rewritten. Query strings survive the rewrite.
- **TypeScript SDK: fixed compile error.** `Runtime::names()` in the napi
  wrapper called a method that had been removed from the Rust `Runtime`
  struct; the `names()` method was restored, so `npm run build` and
  `npm test` pass again.

### Changed

- **Docs restyled.** The README and SDK docs now follow the structure used
  by top YC developer-tool projects: a one-line value proposition, an
  above-the-fold quickstart, a "Why" section, a feature table, and a
  professional footer. The stale `compart.runtime` import in the
  "Advanced Users" example was replaced with the correct
  `compart.compartments` subclass pattern.
- **Tests import the installed package.** The pytest `pythonpath = ["python"]`
  config was removed because the source tree no longer contains a compiled
  native core (`_core` ships inside the wheel). Install the package
  (`pip install .`) before running the test suite.
- **Test isolation.** `Box.enter()` calls in the box-lifecycle tests now pass
  `sandbox=False`, so the irreversible kernel Seatbelt sandbox is no longer
  applied to the shared pytest process (which poisoned `os.getcwd()` for
  later tests).

### Added

- `tests/test_cli.py` - CLI regression tests (stdout/stderr printing, exit
  codes, `goal` positional, `why`).
- `tests/test_proxy.py` - credential proxy regression tests (origin-form,
  absolute-form, no-match pass-through, query preservation).

### Verified

- Python: 107 tests pass (installed wheel).
- Rust core: 423 tests pass.
- Go SDK: `go vet` clean, 15 tests pass.
- TypeScript SDK: builds and all smoke tests pass.
