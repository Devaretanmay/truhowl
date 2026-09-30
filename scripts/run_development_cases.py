#!/usr/bin/env python3
# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Run Truhowl's repair loop against DEVELOPMENT CASES and classify the outcomes.

DEVELOPMENT CASES, not held-out evidence
----------------------------------------
Every repository here has already been observed by Truhowl in an earlier
benchmark, and Truhowl has since been modified in response to those failures.
By construction they can no longer be held-out evidence. They exist to drive
engineering and to be re-run after each change.

Usage
-----
    python3 scripts/run_development_cases.py                    # all cases, live LLM
    python3 scripts/run_development_cases.py --case sentry_hub  # one case
    python3 scripts/run_development_cases.py --time-budget 300  # seconds per case

Outputs
-------
    trials/development/results.json      per-case outcome + classification
    trials/development/journal.md        human-readable development journal
    trials/development/migration_failures.json   taxonomy aggregate

Each record carries ``label: "DEVELOPMENT CASE"`` so no downstream reader can
mistake these for benchmark results.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "python"))

from truhowl.failure_taxonomy import (  # noqa: E402
    build_failure_report,
    classify_report,
    read_audit,
)
from truhowl.hunt import list_findings, run_hunt  # noqa: E402

DEFAULT_CASES_DIR = os.path.join(ROOT, "trials", "development")


# NOTE: an in-process SIGALRM budget does not work here. The repair path is
# required to be fail-soft, so several layers catch broad `Exception` — the
# alarm's own exception gets swallowed and the run continues. Each case is
# therefore executed in its own subprocess with a hard timeout, which is also
# what keeps one throttled case from consuming the whole session.


def _prepare(case_dir: str) -> str:
    work = tempfile.mkdtemp(prefix="truhowl-dev-")
    repo = os.path.join(work, os.path.basename(case_dir.rstrip(os.sep)))
    shutil.copytree(case_dir, repo)
    for cmd in (["git", "init", "-b", "main"],
                ["git", "config", "user.name", "Truhowl Dev"],
                ["git", "config", "user.email", "dev@truhowl.local"],
                ["git", "add", "-A"],
                ["git", "commit", "-m", "baseline"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def _harness_state(repo: str) -> str:
    """Baseline/after state of the repository's own verification command."""
    if not os.path.isfile(os.path.join(repo, "test", "run.js")):
        return "no-harness"
    proc = subprocess.run(["node", "test/run.js"], cwd=repo, capture_output=True, text=True)
    return "green" if proc.returncode == 0 else "red"


def run_case(case_dir: str, time_budget: int, max_iterations: int) -> dict:
    name = os.path.basename(case_dir.rstrip(os.sep))
    record: dict = {
        "case": name,
        "label": "DEVELOPMENT CASE",
        "author": "llm",
        "session": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        repo = _prepare(case_dir)
    except Exception as exc:
        record.update({"outcome": "HARNESS_ERROR", "detail": f"prepare failed: {exc}"})
        return record

    record["baseline_harness"] = _harness_state(repo)
    record["baseline_verification_available"] = record["baseline_harness"] != "no-harness"

    try:
        findings = list_findings(repo)
    except Exception as exc:
        record.update({"outcome": "HARNESS_ERROR", "detail": f"findings failed: {exc}"})
        return record
    if not findings:
        record.update({"outcome": "NO_FINDING"})
        return record

    finding = findings[0]
    record.update({
        "provider": finding.provider,
        "version_from": finding.version_from,
        "version_to": finding.version_to,
        "affected_files": list(finding.affected_files or []),
    })

    started = time.time()
    report = None
    try:
        report = run_hunt(repo, finding.finding_id, max_iterations=max_iterations)
    except Exception as exc:
        record["detail"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()[-1500:]
    record["duration_s"] = round(time.time() - started, 1)

    audit = {}
    if report is not None:
        audit = read_audit(getattr(report, "audit_path", "") or "")
        record.update({
            "outcome": "VERIFIED" if report.success else "REFUSED",
            "iterations": report.iterations,
            "files_modified": list(report.files_modified or []),
            "test_command": report.test_command,
            "test_exit_code": report.test_exit_code,
            "verification_tier": getattr(getattr(report, "verified", None), "verification_tier", ""),
            "reason": (report.reason or "")[:300],
        })
    else:
        record.setdefault("outcome", "REFUSED")

    classification = classify_report(report, audit)
    record["classification"] = classification.to_dict()
    record["migration_plan_complete"] = audit.get("migration_plan_complete")
    record["decisions"] = audit.get("decisions", [])
    if audit.get("residual_drift"):
        record["residual_drift"] = audit["residual_drift"]

    record["after_harness"] = _harness_state(repo)
    # Anti-cheating spot check: a "verified" repair must not have moved the
    # repository's harness to red, nor deleted the tests it was meant to keep.
    if record.get("outcome") == "VERIFIED":
        record["harness_regression"] = (
            record["baseline_harness"] == "green" and record["after_harness"] != "green")
    record["repo_copy"] = repo
    return record


def _run_case_isolated(case_dir: str, time_budget: int, max_iterations: int) -> dict:
    """Run one case in a subprocess with a hard timeout and a guaranteed record."""
    name = os.path.basename(case_dir.rstrip(os.sep))
    cmd = [sys.executable, os.path.abspath(__file__), "--single", case_dir,
           "--time-budget", str(time_budget), "--max-iterations", str(max_iterations)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=time_budget)
        for line in (proc.stdout or "").splitlines():
            if line.startswith("TRUHOWL_RECORD_JSON "):
                return json.loads(line[len("TRUHOWL_RECORD_JSON "):])
        return {
            "case": name, "label": "DEVELOPMENT CASE", "author": "llm",
            "outcome": "HARNESS_ERROR",
            "detail": f"no record emitted (exit {proc.returncode})",
            "stderr_tail": (proc.stderr or "")[-800:],
            "classification": {"category": "OTHER", "detail": "harness error"},
            "duration_s": round(time.time() - started, 1),
        }
    except subprocess.TimeoutExpired:
        return {
            "case": name, "label": "DEVELOPMENT CASE", "author": "llm",
            "outcome": "REFUSED",
            "detail": f"hard timeout after {time_budget}s",
            "classification": {
                "category": "MODEL_RATE_LIMIT",
                "detail": "provider retries exhausted the case budget before a candidate was sealed",
            },
            "duration_s": round(time.time() - started, 1),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Truhowl development cases.")
    parser.add_argument("--cases-dir", default=DEFAULT_CASES_DIR)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--time-budget", type=int, default=240)
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--out-dir", default=DEFAULT_CASES_DIR)
    parser.add_argument("--single", default=None,
                        help="run one case in-process and print its JSON record")
    args = parser.parse_args()

    if args.single:
        record = run_case(args.single, args.time_budget, args.max_iterations)
        print("TRUHOWL_RECORD_JSON " + json.dumps(record))
        return 0

    if not os.path.isdir(args.cases_dir):
        print(f"no development cases at {args.cases_dir}", file=sys.stderr)
        return 2
    names = args.case or sorted(
        d for d in os.listdir(args.cases_dir)
        if os.path.isdir(os.path.join(args.cases_dir, d)) and not d.startswith("."))

    records = []
    for name in names:
        case_dir = os.path.join(args.cases_dir, name)
        print(f"\n=== DEVELOPMENT CASE: {name} ===", flush=True)
        record = _run_case_isolated(case_dir, args.time_budget, args.max_iterations)
        records.append(record)
        print(f"  outcome={record.get('outcome')} "
              f"class={record['classification']['category']} "
              f"tier={record.get('verification_tier', '')} "
              f"duration={record.get('duration_s')}s", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    report = build_failure_report(records)
    report.update({
        "label": "DEVELOPMENT CASES — not held-out benchmark evidence",
        "session": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": ("These repositories were observed by Truhowl before this pass and Truhowl was "
                 "modified in response to them, so they cannot count as held-out evidence."),
    })
    with open(os.path.join(args.out_dir, "migration_failures.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"label": report["label"], "records": records}, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out_dir, "journal.md"), "w", encoding="utf-8") as f:
        f.write("# Truhowl Migration Intelligence — Development Journal\n\n")
        f.write("All entries are **DEVELOPMENT CASES** (previously observed; not held-out).\n\n")
        f.write("| Repo | Provider | Baseline | Result | Class | Tier | Files | Att. |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in records:
            f.write("| {case} | {prov} {vf}->{vt} | {base} | {out} | {cls} | {tier} | {files} | {it} |\n".format(
                case=r.get("case", "?"),
                prov=r.get("provider", "?"),
                vf=r.get("version_from", "?"),
                vt=r.get("version_to", "?"),
                base=r.get("baseline_harness", "?"),
                out=r.get("outcome", "?"),
                cls=r.get("classification", {}).get("category", "?"),
                tier=r.get("verification_tier", "") or "-",
                files=", ".join(r.get("files_modified", []) or []) or "-",
                it=r.get("iterations", "-"),
            ))

    print("\n" + json.dumps(report["counts"], indent=1))
    print(f"\nwrote {os.path.join(args.out_dir, 'results.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
