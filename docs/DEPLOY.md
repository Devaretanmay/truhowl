# Self-Hosting the Truhowl GitHub App Daemon (Operator Guide)

> [!NOTE]
> **This guide is for platform operators hosting their own Truhowl webhook daemon.**
> End-user developers do not need to generate GitHub App private keys themselves. Today the App is
> self-hosted — an operator runs this deployment once for the team — and there is no hosted
> Truhowl service to install from yet.

## 1. GitHub App setup

1. Create a GitHub App with permissions: Contents (read/write), Pull requests
   (read/write), Issues (read/write). Subscribe to `pull_request`,
   `installation`, and `installation_repositories` events.
2. Set the webhook URL to `https://<host>:<port>/webhook` with a secret.
3. Install the App on selected repositories.

## 2. Required environment

| Variable | Required | Purpose |
|---|---|---|
| `TRUHOWL_WEBHOOK_SECRET` | **yes** | HMAC validation. The daemon refuses to serve without it (`--no-secret` is local-debug only). |
| `GITHUB_TOKEN` or App `TRUHOWL_GITHUB_APP_ID` + `TRUHOWL_GITHUB_PRIVATE_KEY` | yes for private repos / PR writes | Clone auth and PR comments. Public repos work anonymously for clones. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` | required for code repair | AI is the exclusive patch author. Read-only checks work without a key. |
| `PORT` | no (default 8080) | Listen port. |
| `TRUHOWL_REPOS_DIR`, `TRUHOWL_INSTALLATIONS_DIR` | no | Managed checkouts and install records. Persist both (volume `/data`). |

## 3. Run with Docker

```bash
docker build -f docker/Dockerfile.app -t truhowl-app:1.2.0 .
docker run -d --name truhowl -p 8080:8080 --env-file .env -v truhowl-data:/data truhowl-app:1.2.0
```

With background monitoring (poll READY repos every 5 minutes):

```bash
docker run -d --name truhowl -p 8080:8080 --env-file .env -v truhowl-data:/data \
  truhowl-app:1.2.0 sh -c "truhowl app serve --port ${PORT:-8080} --watch 300"
```

## 4. Run on bare metal

```bash
pip install truhowl
export TRUHOWL_WEBHOOK_SECRET=... GITHUB_TOKEN=...
truhowl app serve --port 8080 --watch 300
```

## 5. Verify

- `GET /health` → `{"status": "healthy"}`.
- `truhowl doctor` on the host shows GitHub CONNECTED once token env is set.
- Install the App on a test repo: an onboarding issue appears and the repo
  reaches READY; open a PR touching a migrated SDK to see the contract guard.

## 6. Security notes

- Missing webhook secret is a hard startup error, not a warning.
- Git credentials for private clones travel via `http.extraHeader`, never
  written to `.git/config`. AI keys live in 0600 files, never in repo state,
  logs, or knowledge (covered by `tests/test_credential_scoping.py`).
- Plaintext local credential files are operational storage, not enterprise
  encryption — say so in customer docs.

## 7. Secrets policy

- **Local CLI:** `~/.truhowl/credentials.json` (0600) plus per-installation
  scoped files. Operational convenience, explicitly not enterprise-grade.
- **Hosted daemon:** environment-injected secrets only (`TRUHOWL_WEBHOOK_SECRET`,
  `GITHUB_TOKEN`/App credentials, provider keys). The resolver already checks
  env first, files second — deploy with env and no credential files exist.
- **Never:** secrets in Git, logs, knowledge entries, PR bodies, or evidence
  bundles. A failing test (`test_credential_scoping.py`) guards the repo-state
  half of this; treat any violation as a release blocker.
