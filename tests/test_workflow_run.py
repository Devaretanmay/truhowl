"""Tests for `truhowl workflow run <name>` (M5).

Covers dependency-ordered DAG execution, one Execution per node with its own
compartment policy, dependency-failure skipping, cycle detection, and the
file-path fallback.
"""

import json
import os
import textwrap

import pytest

from truhowl.cli.main import _topo_sort, cmd_workflow_run
from truhowl.config import WorkflowNodeConfig
from truhowl.hooks.base import ExecutionResult

CONFIG_YAML = textwrap.dedent("""\
    compartments:
      default:
        filesystem: workspace
        network: restricted
      research:
        filesystem: read-only
        network: allowed
      tester:
        filesystem: read-only
        network: restricted
    workflows:
      feature-development:
        nodes:
          research:
            type: agent
            command: opencode run --task research
            compartment: research
          build:
            type: agent
            command: claude -p build
            compartment: default
            depends_on: [research]
          test:
            type: process
            command: pytest -q
            compartment: tester
            depends_on: [build]
""")


class _WorkflowArgs:
    def __init__(self, workflow, compartment="default", verbose=False):
        self.workflow = workflow
        self.compartment = compartment
        self.verbose = verbose


class _FakeRunner:
    """SandboxRunner stand-in: records calls; fails commands in fail_commands."""

    instances = []
    fail_commands = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.called_with = None
        _FakeRunner.instances.append(self)

    def run(self, command, permissions=None, env=None, **kw):
        self.called_with = {"command": command, "permissions": permissions, "env": env}
        if command in _FakeRunner.fail_commands:
            return ExecutionResult(returncode=1, stderr="boom")
        return ExecutionResult(returncode=0, stdout=f"ran: {command}")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / ".truhowl").mkdir()
    (tmp_path / ".truhowl" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_runner(monkeypatch):
    _FakeRunner.instances = []
    _FakeRunner.fail_commands = set()
    monkeypatch.setattr("truhowl.cli.main.SandboxRunner", _FakeRunner)
    return _FakeRunner


def _load_executions(ws) -> list[dict]:
    exec_dir = os.path.join(str(ws), ".truhowl", "executions")
    records = []
    if os.path.isdir(exec_dir):
        for fname in sorted(os.listdir(exec_dir)):
            if fname.endswith(".json"):
                with open(os.path.join(exec_dir, fname), encoding="utf-8") as f:
                    records.append(json.load(f))
    return records




def _node(name, depends_on=()):
    return WorkflowNodeConfig(name=name, command=name, depends_on=list(depends_on))


def test_topo_sort_dependency_order():
    nodes = [_node("test", ["build"]), _node("build", ["research"]), _node("research")]
    order = [n.name for n in _topo_sort(nodes)]
    assert order.index("research") < order.index("build") < order.index("test")


def test_topo_sort_cycle_detection():
    nodes = [_node("a", ["b"]), _node("b", ["a"])]
    with pytest.raises(ValueError):
        _topo_sort(nodes)




def test_workflow_run_declared_dag(ws, fake_runner, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("feature-development"))
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "TRUHOWL WORKFLOW: feature-development" in out
    assert "3 node(s)" in out

    order = [i.called_with["command"] for i in _FakeRunner.instances]
    assert order == ["opencode run --task research", "claude -p build", "pytest -q"]

    records = _load_executions(ws)
    assert len(records) == 3
    by_cmd = {tuple(r["command"]): r for r in records}

    research = by_cmd[("opencode", "run", "--task", "research")]
    assert research["kind"] == "INTERACTIVE"
    assert research["compartment_id"] == "research"
    assert research["status"] == "COMPLETED"
    assert "network" in research["policy"]["permissions"]
    assert "fs_write" not in research["policy"]["permissions"]

    build = by_cmd[("claude", "-p", "build")]
    assert build["kind"] == "INTERACTIVE"
    assert build["compartment_id"] == "default"

    test = by_cmd[("pytest", "-q")]
    assert test["kind"] == "PROCESS"
    assert test["compartment_id"] == "tester"
    assert test["status"] == "COMPLETED"


def test_workflow_run_dependency_failure_skips_dependents(ws, fake_runner, capsys):
    _FakeRunner.fail_commands = {"claude -p build"}

    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("feature-development"))
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "SKIPPED" in out

    records = _load_executions(ws)
    by_comp = {r["compartment_id"]: r["status"] for r in records}
    assert by_comp["research"] == "COMPLETED"
    assert by_comp["default"] == "FAILED"
    assert by_comp["tester"] == "SKIPPED"


def test_workflow_run_unknown_name(ws, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("nope"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert "feature-development" in out


def test_workflow_run_unknown_compartment(ws, capsys):
    config_path = ws / ".truhowl" / "config.yaml"
    config_path.write_text(CONFIG_YAML + textwrap.dedent("""\
        workflows:
          broken:
            nodes:
              step:
                type: process
                command: echo hi
                compartment: nope
    """), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("broken"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "unknown compartment 'nope'" in out
    assert _load_executions(ws) == []


def test_workflow_run_cycle_detected(ws, capsys):
    config_path = ws / ".truhowl" / "config.yaml"
    config_path.write_text(CONFIG_YAML + textwrap.dedent("""\
        workflows:
          cyclic:
            nodes:
              a:
                type: process
                command: echo a
                depends_on: [b]
              b:
                type: process
                command: echo b
                depends_on: [a]
    """), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("cyclic"))
    assert exc.value.code == 1
    assert "cycle detected" in capsys.readouterr().out




def test_workflow_run_file_fallback(ws, fake_runner, capsys):
    script = ws / "my_workflow.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_workflow_run(_WorkflowArgs("my_workflow.py"))
    assert exc.value.code == 0

    records = _load_executions(ws)
    assert len(records) == 1
    assert records[0]["kind"] == "WORKFLOW"
    assert records[0]["status"] == "COMPLETED"

    sessions = list((ws / ".truhowl" / "sessions").glob("*.json"))
    assert len(sessions) == 1
