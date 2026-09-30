"""Tests for PtySupervisor (non-interactive capture path only - no real PTY in CI)."""

import os
import sys
import shutil
import tempfile
import pytest

from truhowl.engine.pty_supervisor import PtySupervisor


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_capture_echo():
    """Capture a simple echo command through the PTY supervisor."""
    sup = PtySupervisor(workdir=".")
    result = sup.capture(["echo", "hello from pty"])
    assert result.returncode == 0
    assert "hello from pty" in result.stdout
    assert result.success


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_capture_exit_code():
    """Non-zero exit code is propagated correctly."""
    sup = PtySupervisor(workdir=".")
    result = sup.capture(["sh", "-c", "exit 42"])
    assert result.returncode == 42
    assert not result.success


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_capture_with_extra_env():
    """Extra environment variables reach the child process."""
    sup = PtySupervisor(workdir=".", extra_env={"TRUHOWL_TEST_VAR": "workspace_active"})
    result = sup.capture(["sh", "-c", "echo $TRUHOWL_TEST_VAR"])
    assert result.returncode == 0
    assert "workspace_active" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_capture_workdir():
    """Child process respects the workdir."""
    tmp = tempfile.mkdtemp()
    try:
        sup = PtySupervisor(workdir=tmp)
        result = sup.capture(["pwd"])
        assert result.returncode == 0
        # On macOS /var/folders -> /private/var/folders, so compare basenames
        assert os.path.basename(tmp) in result.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_resolve_missing_binary():
    """FileNotFoundError raised for unknown binary."""
    sup = PtySupervisor(workdir=".")
    with pytest.raises(FileNotFoundError):
        sup.capture(["__truhowl_nonexistent_binary__"])
