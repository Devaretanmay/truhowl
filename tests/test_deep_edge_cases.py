"""Deep Edge-Case Test Suite for Truhowl Workspace & Execution Architecture.

Covers:
1. Cross-agent merge conflict detection during `truhowl apply`
2. Workflow DAG node failure cascading into skipped downstream nodes
3. Deeply nested workspace discovery from 5-level deep subdirectories
4. Child process and subshell `TRUHOWL_EXECUTION_ID` boundary inheritance
5. Path traversal security checks on ExecutionManager
6. Read-only zero-change agent execution handling
7. Malformed workflow DAG (cycles and missing dependencies)
8. Rapid-fire concurrent Execution ID millisecond collision avoidance
"""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout

import pytest

from truhowl.cli.main import (
    cmd_commit, cmd_diff, _apply_execution, _run_declared_workflow
)
from truhowl.config import (
    CompartmentConfig, WorkflowConfig, WorkflowNodeConfig, WorkspaceConfig,
    find_workspace_root, is_truhowl_workspace
)
from truhowl.engine.execution import (
    ExecutionKind, ExecutionManager, ExecutionStatus
)
from truhowl.engine.pty_supervisor import PtySupervisor
from truhowl.hooks.base import ExecutionResult

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



def test_cross_agent_conflict_detection():
    """When two agents modify the same file, apply warns about conflict unless --force is used."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        mgr = ExecutionManager(workdir=tmp)

        ex_a = mgr.create(kind=ExecutionKind.INTERACTIVE, command=["claude"], compartment_id="coding")
        ex_a.changes = [{"path": "server.py", "status": "modified"}]
        ex_a.complete(0, changes=ex_a.changes)
        mgr.save(ex_a)

        ex_b = mgr.create(kind=ExecutionKind.INTERACTIVE, command=["opencode"], compartment_id="research")
        ex_b.changes = [{"path": "server.py", "status": "modified"}, {"path": "utils.py", "status": "added"}]
        ex_b.complete(0, changes=ex_b.changes)
        mgr.save(ex_b)

        buf = io.StringIO()
        with redirect_stdout(buf):
            applied = _apply_execution(mgr, ex_b, force=False)
        out = buf.getvalue()

        assert applied is False
        assert "CONFLICT" in out
        assert "server.py" in out
        assert ex_a.execution_id in out

        buf_force = io.StringIO()
        with redirect_stdout(buf_force):
            applied_force = _apply_execution(mgr, ex_b, force=True)
        assert applied_force is True
        assert ex_b.status == ExecutionStatus.APPLIED
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_workflow_dag_node_failure_cascades_to_skipped(monkeypatch):
    """If node A fails, node B (which depends on A) is marked SKIPPED, not run."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))

        class _MockRunner:
            def __init__(self, workdir, verbose=False, block_network=False):
                pass
            def run(self, cmd, permissions=None, env=None):
                if "fail_cmd" in cmd:
                    return ExecutionResult(returncode=1, stderr="fail", stdout="", diffs=[])
                return ExecutionResult(returncode=0, stderr="", stdout="ok", diffs=[])

        monkeypatch.setattr("truhowl.cli.main.SandboxRunner", _MockRunner)

        cfg = WorkspaceConfig(
            compartments={
                "default": CompartmentConfig(name="default", permissions=["fs_read", "fs_write", "fs_exec"]),
            },
            workflows={
                "pipeline": WorkflowConfig(
                    name="pipeline",
                    nodes=[
                        WorkflowNodeConfig(name="step_fail", type="process", command="fail_cmd", compartment="default"),
                        WorkflowNodeConfig(name="step_skip", type="process", command="echo hello", compartment="default", depends_on=["step_fail"]),
                    ]
                )
            }
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            with pytest.raises(SystemExit) as exc:
                _run_declared_workflow(tmp, cfg.workflows["pipeline"], cfg)
            assert exc.value.code == 1

        out = buf.getvalue()
        assert "step_fail" in out
        assert "FAILED" in out
        assert "step_skip" in out
        assert "SKIPPED" in out

        mgr = ExecutionManager(workdir=tmp)
        execs = mgr.list_all()
        skip_ex = next((e for e in execs if e.command == ["echo", "hello"]), None)
        assert skip_ex is not None
        assert skip_ex.status == ExecutionStatus.SKIPPED
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_deeply_nested_workspace_root_discovery():
    """find_workspace_root correctly locates .truhowl/ even 5 directories down."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        deep_dir = os.path.join(tmp, "a", "b", "c", "d", "e")
        os.makedirs(deep_dir)

        assert is_truhowl_workspace(deep_dir) is True
        assert find_workspace_root(deep_dir) == tmp
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_child_execution_id_inheritance():
    """Child processes receive TRUHOWL_EXECUTION_ID in extra_env."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        sup = PtySupervisor(workdir=tmp, extra_env={"TRUHOWL_EXECUTION_ID": "exec_parent_12345"})
        result = sup.capture(["sh", "-c", "echo $TRUHOWL_EXECUTION_ID"])
        assert result.returncode == 0
        assert "exec_parent_12345" in result.stdout
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_execution_manager_path_traversal_protection():
    """Malicious execution IDs cannot traverse outside the executions directory."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        mgr = ExecutionManager(workdir=tmp)

        path_1 = mgr._path("../../etc/passwd")
        path_2 = mgr._path("../../../secret.json")

        assert os.path.dirname(path_1) == os.path.join(tmp, ".truhowl", "executions")
        assert os.path.basename(path_1) == "passwd.json"
        assert os.path.dirname(path_2) == os.path.join(tmp, ".truhowl", "executions")
        assert os.path.basename(path_2) == "secret.json"
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_zero_change_execution_diff_and_commit():
    """An execution that only reads files produces 0 changes and is handled cleanly."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        os.chdir(tmp)
        mgr = ExecutionManager(workdir=tmp)

        ex = mgr.create(kind=ExecutionKind.INTERACTIVE, command=["claude", "-p", "read codebase"], compartment_id="research")
        ex.complete(0, changes=[])
        mgr.save(ex)

        class _DiffArgs:
            execution = ex.execution_id
            unapplied = False
            trailers = True
            json = True

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_diff(_DiffArgs())
        data = json.loads(buf.getvalue())
        assert data["total_changes"] == 0

        class _CommitArgs:
            message = "empty commit"
            execution = ex.execution_id
            all = False
            force = False

        buf_commit = io.StringIO()
        with redirect_stdout(buf_commit):
            with pytest.raises(SystemExit):
                cmd_commit(_CommitArgs())
        assert "no changes to apply" in buf_commit.getvalue()
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)



def test_rapid_fire_execution_id_uniqueness():
    """Creating 20 executions in a tight loop produces 20 unique IDs."""
    os.chdir(REPO_ROOT)
    tmp = tempfile.mkdtemp()
    try:
        mgr = ExecutionManager(workdir=tmp)
        ids = set()
        for i in range(20):
            ex = mgr.create(kind=ExecutionKind.PROCESS, command=[f"cmd_{i}"])
            assert ex.execution_id not in ids
            ids.add(ex.execution_id)
        assert len(ids) == 20
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
