#!/usr/bin/env python3
"""DEVELOPMENT smoke: run one end-to-end Hunt repair on a fixture case.

Not a benchmark. Usage:
    python3 scripts/dev_smoke.py <case_dir> [provider]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from truhowl.hunt import list_findings, run_hunt  # noqa: E402


def main() -> int:
    case = sys.argv[1]
    workdir = tempfile.mkdtemp(prefix="truhowl-dev-")
    repo = os.path.join(workdir, os.path.basename(case.rstrip("/")))
    shutil.copytree(case, repo)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Truhowl Dev"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@truhowl.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

    harness = os.path.join(repo, "test", "run.js")
    if os.path.isfile(harness):
        base = subprocess.run(["node", "test/run.js"], cwd=repo, capture_output=True, text=True)
        print("baseline harness:", base.returncode, (base.stdout or base.stderr).strip()[:200])

    findings = list_findings(repo)
    if not findings:
        print("no findings")
        return 2
    finding = findings[0]
    print(f"finding: {finding.provider} {finding.version_from} -> {finding.version_to}")
    print("affected:", finding.affected_files)

    report = run_hunt(repo, finding.finding_id, max_iterations=3)
    print("\n=== RESULT ===")
    print("success:", report.success, "| iterations:", report.iterations)
    print("reason:", report.reason)
    print("files:", report.files_modified)
    print("test:", report.test_command, "exit", report.test_exit_code)
    if report.verified:
        print("tier:", report.verified.verification_tier, "| replay exit:", report.verified.replay_exit_code)
    if report.unified_diff:
        print("--- diff ---")
        print(report.unified_diff[:4000])

    if os.path.isfile(harness):
        after = subprocess.run(["node", "test/run.js"], cwd=repo, capture_output=True, text=True)
        print("\npost-repair harness:", after.returncode, (after.stdout or after.stderr).strip()[:300])

    audit = {}
    try:
        with open(report.audit_path) as f:
            audit = json.load(f)
    except Exception:
        pass
    print("\ndecisions:", json.dumps(audit.get("decisions", []), indent=1)[:2000])
    if audit.get("residual_drift"):
        print("residual_drift:", json.dumps(audit["residual_drift"], indent=1)[:1500])
    print("\nrepo kept at:", repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
