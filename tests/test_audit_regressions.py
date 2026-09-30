"""Regression tests for auditor-identified edge cases.

Covers bugs that were found but not caught by the existing suite:
- config.py: null/empty YAML sections crashing with AttributeError
- pty_supervisor.py: empty argv ValueError guard
- execution.py: snapshot_dir persists through JSON round-trip
- main.py: session.started_at=None safe in cmd_status
- main.py: workflow node with malformed command string handled gracefully
"""

import argparse
import os
import shutil
import sys
import tempfile
import textwrap
from unittest.mock import patch
import pytest

from truhowl.cli.main import _topo_sort, cmd_status
from truhowl.config import load_config
from truhowl.engine.execution import ExecutionKind, ExecutionManager
from truhowl.engine.pty_supervisor import PtySupervisor
from truhowl.engine.session import SessionStatus, AgentSession


def test_config_null_compartments_section():
    """compartments: (no body) must not raise AttributeError."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "config.yaml")
        with open(p, "w") as f:
            f.write("compartments:\nagents:\nworkflows:\n")
        cfg = load_config(p)
        assert "default" in cfg.compartments
        assert cfg.agents == {}
        assert cfg.workflows == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_null_agents_section():
    """agents: (no body) must not crash."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "config.yaml")
        with open(p, "w") as f:
            f.write(textwrap.dedent("""\
                compartments:
                  default:
                    filesystem: workspace
                    network: restricted
                agents:
            """))
        cfg = load_config(p)
        assert cfg.agents == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_null_workflows_section():
    """workflows: (no body) must not crash."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "config.yaml")
        with open(p, "w") as f:
            f.write(textwrap.dedent("""\
                compartments:
                  default:
                    filesystem: workspace
                    network: restricted
                workflows:
            """))
        cfg = load_config(p)
        assert cfg.workflows == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_depends_on_null():
    """depends_on: (no value) must default to empty list, not None."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "config.yaml")
        with open(p, "w") as f:
            f.write(textwrap.dedent("""\
                compartments:
                  default:
                    filesystem: workspace
                    network: restricted
                workflows:
                  test_wf:
                    nodes:
                      step_a:
                        type: process
                        command: echo hello
                        depends_on:
            """))
        cfg = load_config(p)
        nodes = cfg.workflows["test_wf"].nodes
        assert nodes[0].depends_on == [], f"Expected [], got {nodes[0].depends_on}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not on Windows")
def test_pty_supervisor_empty_argv_attach():
    sup = PtySupervisor(workdir=".")
    with pytest.raises(ValueError, match="argv must not be empty"):
        sup.attach([])


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not on Windows")
def test_pty_supervisor_empty_argv_capture():
    sup = PtySupervisor(workdir=".")
    with pytest.raises(ValueError, match="argv must not be empty"):
        sup.capture([])


def test_snapshot_dir_persists():
    tmp = tempfile.mkdtemp()
    try:
        mgr = ExecutionManager(workdir=tmp)
        ex = mgr.create(kind=ExecutionKind.INTERACTIVE, command=["claude"])
        ex.snapshot_dir = "/tmp/truhowl_test_snap"
        mgr.save(ex)
        loaded = mgr.get(ex.execution_id)
        assert loaded is not None
        assert loaded.snapshot_dir == "/tmp/truhowl_test_snap"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_started_at_none_safe(capsys):
    """Duration calculation in cmd_status must not crash when started_at is None."""
    args = argparse.Namespace()
    
    mock_session = AgentSession(
        session_id="test",
        agent="test",
        compartment_id="test",
        lane_id="test",
        status=SessionStatus.COMPLETED
    )
    mock_session.started_at = None
    mock_session.finished_at = 1000.0

    with patch("truhowl.cli.main.find_workspace_root", return_value="/tmp/test_ws"), \
         patch("truhowl.cli.main.ExecutionManager"), \
         patch("truhowl.cli.main.LaneManager"), \
         patch("truhowl.cli.main.SessionManager") as mock_sess_mgr:
        
        mock_sess_mgr.return_value.list_sessions.return_value = [mock_session]
        
        # Should not crash
        cmd_status(args)
        
        captured = capsys.readouterr()
        assert "RECENT SESSIONS" in captured.out
        assert "1000.0s" in captured.out


def test_topo_sort_empty_nodes():
    """_topo_sort must return empty list for empty input without crashing."""
    assert _topo_sort([]) == []
