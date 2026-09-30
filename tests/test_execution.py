"""Tests for the unified Execution domain primitive."""

import shutil
import tempfile

from truhowl.engine.execution import (
    Execution, ExecutionManager, ExecutionKind, ExecutionStatus
)


def test_execution_lifecycle():
    ex = Execution(
        execution_id="exec_001",
        kind=ExecutionKind.INTERACTIVE,
        command=["claude"],
    )
    assert ex.status == ExecutionStatus.CREATED
    assert ex.duration_s is None

    ex.start(pid=12345)
    assert ex.status == ExecutionStatus.RUNNING
    assert ex.pid == 12345
    assert ex.started_at is not None

    ex.complete(returncode=0, changes=[{"path": "src/auth.py", "status": "modified"}])
    assert ex.status == ExecutionStatus.COMPLETED
    assert ex.returncode == 0
    assert len(ex.changes) == 1
    assert ex.duration_s is not None and ex.duration_s >= 0


def test_execution_failed():
    ex = Execution(execution_id="exec_002", kind=ExecutionKind.PROCESS, command=["pytest"])
    ex.start()
    ex.complete(returncode=1)
    assert ex.status == ExecutionStatus.FAILED


def test_execution_events():
    ex = Execution(execution_id="exec_003", kind=ExecutionKind.WORKFLOW, command=["my_workflow.py"])
    ex.emit("custom.event", {"data": "hello"})
    assert len(ex.events) == 1
    assert ex.events[0]["name"] == "custom.event"


def test_execution_manager_lifecycle():
    tmp = tempfile.mkdtemp()
    try:
        mgr = ExecutionManager(workdir=tmp)
        ex = mgr.create(
            kind=ExecutionKind.INTERACTIVE,
            command=["claude"],
            compartment_id="default",
        )
        assert ex.execution_id.startswith("exec_")
        assert ex.status == ExecutionStatus.CREATED

        ex.start(pid=9999)
        mgr.save(ex)

        loaded = mgr.get(ex.execution_id)
        assert loaded is not None
        assert loaded.pid == 9999
        assert loaded.status == ExecutionStatus.RUNNING

        running = mgr.list_running()
        assert any(e.execution_id == ex.execution_id for e in running)

        ex.complete(returncode=0)
        mgr.save(ex)

        running_after = mgr.list_running()
        assert not any(e.execution_id == ex.execution_id for e in running_after)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_name_derived_from_command():
    ex = Execution(execution_id="exec_004", kind=ExecutionKind.INTERACTIVE, command=["opencode", "--help"])
    assert ex.agent_name == "opencode"
