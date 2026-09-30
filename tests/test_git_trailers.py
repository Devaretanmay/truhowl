"""Tests for Git-native metadata trailers and Git commit integration.

Covers:
- Execution.git_trailers() RFC-5322 format
- Security status in git trailers (clean vs blocked actions)
- `truhowl diff --trailers` output
- `truhowl commit` and `truhowl apply --commit`
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import redirect_stdout


from truhowl.cli.main import _git_commit_execution, cmd_diff
from truhowl.engine.execution import Execution, ExecutionKind, ExecutionManager


def test_execution_git_trailers_format():
    """Execution.git_trailers() emits Agent Provenance Trailers (SPEC.md)."""
    ex = Execution(
        execution_id="exec_123456789",
        kind=ExecutionKind.INTERACTIVE,
        command=["claude"],
        compartment_id="coding",
    )
    trailers = ex.git_trailers()
    assert "Agent-Origin: agent" in trailers
    assert "Agent-Agent: claude" in trailers
    assert "Agent-Execution: exec_123456789" in trailers
    assert "Agent-Compartment: coding" in trailers
    assert "Agent-Sandbox: clean" in trailers


def test_execution_git_trailers_process_origin():
    """Non-agent governed processes classify as agent-assisted."""
    ex = Execution(
        execution_id="exec_proc",
        kind=ExecutionKind.PROCESS,
        command=["pytest"],
        compartment_id="tester",
    )
    assert "Agent-Origin: agent-assisted" in ex.git_trailers()


def test_execution_git_trailers_with_security_violations():
    """Blocked security events collapse to the Agent-Sandbox: blocked enum value."""
    ex = Execution(
        execution_id="exec_999",
        kind=ExecutionKind.INTERACTIVE,
        command=["opencode"],
        compartment_id="research",
    )
    ex.emit("network.blocked", {"host": "malicious.com"})
    ex.emit("fs.denied", {"path": "/etc/passwd"})
    trailers = ex.git_trailers()
    assert "Agent-Sandbox: blocked" in trailers


def test_git_commit_execution_staging_and_trailers():
    """_git_commit_execution stages files and creates a commit with Truhowl trailers."""
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Truhowl Bot"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "bot@truhowl.dev"], cwd=tmp, check=True)

        readme = os.path.join(tmp, "README.md")
        with open(readme, "w") as f:
            f.write("# Hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp, check=True)

        src_file = os.path.join(tmp, "auth.py")
        with open(src_file, "w") as f:
            f.write("def login(): return True\n")

        ex = Execution(
            execution_id="exec_auth_01",
            kind=ExecutionKind.INTERACTIVE,
            command=["claude"],
            compartment_id="coding",
            changes=[{"path": "auth.py", "status": "added"}],
        )

        ok = _git_commit_execution(
            ws_root=tmp,
            ex=ex,
            user_message="Implement login feature",
        )
        assert ok is True

        log_res = subprocess.run(
            ["git", "log", "-n", "1"],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=True,
        )
        log_text = log_res.stdout
        assert "Implement login feature" in log_text
        assert "Agent-Execution: exec_auth_01" in log_text
        assert "Agent-Agent: claude" in log_text
        assert "Agent-Compartment: coding" in log_text
        assert "Agent-Sandbox: clean" in log_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_diff_with_trailers_in_json(monkeypatch):
    """`truhowl diff --json` includes git_trailers in every execution dictionary."""
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        monkeypatch.chdir(tmp)
        mgr = ExecutionManager(workdir=tmp)
        ex = mgr.create(kind=ExecutionKind.INTERACTIVE, command=["claude"], compartment_id="coding")
        ex.changes = [{"path": "main.py", "status": "modified"}]
        ex.complete(0, changes=ex.changes)
        mgr.save(ex)

        class _Args:
            execution = ex.execution_id
            unapplied = False
            trailers = True
            json = True

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_diff(_Args())

        data = json.loads(buf.getvalue())
        assert data["change_sets"] == 1
        assert "git_trailers" in data["executions"][0]
        assert "Agent-Execution: " in data["executions"][0]["git_trailers"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
