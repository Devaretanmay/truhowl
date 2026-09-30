# Agent Provenance Trailers - Specification

Version: 0.1 (draft)
Status: open for comment
Maintainer: Truhowl Labs

## Why this exists

AI coding agents now write a meaningful share of the world's code, and git
records none of it. A commit made by an autonomous agent looks identical to a
commit made by a human at 2am. Teams can't audit agent output. Compliance
teams can't answer "which code was machine-written?" Security teams can't
scope an incident to "everything that one run touched."

This spec fixes that with something git already understands: commit trailers.

No new tools are required to read these trailers. `git log`, GitHub, GitLab,
and every code host render them today. Any tool - Truhowl, your CI, someone
else's agent harness - can write them. That's the point.

## Design rules

1. **Plain git.** Trailers follow RFC-5322 semantics as implemented by
   `git interpret-trailers`. No hooks required to read, no forge integration
   required to display.
2. **Human commits stay clean.** Tools should never add trailers to commits a
   human authored without agent assistance.
3. **Neutral naming.** Fields use the `Agent-` prefix so no single vendor owns
   the namespace.
4. **Fail open.** Absence of trailers means "unknown provenance," not "human."
   Consumers must treat missing data as unknown.

## The fields

| Field | Required | Value |
| :--- | :--- | :--- |
| `Agent-Origin` | yes | `agent` if fully autonomous, `agent-assisted` if a human directed each change, `human` otherwise |
| `Agent-Agent` | if origin is agent* | Agent identity and version, e.g. `claude-code@1.2.3`, `codex@0.133.0` |
| `Agent-Execution` | recommended | Opaque ID for the run/session that produced this commit. Stable across all commits from one run. |
| `Agent-Sandbox` | optional | Sandbox verdict for the execution: `clean`, `blocked` (policy violations occurred), or `none` (unsandboxed) |
| `Agent-Compartment` | optional | Named security profile / stage the agent ran under, e.g. `research`, `builder`, `tester` |
| `Agent-Prompt` | optional | URI or reference to the task/prompt that initiated the work. Never inline secrets or full prompts. |

\* If `Agent-Origin` is `agent` or `agent-assisted`, `Agent-Agent` is required.

Field names are case-insensitive per RFC-5322; writers SHOULD emit title-case.

## Example

```text
commit 8f3d91a
Author: Alex Mercer <alex@company.com>

    Refactor authentication cache for Redis cluster

    Agent-Origin: agent
    Agent-Agent: claude-code@1.2.3
    Agent-Execution: exec_7a9f12bc
    Agent-Compartment: builder
    Agent-Sandbox: clean
```

One execution spanning multiple commits reuses the same `Agent-Execution`
value. This gives incident responders and auditors a single query:

```bash
git log --format="%H %b" | grep -B12 "Agent-Execution: exec_7a9f12bc"
```

or, once tooling catches up:

```bash
git log --trailer "Agent-Execution=exec_7a9f12bc"
```

## Querying

All of these work in vanilla git, today:

```bash
# every agent-authored commit on main
git log --trailer "Agent-Origin"

# everything Claude wrote last quarter
git log --grep "Agent-Agent: claude-code"

# commits from unsandboxed runs (the ones you want to review)
git log --grep "Agent-Sandbox: none"
```

## Compatibility

Truhowl emits `Agent-*` trailers going forward. For compatibility, readers
MUST accept the legacy `Koyote-*` trailers emitted by Koyote v1.x before the
rename to Truhowl (`Koyote-Execution`, `Koyote-Agent`, `Koyote-Compartment`,
`Koyote-Security`). These map 1:1 onto
the `Agent-*` fields above (earlier Volf/Sheepdog/Compart v1.x releases emitted
the same fields under their own names; readers should accept all variants):

| Legacy | Spec |
| :--- | :--- |
| `Koyote-Agent` | `Agent-Agent` |
| `Koyote-Execution` | `Agent-Execution` |
| `Compartment` → `Koyote-Compartment` | `Agent-Compartment` |
| `Koyote-Security` | `Agent-Sandbox` |

Writers SHOULD emit spec names going forward. Readers SHOULD accept both.

## What this spec deliberately does not do

- It does **not** verify authorship cryptographically. Trailers are claims
  written by the tool that created the commit. Sign commits with `git commit -S`
  if you need non-repudiation; combine both for the strong case.
- It does **not** define what counts as "an agent." If a tool writes the code,
  it's an agent. Use judgment for autocomplete-tier assistance
  (`agent-assisted` exists for the gray zone).
- It does **not** require any particular sandbox. `none` is an honest value.

## Adoption

Implementing the write side is ~20 lines around `git commit --trailer`. If you
build an agent harness and start emitting these, open a PR against this file's
"Implementations" section below.

### Implementations

- [Truhowl](https://github.com/Devaretanmay/truhowl) - CLI + Python SDK, emits trailers automatically via `truhowl commit`

## License

The specification text is CC0. Implement it without asking permission.
