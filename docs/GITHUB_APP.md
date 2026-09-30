# Truhowl GitHub App: Customer Onboarding & Bot Behavior

> One agent, two authorities. `Consult` explains and files Issues. `Work` repairs and opens PRs. The reasoning engine is identical; only what it may touch differs.

## Customer Onboarding (Zero Infrastructure Setup)

For developers and engineering teams, adding Truhowl to a GitHub repository is completely automated:

```text
GitHub
  ↓
Install Truhowl GitHub App on repo
  ↓
Choose repository
  ↓
Truhowl runs Day-0 AST evidence scan
  ↓
Connect AI provider reasoning key (BYOK)
  ↓
Repository status: READY
```

Developers **never** generate private keys, download PEM files, or configure webhook secrets. The hosted GitHub App manages repository permissions directly.

---

## 1. Events handled

| Event | Behavior |
|---|---|
| `pull_request.opened/synchronize/reopened` | Full pipeline on the exact PR head SHA (fetched via `pull/N/head`); falls back to the tracked branch with an explicit `[checkout: tracked branch, PR head unfetchable]` marker — never silently claimed |
| `issue_comment.created` with `@truhowl` | Re-runs the pipeline on the PR |
| `issue_comment.created` with `@truhowl explain` | Posts read-only impact reasoning; modifies nothing |
| Other comments, bot comments, non-PR comments | Ignored |
| `installation.*` / `installation_repositories.*` | Persist record → clone → Day-0 index → `INDEXED`, then `READY` once surfaced |
| `external.change.*` | Watch-loop findings enter the shared pipeline |

## 2. PR comment anatomy

Every PR comment opens with a summary (what changed, who it affects, evidence-grounded confidence — `high` only when verified green), followed by the trust body or findings, a mermaid `change → files → verification` diagram when findings exist, and a footer with the reviewed commit SHA plus a `@truhowl` re-run note.

Findings carry severity badges: **P0** needs a human (unrepairable/quarantined), **P1** is repairable. Verified repairs keep `[VERIFIED]` semantics: real command, real exit 0, zero unintended files — otherwise the badge never appears.

## 3. Bot configuration (`.truhowl/config.yaml`)

```yaml
bot:
  mode: consult            # consult = report only, work = repair (default)
  pr_review: true
  pr_auto_fix: false
  external_auto_fix: false
  auto_fix_providers: []   # empty = all providers allowed
  ignore_paths: ["docs/**", "*.md"]
  exclude_labels: ["dependencies"]
  always_report_clean: true
  inline_comments: true
```

PRs touching only `ignore_paths`, or carrying an `exclude_labels` label, are skipped with a recorded reason. Unknown `mode` values fall back to `work`.

## 4. Adoption ladder

New installations start in `consult`: accurate Issues build trust in the reasoning before anyone grants repair authority. Flip one line to `work` when ready — the engine never changes.

## 5. Two Product Modes: Consult & Work (Personas: Howl & Hunt)

Truhowl cleanly defines its product abstractions:
- **Consult (`@howl` / `truhowl consult`)**: Explains maintenance problems with deep AI reasoning. For any detected maintenance problem (dependency drift, contract breaking bump, external change), Consult files a **GitHub Issue** detailing what changed, what is affected, why, what should change, and what must NOT change. When invoked on a PR (`@howl`), it provides an advisory impact breakdown on the PR thread. Consult is strictly read-only: its GitHub client possesses read-only permissions and **never modifies files, never commits, and never opens PRs**.
- **Migrate (`truhowl migrate <id>`, deprecated alias `truhowl hunt`)**: Autonomous repair worker with repository write authority. Reasons with AI, authors the patch with AI, executes the real test suite in an isolated checkout, and delivers a verified merge-ready **GitHub PR** with BLAKE3 cryptographic receipts only on a sealed, green, scope-clean repair. Anything else fails closed without opening a PR.

## 6. Anatomy of a review

When a pull request opens against a monitored repository, Truhowl:

1. **Checks out the exact PR head** (falls back to the tracked branch with a disclosed marker if the fetch fails).
2. **Scans for contract impact** — dependency drift mapped to callsites, zero model calls.
3. **Reasons and repairs** (Work) or **assesses impact** (Consult: posts an advisory comment on the PR thread for PR-triggered events, and files a GitHub Issue on watch/scheduled drift events).
4. **Verifies** — real test command, real exit code, zero unintended files, or no merge-ready claim.
5. **Posts** the summary comment: header, findings with P0/P1 badges, change diagram, evidence, footer.

## 7. Troubleshooting

- **No comment appeared**: check the webhook deliveries tab for failures, confirm the secret matches `TRUHOWL_WEBHOOK_SECRET`, verify the repo reached READY (`truhowl doctor`), and confirm the PR isn't filtered by `ignore_paths` or `exclude_labels`.
- **Stale results**: comment `@truhowl` on the PR to re-run against the current head.
- **REFUSED / NOT RUN**: the bot found impact it cannot safely repair (often missing AI credentials or no test suite). Run `truhowl doctor` for the exact missing piece.

## 8. Deployment

See [DEPLOY.md](DEPLOY.md) for the webhook secret requirement, environment table, Docker image, `--watch` background monitoring, volumes, and security notes.
