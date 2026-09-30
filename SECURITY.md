# Truhowl Security Architecture & Policy (B5)

## 1. Vulnerability Reporting

If you discover a security vulnerability or credential handling concern within Truhowl, please report it privately:
- **Email**: `security@truhowl.dev`
- **GitHub**: Private Security Advisory via the repository's Security tab

Please do **not** disclose security vulnerabilities through public GitHub issues or forum discussions. All reports receive an acknowledgment within 24 hours and priority triage.

---

## 2. Core Security Guarantees

Truhowl is engineered around a zero-trust model for autonomous AI maintenance:

### A. Kernel-Level Syscall Isolation
Untrusted agent repair attempts and test commands execute inside strict kernel compartments where supported:
- **macOS**: Sandboxed with Apple Seatbelt SBPL (`sandbox_init`).
- **Linux**: Sandboxed via Landlock LSM and Linux network/PID namespaces (Linux >= 5.13).
- **Fallback**: On environments where kernel LSM primitives are restricted by host container runtime policies, execution falls back to process-level execution isolation with restricted environment variables and strict timeouts.
- Outbound network requests, unauthorized disk traversals, and privilege escalations are denied by default during sandboxed verification.

### B. Universal Clean-Room Replay
No repair can achieve a `VERIFIED` state or produce a pull request without passing clean-room replay verification:
1. Candidate changes are isolated and hashed (`SHA-256`).
2. The pristine baseline workspace is completely restored.
3. The candidate patch is re-applied from scratch.
4. Independent test runner replay re-verifies exit code 0.
5. Strict scope evaluation confirms no files outside declared dependency boundaries were modified.

---

## 3. What Truhowl Accesses and Why

Truhowl operates strictly on the principle of least privilege:

| Resource | Access Level | Rationale |
| :--- | :--- | :--- |
| **Package Manifests** (`package.json`, `pyproject.toml`, etc.) | Read-only | To identify installed dependency versions and detect upstream releases. |
| **Callsite AST Nodes** | Read-only | To map imports and usage patterns matching breaking SDK/API changes. |
| **Local Test Commands** | Local Execution | To execute declared verification commands (`npm test`, `pytest`) in sandbox. |
| **Git Branches & PRs** | Write (opt-in) | Only under `DELIVER` automation policy to publish verified migrations. |

Truhowl **never** scans, indexes, or transmits unrelated business logic, environment secrets, customer databases, or proprietary assets.

---

## 4. Credential Storage & Redaction

- **GitHub App Credentials**: GitHub App private keys (`TRUHOWL_GITHUB_PRIVATE_KEY`), client secrets, and installation tokens are held exclusively in runtime memory or secret vaults. They are never written to repository files, JSON stores, or client bundles.
- **Model Credentials (BYOK)**: Model API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`) remain under customer control.
- **Multi-Vendor Secret Redaction**: Truhowl automatically sanitizes all exception traces, diffs, PR bodies, commit messages, and agent logs via `truhowl.redact.redact_secrets`. Patterns matching GitHub PATs, AWS access keys, OpenAI/Anthropic keys, Slack tokens, and RSA private keys are scrubbed before reaching any persistence layer.
- **Session Security**: Hosted control plane sessions use cryptographically signed HMAC tokens stored in `HttpOnly`, `SameSite=Lax`, secure cookies with strict tenant isolation.

---

## 5. What Code Leaves Your Repository

Truhowl does **not** ingest or transmit entire repositories to external AI models:
- Only isolated, AST-extracted code snippets containing the specific breaking callsite and its immediate enclosing function are shared with the configured LLM provider.
- Context is strictly bounded to the migration plan.
- Upstream verification runs locally; test stdout/stderr containing proprietary output is not sent to external models.

---

## 6. LLM Providers & Data Retention

- **Supported Providers**: Anthropic Claude, OpenAI, Groq, and local Ollama instances.
- **Data Retention**: Under commercial API terms, code snippets sent to Anthropic and OpenAI are not retained for training or product improvement.
- **Local Isolation**: For air-gapped or regulated environments, Truhowl supports local model inference (e.g. Ollama / vLLM) where zero network traffic leaves the host.

---

## 7. How to Disconnect and Uninstall

You maintain absolute operational control at all times:
1. **GitHub App Uninstall**: Navigate to `GitHub Settings -> Applications -> Truhowl -> Uninstall`. All webhook subscriptions and access tokens are immediately revoked by GitHub.
2. **Local Repository Cleanup**: Run:
   ```bash
   truhowl clean
   rm -rf .truhowl/
   ```
   All local cached states, knowledge indexes, and session files are completely deleted.
