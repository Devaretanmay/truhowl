# Cross-Repository Active-Work Impact

Truhowl detects when a change in one connected repository is likely to affect
active developer work in another connected repository.

```text
api-service/feature-payments push
  → observe → reason → candidate → confirm
  → Howl Issue on admin (affected work)
```

## Core invariant

A push is an observation, not automatically an alert. Only CONFIRMED impact
produces a user-visible notification. Transient impact retracts silently.

## How it works

1. **Observe.** The `push` webhook is ingested into one evolving candidate per
   repository + branch. Truhowl attempts to check out the exact pushed SHA;
   when unavailable, analysis is explicitly marked as fallback, never as
   exact-revision analysis.
2. **Filter cheaply.** Installed repositories only (v1 scope — no external or
   inferred consumers). Consumption evidence comes from the existing
   repository/dependency graph (`scan_callsites`); the Work Graph stores work
   state only (branches, heads, push history, pushers, open PRs, candidates).
3. **Reason.** AI receives Repository Context + Change Context + Maintenance
   Memory + Active Work Context (source work/head, push trajectory, latest
   push, consumer work/head, relationship evidence, exact-vs-fallback note)
   and judges truly-affected vs inherited vs unaffected work, what must not
   be touched, and whether impact is provisional or confirmed.
4. **Confirm.** Quiet-period stability (rapid pushes coalesce; the sweep
   evaluates the latest head only), PR-open fast path, merge fast path, or
   high-confidence fast path.
5. **Notify.** Howl files exactly one Issue on the **affected** repository
   naming the affected branch/work, source work, contract, confidence, and
   "No code was modified." An open PR on the affected work gets a link
   comment; the Issue stays the source of truth. Re-confirmation updates the
   same Issue; retracted impact posts a correction and closes it.

## Active work

Active work is a branch with recent pushes and/or an open PR. A branch
without a PR is fully trackable. Hunt never runs from push impact by
default; repair requires explicitly enabled Hunt policy and follows the
normal AI-authored, sandbox-verified PR flow.

## State scope

Work Graph state is local JSON (`~/.truhowl/work_graph.json`, 0600). One
device's candidates are not automatically visible on another device.
