# Truhowl CLI Reference & User Guide

Truhowl is an autonomous SDK/API migration worker that repairs breaking upgrades and proves the migration works before opening a PR.

> **"Truhowl understands the changes the outside world makes to software — and repairs them."**

---

## The Public CLI Contract

```text
Workflow:
  truhowl status [path]            Show repos, dependencies, and maintenance state
  truhowl ask "question"           Ask about impact, failures, or readiness (read-only)
  truhowl check [path]             Detect breaking SDK/API drift and show affected usage
  truhowl migrate <finding|provider>  Plan, edit, build, test, repair, and prepare/open PR
  truhowl consult [path]           Analyze drift and file a GitHub issue (report-only)

Configuration & Diagnostics:
  truhowl login                    Connect & configure AI provider credentials (OpenAI, Anthropic, Groq, etc.)
  truhowl auth                     Same as login (all auth flags accepted)
  truhowl doctor                   Verify environment, credentials, test runner, and repository health
  truhowl status                   Show current workspace, repository context, and provider status

Advanced / Plumbing:
  truhowl graph [path]             Inspect external dependency graph and callsites
  truhowl diff                     Inspect unapplied/recorded agent execution changes
  truhowl undo                     Reverse the last applied change set
  truhowl providers                List supported providers and migration contract catalog
  truhowl app                      Manage GitHub App webhook server daemon
```

> `hunt` is a deprecated alias for `migrate` and still works.

---

## 1. Core Migration Workflow

### `truhowl check [path]` (aliases: `scan`, `audit`)
Scans package manifests, lockfiles, and code callsites to detect breaking SDK/API drift and display affected repository locations.

- **Zero Tokens**: Runs purely through static manifest inspection and native AST callsite mapping.
- **Zero File Changes**: Read-only; does not modify workspace files.
- **Finding IDs**: Each identified breaking change or deprecation receives a deterministic finding ID (e.g., `stripe-df9562`, `openai-3a9c79`).

```bash
truhowl check .
truhowl check . --format=github-issue    # Markdown formatted for GitHub Issues
truhowl check . --format=json            # Machine-readable JSON risk register
truhowl check . --write-graph            # Persists AST graph to .truhowl/graph.json
```

---

### `truhowl migrate <finding-id>` (aliases: `fix`, `maintain`, `update`; deprecated alias: `hunt`)
Autonomous migration worker. Resolves the finding, gathers migration guidance and affected callsites, formulates an AI repair plan, edits code, runs compiler/test checks in an isolated sandbox, repairs test failures, verifies scope boundaries, and prepares or opens a PR.

```bash
# Repair from a finding ID discovered by truhowl check
truhowl migrate stripe-df9562

# Create a pull request once tests pass
truhowl migrate stripe-df9562 --create-pr --repo owner/repo

# Provider-driven targeting (alternative invocation)
truhowl migrate --provider stripe
truhowl migrate --provider openai --from v3.28.0 --to v4.0.0
```

#### The Migration Guarantees
1. **AI-Authored Repair**: Reasons about affected callsites and applies surgical SEARCH/REPLACE edits.
2. **Compiler & Test Feedback Loop**: Captures structured traceback, compiler, and pytest diagnostics to re-prompt the AI if initial edits fail tests.
3. **Isolated Verification**: Executes in an isolated sandbox with the project's genuine build and test commands.
4. **Scope-Enforced**: Any unapproved file edits cause an immediate fail-closed abort.
5. **Fail-Closed on Missing Tests**: If no test command exists or tests cannot run, migration refuses loudly and creates no PR.

---

### `truhowl consult [path]` (aliases: `howl`, `@howl`)
Advisory mode. Reasons through breaking changes, diagnoses architectural impact, and generates a structured GitHub Issue without modifying repository code.

```bash
truhowl consult .
truhowl consult . --repo owner/repo
```

---

### `truhowl ask "question"`
Answers maintenance questions from local state. Read-only; needs no AI key.

```bash
truhowl ask "what breaks if we upgrade stripe?"
truhowl ask "why did the billing migration fail?"
truhowl ask "are we ready to migrate openai?"
truhowl ask "what breaks if we upgrade stripe?" --json
```

---

## 2. Configuration & Diagnostics

### `truhowl login` / `truhowl auth`
Configures customer BYOK (Bring Your Own Key) AI provider credentials. `login` and `auth` accept the same flags.

```bash
truhowl login                            # Interactive setup
truhowl login --provider anthropic --api-key sk-ant-...
truhowl auth --status                    # Display active provider and key mask
truhowl auth --clear                     # Remove stored credentials
```

Stored securely in `~/.truhowl/credentials.json` with `0600` permissions.

---

### `truhowl doctor`
Performs an end-to-end system health check verifying:
- Host platform and kernel sandboxing capabilities.
- AI provider configuration and connectivity.
- Repository status and detected test runner commands.
- Knowledge base and historical repair ledger state.

```bash
truhowl doctor
```

---

### `truhowl status`
Displays the active workspace context, repository registration, detected test commands, and configured provider.

```bash
truhowl status
```

---

## 3. Sandboxing & Supported Platforms

Truhowl executes test suites and build scripts under strict kernel sandboxing to prevent credential exfiltration or side effects:

- **macOS**: Kernel isolation enforced via Seatbelt (`sandbox-exec` profiles) restricting filesystem and network access.
- **Linux**: Kernel isolation enforced via Landlock LSM system calls restricting directory hierarchies.
- **Fail-Closed Fallback**: If running on an unsupported platform or in an unverified sandbox environment, Truhowl enforces strict execution boundaries or fails closed rather than running unconfined.
- **2ms Instant Undo**: Pre-execution BLAKE3 hash snapshots enable physical rollback of all modified files within 2 milliseconds.

---

## 4. Advanced & Plumbing Commands

### `truhowl graph [path]`
Inspects the repository's external dependency graph (providers, contracts, manifest dependencies, wrapper clients, and AST callsites):

```bash
truhowl graph .
truhowl graph . --json
```

---

### `truhowl diff`
Inspects unapplied or recorded execution change sets and RFC-5322 metadata trailers.

```bash
truhowl diff
truhowl diff --trailers
```

---

### `truhowl undo`
Instantly rolls back workspace changes to the pre-execution BLAKE3 hash snapshot.

```bash
truhowl undo
```

---

### `truhowl providers`
Lists the built-in provider contract registry and available breaking-change migration specifications.

```bash
truhowl providers
truhowl providers --json
```

---

### `truhowl app [serve|status]`
Runs the GitHub App continuous webhook listener daemon for automated PR drift detection and verification.

```bash
truhowl app serve --port 8080 --secret $TRUHOWL_WEBHOOK_SECRET
```

---

## 5. Deprecated Commands

> **Note:** Direct coding-agent wrappers (`truhowl claude`, `truhowl opencode`, `truhowl codex`, `truhowl cursor`, `truhowl aider`) and generic workflow pipelines (`truhowl step`, `truhowl run`) are legacy utilities retained for backward compatibility. They are not part of the primary migration product workflow and emit a deprecation notice when invoked.
