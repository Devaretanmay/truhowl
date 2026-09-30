"""Tests for Git-branch-style workflow creation and auto-inferred step additions.

Covers:
- `truhowl branch <name>` / `truhowl workflow branch <name>`
- `truhowl step <workflow> <target>` with smart property auto-inference
- Auto-chaining of `depends_on` between sequential steps
- Multi-step workflow execution from `workflows/<name>.yaml`
"""

import io
import os
import shutil
import tempfile
from contextlib import redirect_stdout

import pytest
import yaml

from truhowl.cli.main import (
    cmd_workflow_branch, cmd_step, cmd_workflow_run, cmd_run, _infer_step_properties
)
from truhowl.hooks.base import ExecutionResult


def test_infer_step_properties():
    """_infer_step_properties auto-detects name, command, type, and compartment."""
    name, cmd, stype, comp = _infer_step_properties("src/ocr_scrape.py")
    assert name == "ocr-scrape"
    assert "ocr_scrape.py" in cmd
    assert stype == "process"
    assert comp == "research"

    name2, cmd2, stype2, comp2 = _infer_step_properties("src/langchain_rag_agent.py")
    assert name2 == "langchain-rag-agent"
    assert "langchain_rag_agent.py" in cmd2
    assert stype2 == "agent"
    assert comp2 == "builder"

    name3, cmd3, stype3, comp3 = _infer_step_properties("src/send_emails.py")
    assert name3 == "send-emails"
    assert comp3 == "network"

    name4, cmd4, stype4, comp4 = _infer_step_properties("pytest tests/")
    assert name4 == "pytest"
    assert comp4 == "tester"


def test_workflow_branch_creation():
    """`truhowl branch <name>` creates workflows/<name>.yaml."""
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))

        class _BranchArgs:
            name = "doc-pipe"

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_workflow_branch(_BranchArgs())

        wf_path = os.path.join(tmp, "workflows", "doc-pipe.yaml")
        assert os.path.exists(wf_path)

        with open(wf_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["name"] == "doc-pipe"
        assert "steps" in data
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_step_addition_and_autochaining():
    """`truhowl step` appends steps and auto-chains depends_on."""
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))

        class _BranchArgs:
            name = "invoice-flow"
        cmd_workflow_branch(_BranchArgs())

        class _Step1Args:
            workflow = "invoice-flow"
            target = "src/ocr_scrape.py"
            name = None
            compartment = None
            type = None
            depends_on = None
        cmd_step(_Step1Args())

        class _Step2Args:
            workflow = "invoice-flow"
            target = "src/langchain_patch.py"
            name = None
            compartment = None
            type = None
            depends_on = None
        cmd_step(_Step2Args())

        class _Step3Args:
            workflow = "invoice-flow"
            target = "src/send_emails.py"
            name = None
            compartment = None
            type = None
            depends_on = None
        cmd_step(_Step3Args())

        wf_path = os.path.join(tmp, "workflows", "invoice-flow.yaml")
        with open(wf_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        steps = data["steps"]
        assert len(steps) == 3

        assert steps[0]["name"] == "ocr-scrape"
        assert steps[0]["compartment"] == "research"
        assert "depends_on" not in steps[0]

        assert steps[1]["name"] == "langchain-patch"
        assert steps[1]["compartment"] == "builder"
        assert steps[1]["depends_on"] == ["ocr-scrape"]

        assert steps[2]["name"] == "send-emails"
        assert steps[2]["compartment"] == "network"
        assert steps[2]["depends_on"] == ["langchain-patch"]

    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_workflow_run_from_workflows_dir(monkeypatch):
    """`truhowl workflow run <name>` executes workflow defined in workflows/<name>.yaml."""
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        with open(os.path.join(tmp, ".truhowl", "config.yaml"), "w") as f:
            f.write("compartments:\n  default:\n    filesystem: workspace\n  research:\n    filesystem: read-only\n")

        recorded_commands = []
        class _MockRunner:
            def __init__(self, workdir, verbose=False, block_network=False):
                pass
            def run(self, cmd, permissions=None, env=None):
                recorded_commands.append(cmd)
                return ExecutionResult(returncode=0, stderr="", stdout="ok", diffs=[])

        monkeypatch.setattr("truhowl.cli.main.SandboxRunner", _MockRunner)

        class _BranchArgs:
            name = "my-pipeline"
        cmd_workflow_branch(_BranchArgs())

        class _StepArgs:
            workflow = "my-pipeline"
            target = "echo step1"
            name = "first-step"
            compartment = "default"
            type = "process"
            depends_on = None
        cmd_step(_StepArgs())

        class _RunArgs:
            workflow = "my-pipeline"
            compartment = "default"
            verbose = False

        with pytest.raises(SystemExit) as exc:
            cmd_workflow_run(_RunArgs())
        assert exc.value.code == 0
        assert len(recorded_commands) == 1
        assert "echo step1" in recorded_commands[0]
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_step_directory_batch_scan():
    """`truhowl step <wf> src/` scans directory and adds all scripts sequentially."""
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        src_dir = os.path.join(tmp, "src")
        os.makedirs(src_dir)

        with open(os.path.join(src_dir, "ocr_extract.py"), "w") as f:
            f.write("# ocr\n")
        with open(os.path.join(src_dir, "langchain_rag.py"), "w") as f:
            f.write("# rag\n")
        with open(os.path.join(src_dir, "send_emails.py"), "w") as f:
            f.write("# email\n")

        class _BranchArgs:
            name = "batch-flow"
        cmd_workflow_branch(_BranchArgs())

        class _DirStepArgs:
            workflow = "batch-flow"
            target = "src"
            name = None
            compartment = None
            type = None
            depends_on = None

        cmd_step(_DirStepArgs())

        wf_path = os.path.join(tmp, "workflows", "batch-flow.yaml")
        with open(wf_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        steps = data["steps"]
        assert len(steps) == 3

        assert steps[0]["name"] == "langchain-rag"
        assert steps[0]["compartment"] == "builder"
        assert "depends_on" not in steps[0]

        assert steps[1]["name"] == "ocr-extract"
        assert steps[1]["compartment"] == "research"
        assert steps[1]["depends_on"] == ["langchain-rag"]

        assert steps[2]["name"] == "send-emails"
        assert steps[2]["compartment"] == "network"
        assert steps[2]["depends_on"] == ["ocr-extract"]

    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_truhowl_run_short_command(monkeypatch):
    """`truhowl run <name>` executes declared workflow directly without 'workflow run'."""
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        os.makedirs(os.path.join(tmp, ".truhowl"))
        with open(os.path.join(tmp, ".truhowl", "config.yaml"), "w") as f:
            f.write("compartments:\n  default:\n    filesystem: workspace\n")

        executed = []
        class _MockRunner:
            def __init__(self, workdir, verbose=False, block_network=False):
                pass
            def run(self, cmd, permissions=None, env=None):
                executed.append(cmd)
                return ExecutionResult(returncode=0, stderr="", stdout="ok", diffs=[])

        monkeypatch.setattr("truhowl.cli.main.SandboxRunner", _MockRunner)

        class _BranchArgs:
            name = "short-run-pipe"
        cmd_workflow_branch(_BranchArgs())

        class _StepArgs:
            workflow = "short-run-pipe"
            target = "echo hello_short_run"
            name = "step-one"
            compartment = "default"
            type = "process"
            depends_on = None
        cmd_step(_StepArgs())

        class _RunArgs:
            target = "short-run-pipe"
            compartment = "default"
            verbose = False

        with pytest.raises(SystemExit) as exc:
            cmd_run(_RunArgs())
        assert exc.value.code == 0
        assert len(executed) == 1
        assert "hello_short_run" in executed[0]
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)

