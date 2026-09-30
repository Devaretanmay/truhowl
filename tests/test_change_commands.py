"""Tests for the Git-like change commands (M7).

Covers `truhowl diff` / `apply` / `undo` / `restore` and the fixed
SessionManager.rollback_session (which restores from a snapshot checkpoint
instead of being a silent no-op).
"""

import json
import os
import time

import pytest

from truhowl.cli.main import cmd_apply, cmd_diff, cmd_restore, cmd_undo
from truhowl.engine.execution import Execution, ExecutionKind, ExecutionManager, ExecutionStatus
from truhowl.engine.session import SessionManager, SessionStatus
from truhowl.sandbox.snapshot import SnapshotManager


class _DiffArgs:
    def __init__(self, execution=None, unapplied=False, json=False):
        self.execution = execution
        self.unapplied = unapplied
        self.json = json


class _ApplyArgs:
    def __init__(self, execution=None, force=False):
        self.execution = execution
        self.force = force


class _UndoArgs:
    def __init__(self, execution=None):
        self.execution = execution


class _RestoreArgs:
    def __init__(self, session_id):
        self.session_id = session_id


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A Truhowl workspace; cwd is moved inside it."""
    (tmp_path / ".truhowl").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_execution(ws, eid, changes, status=ExecutionStatus.COMPLETED, finished_at=None):
    mgr = ExecutionManager(workdir=str(ws))
    ex = Execution(
        execution_id=eid,
        kind=ExecutionKind.PROCESS,
        command=["echo"],
        compartment_id="default",
        changes=changes,
        returncode=0,
        finished_at=finished_at if finished_at is not None else time.time(),
        status=status,
    )
    mgr.save(ex)
    return ex


def _exec_status(ws, eid) -> str:
    return ExecutionManager(workdir=str(ws)).get(eid).status




def test_diff_shows_change_sets(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "src/a.py", "status": "modified"}], finished_at=100.0)
    _seed_execution(ws, "exec_b", [{"path": "src/b.py", "status": "added"}], finished_at=200.0)

    cmd_diff(_DiffArgs())

    out = capsys.readouterr().out
    assert "TRUHOWL DIFF" in out
    assert "exec_a" in out and "exec_b" in out
    assert "MODIFIED" in out and "src/a.py" in out
    assert "ADDED" in out and "src/b.py" in out
    assert "2 change set(s), 2 file change(s) total" in out


def test_diff_execution_filter(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "a.py", "status": "modified"}])
    _seed_execution(ws, "exec_b", [{"path": "b.py", "status": "modified"}])

    cmd_diff(_DiffArgs(execution="exec_a"))

    out = capsys.readouterr().out
    assert "exec_a" in out
    assert "exec_b" not in out


def test_diff_execution_not_found(ws, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_diff(_DiffArgs(execution="exec_missing"))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().out


def test_diff_unapplied_excludes_applied(ws, capsys):
    _seed_execution(ws, "exec_applied", [{"path": "a.py", "status": "modified"}], status=ExecutionStatus.APPLIED)
    _seed_execution(ws, "exec_pending", [{"path": "b.py", "status": "modified"}], finished_at=300.0)

    cmd_diff(_DiffArgs(unapplied=True))

    out = capsys.readouterr().out
    assert "exec_pending" in out
    assert "exec_applied" not in out


def test_diff_json(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "src/a.py", "status": "modified"}])

    cmd_diff(_DiffArgs(json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["change_sets"] == 1
    assert data["total_changes"] == 1
    assert data["executions"][0]["execution_id"] == "exec_a"




def test_apply_marks_applied(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "src/a.py", "status": "modified"}])

    with pytest.raises(SystemExit) as exc:
        cmd_apply(_ApplyArgs())
    assert exc.value.code == 0
    assert "Applied 1 change set(s)" in capsys.readouterr().out
    assert _exec_status(ws, "exec_a") == ExecutionStatus.APPLIED


def test_apply_single_execution(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "a.py", "status": "modified"}])
    _seed_execution(ws, "exec_b", [{"path": "b.py", "status": "modified"}])

    with pytest.raises(SystemExit) as exc:
        cmd_apply(_ApplyArgs(execution="exec_b"))
    assert exc.value.code == 0
    assert _exec_status(ws, "exec_b") == ExecutionStatus.APPLIED
    assert _exec_status(ws, "exec_a") == ExecutionStatus.COMPLETED


def test_apply_nothing_to_apply(ws, capsys):
    _seed_execution(ws, "exec_a", [])

    cmd_apply(_ApplyArgs())

    assert "Nothing to apply" in capsys.readouterr().out


def test_apply_conflict_refused_then_forced(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "src/shared.py", "status": "modified"}], finished_at=100.0)
    _seed_execution(ws, "exec_b", [{"path": "src/shared.py", "status": "modified"}], finished_at=200.0)

    with pytest.raises(SystemExit) as exc:
        cmd_apply(_ApplyArgs())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "CONFLICT" in out
    assert "src/shared.py" in out
    assert _exec_status(ws, "exec_a") == ExecutionStatus.COMPLETED

    with pytest.raises(SystemExit) as exc:
        cmd_apply(_ApplyArgs(execution="exec_a", force=True))
    assert exc.value.code == 0
    assert _exec_status(ws, "exec_a") == ExecutionStatus.APPLIED


def test_undo_reverses_last_apply(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "a.py", "status": "modified"}], finished_at=100.0)
    _seed_execution(ws, "exec_b", [{"path": "b.py", "status": "modified"}], finished_at=200.0)

    with pytest.raises(SystemExit):
        cmd_apply(_ApplyArgs())
    assert _exec_status(ws, "exec_b") == ExecutionStatus.APPLIED

    cmd_undo(_UndoArgs())
    assert _exec_status(ws, "exec_b") == ExecutionStatus.COMPLETED
    assert _exec_status(ws, "exec_a") == ExecutionStatus.APPLIED

    cmd_undo(_UndoArgs(execution="exec_a"))
    assert _exec_status(ws, "exec_a") == ExecutionStatus.COMPLETED


def test_undo_nothing_to_undo(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "a.py", "status": "modified"}])

    cmd_undo(_UndoArgs())

    assert "Nothing to undo" in capsys.readouterr().out


def test_undo_not_applied_exits(ws, capsys):
    _seed_execution(ws, "exec_a", [{"path": "a.py", "status": "modified"}])

    with pytest.raises(SystemExit) as exc:
        cmd_undo(_UndoArgs(execution="exec_a"))
    assert exc.value.code == 1
    assert "not applied" in capsys.readouterr().out




def _session_with_checkpoint(ws, snap_dir=None):
    sm = SessionManager(workdir=str(ws))
    sess = sm.create_session(agent_name="TestAgent", task="rollback me")
    snap_dir = snap_dir or os.path.join(str(ws), ".truhowl", "snapshots", sess.session_id)
    SnapshotManager(workdir=str(ws), snapshot_dir=snap_dir).snapshot()
    sess.create_checkpoint("pre-execution", snapshot_manifest=snap_dir)
    sm.save_session(sess)
    return sess, snap_dir


def test_restore_restores_workspace_from_checkpoint(ws, capsys):
    (ws / "file.txt").write_text("original")
    sess, _ = _session_with_checkpoint(ws)

    (ws / "file.txt").write_text("changed by agent")
    (ws / "new.txt").write_text("created by agent")

    cmd_restore(_RestoreArgs(sess.session_id))

    assert "restored" in capsys.readouterr().out
    assert (ws / "file.txt").read_text() == "original"
    assert not (ws / "new.txt").exists(), "files created after the snapshot are removed"


def test_restore_unknown_session(ws, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_restore(_RestoreArgs("sess_nope"))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().out


def test_restore_without_checkpoint_exits(ws, capsys):
    sm = SessionManager(workdir=str(ws))
    sess = sm.create_session(agent_name="A", task="no snapshot")

    with pytest.raises(SystemExit) as exc:
        cmd_restore(_RestoreArgs(sess.session_id))
    assert exc.value.code == 1
    assert "no snapshot checkpoint" in capsys.readouterr().out


def test_rollback_session_restores_and_marks_rolled_back(ws):
    (ws / "file.txt").write_text("original")
    sess, _ = _session_with_checkpoint(ws)

    (ws / "file.txt").write_text("mutated")

    sm = SessionManager(workdir=str(ws))
    assert sm.rollback_session(sess.session_id) is True
    assert (ws / "file.txt").read_text() == "original"

    reloaded = sm.get_session(sess.session_id)
    assert reloaded.status == SessionStatus.ROLLED_BACK


def test_rollback_without_snapshot_returns_false(ws):
    sm = SessionManager(workdir=str(ws))
    sess = sm.create_session(agent_name="A", task="no snapshot")

    assert sm.rollback_session(sess.session_id) is False


def test_rollback_unknown_session_returns_false(ws):
    sm = SessionManager(workdir=str(ws))
    assert sm.rollback_session("sess_missing") is False
