"""Tests for `truhowl exec --compartment X <agent>` (M4).

Covers compartment resolution (explicit override > agent default > default),
Execution recording for both the PROCESS and INTERACTIVE paths, and the
shim/binary resolution behaviour shared with `_exec_shim`.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from truhowl.cli.main import _launch_agent, _resolve_compartment, cmd_exec
from truhowl.config import load_config
from truhowl.hooks.base import ExecutionResult

REPO_PYTHON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"
)

CONFIG_YAML = textwrap.dedent("""\
    compartments:
      default:
        filesystem: workspace
        network: restricted
      research:
        filesystem: read-only
        network: allowed
      coding:
        filesystem: read-write
        network: denied
    agents:
      claude:
        compartment: coding
""")


class _Args:
    def __init__(self, cmd, compartment=None):
        self.cmd = cmd
        self.compartment = compartment


@pytest.fixture
def workspace(tmp_path):
    """A Truhowl workspace with default/research/coding compartments."""
    (tmp_path / ".truhowl").mkdir()
    (tmp_path / ".truhowl" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    return tmp_path


def _write_executable(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)
    return path


def _load_executions(workspace) -> list[dict]:
    exec_dir = os.path.join(str(workspace), ".truhowl", "executions")
    records = []
    if os.path.isdir(exec_dir):
        for fname in sorted(os.listdir(exec_dir)):
            if fname.endswith(".json"):
                with open(os.path.join(exec_dir, fname), encoding="utf-8") as f:
                    records.append(json.load(f))
    return records




def test_resolve_compartment_explicit_override_wins(workspace):
    cfg = load_config(os.path.join(str(workspace), ".truhowl", "config.yaml"))
    assert _resolve_compartment(cfg, "claude", "research").name == "research"


def test_resolve_compartment_agent_default(workspace):
    cfg = load_config(os.path.join(str(workspace), ".truhowl", "config.yaml"))
    assert _resolve_compartment(cfg, "claude", None).name == "coding"
    assert _resolve_compartment(cfg, "unknown-agent", None).name == "default"


def test_resolve_compartment_unknown_override_exits(workspace, capsys):
    cfg = load_config(os.path.join(str(workspace), ".truhowl", "config.yaml"))
    with pytest.raises(SystemExit) as exc:
        _resolve_compartment(cfg, "claude", "nope")
    assert exc.value.code == 1
    assert "Unknown compartment 'nope'" in capsys.readouterr().out




class _FakeRunner:
    """SandboxRunner stand-in: records the call, never touches the kernel."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.called_with = None
        _FakeRunner.instances.append(self)

    def run(self, cmd_str, permissions=None, env=None, **kw):
        self.called_with = {"cmd": cmd_str, "permissions": permissions, "env": env}
        return ExecutionResult(returncode=0, stdout="ok", diffs=[])


@pytest.fixture
def fake_runner(monkeypatch):
    _FakeRunner.instances = []
    monkeypatch.setattr("truhowl.cli.main.SandboxRunner", _FakeRunner)
    return _FakeRunner


def test_cmd_exec_process_records_execution(workspace, fake_runner, monkeypatch):
    monkeypatch.chdir(workspace)
    cmd_exec(_Args(cmd=["echo", "hi"]))

    records = _load_executions(workspace)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "PROCESS"
    assert rec["command"] == ["echo", "hi"]
    assert rec["compartment_id"] == "default"
    assert rec["returncode"] == 0
    assert rec["status"] == "COMPLETED"

    runner = _FakeRunner.instances[0]
    assert runner.called_with["permissions"] == ["fs_read", "fs_write", "fs_exec"]
    assert runner.kwargs["block_network"] is True


def test_cmd_exec_process_compartment_override(workspace, fake_runner, monkeypatch):
    monkeypatch.chdir(workspace)
    cmd_exec(_Args(cmd=["echo", "hi"], compartment="research"))

    records = _load_executions(workspace)
    assert len(records) == 1
    assert records[0]["compartment_id"] == "research"
    assert "fs_write" not in records[0]["policy"]["permissions"]
    assert "network" in records[0]["policy"]["permissions"]

    runner = _FakeRunner.instances[0]
    assert runner.kwargs["block_network"] is False


def test_cmd_exec_process_sets_execution_marker_env(workspace, fake_runner, monkeypatch):
    """Nested agent launches inside a governed process inherit its boundary."""
    monkeypatch.chdir(workspace)
    cmd_exec(_Args(cmd=["echo", "hi"]))

    env = _FakeRunner.instances[0].called_with["env"]
    exec_id = _load_executions(workspace)[0]["execution_id"]
    assert env["TRUHOWL_EXECUTION_ID"] == exec_id


def test_cmd_exec_unknown_compartment_exits(workspace, fake_runner, monkeypatch, capsys):
    monkeypatch.chdir(workspace)
    with pytest.raises(SystemExit) as exc:
        cmd_exec(_Args(cmd=["echo", "hi"], compartment="bogus"))
    assert exc.value.code == 1
    assert "Unknown compartment 'bogus'" in capsys.readouterr().out
    assert _load_executions(workspace) == []


def test_cmd_exec_no_command_exits(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    with pytest.raises(SystemExit) as exc:
        cmd_exec(_Args(cmd=[], compartment=None))
    assert exc.value.code == 1




@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_launch_agent_records_execution_and_session(workspace, monkeypatch):
    """Launching an agent creates an INTERACTIVE Execution + Session."""
    realbin = workspace / "realbin"
    _write_executable(
        str(realbin / "truhowl_test_agent"),
        "#!/usr/bin/env bash\nexec /bin/echo \"$@\"\n",
    )
    monkeypatch.setenv("PATH", f"{realbin}:{os.environ.get('PATH', '')}")
    monkeypatch.chdir(workspace)

    returncode = _launch_agent(
        "truhowl_test_agent", str(workspace), user_argv=["hello-exec"]
    )
    assert returncode == 0

    records = _load_executions(workspace)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "INTERACTIVE"
    assert rec["command"] == ["truhowl_test_agent", "hello-exec"]
    assert rec["compartment_id"] == "default"
    assert rec["returncode"] == 0
    assert rec["status"] == "COMPLETED"

    session_dir = workspace / ".truhowl" / "sessions"
    sessions = list(session_dir.glob("*.json"))
    assert len(sessions) == 1
    session = json.loads(sessions[0].read_text(encoding="utf-8"))
    assert session["agent"] == "truhowl_test_agent"
    assert session["status"] == "COMPLETED"


@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_launch_agent_respects_compartment_override(workspace, monkeypatch):
    """Two launches of the same agent can carry different policies."""
    realbin = workspace / "realbin"
    _write_executable(
        str(realbin / "truhowl_test_agent"),
        "#!/usr/bin/env bash\nexec /bin/echo \"$@\"\n",
    )
    monkeypatch.setenv("PATH", f"{realbin}:{os.environ.get('PATH', '')}")
    monkeypatch.chdir(workspace)

    _launch_agent("truhowl_test_agent", str(workspace), user_argv=["a"], compartment_name="default")
    _launch_agent("truhowl_test_agent", str(workspace), user_argv=["b"], compartment_name="research")

    records = _load_executions(workspace)
    assert len(records) == 2
    by_comp = {r["compartment_id"] for r in records}
    assert by_comp == {"default", "research"}
    research = next(r for r in records if r["compartment_id"] == "research")
    assert "fs_write" not in research["policy"]["permissions"]


def test_launch_agent_missing_binary_exits_127(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    with pytest.raises(SystemExit) as exc:
        _launch_agent("truhowl_no_such_agent_xyz", str(workspace))
    assert exc.value.code == 127
    assert _load_executions(workspace) == []




@pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")
def test_cli_exec_end_to_end(workspace):
    """`truhowl exec --compartment research -- echo hi` records an Execution."""
    result = subprocess.run(
        [
            sys.executable, "-m", "truhowl.cli.main",
            "exec", "--compartment", "research", "--", "echo", "hello-exec",
        ],
        cwd=str(workspace),
        env={**os.environ, "PYTHONPATH": REPO_PYTHON_DIR},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert "hello-exec" in result.stdout

    records = _load_executions(workspace)
    assert len(records) == 1
    assert records[0]["kind"] == "PROCESS"
    assert records[0]["compartment_id"] == "research"
    assert records[0]["returncode"] == 0
