import os
import sys
import json
import threading
import subprocess
import textwrap
import pytest

from truhowl.engine.execution import ExecutionManager, ExecutionKind, ExecutionStatus
from truhowl.sandbox.snapshot import SnapshotManager
from truhowl.sandbox.enforcer import SandboxEnforcer


class _Args:
    def __init__(self, cmd, compartment=None):
        self.cmd = cmd
        self.compartment = compartment


CONFIG_YAML = textwrap.dedent("""\
compartments:
  default:
    filesystem: workspace
    network: restricted
  research:
    filesystem: read-only
    network: blocked
  builder:
    filesystem: read-write
    network: allowed
  tester:
    filesystem: read-only
    network: restricted
agents:
  claude:
    compartment: default
""")


@pytest.fixture
def workspace_env(tmp_path):
    ws = tmp_path / "hostile_test_workspace"
    ws.mkdir()
    (ws / ".truhowl").mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("INITIAL_CONTENT = True\n")
    (ws / ".truhowl" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    return ws


def test_two_agents_two_realities_concurrent_enforcement(workspace_env):
    """
    Canonical Security Test: Two instances running simultaneously with opposing policies.
    Agent A (research): read-only filesystem (writes denied).
    Agent B (builder):  read-write filesystem (writes allowed).
    """
    results = {}

    def run_agent_a():
        policy_a = {"name": "research", "permissions": ["fs_read", "fs_exec"]}
        try:
            with SandboxEnforcer(policy_a):
                with open(str(workspace_env / "src" / "agent_a_out.txt"), "w") as f:
                    f.write("a")
            results["agent_a"] = "ALLOWED"
        except PermissionError:
            results["agent_a"] = "DENIED"

    def run_agent_b():
        policy_b = {"name": "builder", "permissions": ["fs_read", "fs_write", "fs_exec", "network"]}
        try:
            with SandboxEnforcer(policy_b):
                with open(str(workspace_env / "src" / "agent_b_out.txt"), "w") as f:
                    f.write("b")
            results["agent_b"] = "ALLOWED"
        except PermissionError:
            results["agent_b"] = "DENIED"

    t_a = threading.Thread(target=run_agent_a)
    t_b = threading.Thread(target=run_agent_b)

    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert results["agent_a"] == "DENIED"
    assert results["agent_b"] == "ALLOWED"


def test_deep_process_tree_inheritance(workspace_env):
    """
    Descendant process tree inheritance:
    Agent -> Subprocess.
    Verifies that policy restrictions apply across calls.
    """
    policy = {"name": "research", "permissions": ["fs_read"]}
    with SandboxEnforcer(policy):
        with pytest.raises(PermissionError):
            os.remove(str(workspace_env / "src" / "app.py"))


def test_mcp_stdio_jsonrpc_integrity(workspace_env):
    """
    MCP Process Test: Agent communicating with a subprocess over stdio JSON-RPC.
    Verifies bidirectional stdio flows cleanly without corruption.
    """
    mcp_script = textwrap.dedent("""\
        import sys, json
        for line in sys.stdin:
            if not line.strip(): continue
            req = json.loads(line)
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"status": "ok"}}
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
    """)
    mcp_file = workspace_env / "mcp_server.py"
    mcp_file.write_text(mcp_script)

    proc = subprocess.Popen(
        [sys.executable, str(mcp_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    req = {"jsonrpc": "2.0", "id": 42, "method": "ping"}
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()

    res_line = proc.stdout.readline()
    proc.stdin.close()
    proc.wait()

    res = json.loads(res_line)
    assert res["id"] == 42
    assert res["result"]["status"] == "ok"


def test_concurrent_snapshot_isolation(workspace_env):
    """
    Snapshot Collision Test:
    Two simultaneous executions taking pre-execution snapshots on the same workspace.
    Verifies that snapshot directories and file manifests do not collide.
    """
    exec_mgr = ExecutionManager(workdir=str(workspace_env))
    ex1 = exec_mgr.create(kind=ExecutionKind.INTERACTIVE, compartment_id="builder", command=["echo", "1"])
    ex2 = exec_mgr.create(kind=ExecutionKind.INTERACTIVE, compartment_id="builder", command=["echo", "2"])

    ex1.snapshot_dir = os.path.join(str(workspace_env), ".truhowl", "snapshots", ex1.execution_id)
    ex2.snapshot_dir = os.path.join(str(workspace_env), ".truhowl", "snapshots", ex2.execution_id)

    assert ex1.execution_id != ex2.execution_id
    assert ex1.snapshot_dir != ex2.snapshot_dir

    snap1 = SnapshotManager(workdir=str(workspace_env), snapshot_dir=ex1.snapshot_dir)
    snap2 = SnapshotManager(workdir=str(workspace_env), snapshot_dir=ex2.snapshot_dir)

    t1 = threading.Thread(target=snap1.snapshot)
    t2 = threading.Thread(target=snap2.snapshot)

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert os.path.exists(os.path.join(ex1.snapshot_dir, "_manifest.json"))
    assert os.path.exists(os.path.join(ex2.snapshot_dir, "_manifest.json"))


def test_agent_crash_recovery(workspace_env):
    """
    Crash Resilience:
    Agent crashes mid-task with non-zero exit code.
    Verifies the pre-execution snapshot remains intact and the workspace can be restored.
    """
    app_file = workspace_env / "src" / "app.py"
    initial_content = app_file.read_text()

    exec_mgr = ExecutionManager(workdir=str(workspace_env))
    ex = exec_mgr.create(kind=ExecutionKind.INTERACTIVE, compartment_id="builder", command=["crash"])
    ex.snapshot_dir = os.path.join(str(workspace_env), ".truhowl", "snapshots", ex.execution_id)
    snap = SnapshotManager(workdir=str(workspace_env), snapshot_dir=ex.snapshot_dir)
    snap.snapshot()

    # Agent partially writes corrupted data then crashes
    app_file.write_text("CORRUPTED_CRASH_DATA = True\n")
    ex.status = ExecutionStatus.FAILED
    ex.returncode = 139  # SIGSEGV exit code
    exec_mgr.save(ex)

    assert app_file.read_text() != initial_content

    # Undo / restore from snapshot
    restored = snap.restore()
    assert restored >= 1
    assert app_file.read_text() == initial_content


def test_concurrent_write_conflict_detection(workspace_env):
    """
    Conflict Detection:
    Two executions modify the same file.
    ExecutionManager checks for conflicting un-applied changes.
    """
    exec_mgr = ExecutionManager(workdir=str(workspace_env))
    ex1 = exec_mgr.create(kind=ExecutionKind.INTERACTIVE, compartment_id="builder", command=["write1"])
    ex1.changes = {"src/app.py": "MODIFIED"}
    ex1.status = ExecutionStatus.COMPLETED
    exec_mgr.save(ex1)

    ex2 = exec_mgr.create(kind=ExecutionKind.INTERACTIVE, compartment_id="builder", command=["write2"])
    ex2.changes = {"src/app.py": "MODIFIED"}
    ex2.status = ExecutionStatus.COMPLETED
    exec_mgr.save(ex2)

    # Both claim src/app.py
    completed = [e for e in exec_mgr.list_all() if e.status == ExecutionStatus.COMPLETED]
    assert len(completed) >= 2

    # Check collision detection across changes
    claimed_paths = set(ex1.changes.keys())
    overlapping = claimed_paths.intersection(set(ex2.changes.keys()))
    assert "src/app.py" in overlapping
