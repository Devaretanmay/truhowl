# Truhowl Workspace Initialization & Agent Execution

## 1. What It Is

When you run `truhowl init`, Truhowl turns your project directory into a **managed agent workspace**. You launch your favorite agent directly inside an isolated kernel sandbox.

---

## 2. How It Works

```text
truhowl init
└── creates .truhowl/
    ├── config.yaml    <- workspace compartment policy
    ├── state/         <- runtime state
    ├── snapshots/     <- BLAKE3 worktree diff snapshots
    └── executions/    <- execution records

Direct Execution:
  $ truhowl claude      -> Launches Claude Code in kernel sandbox
  $ truhowl opencode    -> Launches OpenCode in kernel sandbox
  $ truhowl codex       -> Launches Codex in kernel sandbox
  $ truhowl cursor      -> Launches Cursor in kernel sandbox
  $ truhowl aider       -> Launches Aider in kernel sandbox
```

---

## 3. Running Interactive Coding Agents

```bash
truhowl claude
truhowl opencode
truhowl codex
truhowl cursor
truhowl aider
```

Each interactive agent runs with:
- Full native TUI support (colors, alternate screen, Ctrl+C, Ctrl+D, window resize).
- Hard OS-level kernel isolation (Seatbelt on macOS / Landlock on Linux).
- Deny-by-default credential protection (`~/.ssh`, `~/.aws`, `~/.config/gcloud` blocked).
- Automatic BLAKE3 pre-execution snapshots for physical instant rollback (`truhowl undo`).

---

## 4. Checking Workspace Health

```bash
truhowl status
```

```text
================================================================================
                              TRUHOWL STATUS
================================================================================

GitHub:             CONNECTED (Devaretanmay)
AI:                 CONNECTED (groq)
Active repo:        acme/checkout-service
Repositories:       3
Repository Key:     kyp_da1358315f6c9d1ad8791cbf8cb9
Consult (advisory): AVAILABLE
Migrate (repair):   AVAILABLE
Status:             READY

================================================================================
```

---

## 5. Running Multi-Agent Workflows

Run a declared workflow DAG:

```bash
truhowl --run invoice-pipeline
```

Or run standalone Python agent scripts:

```bash
truhowl exec --compartment research -- python3 scraper.py
```
