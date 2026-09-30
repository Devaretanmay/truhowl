"""Tests for workspace activation hardening (M0-M2).

Covers:
- real-binary resolution that skips the workspace's own .truhowl/bin shim dir
  (prevents shim self-recursion when the workspace is activated),
- the deterministic nested-workspace rule (innermost .truhowl wins),
- PtySupervisor resolving real binaries past the shim dir.
"""

import os
import sys

import pytest

from truhowl.cli.main import _resolve_real_binary
from truhowl.config import find_workspace_root
from truhowl.engine.pty_supervisor import PtySupervisor


def _write_executable(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)
    return path


def test_resolve_real_binary_skips_workspace_shim_dir(tmp_path, monkeypatch):
    """When activated, .truhowl/bin shadows PATH; the shim must be skipped."""
    shim_dir = tmp_path / ".truhowl" / "bin"
    real_dir = tmp_path / "realbin"
    _write_executable(str(shim_dir / "claude"), "#!/usr/bin/env bash\n")
    real = _write_executable(str(real_dir / "claude"), "#!/usr/bin/env bash\n")

    monkeypatch.setenv("PATH", f"{shim_dir}:{real_dir}:/usr/bin:/bin")

    resolved = _resolve_real_binary("claude", str(tmp_path))
    assert resolved == real


def test_resolve_real_binary_returns_none_when_only_shim_present(tmp_path, monkeypatch):
    """If the only match on PATH is our own shim, resolution must fail cleanly."""
    shim_dir = tmp_path / ".truhowl" / "bin"
    _write_executable(str(shim_dir / "claude"), "#!/usr/bin/env bash\n")

    monkeypatch.setenv("PATH", f"{shim_dir}:/usr/bin:/bin")

    assert _resolve_real_binary("claude", str(tmp_path)) is None


def test_resolve_real_binary_ignores_other_workspace_shims(tmp_path, monkeypatch):
    """Only the governing workspace's shim dir is excluded, not other projects'."""
    shim_dir = tmp_path / ".truhowl" / "bin"
    other_shim_dir = tmp_path / "other-project" / ".truhowl" / "bin"
    _write_executable(str(shim_dir / "claude"), "#!/usr/bin/env bash\n")
    other = _write_executable(str(other_shim_dir / "claude"), "#!/usr/bin/env bash\n")

    monkeypatch.setenv("PATH", f"{shim_dir}:{other_shim_dir}:/usr/bin:/bin")

    assert _resolve_real_binary("claude", str(tmp_path)) == other


def test_find_workspace_root_innermost_wins(tmp_path):
    """A workspace nested inside another resolves to the innermost root."""
    (tmp_path / ".truhowl").mkdir()
    inner = tmp_path / "inner"
    (inner / ".truhowl").mkdir(parents=True)
    deep = inner / "src" / "deep"
    deep.mkdir(parents=True)

    assert find_workspace_root(str(deep)) == str(inner)
    assert find_workspace_root(str(tmp_path)) == str(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_pty_supervisor_resolve_skips_shim_dir(tmp_path, monkeypatch):
    """Standalone PtySupervisor must not launch the workspace's own shim."""
    shim_dir = tmp_path / ".truhowl" / "bin"
    _write_executable(str(shim_dir / "echo"), "#!/usr/bin/env bash\n")

    monkeypatch.setenv("PATH", f"{shim_dir}:/usr/bin:/bin")

    sup = PtySupervisor(workdir=str(tmp_path))
    resolved = sup._resolve("echo")
    assert os.path.basename(resolved) == "echo"
    assert os.path.abspath(resolved) != os.path.abspath(str(shim_dir / "echo"))
    assert os.path.isfile(resolved)
