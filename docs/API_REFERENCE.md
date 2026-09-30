# Truhowl Python & Engine API Reference

**Version:** 1.1.3  
**Package:** `truhowl` (PyPI)

---

## 1. Documentation Index

- **[Quickstart Guide](QUICKSTART.md)**: 2-minute quickstart guide for CLI and Python workflows.
- **[CLI Reference Guide](CLI.md)**: Complete guide to the frozen public CLI contract (`init`, `status`, `inspect`, `claude`, `opencode`, `codex`, `cursor`, `aider`, `exec`, `-w`, `step`, `--run`, `diff`, `apply`, `commit`, `undo`, `restore`).
- **[Agent Execution & TUI Supervision](AGENT_EXECUTION.md)**: Details on PTY terminal supervision, interactive coding agents, and kernel isolation.
- **[Zero-Trust Credential Proxy](CREDENTIAL_PROXY.md)**: Safe API key injection and request routing without exposing raw secrets.
- **[BLAKE3 Snapshots & Rollback](SNAPSHOTS.md)**: Fast workspace hashing, diff tracking, and physical restoration with `truhowl undo`.
- **[Output Compression & Token Crushing](COMPRESSION.md)**: High-speed Rust token reduction engines (`SmartCrusher`, `LogCompressor`, `DiffCompressor`).
- **[TypeScript & Node.js SDK](TYPESCRIPT_SDK.md)**: Native NAPI-RS bindings and TypeScript API reference.
- **[Use Cases & Working Examples](USE_CASES.md)**: Practical security scenarios, prompt injection defense, and REPL sandboxing patterns.

---

## 2. Core Python SDK Classes

### `Truhowl(workdir=".", config=None, verbose=False)`
Base compartment container for custom agent pipelines.
- `add(compartment: Compartment) -> Truhowl`: Register an inner isolated compartment.
- `edge(from_name: str, to_name: str) -> Truhowl`: Wire a directional dependency/communication path.
- `enable_snapshot() / enable_credential_proxy() / enable_compression() -> Truhowl`: Opt-in to insulation directly.
- `run(entry=None, request="") -> TruhowlResult`: Execute the topology under OS kernel isolation.

### `AgentTruhowl(workdir=".", config=None, verbose=False)`
Agent-oriented outer compartment container. Automatically enables insulation (Credential Proxy, Snapshots, Compression).

### `Compartment(name, fn=None, config=None)`
An individual unit of work executed in a specific kernel sandbox.
- `deliver(message: Message)`: Queue an inbound message.
- `receive() -> list[Message]`: Retrieve pending messages.
- `run(ctx: CompartmentContext)`: Execute compartment logic.

### `CompartmentConfig`
Configuration dataclass defining isolation rules:
- `permissions`: List of permissions (`"fs_read"`, `"fs_write"`, `"fs_exec"`, `"network"`).
- `filesystem`: Filesystem access mode (`"workspace"`, `"read-only"`, `"read-write"`, `"blocked"`).
- `network`: Network mode (`"allowed"`, `"restricted"`, `"blocked"`).
- `timeout_s`: Hard execution timeout in seconds.
- `allow_inbound_from`: Allowed source compartment names (`["*"]` for all).
- `allow_outbound_to`: Allowed target compartment names (`["*"]` for all).

### `RouteConfig`
Credential proxy routing rule:
- `prefix`: Path prefix to intercept (e.g. `"/openai"`).
- `upstream`: Target base URL (e.g. `"https://api.openai.com"`).
- `header`: Header name to inject (default `"Authorization"`).
- `format`: Format template (default `"Bearer {credential}"`).
- `credential_source`: Environment variable name (e.g. `"env:OPENAI_API_KEY"`).

### `SandboxRunner(workdir=".", verbose=False, block_network=False)`
Low-level process execution runner that applies kernel sandbox (Seatbelt / Landlock) to shell commands and captures file diffs.
- `run(command: str, permissions=None, env=None) -> ExecutionResult`

---

## 3. External-Change Intelligence & AutoPatch APIs

### `from truhowl import autopatch`
- `plan_maintenance(old_spec: str, new_spec: str, repo_root: str = ".", config: ScanConfig = None) -> MaintenancePlan`: Generates breaking-change diff, scans callsites, and computes patch targets as evidence for AI reasoning.
- `synthesize_contracts(api_name: str, old_ver: str, new_ver: str, specs: List[VerificationSpec], lang: str = "ts") -> str`: Synthesizes Vitest/pytest contract test suites.

### `from truhowl.graph import build_dependency_graph, audit_dependency_graph`
- `build_dependency_graph(repo_root: str = ".") -> Dict[str, Any]`: Constructs the full External Dependency Graph across manifests, wrappers, and AST callsites.
- `audit_dependency_graph(repo_root: str = ".") -> Dict[str, Any]`: Generates structured audit summary (at-risk, watchlist, healthy).

### `from truhowl.maintenance import run_maintenance_cycle, detect_drift`
- `detect_drift(repo_dir: str, provider_name: str) -> List[Dict[str, Any]]`: Scans for outdated external dependencies.
- `run_maintenance_cycle(repo_dir: str, provider_name: str, ...) -> MaintenanceReport`: Runs end-to-end drift detection, AI-authored repair, sandbox verification, and PR creation.

### `from truhowl.pipeline import MaintenancePipeline, PipelinePolicy, TriggerContext`
- `MaintenancePipeline`: Coordinates the full maintenance lifecycle end-to-end (drift detection, impact analysis, planning, sandboxed verification, PR generation).
- `PipelinePolicy`: Execution policy governing maintenance run modes (`work` vs. `consult`), auto-fix providers, ignore paths, and label filters.
- `TriggerContext`: Structured event context describing the trigger source (e.g. pull request, scheduled watch loop, webhook event).

### `from truhowl.ai_planner import AIPatchPlanner`
- `AIPatchPlanner`: Authors surgical repairs via the configured LLM (SEARCH/REPLACE blocks applied by exact string match only — never regex, AST, or template rewrites); consults verified knowledge memory before model calls.

### `from truhowl.maintenance_agents import analyze_impact, ImpactAnalysisResult`
- `analyze_impact(repo_dir, provider_name) -> ImpactAnalysisResult`: Matches the provider identity against wrapper metadata, callsite patterns, and file paths; returns affected files, wrapper files, and matched callsites as evidence.

---

## 4. Change Sources, Decisions, Knowledge & Installations

### `from truhowl.change_source import ChangeSource, Detection`
- `ChangeSource(kind, identity, version_from, version_to, contract_hash, origin)`: kinds `external_api, sdk, openapi, graphql, protobuf, webhook, mcp_server, internal_service`. Helpers: `ChangeSource.sdk(provider, …)`, `.provider`, `.key()`.
- `Detection(source, outcome, reason, affected_files, callsite_count, ai_dependent, confidence)`: outcomes `NO_IMPACT / IMPACT_AI / IMPACT_QUARANTINE` (fail-closed).

### `from truhowl.drift import detect_drift, detect_changes`
- `detect_changes(repo_dir, provider_name=None) -> List[Detection]`: read-only classification — never patches.

### `from truhowl.intelligence import TruhowlIntelligence, Decision, resolve_migration`
- `Decision(strategy, reason, elapsed_ms, provider, from_version, to_version, confidence)`: strategies `AI / QUARANTINE`. AI credentials present → `AI`; absent → `QUARANTINE` (fail-closed, no source modification).
- `decide_for_source(repo_dir, source)`: routes any `ChangeSource`; AI-if-credentials, else quarantine.

### `from truhowl.providers.registry import find_migration_for`
- `find_migration_for(source) -> Optional[ProviderMigration]`: resolves `sdk`/`external_api` sources; all other kinds return `None` by design (no connectors yet).

### `from truhowl.knowledge import lookup, upsert_learned, record_failure, ensure_test_recipe`
- Entries live at `.truhowl/knowledge/{kind}/{identity}/{contract}.json` with legacy provider-path fallback reads. Only verified fixes upsert executable patterns; `record_failure()` quarantines guesses separately.

### `from truhowl.github.installations import record_installation_event, set_repo_state, list_ready_repos`
- Flat-JSON install records (0600): repos tracked PENDING → INDEXED → READY.

### `from truhowl.github.provisioning import ensure_repo_checkout, ensure_pr_checkout, resolve_pr_workdir`
- Managed clone/pull cache; exact PR-head checkout with `(path, exact)` honesty flag.

### `from truhowl.github.watch import watch_once`
- Poll READY repos; run the pipeline only on new drift signatures.

### `from truhowl.github.pr_render import render_consult_issue, render_pr_summary, render_flow_diagram`
- Consult Issue bodies, PR summary headers with evidence-grounded confidence, mermaid change→files→verification diagrams. Severity: P0 needs a human, P1 is repairable.

### `from truhowl.github.howl_bot import HowlBot, ConsultBot`
- `HowlBot` (alias `ConsultBot`): Consult-mode advisor agent that diagnoses drift, assesses impact, and opens GitHub Issues without modifying code.

### `from truhowl.github.hunt_bot import HuntBot, WorkBot`
- `HuntBot` (alias `WorkBot`): Worker/repair agent that detects breaking drift, synthesizes patches, verifies in an OS sandbox, and delivers commits or PRs.

### `BotConfig.mode`
- `consult` (report only) or `work` (repair, default) in `.truhowl/config.yaml`, plus `ignore_paths` / `exclude_labels` PR filters. See [GitHub App behavior](GITHUB_APP.md).

### `from truhowl.credentials import save_credentials, load_credentials, has_valid_credentials`
- All accept optional `(installation_id, repo)` scope: env → scoped file → global file. Credential files are 0600. Secrets are scrubbed before provider submission and audit persistence (see `truhowl.redact`).

---

## 5. Hunt Autonomous Repair

Hunt starts from a `truhowl check` finding ID and runs the full
reasoning → repair → sandbox → verification lifecycle. AI authors every
semantic change; deterministic code provides evidence and execution only.

### `from truhowl.hunt import run_hunt, list_findings, resolve_finding`
- `list_findings(repo_dir) -> List[HuntFinding]`: live re-detection; IDs derive from repo-relative paths so spellings (`/tmp/x` vs `/private/tmp/x`) resolve identically.
- `resolve_finding(repo_dir, ref)`: accepts a finding ID, provider name, or `issue:<n>` reference. Stored IDs are hints; context is always rebuilt live.
- `run_hunt(repo_dir, finding_ref, create_pr=False, github_repo=None, auto_approve_pr=False, max_iterations=3, ports=None, lock_timeout_s=120.0) -> HuntReport`: bounded AI-directed iterations; fails closed with no PR unless a sealed repair verifies green and scope-clean. One Hunt per repository at a time (repo-level lock); concurrent attempts serialize or refuse loudly.

### `from truhowl.hunt_ports import AIAuthoredPatch, VerifiedRepair, seal_ai_patch, seal_verified_repair, HuntPorts`
- `AIAuthoredPatch` (frozen, sealed): constructible only via `seal_ai_patch()`; carries `author="ai"`, model identity, diff, and admission provenance.
- `VerifiedRepair` (frozen capability token): mintable only via `seal_verified_repair()`, which derives acceptance from sealed patches, real command + exit 0, convinced interpretation, and its own scope evaluation. `decide_pr` / `PRPublisher.publish` accept nothing else.
- `HuntPorts`: `ContextProvider / RepairReasoner / PatchAuthor / SandboxProvider / Verifier / RepairInterpreter / PRPublisher` protocols. Future extensions implement protocols; the loop never imports concrete repair modules.

### `from truhowl.redact import redact_secrets, redact_record`
- Heuristic scrubbing of provider keys, tokens, and private-key blocks before LLM submission and audit persistence. Precision over recall; extend centrally, never per-callsite.

