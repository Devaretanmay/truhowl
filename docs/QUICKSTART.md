# Truhowl Quickstart Guide

Get up and running with Truhowl in under 2 minutes.

> **“Truhowl understands the changes the outside world makes to software — and repairs them.”**

---

## 1. Installation

```bash
pip install truhowl
```

---

## 2. Onboarding (in under 10 seconds)

```bash
cd my-project

truhowl init              # Detects repo, checks GitHub & AI, runs AST evidence scan
truhowl login             # Connect your BYOK AI provider (Anthropic, OpenAI, Ollama)
```

Outputs your repository readiness:

```text
[OK] GitHub connected (account: Devaretanmay)
[OK] Repository detected: acme/payments
[OK] Repository indexed (3 providers detected, 14 callsites mapped)
[OK] AI provider: Anthropic (claude-3-5-sonnet)
[OK] Maintenance memory initialized (.truhowl/knowledge/)

READY

Truhowl can now:
  Check   — detect breaking SDK/API drift (truhowl check)
  Migrate — repair breaking upgrades and verify PRs (truhowl migrate <finding>)
  Consult — find and explain maintenance issues (truhowl consult)
```

---

## 3. Day-0 Dependency Check & Risk Register

Immediately scan your codebase for breaking upstream changes, deprecated callsites, and auto-repairable integrations. Read-only — works with no AI credentials configured:

```bash
# Run terminal risk register:
truhowl check .

# Export as GitHub Issue markdown:
truhowl check . --format=github-issue

# Inspect the External-Change Dependency Graph:
truhowl graph .
```

---

## 4. Consult First, Then Migrate

New teams start in Consult: same AI reasoning, zero code changes, findings filed
as a GitHub Issue. Graduate to Migrate when the reasoning earns it.

```bash
truhowl consult . --repo owner/repo   # Assess only, files an Issue
truhowl check .                       # note the finding ID, e.g. stripe-3a9c79
truhowl ask "what breaks if we upgrade stripe?"  # Read-only impact answer
truhowl migrate stripe-3a9c79         # Repair, verify, report
```

See [GitHub App behavior](GITHUB_APP.md) for modes, triggers, and bot config.

## 5. Autonomous Continuous Maintenance

Run autonomous maintenance on external providers (e.g. Stripe, OpenAI, Anthropic, Clerk, AWS).
Truhowl's AI reasons about the change against your repository and authors verified repairs —
there is no engine flag to choose. Unsafe repairs refuse loudly with zero files touched:

```bash
# Auto-detect provider and repair:
truhowl migrate .

# Targeted migration and open PR:
truhowl migrate . --provider stripe
truhowl migrate . --provider openai --from v3.28.0 --to v4.0.0 --create-pr --repo owner/repo
```

---

## 6. Interactive Coding Agents & Sandboxed Governance (advanced)

Run terminal coding agents inside a kernel-enforced sandbox with full native TUI fidelity:

```bash
# Launch Claude Code, OpenCode, Codex, Cursor, or Aider directly:
truhowl claude

# When the agent finishes:
truhowl diff    # Review what the agent changed
truhowl undo    # Instantly restore files if the agent made a mistake
truhowl commit  # Commit to Git with verified provenance trailers
```

---

## 7. Key Guarantees

- **External Intelligence**: Full-codebase AST mapping of providers, contracts, wrappers, and callsites.
- **Continuous Maintenance**: AI-authored repairs with sandbox verification and automated Developer Trust PRs (verified repairs only; refusals are loud and empty-handed).
- **Kernel Enforcement**: Built on native OS isolation (macOS Seatbelt / Linux Landlock).
- **Credential Protection**: `~/.ssh`, `~/.aws`, `~/.config/gcloud`, git credentials, and keychains are denied by default.
- **Hash-Verified Rollback**: BLAKE3 snapshots taken before execution restore modified and deleted files deterministically.
- **Zero Infrastructure**: No Docker, no daemon, no cloud account required.
