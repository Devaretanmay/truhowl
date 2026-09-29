# Koyote CLI Reference & User Guide

Koyote is an autonomous SDK/API migration worker that repairs breaking upgrades and proves the migration works before opening a PR.

> **"Koyote understands the changes the outside world makes to software — and repairs them."**

---

## The Public CLI Contract

```text
Workflow:
  koyote check [path]             Detect breaking SDK/API drift and show affected usage
  koyote hunt <finding|provider>  Plan, edit, build, test, repair, and prepare/open PR
  koyote consult [path]           Analyze drift and file a GitHub issue (report-only)

Configuration & Diagnostics:
  koyote auth                     Connect & configure AI provider credentials (OpenAI, Anthropic, Groq, etc.)
  koyote doctor                   Verify environment, credentials, test runner, and repository health
  koyote status                   Show current workspace, repository context, and provider status

Advanced / Plumbing:
  koyote graph [path]             Inspect external dependency graph and callsites
  koyote diff                     Inspect unapplied/recorded agent execution changes
  koyote undo                     Reverse the last applied change set
  koyote providers                List supported providers and migration contract catalog
  koyote app                      Manage GitHub App webhook server daemon
```

---

## 1. Core Migration Workflow

### `koyote check [path]` (aliases: `scan`, `audit`)
Scans package manifests, lockfiles, and code callsites to detect breaking SDK/API drift and display affected repository locations.

- **Zero Tokens**: Runs purely through static manifest inspection and native AST callsite mapping.
- **Zero File Changes**: Read-only; does not modify workspace files.
- **Finding IDs**: Each identified breaking change or deprecation receives a deterministic finding ID (e.g., `stripe-df9562`, `openai-3a9c79`).

```bash
koyote check .
koyote check . --format=github-issue    # Markdown formatted for GitHub Issues
koyote check . --format=json            # Machine-readable JSON risk register
koyote check . --write-graph            # Persists AST graph to .koyote/graph.json
```

---

### `koyote hunt <finding-id>` (aliases: `fix`, `maintain`, `update`, `work`, `@hunt`)
Autonomous migration worker. Resolves the finding, gathers migration guidance and affected callsites, formulates an AI repair plan, edits code, runs compiler/test checks in an isolated sandbox, repairs test failures, verifies scope boundaries, and prepares or opens a PR.

```bash
# Repair from a finding ID discovered by koyote check
koyote hunt stripe-df9562

# Create a pull request once tests pass
koyote hunt stripe-df9562 --create-pr --repo owner/repo

# Provider-driven targeting (alternative invocation)
koyote hunt --provider stripe
koyote hunt --provider openai --from v3.28.0 --to v4.0.0
```

#### The Hunt Guarantees
1. **AI-Authored Repair**: Reasons about affected callsites and applies surgical SEARCH/REPLACE edits.
2. **Compiler & Test Feedback Loop**: Captures structured traceback, compiler, and pytest diagnostics to re-prompt the AI if initial edits fail tests.
3. **Isolated Verification**: Executes in an isolated sandbox with the project's genuine build and test commands.
4. **Scope-Enforced**: Any unapproved file edits cause an immediate fail-closed abort.
5. **Fail-Closed on Missing Tests**: If no test command exists or tests cannot run, Hunt refuses loudly and creates no PR.

---

### `koyote consult [path]` (aliases: `howl`, `@howl`)
Advisory mode. Reasons through breaking changes, diagnoses architectural impact, and generates a structured GitHub Issue without modifying repository code.

```bash
koyote consult .
koyote consult . --repo owner/repo
```

---

## 2. Configuration & Diagnostics

### `koyote auth`
Configures customer BYOK (Bring Your Own Key) AI provider credentials.

```bash
koyote auth                             # Interactive setup
koyote auth --provider anthropic --api-key sk-ant-...
koyote auth --status                    # Display active provider and key mask
koyote auth --clear                     # Remove stored credentials
```

Stored securely in `~/.koyote/credentials.json` with `0600` permissions.

---

### `koyote doctor`
Performs an end-to-end system health check verifying:
- Host platform and kernel sandboxing capabilities.
- AI provider configuration and connectivity.
- Repository status and detected test runner commands.
- Knowledge base and historical repair ledger state.

```bash
koyote doctor
```

---

### `koyote status`
Displays the active workspace context, repository registration, detected test commands, and configured provider.

```bash
koyote status
```

---

## 3. Sandboxing & Supported Platforms

Koyote executes test suites and build scripts under strict kernel sandboxing to prevent credential exfiltration or side effects:

- **macOS**: Kernel isolation enforced via Seatbelt (`sandbox-exec` profiles) restricting filesystem and network access.
- **Linux**: Kernel isolation enforced via Landlock LSM system calls restricting directory hierarchies.
- **Fail-Closed Fallback**: If running on an unsupported platform or in an unverified sandbox environment, Koyote enforces strict execution boundaries or fails closed rather than running unconfined.
- **2ms Instant Undo**: Pre-execution BLAKE3 hash snapshots enable physical rollback of all modified files within 2 milliseconds.

---

## 4. Advanced & Plumbing Commands

### `koyote graph [path]`
Inspects the repository's external dependency graph (providers, contracts, manifest dependencies, wrapper clients, and AST callsites):

```bash
koyote graph .
koyote graph . --json
```

---

### `koyote diff`
Inspects unapplied or recorded execution change sets and RFC-5322 metadata trailers.

```bash
koyote diff
koyote diff --trailers
```

---

### `koyote undo`
Instantly rolls back workspace changes to the pre-execution BLAKE3 hash snapshot.

```bash
koyote undo
```

---

### `koyote providers`
Lists the built-in provider contract registry and available breaking-change migration specifications.

```bash
koyote providers
koyote providers --json
```

---

### `koyote app [serve|status]`
Runs the GitHub App continuous webhook listener daemon for automated PR drift detection and verification.

```bash
koyote app serve --port 8080 --secret $KOYOTE_WEBHOOK_SECRET
```

---

## 5. Deprecated Commands

> **Note:** Direct coding-agent wrappers (`koyote claude`, `koyote opencode`, `koyote codex`, `koyote cursor`, `koyote aider`) and generic workflow pipelines (`koyote step`, `koyote run`) are legacy utilities retained for backward compatibility. They are not part of the primary migration product workflow and emit a deprecation notice when invoked.
