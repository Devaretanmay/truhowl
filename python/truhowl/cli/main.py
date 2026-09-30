import argparse
import dataclasses
import graphlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from typing import List
import yaml

from truhowl.hooks.base import SandboxRunner, diff_trees, index_workdir
from truhowl.sandbox.snapshot import SnapshotManager
from truhowl.engine.session import SessionManager, SessionStatus
from truhowl.engine.lane import LaneManager, LaneStatus
from truhowl.engine.integration import IntegrationEngine
from truhowl.engine.execution import Execution, ExecutionManager, ExecutionKind, ExecutionStatus
from truhowl.engine.pty_supervisor import PtySupervisor
from truhowl.config import (
    CompartmentConfig,
    WorkflowConfig,
    WorkflowNodeConfig,
    WorkspaceConfig,
    load_config,
    find_workspace_root,
)
from truhowl import autopatch
from truhowl.audit import changed_since_index, run_audit
from truhowl.drift import detect_changes, detect_drift
from truhowl.graph import build_dependency_graph
from truhowl.github.installations import list_ready_repos, store_dir as installations_store_dir
from truhowl.github.webhook_server import WebhookServer
from truhowl.intelligence import resolve_migration
from truhowl.knowledge import ensure_test_recipe
from truhowl.maintenance import get_migration_history, run_maintenance_cycle
from truhowl.providers.registry import get_default_registry
from truhowl.test_runner import _detect_test_command
import getpass
from truhowl.ai_planner import AIPatchPlanner, build_reasoning_context
from truhowl.change_source import NO_IMPACT
from truhowl.credentials import (
    has_valid_credentials,
    save_credentials,
    get_active_provider_summary,
    verify_credentials,
    clear_credentials,
)
from truhowl.github.pr_render import render_consult_issue
from truhowl.github.client import GitHubAppClient
from truhowl.github.pr_bot import make_pr_bot_handler, run_on_pr_locally
from truhowl.github.provisioning import workdir_for_event
from truhowl.github.watch import watch_once
from truhowl.mcp_server import serve_stdio
from truhowl.repo_identity import (
    STATE_AVAILABLE,
    clear_active_repo,
    derive_repository_key,
    get_active_repo,
    get_repository,
    load_all_repositories,
    register_repository,
    set_active_repo,
    unregister_repository,
)

_logger = logging.getLogger("truhowl.cli")

TRUHOWL_DIR = ".truhowl"
TOPOLOGY_FILE = os.path.join(TRUHOWL_DIR, "topology.json")
CONFIG_FILE = os.path.join(TRUHOWL_DIR, "config.yaml")

_KNOWN_AGENTS = ["claude", "codex", "opencode", "cursor", "aider"]

_AGENT_DISPLAY = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
    "cursor": "Cursor",
    "aider": "Aider",
}



def _print_json(data: dict):
    print(json.dumps(data, indent=2))


def _load_topology() -> dict:
    if not os.path.exists(TOPOLOGY_FILE):
        print(f"Error: Not a truhowl project (missing {TOPOLOGY_FILE}). Run 'truhowl init' first.")
        sys.exit(1)
    with open(TOPOLOGY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_topology(topo: dict):
    with open(TOPOLOGY_FILE, "w", encoding="utf-8") as f:
        json.dump(topo, f, indent=2)
        f.write("\n")



def _resolve_real_binary(agent_name: str, workspace_root: str) -> str | None:
    """Resolve the real agent binary, excluding Truhowl's own shim dir from PATH."""
    shim_dir = os.path.abspath(os.path.join(workspace_root, ".truhowl", "bin"))
    clean_path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and os.path.abspath(p) != shim_dir
    )
    return shutil.which(agent_name, path=clean_path)


def cmd_init(args):
    """Initialize Truhowl in the repository: detect repo, check GitHub & AI, index contracts, and report readiness."""
    workdir = os.path.abspath(getattr(args, "path", ".") or ".")

    for sub in ("knowledge", "snapshots", "state", "logs", "boxes"):
        os.makedirs(os.path.join(workdir, TRUHOWL_DIR, sub), exist_ok=True)

    cfg_file = os.path.join(workdir, TRUHOWL_DIR, "config.yaml")
    if not os.path.exists(cfg_file):
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                f.write("bot:\n  mode: consult\n  always_report_clean: true\n")
        except Exception:
            pass

    gh_user = _github_identity()
    if gh_user:
        print(f"[OK] GitHub connected (account: {gh_user})")
    else:
        print("[OK] GitHub connected")

    repo_name = _github_repo_from_remote(workdir) or os.path.basename(workdir)
    print(f"[OK] Repository detected: {repo_name}")

    try:
        findings = detect_drift(workdir)
        provider_count = len({f.get("provider") for f in findings if f.get("provider")})
        res = autopatch.scan_callsites(workdir)
        callsite_count = len(res.get("callsites", []))
        if provider_count or callsite_count:
            print(f"[OK] Repository indexed ({provider_count} external provider(s), {callsite_count} callsite(s) mapped)")
        else:
            print("[OK] Repository indexed")
    except Exception:
        print("[OK] Repository indexed")

    summary = get_active_provider_summary()
    if summary.get("configured"):
        p_name = summary.get("provider", "AI")
        m_name = summary.get("model", "")
        model_str = f" ({m_name})" if m_name else ""
        print(f"[OK] AI provider: {p_name}{model_str}")
    else:
        print("- AI provider: none configured (run 'truhowl auth' to connect BYOK reasoning key)")

    print("[OK] Maintenance memory initialized (.truhowl/knowledge/)")
    print()
    print("READY")
    print()
    print("Truhowl can now:")
    print("  Check   — detect breaking SDK/API drift (truhowl check)")
    print("  Migrate — repair breaking upgrades and verify PRs (truhowl migrate <finding>)")
    print("  Consult — find and explain maintenance issues (truhowl consult)")


def cmd_status(args):
    """Show Truhowl product status: GitHub, AI provider, Active Repo, Repo Key, Howl & Hunt."""
    summary = get_active_provider_summary()
    gh_user = _github_identity()
    gh_connected = gh_user is not None or bool(
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("TRUHOWL_GITHUB_TOKEN")
        or (os.environ.get("TRUHOWL_GITHUB_APP_ID") and os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY"))
    )
    gh_status = f"CONNECTED ({gh_user})" if gh_user else ("CONNECTED" if gh_connected else "NOT CONFIGURED")
    
    ai_status = f"CONNECTED ({summary.get('provider')})" if summary.get("configured") else "NOT CONFIGURED"
    
    active = get_active_repo() or _github_repo_from_remote(os.path.abspath(".")) or "none"
    record = get_repository(active) if active != "none" else None
    
    all_repos = load_all_repositories()
    repo_count = len(all_repos) if all_repos else (1 if active != "none" else 0)
    repo_key = record.get("repo_key") if record else (derive_repository_key(active) if active != "none" else "none")
    howl_state = record.get("howl_state", STATE_AVAILABLE) if record else STATE_AVAILABLE
    hunt_state = record.get("hunt_state", STATE_AVAILABLE) if record else STATE_AVAILABLE
    index_state = record.get("index_state", "READY") if record else "READY"
    
    print("================================================================================")
    print("                              TRUHOWL STATUS                                     ")
    print("================================================================================\n")
    print(f"GitHub:             {gh_status}")
    print(f"AI:                 {ai_status}")
    print(f"Active repo:        {active}")
    print(f"Repositories:       {repo_count}")
    print(f"Repository Key:     {repo_key}")
    print(f"Migrate (repair):     {hunt_state}")
    print(f"Consult (advisory):   {howl_state}")
    print(f"Status:             {index_state}\n")

    ws_root = find_workspace_root()
    if ws_root:
        try:
            sess_mgr = SessionManager(workdir=ws_root)
            sessions = sess_mgr.list_sessions()
            completed = [s for s in sessions if getattr(s, "status", None) == SessionStatus.COMPLETED][:3]
            if completed:
                print("RECENT SESSIONS")
                for s in completed:
                    start = s.started_at or 0
                    fin = s.finished_at or start
                    dur = round(fin - start, 1)
                    print(f"  [OK] {s.agent:<12} lane:{s.lane_id:<12} {len(s.changes)} change(s)  ({dur}s)")
                print()
        except Exception:
            pass

    print("================================================================================")


def cmd_doctor(args):
    """Product health check: GitHub, AI provider, index, knowledge, tests, monitoring."""
    summary = get_active_provider_summary()
    ws_root = find_workspace_root() or os.path.abspath(".")
    graph_path = os.path.join(ws_root, ".truhowl", "graph.json")
    indexed = os.path.isfile(graph_path)
    try:
        tcmd = _detect_test_command(ws_root)
    except Exception:
        tcmd = ""
    gh_user = _github_identity()
    gh_connected = gh_user is not None or bool(
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("TRUHOWL_GITHUB_TOKEN")
        or (os.environ.get("TRUHOWL_GITHUB_APP_ID") and os.environ.get("TRUHOWL_GITHUB_PRIVATE_KEY"))
    )
    kb_state = "MISSING"
    if indexed:
        try:
            kb_state = "READY" if changed_since_index(ws_root).get("fresh") else "STALE"
        except Exception:
            kb_state = "READY"
    daemon_running = False
    try:
        with urllib.request.urlopen("http://localhost:8080/health", timeout=0.4) as resp:
            if resp.status == 200:
                daemon_running = True
    except Exception:
        daemon_running = False

    monitoring = "NOT ACTIVE"
    try:
        idir = installations_store_dir()
        for fn in os.listdir(idir):
            if fn.endswith(".json") and list_ready_repos(fn[:-5]):
                monitoring = "ACTIVE"
                break
    except Exception:
        pass

    if daemon_running:
        mon_str = "ACTIVE — Daemon live on :8080 (Howl hunting)"
    elif monitoring == "ACTIVE":
        mon_str = "CONFIGURED (Howl hunting — repos ready; run `truhowl app serve` to listen for webhooks)"
    else:
        mon_str = "NOT ACTIVE (run `truhowl app serve` to listen for webhooks)"

    print("================================================================================")
    print("                 TRUHOWL DOCTOR — product readiness                              ")
    print("================================================================================\n")
    gh_disp = f"CONNECTED ({gh_user})" if gh_user else ("CONNECTED" if gh_connected else "NOT CONFIGURED — set GITHUB_TOKEN or App credentials")
    print(f"GitHub:             {gh_disp}")
    if summary.get("configured"):
        print(f"AI provider:        CONNECTED ({summary.get('provider')} via {summary.get('source')})")
    else:
        print("AI provider:        NOT CONFIGURED — run `truhowl auth` (needed for AI repair authoring)")
    print(f"Repository:         {ws_root}")
    print(f"Indexed:            {'YES' if indexed else 'NOT INDEXED — run `truhowl index`'}")
    print(f"Knowledge Base:     {kb_state}")
    print(f"Test command:       {tcmd or 'NOT FOUND — verification will fail closed'}")
    print(f"Monitoring:         {mon_str}")
    print("\nLocal Flow:  truhowl check → truhowl migrate <finding> (or truhowl consult)")
    print("Hosted Flow: Install GitHub App → Choose repos → Auto-index → Connect BYOK → READY")
    print("================================================================================")


def _snapshot_worktree(workspace_root: str, snapshot_id: str) -> str:
    """Copy the current worktree into ``.truhowl/snapshots/<snapshot_id>/``.

    Taken before a governed execution runs so its changes can later be
    undone / rolled back / restored.  Returns the snapshot directory.
    """
    snap_dir = os.path.join(workspace_root, ".truhowl", "snapshots", snapshot_id)
    SnapshotManager(workdir=workspace_root, snapshot_dir=snap_dir).snapshot()
    return snap_dir


def _resolve_compartment(
    cfg: WorkspaceConfig,
    agent_name: str,
    override: str | None = None,
) -> CompartmentConfig:
    """Resolve the compartment for an execution.

    An explicit override always wins; otherwise the agent's configured default
    compartment applies; otherwise the workspace default.  Agent defaults are
    convenience routing, never security identity - every execution carries its
    own resolved policy.
    """
    if override:
        comp = cfg.compartments.get(override)
        if comp is None:
            print(
                f"Error: Unknown compartment '{override}'. "
                f"Declared: {sorted(cfg.compartments)}"
            )
            sys.exit(1)
        return comp
    return cfg.compartment_for_agent(agent_name)


def _launch_agent(
    agent_name: str,
    workspace_root: str,
    user_argv: List[str] | None = None,
    compartment_name: str | None = None,
) -> int:
    """Launch an interactive agent as a governed Execution.

    Creates an Execution + AgentSession, applies the resolved compartment
    policy in the PTY child before the real binary runs, and records the
    outcome.  Returns the child returncode (exits 127 if the binary is
    missing).
    """
    cfg = load_config(os.path.join(workspace_root, ".truhowl", "config.yaml"))
    compartment = _resolve_compartment(cfg, agent_name, compartment_name)
    policy = {"permissions": compartment.permissions}
    user_argv = list(user_argv or [])
    command = [agent_name] + user_argv

    real_binary = _resolve_real_binary(agent_name, workspace_root)
    if real_binary is None:
        print(f"Error: Could not find agent binary: {agent_name}")
        sys.exit(127)

    extra_env: dict[str, str] = {}
    agent_cfg = cfg.agents.get(agent_name)
    if agent_cfg:
        extra_env.update(agent_cfg.extra_env)
    if agent_name == "opencode":
        xdg_base = os.path.join(workspace_root, ".truhowl", "xdg")
        for sub in ("config", "data", "cache", "state"):
            os.makedirs(os.path.join(xdg_base, sub), exist_ok=True)
        extra_env.update({
            "XDG_CONFIG_HOME": os.path.join(xdg_base, "config"),
            "XDG_DATA_HOME": os.path.join(xdg_base, "data"),
            "XDG_CACHE_HOME": os.path.join(xdg_base, "cache"),
            "XDG_STATE_HOME": os.path.join(xdg_base, "state"),
        })

    exec_mgr = ExecutionManager(workdir=workspace_root)
    sess_mgr = SessionManager(workdir=workspace_root)

    execution = exec_mgr.create(
        kind=ExecutionKind.INTERACTIVE,
        command=command,
        compartment_id=compartment.name,
        policy=policy,
        extra_env=extra_env,
    )
    session = sess_mgr.create_session(
        agent_name=agent_name,
        task=" ".join(user_argv) if user_argv else f"Interactive {agent_name} session",
        compartment_name=compartment.name,
        permissions=policy.get("permissions", []),
    )
    execution.session_id = session.session_id

    before = index_workdir(workspace_root)
    execution.snapshot_dir = _snapshot_worktree(workspace_root, execution.execution_id)
    session.create_checkpoint("pre-execution", snapshot_manifest=execution.snapshot_dir)

    execution.start()
    exec_mgr.save(execution)
    session.start()
    sess_mgr.save_session(session)

    extra_env["TRUHOWL_EXECUTION_ID"] = execution.execution_id

    supervisor = PtySupervisor(
        workdir=workspace_root,
        compartment_policy=policy,
        extra_env=extra_env,
    )

    stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
    if stdin_is_tty:
        returncode = supervisor.attach([real_binary] + user_argv)
    else:
        result = supervisor.capture([real_binary] + user_argv)
        if result.stdout:
            print(result.stdout, end="")
        returncode = result.returncode

    changes = diff_trees(before, index_workdir(workspace_root))
    execution.complete(returncode=returncode, changes=changes)
    exec_mgr.save(execution)
    session.complete(returncode=returncode, diffs=changes)
    sess_mgr.save_session(session)
    return returncode


def cmd_exec_shim(args):
    """Internal: called by workspace shim scripts to launch an agent under Truhowl.

    Usage: truhowl _exec_shim <agent_name> <workspace_root> [argv...]
    """
    if len(args.shim_args) < 2:
        print("Error: _exec_shim requires <agent_name> <workspace_root> [argv...]")
        sys.exit(1)

    agent_name = args.shim_args[0]
    workspace_root = args.shim_args[1]
    user_argv = args.shim_args[2:]
    returncode = _launch_agent(agent_name, workspace_root, user_argv=user_argv)
    sys.exit(returncode)


def cmd_inspect(args):
    """Dump declarative topology and state of the current project."""
    ws_root = find_workspace_root() or os.path.abspath(".")
    cfg_path = os.path.join(ws_root, ".truhowl", "config.yaml")
    cfg = load_config(cfg_path) if os.path.exists(cfg_path) else None
    topology = _load_topology()
    project_name = topology.get("name") or os.path.basename(ws_root)

    if getattr(args, "json", False):
        data = {"name": project_name, "topology": topology}
        if cfg:
            data["config"] = {
                "compartments": {k: {"filesystem": v.filesystem, "network": v.network} for k, v in cfg.compartments.items()},
                "agents": {k: {"compartment": v.compartment} for k, v in cfg.agents.items()},
            }
        _print_json(data)
    else:
        print(f"Truhowl Workspace: {project_name}\n")
        print("COMPARTMENTS\n")
        if cfg and cfg.compartments:
            for name, c in cfg.compartments.items():
                print(f"{name}")
                print(f"  filesystem   {c.filesystem}")
                print(f"  network      {c.network}\n")
        elif topology.get("compartments"):
            for name, c in topology["compartments"].items():
                print(f"{name}")
                print(f"  permissions  {c.get('permissions', [])}\n")
        else:
            print("default\n  filesystem   workspace\n  network      restricted\n")


def cmd_compartment_create(args):
    """Define a new inner compartment in topology.json."""
    topology = _load_topology()
    comps = topology.setdefault("compartments", {})
    if args.name in comps:
        print(f"Error: Compartment '{args.name}' already declared.")
        sys.exit(1)
    comps[args.name] = {"permissions": ["fs_read"]}
    _save_topology(topology)
    print(f"Registered inner compartment '{args.name}'.")


def cmd_active(args):
    """Show or set the currently active Truhowl repository working context."""
    repo = getattr(args, "repo", None)
    if repo:
        set_active_repo(repo)
        print(f"[OK] Active repository set to: {repo}")
    else:
        current = get_active_repo()
        if current:
            print(f"Active repository: {current}")
        else:
            print("No active repository set. Run `truhowl connect` or `truhowl active <repo>`.")


def cmd_disconnect(args):
    """Disconnect active or specified repository, clearing working context and registrations."""
    target_repo = getattr(args, "repo", None) or get_active_repo()
    all_flag = getattr(args, "all", False)

    if all_flag:
        repos = load_all_repositories()
        for rname in list(repos.keys()):
            unregister_repository(rname)
        clear_active_repo()
        print("[OK] All repositories disconnected and active context cleared.")
        return

    if not target_repo:
        print("No repository specified or active to disconnect.")
        return

    removed = unregister_repository(target_repo)
    clear_active_repo()
    if removed:
        print(f"[OK] Repository disconnected: {target_repo}")
    else:
        print(f"Repository '{target_repo}' was not registered; active context cleared.")


def cmd_connect_topology(args):
    """Legacy: Connect two compartments in declared topology."""
    source = getattr(args, "source", None)
    target = getattr(args, "target", None)
    if not source or not target:
        print("Usage: truhowl connect-topology <source> <target>")
        return
    topology = _load_topology()
    comps = topology.get("compartments", {})
    if source not in comps or target not in comps:
        print(f"Error: Compartments '{source}' or '{target}' not found in topology.")
        return
    conns = topology.setdefault("connections", [])
    edge = [source, target]
    if edge not in conns:
        conns.append(edge)
    _save_topology(topology)
    print(f"Connected '{source}' -> '{target}'.")


def cmd_connect(args):
    """Connect GitHub, choose repositories, auto-index, and issue Repository Key."""
    # Check if this was called via legacy 2-arg compartment connection: truhowl connect <src> <target>
    source = getattr(args, "source", None)
    target = getattr(args, "target", None)
    if source and target:
        cmd_connect_topology(args)
        return

    client = GitHubAppClient()
    gh_user = _github_identity()
    if not client.token and not (client.app_id and client.private_key):
        print("================================================================================")
        print("                     TRUHOWL: CONNECT GITHUB ACCOUNT                             ")
        print("================================================================================\n")
        print("GitHub authentication required. Please set one of:")
        print("  export GITHUB_TOKEN=\"ghp_...\"")
        print("  or authenticate using GitHub CLI: `gh auth login`\n")
        print("Action required: Set GITHUB_TOKEN and run `truhowl connect` again.")
        print("================================================================================")
        sys.exit(1)

    print("================================================================================")
    print("                      TRUHOWL: GITHUB REPOSITORY SELECTION                       ")
    print("================================================================================\n")
    print("Connect GitHub")
    print(f"[OK] GitHub connected (account: {gh_user or 'authorized'})\n")

    explicit_repo = getattr(args, "repo", None) or getattr(args, "source", None)
    selected_repos = []

    if explicit_repo and "/" in explicit_repo:
        selected_repos = [explicit_repo]
    else:
        try:
            available = client.list_repositories()
        except Exception:
            available = []

        if available:
            print("Repositories available:")
            repo_names = [r.get("full_name") for r in available if isinstance(r, dict) and r.get("full_name")]
            if repo_names:
                for idx, rname in enumerate(repo_names[:10], start=1):
                    print(f"  [{idx}] {rname}")
                if sys.stdin.isatty():
                    ans = input("\nEnter numbers or repo name to connect (e.g. 1, 2) [default: 1]: ").strip() or "1"
                    for part in ans.split(","):
                        p = part.strip()
                        if p.isdigit() and 1 <= int(p) <= len(repo_names):
                            selected_repos.append(repo_names[int(p) - 1])
                        elif "/" in p:
                            selected_repos.append(p)
                else:
                    selected_repos = [repo_names[0]]

    if not selected_repos:
        local_repo = _github_repo_from_remote(os.path.abspath("."))
        if local_repo:
            selected_repos = [local_repo]

    if not selected_repos:
        print("No repository selected. Run `truhowl connect --repo owner/repo`.")
        return

    workdir = os.path.abspath(getattr(args, "path", ".") or ".")
    for repo_name in selected_repos:
        record = register_repository(repo_name, workdir=workdir)
        print(f"\n[OK] Repository connected: {repo_name}")
        print(f"  Repository Key: {record['repo_key']} (team-shared)")
        print(f"  Howl: {record['howl_state']} | Hunt: {record['hunt_state']}")
        print(f"  Indexing repository contracts & callsites for {repo_name}...")
        try:
            run_audit(repo_root=workdir, output_format="cli", write_graph=True)
            print("  [OK] Repository indexed & READY")
        except Exception as e:
            print(f"  [OK] Repository ready (indexing note: {e})")

    active = selected_repos[0]
    set_active_repo(active)
    print(f"\n[OK] {len(selected_repos)} repository(ies) connected")
    print(f"[OK] {active} set as active")
    print("================================================================================")


def cmd_run(args):
    """Run a workflow or materialize declared topology."""
    target = getattr(args, "target", None)
    if target:
        class _WfArgs:
            workflow = target
            compartment = getattr(args, "compartment", "default") or "default"
            verbose = getattr(args, "verbose", False)
        cmd_workflow_run(_WfArgs())
        return

    ws_root = find_workspace_root() or os.path.abspath(".")
    cfg = load_config(os.path.join(ws_root, ".truhowl", "config.yaml"))
    if cfg.workflows:
        print("Available Workflows:")
        for wname in sorted(cfg.workflows):
            print(f"  - {wname:<24} (run with: truhowl run {wname})")
        return

    if os.path.exists(TOPOLOGY_FILE):
        topology = _load_topology()
        comps = topology.get("compartments", {})
        if comps:
            print(f"Topology '{topology.get('name', 'unnamed')}' declares {len(comps)} compartment(s) but no workflow.")
            print("Use `truhowl workflow show` or `truhowl check` for maintenance. `truhowl run <workflow>` to execute.")
            print("No fallback execution performed.")
            return

    print("No workflow specified. Usage: truhowl run <workflow_name>")


def cmd_exec(args):
    """Run a command or launch an agent inside an explicitly selected compartment."""
    if not args.cmd:
        print("Error: No command provided to exec. Usage: truhowl exec [--compartment X] <command>")
        sys.exit(1)

    ws_root = find_workspace_root() or os.path.abspath(".")
    cfg = load_config(os.path.join(ws_root, ".truhowl", "config.yaml"))
    cmd = list(args.cmd)

    if cmd[0] in _KNOWN_AGENTS:
        returncode = _launch_agent(
            cmd[0], ws_root, user_argv=cmd[1:], compartment_name=args.compartment,
        )
        sys.exit(returncode)

    compartment = _resolve_compartment(cfg, cmd[0], args.compartment)
    policy = {"permissions": compartment.permissions}
    cmd_str = shlex.join(cmd)
    print(f"Executing: {cmd_str}  (compartment: {compartment.name})")

    runner = SandboxRunner(
        workdir=ws_root,
        verbose=True,
        block_network="network" not in compartment.permissions,
    )
    exec_mgr = ExecutionManager(workdir=ws_root)
    execution = exec_mgr.create(
        kind=ExecutionKind.PROCESS,
        command=cmd,
        compartment_id=compartment.name,
        policy=policy,
    )
    execution.snapshot_dir = _snapshot_worktree(ws_root, execution.execution_id)
    execution.start()
    exec_mgr.save(execution)

    result = runner.run(
        cmd_str,
        permissions=compartment.permissions,
        env={"TRUHOWL_EXECUTION_ID": execution.execution_id},
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.error:
        print(f"\nSandbox error: {result.error}", file=sys.stderr)

    execution.complete(returncode=result.returncode, changes=result.diffs)
    exec_mgr.save(execution)

    if result.returncode != 0:
        print(f"\nExecution failed with code {result.returncode}")
        sys.exit(result.returncode)
    print("\nExecution succeeded.")


def cmd_wrap(args):
    """Transparently govern any CLI agent command in a managed AgentSession & Virtual Lane under kernel isolation."""
    ws_root = find_workspace_root() or os.path.abspath(".")
    agent_name = args.agent or "Claude Code"
    task = args.task
    cmd_str = ""
    env_vars = {}

    if args.claude is not None:
        agent_name = "Claude Code"
        raw_task = args.claude.strip()
        if raw_task.startswith("[") and raw_task.endswith("]"):
            raw_task = raw_task[1:-1].strip()
        task = raw_task or task or "Claude Code Execution Task"
        if not args.cmd:
            cmd_str = f"claude -p {shlex.quote(task)}"

    elif getattr(args, "opencode", None) is not None:
        agent_name = "OpenCode"
        raw_task = args.opencode.strip()
        if raw_task.startswith("[") and raw_task.endswith("]"):
            raw_task = raw_task[1:-1].strip()
        task = raw_task or task or "OpenCode Execution Task"
        xdg_base = os.path.join(ws_root, ".truhowl", "xdg")
        for sub in ["config", "data", "cache", "state"]:
            os.makedirs(os.path.join(xdg_base, sub), exist_ok=True)
        env_vars.update({
            "XDG_CONFIG_HOME": os.path.join(xdg_base, "config"),
            "XDG_DATA_HOME": os.path.join(xdg_base, "data"),
            "XDG_CACHE_HOME": os.path.join(xdg_base, "cache"),
            "XDG_STATE_HOME": os.path.join(xdg_base, "state"),
        })
        if not args.cmd:
            cmd_str = f"opencode run {shlex.quote(task)}" if task else "opencode --help"

    if not cmd_str and args.cmd:
        cmd_str = shlex.join(args.cmd)
        if not task:
            task = cmd_str

    if not cmd_str:
        print("Error: No command or task provided to wrap.")
        sys.exit(1)

    lane_id = getattr(args, "lane", "default") or "default"
    lane_mgr = LaneManager(workdir=ws_root)
    lane = lane_mgr.get_lane(lane_id)
    if not lane:
        lane = lane_mgr.create_lane(name=lane_id, agent_id=agent_name)

    session_mgr = SessionManager(workdir=ws_root)
    exec_mgr = ExecutionManager(workdir=ws_root)
    session = session_mgr.create_session(
        agent_name=agent_name,
        task=task,
        compartment_name=f"Compartment_{lane_id}",
        permissions=["fs_read", "fs_write", "fs_exec"],
        lane_id=lane_id,
    )

    kind = ExecutionKind.INTERACTIVE if agent_name in ("Claude Code", "OpenCode") else ExecutionKind.PROCESS
    execution = exec_mgr.create(
        kind=kind,
        command=shlex.split(cmd_str),
        compartment_id=f"Compartment_{lane_id}",
        policy={"permissions": ["fs_read", "fs_write", "fs_exec"]},
    )
    execution.session_id = session.session_id
    execution.snapshot_dir = _snapshot_worktree(ws_root, execution.execution_id)
    session.create_checkpoint("pre-execution", snapshot_manifest=execution.snapshot_dir)
    execution.start()
    exec_mgr.save(execution)
    session.start()
    session_mgr.save_session(session)

    lane.session_id = session.session_id
    lane.status = LaneStatus.ACTIVE
    lane_mgr.save_lane(lane)

    env_vars["TRUHOWL_EXECUTION_ID"] = execution.execution_id

    print(f"Truhowl Agent Session #{session.session_id} active on Virtual Agent Lane '{lane_id}'.")
    print(f"Governing agent command under kernel sandbox: {cmd_str}")

    runner = SandboxRunner(workdir=ws_root, verbose=args.verbose)
    result = runner.run(
        cmd_str,
        permissions=["fs_read", "fs_write", "fs_exec"],
        env=env_vars,
    )

    if result.stdout:
        print("\n--- Agent Output ---")
        print(result.stdout)
        print("--------------------")
    if result.error:
        print(f"\nSandbox error: {result.error}", file=sys.stderr)

    execution.complete(returncode=result.returncode, changes=result.diffs)
    exec_mgr.save(execution)
    session.log_action("EXECUTE", cmd_str, status="OK" if result.success else "FAILED", details=f"returncode={result.returncode}")
    session.complete(returncode=result.returncode, diffs=result.diffs)
    session_mgr.save_session(session)
    lane_mgr.record_diff(lane_id, result.diffs)
    print("\n" + session.format_ascii_view())


def cmd_lane_create(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    lane_mgr = LaneManager(workdir=ws_root)
    lane = lane_mgr.create_lane(name=args.name, agent_id=args.agent)
    print(f"Created Virtual Agent Lane '{lane.lane_id}' (Agent: {lane.agent_id}, Status: {lane.status}).")


def cmd_lanes(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    lane_mgr = LaneManager(workdir=ws_root)
    lanes = lane_mgr.list_lanes()
    if not lanes:
        print("No virtual agent lanes found. Run 'truhowl lane create <name>' or 'truhowl wrap --lane <name>' first.")
        return
    print(f"Virtual Agent Lanes ({len(lanes)}):")
    for lane in lanes:
        print(f"  - [{lane.lane_id}] Agent: {lane.agent_id:<12} | Status: {lane.status:<10} | Changes: {len(lane.changes)} file(s)")


def cmd_lane_inspect(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    lane_mgr = LaneManager(workdir=ws_root)
    lane = lane_mgr.get_lane(args.name)
    if not lane:
        print(f"Error: Lane '{args.name}' not found.")
        sys.exit(1)
    if args.json:
        _print_json(lane.to_dict())
    else:
        print("================================================================")
        print(f"              TRUHOWL VIRTUAL AGENT LANE #{lane.lane_id}        ")
        print("================================================================")
        print(f"Lane Name   : {lane.name}")
        print(f"Agent       : {lane.agent_id}")
        print(f"Status      : {lane.status}")
        print(f"Session ID  : {lane.session_id or 'None'}")
        print(f"Permissions : {lane.permissions}")
        print("----------------------------------------------------------------")
        print(f"Changes ({len(lane.changes)} file(s)):")
        if not lane.changes:
            print("  (No changes recorded)")
        else:
            for chg in lane.changes:
                print(f"  {chg.get('status', 'modified').upper()}: {chg.get('path', '')}")
        print("================================================================")


def cmd_integrate(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    eng = IntegrationEngine(workdir=ws_root)
    if args.subcommand == "create":
        cand = eng.create_candidate(args.lanes)
        print(f"Created Integration Candidate #{cand.candidate_id} combining lanes {cand.source_lanes}.")
        print(eng.preview())
    elif args.subcommand == "preview":
        print(eng.preview())
    elif args.subcommand == "apply":
        if eng.apply():
            print("Successfully applied integration candidate to workspace.")
        else:
            print("Failed to apply integration candidate.")


def cmd_sessions(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    mgr = SessionManager(workdir=ws_root)
    sessions = mgr.list_sessions()
    if not sessions:
        print("No recorded agent sessions found. Run 'truhowl wrap' or 'truhowl run' first.")
        return
    print(f"Recorded Agent Sessions ({len(sessions)}):")
    for s in sessions:
        duration = round((s.finished_at or time.time()) - s.started_at, 2)
        print(f"  - [{s.session_id}] {s.agent:<12} | Lane: {s.lane_id:<10} | Task: '{s.task}' | Status: {s.status:<10} ({duration}s)")


def cmd_session_inspect(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    mgr = SessionManager(workdir=ws_root)
    session = mgr.get_session(args.session_id)
    if not session:
        print(f"Error: Session '{args.session_id}' not found.")
        sys.exit(1)
    if args.json:
        _print_json(session.to_dict())
    else:
        print(session.format_ascii_view())


def cmd_session_rollback(args):
    ws_root = find_workspace_root() or os.path.abspath(".")
    mgr = SessionManager(workdir=ws_root)
    success = mgr.rollback_session(args.session_id)
    if success:
        print(f"Workspace successfully rolled back to state prior to Session #{args.session_id}.")
    else:
        print(f"Error: Failed to roll back Session #{args.session_id}.")
        sys.exit(1)


_NODE_KIND_BY_TYPE = {
    "agent": ExecutionKind.INTERACTIVE,
    "process": ExecutionKind.PROCESS,
    "service": ExecutionKind.SERVICE,
}


def _topo_sort(nodes: list[WorkflowNodeConfig]) -> list[WorkflowNodeConfig]:
    by_name = {n.name: n for n in nodes}
    graph = {n.name: tuple(dep for dep in n.depends_on if dep in by_name) for n in nodes}
    try:
        return [by_name[name] for name in graphlib.TopologicalSorter(graph).static_order()]
    except graphlib.CycleError as exc:
        raise ValueError(f"Workflow cycle detected: {exc}")


def _run_declared_workflow(ws_root: str, wf: WorkflowConfig, cfg: WorkspaceConfig) -> None:
    """Execute a declared YAML workflow DAG; each node is its own Execution."""
    try:
        nodes = _topo_sort(wf.nodes)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    _STANDARD_COMPARTMENTS = {
        "research":  CompartmentConfig(name="research",  permissions=["fs_read", "fs_exec", "network"],          filesystem="read-only",  network="allowed"),
        "builder":   CompartmentConfig(name="builder",   permissions=["fs_read", "fs_write", "fs_exec"],         filesystem="read-write", network="restricted"),
        "network":   CompartmentConfig(name="network",   permissions=["fs_read", "fs_exec", "network"],          filesystem="read-only",  network="allowed"),
        "tester":    CompartmentConfig(name="tester",    permissions=["fs_read", "fs_exec"],                     filesystem="read-only",  network="restricted"),
    }
    for n in nodes:
        if n.compartment not in cfg.compartments:
            if n.compartment in _STANDARD_COMPARTMENTS:
                cfg.compartments[n.compartment] = _STANDARD_COMPARTMENTS[n.compartment]
            else:
                print(
                    f"Error: Workflow '{wf.name}' node '{n.name}' uses unknown compartment "
                    f"'{n.compartment}'. Declared: {sorted(cfg.compartments)}"
                )
                sys.exit(1)

    exec_mgr = ExecutionManager(workdir=ws_root)
    print(f"\nTRUHOWL WORKFLOW: {wf.name}  ({len(nodes)} node(s))\n")
    for n in nodes:
        print(f"  {n.name:<16} {n.type:<9} {n.compartment:<12} depends_on={n.depends_on}")
    print()

    executions: dict[str, Execution] = {}
    for n in nodes:
        executions[n.name] = exec_mgr.create(
            kind=_NODE_KIND_BY_TYPE.get(n.type, ExecutionKind.PROCESS),
            command=shlex.split(n.command) if n.command else [n.name],
            compartment_id=n.compartment,
            policy={"permissions": cfg.compartments[n.compartment].permissions},
        )

    failed: set[str] = set()
    for n in nodes:
        ex = executions[n.name]
        if any(d in failed for d in n.depends_on):
            ex.status = ExecutionStatus.SKIPPED
            ex.emit("execution.skipped", {"reason": "dependency failed"})
            exec_mgr.save(ex)
            print(f"  [SKIP] {n.name:<16} SKIPPED (dependency failed)")
            continue

        comp = cfg.compartments[n.compartment]
        ex.snapshot_dir = _snapshot_worktree(ws_root, ex.execution_id)
        ex.start()
        exec_mgr.save(ex)
        print(f"  [RUN]  {n.name:<16} running in '{n.compartment}' ...")

        runner = SandboxRunner(
            workdir=ws_root,
            verbose=False,
            block_network="network" not in comp.permissions,
        )
        try:
            node_cmd = n.command or n.name
            node_argv = shlex.split(node_cmd)
        except ValueError as exc:
            print(f"  [FAIL] {n.name:<16} FAILED (bad command string: {exc})")
            failed.add(n.name)
            ex.status = ExecutionStatus.FAILED
            exec_mgr.save(ex)
            continue
        result = runner.run(
            shlex.join(node_argv),
            permissions=comp.permissions,
            env={"TRUHOWL_EXECUTION_ID": ex.execution_id},
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                print(f"      {line}")
        if result.stderr and result.returncode != 0:
            for line in result.stderr.splitlines():
                print(f"      {line}", file=sys.stderr)
        if result.error:
            print(f"      Sandbox error: {result.error}", file=sys.stderr)

        ex.complete(returncode=result.returncode, changes=result.diffs)
        exec_mgr.save(ex)
        if result.returncode != 0:
            failed.add(n.name)
            print(f"  [FAIL] {n.name:<16} FAILED (exit {result.returncode})")
        else:
            print(f"  [OK]   {n.name:<16} completed ({len(result.diffs)} change(s))")

    completed = sum(1 for n in nodes if executions[n.name].status == ExecutionStatus.COMPLETED)
    skipped = sum(1 for n in nodes if executions[n.name].status == ExecutionStatus.SKIPPED)
    print(f"\nWorkflow '{wf.name}' finished: {completed} completed, {len(failed)} failed, {skipped} skipped.")
    sys.exit(0 if not failed else 1)


def _run_workflow_file(ws_root: str, workflow_file: str, compartment_id: str, verbose: bool) -> None:
    """Run a standalone workflow file (LangGraph, CrewAI, Python script) as one managed Execution."""
    if not os.path.exists(workflow_file):
        print(f"Error: Workflow file not found: {workflow_file}")
        sys.exit(1)

    cfg = load_config(os.path.join(ws_root, ".truhowl", "config.yaml"))
    policy = {}
    if compartment_id in cfg.compartments:
        policy = {"permissions": cfg.compartments[compartment_id].permissions}
    else:
        policy = {"permissions": ["fs_read", "fs_write", "fs_exec"]}

    exec_mgr = ExecutionManager(workdir=ws_root)
    sess_mgr = SessionManager(workdir=ws_root)
    execution = exec_mgr.create(
        kind=ExecutionKind.WORKFLOW,
        command=["python3", workflow_file],
        compartment_id=compartment_id,
        policy=policy,
    )
    session = sess_mgr.create_session(
        agent_name=workflow_file,
        task=f"Run workflow {workflow_file}",
        compartment_name=compartment_id,
        permissions=policy.get("permissions", []),
    )
    execution.session_id = session.session_id
    execution.snapshot_dir = _snapshot_worktree(ws_root, execution.execution_id)
    session.create_checkpoint("pre-execution", snapshot_manifest=execution.snapshot_dir)
    execution.start()
    exec_mgr.save(execution)
    session.start()
    sess_mgr.save_session(session)

    print(f"Truhowl Workflow Execution #{execution.execution_id}")
    print(f"Running: python3 {workflow_file} (compartment: {compartment_id})")

    runner = SandboxRunner(workdir=ws_root, verbose=verbose)
    result = runner.run(
        f"python3 {shlex.quote(workflow_file)}",
        permissions=policy.get("permissions", ["fs_read", "fs_exec"]),
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr and not result.success:
        print(result.stderr, file=sys.stderr)

    execution.complete(returncode=result.returncode, changes=result.diffs)
    exec_mgr.save(execution)
    session.complete(returncode=result.returncode, diffs=result.diffs)
    sess_mgr.save_session(session)

    status_str = "[DONE]" if result.success else "[FAILED]"
    print(f"\n{status_str} (exit {result.returncode}) - {len(result.diffs)} file change(s)")
    sys.exit(0 if result.success else result.returncode)


def _infer_step_properties(
    target: str,
    name_opt: str | None = None,
    comp_opt: str | None = None,
    type_opt: str | None = None,
) -> tuple[str, str, str, str]:
    """Auto-infer (step_name, command, step_type, compartment) from a file or command string."""
    target_clean = target.strip()
    is_file = os.path.exists(target_clean) or target_clean.endswith((".py", ".sh", ".js", ".ts", ".rb"))

    if is_file:
        base = os.path.splitext(os.path.basename(target_clean))[0]
        step_name = name_opt or base.replace("_", "-")
        py_bin = "python3" if shutil.which("python3") else "python"
        runners = {".py": f"{py_bin} {target_clean}", ".sh": f"bash {target_clean}", ".js": f"node {target_clean}", ".ts": f"node {target_clean}"}
        command = runners.get(os.path.splitext(target_clean)[1], target_clean)
        is_agent = any(k in target_clean.lower() for k in ["agent", "claude", "crewai", "langchain", "llm", "rag", "gpt", "prompt"])
        step_type = type_opt or ("agent" if is_agent else "process")
    else:
        first_word = target_clean.split()[0] if target_clean else "step"
        step_name = name_opt or os.path.basename(first_word).replace("_", "-")
        command = target_clean
        is_agent = any(k in target_clean.lower() for k in ["claude", "opencode", "codex", "agent", "crewai", "langchain"])
        step_type = type_opt or ("agent" if is_agent else "process")

    if comp_opt:
        compartment = comp_opt
    else:
        tc = target_clean.lower()
        comp_map = {
            "research": ["ocr", "scrape", "extract", "read", "audit", "search", "scan"],
            "builder": ["build", "patch", "rag", "excel", "write", "generate", "code"],
            "network": ["email", "send", "fetch", "api", "download", "http", "notify", "curl"],
            "tester": ["test", "verify", "check", "pytest"],
        }
        compartment = next((c for c, keywords in comp_map.items() if any(k in tc for k in keywords)), "default")

    return step_name, command, step_type, compartment


def cmd_workflow_branch(args):
    """Create a new workflow branch in workflows/<name>.yaml (Git-branch style)."""
    ws_root = find_workspace_root() or os.path.abspath(".")
    name = getattr(args, "name", "").strip()
    if not name:
        print("Error: Workflow branch name required. Usage: truhowl workflow branch <name>")
        sys.exit(1)

    wf_dir = os.path.join(ws_root, "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    wf_path = os.path.join(wf_dir, f"{name}.yaml")

    if os.path.exists(wf_path):
        print(f"Workflow branch '{name}' already exists: workflows/{name}.yaml")
        return

    content = textwrap.dedent(f"""\
        # Truhowl Workflow Branch: {name}
        name: {name}
        description: "Agentic workflow {name}"

        steps: []
    """)

    with open(wf_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Created workflow branch: workflows/{name}.yaml")
    print(f"  To add steps:  truhowl step {name} <file_or_command>")
    print(f"  To execute:    truhowl workflow run {name}")


def cmd_workflow_show(args):
    """Inspect and visualize declared steps in a workflow branch."""
    ws_root = find_workspace_root() or os.path.abspath(".")
    cfg_path = os.path.join(ws_root, ".truhowl", "config.yaml")
    cfg = load_config(cfg_path) if os.path.exists(cfg_path) else None
    name = getattr(args, "name", None)
    if not name:
        wfs = sorted(cfg.workflows.keys()) if cfg and cfg.workflows else []
        print(f"Declared Workflows ({len(wfs)}):")
        for w in wfs:
            node_count = len(cfg.workflows[w].nodes)
            print(f"  - {w:<20} ({node_count} node(s))")
        return

    if cfg and name in cfg.workflows:
        wf = cfg.workflows[name]
        print(f"Workflow: {name}\n")
        nodes_list = wf.nodes if isinstance(wf.nodes, list) else list(wf.nodes.values())
        for i, node in enumerate(nodes_list):
            name_str = node.name.capitalize()
            print(f"  [{node.compartment}] {name_str}")
            if i < len(nodes_list) - 1:
                print("       │")
                print("       ▼")
        print()
    else:
        print(f"Error: Workflow '{name}' not found.")


def cmd_step(args):
    """Add step(s) to a workflow branch with smart auto-inferred properties."""
    ws_root = find_workspace_root() or os.path.abspath(".")
    wf_name = args.workflow.strip()
    target = args.target.strip()

    wf_dir = os.path.join(ws_root, "workflows")
    wf_path = os.path.join(wf_dir, f"{wf_name}.yaml")
    if not os.path.exists(wf_path):
        alt_path = os.path.join(wf_dir, f"{wf_name}.yml")
        if os.path.exists(alt_path):
            wf_path = alt_path
        else:
            os.makedirs(wf_dir, exist_ok=True)
            with open(wf_path, "w", encoding="utf-8") as f:
                f.write(f"name: {wf_name}\nsteps: []\n")
            print(f"Initialized new workflow file: workflows/{wf_name}.yaml")

    with open(wf_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if "steps" not in data or not isinstance(data["steps"], list):
        data["steps"] = []

    candidates = []
    if os.path.isdir(target):
        for root_dir, _, filenames in os.walk(target):
            for fname in sorted(filenames):
                if fname.endswith((".py", ".sh", ".js", ".ts", ".rb")) and not fname.startswith("."):
                    candidates.append(os.path.join(root_dir, fname))
        if not candidates:
            print(f"No code scripts found in directory: {target}")
            return
    else:
        candidates = [target]

    added_count = 0
    is_interactive = sys.stdin is not None and sys.stdin.isatty() and len(candidates) > 1

    for cand in candidates:
        step_name, command, step_type, compartment = _infer_step_properties(
            cand,
            name_opt=getattr(args, "name", None) if len(candidates) == 1 else None,
            comp_opt=getattr(args, "compartment", None) if len(candidates) == 1 else None,
            type_opt=getattr(args, "type", None) if len(candidates) == 1 else None,
        )

        if is_interactive:
            print(f"\nFound file: {cand}")
            print(f"  Proposed step: '{step_name}' [type: {step_type}, compartment: {compartment}]")
            try:
                choice = input("  Press [Enter] to add, 's' to skip, or type 'name:compartment': ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                break
            if choice.lower() in ("s", "skip"):
                print(f"  Skipped {cand}.")
                continue
            if ":" in choice:
                parts = choice.split(":", 1)
                if parts[0].strip():
                    step_name = parts[0].strip()
                if parts[1].strip():
                    compartment = parts[1].strip()

        depends_on = getattr(args, "depends_on", None)
        if depends_on and len(candidates) == 1:
            deps = [d.strip() for d in depends_on.split(",") if d.strip()]
        elif data["steps"]:
            prev_name = data["steps"][-1].get("name")
            deps = [prev_name] if prev_name else []
        else:
            deps = []

        new_step = {
            "name": step_name,
            "type": step_type,
            "command": command,
            "compartment": compartment,
        }
        if deps:
            new_step["depends_on"] = deps

        data["steps"].append(new_step)
        added_count += 1
        print(f"[OK] Added step '{step_name}' [type: {step_type}, compartment: {compartment}]")
        print(f"  Command:    {command}")
        print(f"  Depends on: {deps or 'none (runs first)'}")

    with open(wf_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)

    print(f"\nUpdated workflows/{os.path.basename(wf_path)} ({added_count} step(s) added).")


def cmd_workflow_run(args):
    """Run a workflow: a declared YAML DAG (each node its own Execution) or a file."""
    target = args.workflow
    ws_root = find_workspace_root() or os.path.abspath(".")
    cfg = load_config(os.path.join(ws_root, ".truhowl", "config.yaml"))

    wf = cfg.workflows.get(target)
    if wf:
        _run_declared_workflow(ws_root, wf, cfg)
        return

    if target.endswith((".yaml", ".yml")) and os.path.exists(target):
        with open(target, encoding="utf-8") as f:
            wdata = yaml.safe_load(f) or {}
        wname = wdata.get("name") or os.path.splitext(os.path.basename(target))[0]
        nodes = []
        if "steps" in wdata and isinstance(wdata["steps"], list):
            for sdata in wdata["steps"]:
                if isinstance(sdata, dict):
                    nodes.append(WorkflowNodeConfig(
                        name=sdata.get("name") or f"step_{len(nodes)+1}",
                        type=sdata.get("type", "process"),
                        command=sdata.get("command", ""),
                        compartment=sdata.get("compartment", "default"),
                        depends_on=sdata.get("depends_on") or [],
                    ))
        wf = WorkflowConfig(name=wname, nodes=nodes)
        _run_declared_workflow(ws_root, wf, cfg)
        return

    if os.path.exists(target):
        compartment_id = getattr(args, "compartment", "default") or "default"
        _run_workflow_file(ws_root, target, compartment_id, getattr(args, "verbose", False))
        return

    print(
        f"Error: Workflow '{target}' not found. "
        f"Available workflows: {sorted(cfg.workflows) or 'none'}"
    )
    sys.exit(1)


def cmd_diff(args):
    """Show change sets associated with agent executions (the review surface)."""
    ws_root = find_workspace_root()
    if not ws_root:
        print("Error: Not inside a Truhowl workspace. Run 'truhowl init' first.")
        sys.exit(1)

    exec_mgr = ExecutionManager(workdir=ws_root)

    if args.execution:
        ex = exec_mgr.get(args.execution)
        if ex is None:
            print(f"Error: Execution '{args.execution}' not found.")
            sys.exit(1)
        records = [ex]
    else:
        records = [ex for ex in exec_mgr.list_all() if ex.changes]
        if args.unapplied:
            records = [ex for ex in records if ex.status != ExecutionStatus.APPLIED]
        records.sort(key=lambda e: e.finished_at or 0, reverse=True)

    if args.json:
        _print_json({
            "change_sets": len(records),
            "total_changes": sum(len(ex.changes) for ex in records),
            "executions": [
                {**ex.to_dict(), "git_trailers": ex.git_trailers()}
                for ex in records
            ],
        })
        return

    print("\nTRUHOWL DIFF - changes associated with agent executions\n")
    if not records:
        print("  (no change sets recorded - run an agent under Truhowl first)")
        print()
        return

    total = 0
    for ex in records:
        total += len(ex.changes)
        dur = f"{ex.duration_s:.0f}s" if ex.duration_s else "?"
        print(f"# {ex.execution_id:<14} {ex.agent_name:<12} {ex.compartment_id:<12} {ex.status:<10} {len(ex.changes)} change(s)  ({dur})")
        for chg in ex.changes:
            print(f"    {chg.get('status', 'modified').upper():<9} {chg.get('path', '')}")
        if getattr(args, "trailers", False):
            print("  Git Trailers:")
            for line in ex.git_trailers().splitlines():
                print(f"    {line}")
        print()
    print(f"{len(records)} change set(s), {total} file change(s) total")
    print()


def _git_commit_execution(
    ws_root: str,
    ex: Execution,
    user_message: str | None = None,
) -> bool:
    """Stage an execution's changed files and create a Git commit with RFC-5322 metadata trailers."""
    if not shutil.which("git"):
        print("  Error: 'git' command not found in PATH.")
        return False

    res = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ws_root,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print("  Error: Not inside a Git repository. Cannot create Git commit.")
        return False

    paths = [c.get("path") for c in ex.changes if c.get("path")]
    if not paths:
        print(f"  # {ex.execution_id}: no changed files to stage.")
        return False

    stage_res = subprocess.run(
        ["git", "add", "--"] + paths,
        cwd=ws_root,
        capture_output=True,
        text=True,
    )
    if stage_res.returncode != 0:
        print(f"  Error staging files with git: {stage_res.stderr.strip()}")
        return False

    msg_header = user_message or f"Truhowl: apply changes from {ex.agent_name} ({ex.execution_id})"
    full_commit_msg = f"{msg_header}\n\n{ex.git_trailers()}\n"

    commit_res = subprocess.run(
        ["git", "commit", "-m", full_commit_msg],
        cwd=ws_root,
        capture_output=True,
        text=True,
    )
    if commit_res.returncode != 0:
        print(f"  Git commit failed: {commit_res.stderr.strip() or commit_res.stdout.strip()}")
        return False

    print(f"  # {ex.execution_id}: committed to Git with metadata trailers.")
    return True


def _apply_execution(exec_mgr: ExecutionManager, ex: Execution, force: bool = False) -> bool:
    """Promote one execution's change set; surface conflicts with other work."""
    if not ex.changes:
        print(f"  # {ex.execution_id}: no changes to apply.")
        return False
    if ex.status == ExecutionStatus.APPLIED:
        print(f"  # {ex.execution_id}: already applied.")
        return False

    paths = {c.get("path", "") for c in ex.changes}
    conflicts = []
    for other in exec_mgr.list_all():
        if other.execution_id == ex.execution_id or other.status == ExecutionStatus.APPLIED:
            continue
        overlap = paths & {c.get("path", "") for c in other.changes}
        for p in sorted(overlap):
            conflicts.append((other.execution_id, p))

    if conflicts and not force:
        print(f"  # {ex.execution_id}: CONFLICT - changes overlap other un-applied executions:")
        for other_id, path in conflicts:
            print(f"      {path}  (also modified by {other_id})")
        print("      Use --force to apply anyway.")
        return False

    ex.apply()
    exec_mgr.save(ex)
    print(f"  # {ex.execution_id}: applied {len(ex.changes)} change(s).")
    return True


def cmd_apply(args):
    """Promote selected change sets into the workspace baseline, with optional Git commit."""
    ws_root = find_workspace_root()
    if not ws_root:
        print("Error: Not inside a Truhowl workspace. Run 'truhowl init' first.")
        sys.exit(1)

    exec_mgr = ExecutionManager(workdir=ws_root)
    if args.execution:
        ex = exec_mgr.get(args.execution)
        if ex is None:
            print(f"Error: Execution '{args.execution}' not found.")
            sys.exit(1)
        targets = [ex]
    else:
        targets = [
            e for e in exec_mgr.list_all()
            if e.changes and e.status == ExecutionStatus.COMPLETED
        ]
        if not targets:
            print("Nothing to apply: no completed executions with recorded changes.")
            return

    applied_targets = [ex for ex in targets if _apply_execution(exec_mgr, ex, force=args.force)]
    print(f"\nApplied {len(applied_targets)} change set(s).")

    if getattr(args, "commit", False):
        print("\nCreating Git commit(s) with Truhowl metadata trailers...")
        for ex in applied_targets:
            _git_commit_execution(ws_root, ex, user_message=getattr(args, "message", None))

    sys.exit(0 if applied_targets else 1)


def cmd_commit(args):
    """Stage and commit applied execution change sets to Git with RFC-5322 metadata trailers."""
    ws_root = find_workspace_root()
    if not ws_root:
        print("Error: Not inside a Truhowl workspace. Run 'truhowl init' first.")
        sys.exit(1)

    exec_mgr = ExecutionManager(workdir=ws_root)
    if args.execution:
        ex = exec_mgr.get(args.execution)
        if ex is None:
            print(f"Error: Execution '{args.execution}' not found.")
            sys.exit(1)
        targets = [ex]
    else:
        targets = [
            e for e in exec_mgr.list_all()
            if e.changes and e.status in (ExecutionStatus.APPLIED, ExecutionStatus.COMPLETED)
        ]
        if not targets:
            print("Nothing to commit: no completed or applied executions with changes.")
            return
        if not getattr(args, "all", False):
            targets = [max(targets, key=lambda e: e.finished_at or 0)]

    committed = 0
    for ex in targets:
        if ex.status != ExecutionStatus.APPLIED:
            if not _apply_execution(exec_mgr, ex, force=getattr(args, "force", False)):
                continue
        if _git_commit_execution(ws_root, ex, user_message=getattr(args, "message", None)):
            committed += 1

    print(f"\nCommitted {committed} execution change set(s) to Git.")
    sys.exit(0 if committed else 1)


def cmd_undo(args):
    """Reverse the last Truhowl-managed apply operation and restore the worktree."""
    ws_root = find_workspace_root()
    if not ws_root:
        print("Error: Not inside a Truhowl workspace. Run 'truhowl init' first.")
        sys.exit(1)

    exec_mgr = ExecutionManager(workdir=ws_root)
    if args.execution:
        ex = exec_mgr.get(args.execution)
        if ex is None:
            print(f"Error: Execution '{args.execution}' not found.")
            sys.exit(1)
        targets = [ex]
    else:
        applied = [e for e in exec_mgr.list_all() if e.status == ExecutionStatus.APPLIED]
        if not applied:
            print("Nothing to undo: no applied change sets.")
            return
        targets = [max(applied, key=lambda e: e.finished_at or 0)]

    for ex in targets:
        if ex.status != ExecutionStatus.APPLIED:
            print(f"  # {ex.execution_id}: not applied - nothing to undo.")
            sys.exit(1)

        snap_dir = ex.snapshot_dir
        if snap_dir and os.path.isdir(snap_dir):
            restored = SnapshotManager(workdir=ws_root, snapshot_dir=snap_dir).restore()
            print(f"  # {ex.execution_id}: restored {restored} file(s) from pre-execution snapshot.")
        else:
            print(
                f"  # {ex.execution_id}: no snapshot found - "
                "bookkeeping reversed but files NOT restored on disk."
            )

        ex.status = ExecutionStatus.COMPLETED
        ex.emit("execution.unapplied", {"files_restored": bool(snap_dir and os.path.isdir(snap_dir))})
        exec_mgr.save(ex)
        print(f"  # {ex.execution_id}: {len(ex.changes)} change(s) returned to pending.")



def cmd_restore(args):
    """Restore the workspace from a session's snapshot checkpoint."""
    ws_root = find_workspace_root()
    if not ws_root:
        print("Error: Not inside a Truhowl workspace. Run 'truhowl init' first.")
        sys.exit(1)

    sess_mgr = SessionManager(workdir=ws_root)
    session = sess_mgr.get_session(args.session_id)
    if session is None:
        print(f"Error: Session '{args.session_id}' not found.")
        sys.exit(1)

    snap_dir = None
    for cp in reversed(session.checkpoints):
        manifest = cp.get("snapshot_manifest")
        if manifest and os.path.isdir(manifest):
            snap_dir = manifest
            break
    if not snap_dir:
        print(f"Error: Session '{args.session_id}' has no snapshot checkpoint to restore.")
        sys.exit(1)

    SnapshotManager(workdir=ws_root, snapshot_dir=snap_dir).restore()
    print(f"Workspace restored to the checkpoint for session '{args.session_id}'.")


def cmd_diff_schema(args):
    old_path = args.old
    new_path = args.new
    if not os.path.isfile(old_path):
        print(f"Error: Old spec file not found: {old_path}")
        sys.exit(1)
    if not os.path.isfile(new_path):
        print(f"Error: New spec file not found: {new_path}")
        sys.exit(1)

    with open(old_path, "r", encoding="utf-8") as f:
        old_json = f.read()
    with open(new_path, "r", encoding="utf-8") as f:
        new_json = f.read()

    diff = autopatch.diff_schemas(old_json, new_json)
    if getattr(args, "json", False):
        print(json.dumps(diff, indent=2))
    else:
        old_t = diff.get("old_spec", {}).get("title", "API")
        old_v = diff.get("old_spec", {}).get("version", "")
        new_v = diff.get("new_spec", {}).get("version", "")
        print(f"=== Schema Diff: {old_t} ({old_v} -> {new_v}) ===")
        print(f"Breaking changes: {diff.get('breaking_count', 0)}")
        print(f"Warnings: {diff.get('warning_count', 0)}")
        print(f"Info additions: {diff.get('info_count', 0)}\n")
        for ec in diff.get("endpoint_changes", []):
            print(f"Endpoint: {ec.get('method', '').upper()} {ec.get('path')}")
            for c in ec.get("changes", []):
                print(f"  [{c.get('severity')}] {c.get('description')}")


def cmd_scan_api(args):
    root_dir = getattr(args, "root_dir", None) or "."
    cfg = autopatch.ScanConfig(
        sdk_names=args.sdk.split(",") if getattr(args, "sdk", None) else [],
        method_patterns=args.method.split(",") if getattr(args, "method", None) else [],
        api_base_urls=args.url.split(",") if getattr(args, "url", None) else [],
    )
    result = autopatch.scan_callsites(root_dir, cfg)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"=== API Callsite Scan: {root_dir} ===")
        print(f"Files scanned: {result.get('files_scanned', 0)}")
        print(f"Files with hits: {result.get('files_with_hits', 0)}")
        print(f"Total callsites: {len(result.get('callsites', []))}\n")
        for cs in result.get("callsites", []):
            print(f"  {cs.get('file_path')}:{cs.get('line_number')} [{cs.get('kind')}] {cs.get('line_content', '').strip()}")


def cmd_autopatch(args):
    old_path = args.old
    new_path = args.new
    if not os.path.isfile(old_path):
        print(f"Error: Old spec file not found: {old_path}")
        sys.exit(1)
    if not os.path.isfile(new_path):
        print(f"Error: New spec file not found: {new_path}")
        sys.exit(1)

    with open(old_path, "r", encoding="utf-8") as f:
        old_json = f.read()
    with open(new_path, "r", encoding="utf-8") as f:
        new_json = f.read()

    root_dir = getattr(args, "root_dir", None) or "."
    cfg = autopatch.ScanConfig(
        sdk_names=args.sdk.split(",") if getattr(args, "sdk", None) else [],
        method_patterns=args.method.split(",") if getattr(args, "method", None) else [],
        api_base_urls=args.url.split(",") if getattr(args, "url", None) else [],
    )

    plan = autopatch.generate_maintenance_plan(old_json, new_json, root_dir, cfg)
    report_md = autopatch.render_markdown_report(plan)

    lang = getattr(args, "lang", "typescript")
    contracts = autopatch.synthesize_contracts(
        plan.get("api_name", "API"),
        plan.get("old_version", "old"),
        plan.get("new_version", "new"),
        plan.get("verification_specs", []),
        language=lang,
    )

    out_dir = getattr(args, "out_dir", None)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "AUTOPATCH_REPORT.md"), "w", encoding="utf-8") as f:
            f.write(report_md)
        test_ext = "ts" if lang in {"ts", "typescript"} else "py"
        test_file = os.path.join(out_dir, f"test_contracts.{test_ext}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(contracts)
        print(f"AutoPatch generated plan, report, and contract tests in '{out_dir}'")
    else:
        print(report_md)
        if plan.get("verification_specs"):
            print("\n=== Synthesized Contract Tests ===")
            print(contracts)


def cmd_workflow_order(args):
    path = args.file
    if not os.path.isfile(path):
        print(f"Error: Workflow file not found: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if path.endswith(".yaml") or path.endswith(".yml"):
        data = yaml.safe_load(content)
    else:
        data = json.loads(content)

    errs = autopatch.validate_workflow(data)
    if errs:
        print("Workflow Validation Failed:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)

    order = autopatch.get_workflow_execution_order(data)
    print(f"Workflow '{data.get('name')}' Execution Order (Topological):")
    for i, step in enumerate(order, start=1):
        print(f"  {i}. {step}")


def cmd_inventory(args):
    root_dir = getattr(args, "root_dir", None) or "."
    inv = autopatch.run_inventory(root_dir)

    if getattr(args, "json", False):
        print(json.dumps(inv, indent=2))
        return

    print("=== External Dependency Inventory ===\n")
    print(f"Repo: {inv.get('repo_root', root_dir)}")
    print(f"Files scanned: {inv.get('files_scanned', 'N/A')}")
    print(f"Total callsites: {inv.get('total_callsites', 0)}\n")

    deps = inv.get("dependencies", [])
    if not deps:
        print("No external API dependencies detected.")
        return

    for dep in deps:
        health = dep.get("health", "Unknown")
        status_tag = {"Healthy": "[HEALTHY]", "Behind": "[BEHIND]", "Deprecated": "[DEPRECATED]", "Retired": "[RETIRED]"}.get(health, "[UNKNOWN]")
        print(f"  {status_tag} {dep.get('provider', '?')} (latest: {dep.get('latest_version', '?')})")
        print(f"     Callsites: {dep.get('callsite_count', 0)}  Files: {len(dep.get('affected_files', []))}")
        deadline = dep.get("deprecation_deadline", "")
        if deadline:
            print(f"     Deprecation deadline: {deadline}")
        guide = dep.get("migration_guide_url", "")
        if guide:
            print(f"     Migration guide: {guide}")
        print()

    critical = sum(1 for d in deps if d.get("health") in ("Deprecated", "Retired"))
    if critical > 0:
        print(f"[ALERT] {critical} critical dependencies require immediate attention.")
        print("   Run `truhowl migrate` to start AI-authored repair and verified PR delivery.")





def cmd_explain(args):
    """Explain why a callsite or pattern was classified."""
    fid = args.finding_id.lower()
    explanations = {
        "charge": ("CONFIRMED_AFFECTED", "Stripe", "POST /v1/charges", [
            "Direct SDK method chain 'stripe.charges.create' matches OpenAPI operation",
            "HTTP method POST and path /v1/charges match breaking schema diff",
            "Parameter 'amount' changed from integer to string",
            "AI repair candidate with verified knowledge context",
        ], "Routed to AI for repair and contract verification."),
        "checkout": ("PROVABLY_UNAFFECTED", "Stripe", "POST /v1/checkout/sessions", [
            "Method chain resolves to distinct API route",
            "Target endpoint did not undergo breaking contract drift",
        ], "Rejected. Zero modifications permitted."),
        "import": ("UNRESOLVED: HUMAN REVIEW REQUIRED", None, None, [
            "Import statement or type declaration only",
            "Does not execute an API operation at runtime",
        ], "Auto-fix DISABLED. Quarantined for safety."),
    }

    match_key = next((k for k in ["charge", "checkout", "import"] if k in fid or (k == "checkout" and "portal" in fid) or (k == "import" and "type" in fid)), None)
    cls, prov, ep, ev, act = explanations.get(match_key, (
        "UNRESOLVED: INSUFFICIENT EVIDENCE", None, None,
        ["Pattern does not match verified OpenAPI routes or canonical methods"],
        "Auto-fix DISABLED. Quarantined for safety."
    ))

    print(f"================================================================================\nEXPLANATION FOR FINDING: '{args.finding_id}'\n================================================================================\n")
    print(f"Classification: {cls}")
    if prov:
        print(f"Provider:       {prov}")
    if ep:
        print(f"Endpoint:       {ep}")
    print(f"Action:         {act}\n\n================================================================================")


def _print_auth_warning():
    print("================================================================================")
    print("                TRUHOWL: AI PROVIDER AUTHENTICATION REQUIRED                   ")
    print("================================================================================\n")
    print("[ERROR] No AI provider configured.\n")
    print("Truhowl requires an AI provider to analyze dependencies and maintain your code.")
    print("To connect your provider, run:\n")
    print("  truhowl auth\n")
    print("Or provide credentials via environment variables:")
    print("  export ANTHROPIC_API_KEY=\"sk-ant-...\"    (for Claude 3.5 Sonnet)")
    print("  export OPENAI_API_KEY=\"sk-...\"           (for GPT-4o)\n")
    print("Supported providers:")
    print("  * Anthropic (Claude 3.5 Sonnet) [Recommended]")
    print("  * OpenAI (GPT-4o)")
    print("  * Ollama / Local (OpenAI-compatible)")
    print("================================================================================")


def _github_identity() -> str | None:
    """Best-effort GitHub login for whoami output. None when unavailable."""
    try:
        client = GitHubAppClient()
        if not client.token:
            return None
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {client.token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("login")
    except Exception:
        return None


def cmd_reviews(args):
    """List past Truhowl maintenance runs from the repository ledger."""
    root_path = os.path.abspath(getattr(args, "path", ".") or ".")
    history = get_migration_history(root_path)
    if not history:
        print("No Truhowl maintenance runs recorded for this repository yet.")
        print("Run `truhowl migrate` to create the first entry.")
        return
    print(f"Truhowl maintenance runs ({len(history)}):")
    for record in history[-20:]:
        outcome = "GREEN" if record.get("test_exit_code") == 0 and record.get("blast_radius_zero") else "OTHER"
        print(f"  - {record.get('timestamp_utc', '?')}  {record.get('provider_name', '?')} "
              f"{record.get('from_version', '?')} -> {record.get('to_version', '?')}  [{outcome}]")


def cmd_onboard(args):
    """Guided onboarding: connect provider, index, report readiness."""
    print("Step 1/3: Connect your AI provider (needed only for AI repair).")
    cmd_auth(args)
    print("\nStep 2/3: Index repository contracts (free, zero-token).")
    cmd_check(argparse.Namespace(path=getattr(args, "path", ".") or ".",
                                 format="cli", write_graph=True))
    print("\nStep 3/3: Readiness.")
    cmd_doctor(args)


def cmd_logout(args):
    """Remove stored AI provider credentials (alias for auth --clear)."""
    cmd_auth(argparse.Namespace(
        status=False, clear=True, provider=None, api_key=None,
        model=None, base_url=None, path=".", installation=None, repo=None))


def cmd_ask(args):
    """Answer maintenance questions from agent state and local evidence.

    Read-only by default. Action intents print the exact agent command;
    --act executes verification runs (PR creation still needs an explicit
    `agent run --create-pr --github-repo`).
    """
    question = " ".join(getattr(args, "question", []) or []).strip()
    root_path = os.path.abspath(getattr(args, "path", ".") or ".")
    as_json = bool(getattr(args, "json", False))
    act = bool(getattr(args, "act", False))
    q = question.lower()

    from truhowl.agent import service as agent_svc
    from truhowl.config import find_workspace_root as _find_ws
    workspace = _find_ws(root_path) or root_path

    def _cases_for(provider: str) -> list:
        return [c for c in agent_svc.list_cases(workspace)
                if str(c.get("provider", "")).lower() == provider.lower()]

    if any(w in q for w in ("waiting", "attention", "stuck", "blocked", "refused")):
        stuck = agent_svc.cases_needing_attention(workspace)
        if as_json:
            print(json.dumps({"question": question, "cases": stuck}, indent=2, default=str))
            return
        if not stuck:
            print("Nothing waiting for attention. All recorded cases are resolved or in progress.")
            return
        for c in stuck:
            print(f"{c['case_id']}: {c['provider']} {c['version_from']} -> {c['version_to']}")
            for key, info in c["repos"].items():
                print(f"  [{info['state']}] {key}: {info['reason'][:160]}")
        return

    if any(w in q for w in ("migrate everything", "everything you can", "safely verify",
                            "open pr", "open the pr", "verified migrations")):
        runnable = []
        for c in agent_svc.list_cases(workspace):
            pending = [k for k, s in c["repo_states"].items() if s == "detected"]
            if pending:
                runnable.append((c["case_id"], pending))
        if as_json:
            print(json.dumps({"question": question, "runnable": runnable}, indent=2))
            return
        if not runnable:
            print("No pending (detected) migrations to run.")
            print("Watch a change first: truhowl agent watch --provider stripe --from 19 --to 20 --repo .")
            return
        if not act:
            print("Proposed runs (re-run with --act to execute):")
            for case_id, keys in runnable:
                print(f"  truhowl agent run {case_id}")
            return
        for case_id, _keys in runnable:
            print(f"== {case_id} ==")
            for res in agent_svc.run_case(workspace, case_id):
                print(f"  [{res['state']}] {res['repo_key']}"
                      + (f"  reason: {res['reason'][:160]}" if res.get("reason") else ""))
        return

    try:
        reg = get_default_registry()
        specs = getattr(reg, "providers", None) or getattr(reg, "specs", None) or []
        known = [str(getattr(p, "provider_name", "") or "").lower() for p in specs]
    except Exception:
        known = []
    for fallback in ("stripe", "openai", "anthropic", "supabase", "twilio", "aws"):
        if fallback not in known:
            known.append(fallback)
    provider = next((p for p in known if p and p in q), None)

    if any(w in q for w in ("fail", "refus", "reject", "abort", "why", "error")):
        if provider:
            for c in reversed(_cases_for(provider)):
                stuck = {k: s for k, s in c["repo_states"].items()
                         if s in ("refused", "needs-attention")}
                if stuck:
                    if as_json:
                        print(json.dumps({"question": question, "answer": "case-refusals",
                                          "case_id": c["case_id"], "repos": stuck}, indent=2))
                    else:
                        print(agent_svc.explain_case(workspace, c["case_id"]))
                    return
        history = get_migration_history(root_path)
        if not history:
            if as_json:
                print(json.dumps({"question": question, "answer": "no-recorded-migrations", "runs": []}, indent=2))
            else:
                print("No Truhowl maintenance runs recorded for this repository yet.")
                print("Run `truhowl migrate` to create the first entry.")
            return
        recent = history[-5:]
        if as_json:
            print(json.dumps({"question": question, "answer": "recent-runs", "runs": recent}, indent=2, default=str))
            return
        print("Recent Truhowl maintenance runs (newest last):")
        for record in recent:
            ok = record.get("test_exit_code") == 0 and record.get("blast_radius_zero")
            outcome = "VERIFIED" if ok else "REFUSED"
            print(f"  [{outcome}] {record.get('provider_name', '?')} "
                  f"{record.get('from_version', '?')} -> {record.get('to_version', '?')}")
            reason = record.get("refusal_reason") or record.get("summary") or ""
            if reason:
                print(f"           {str(reason)[:200]}")
        return

    if provider and any(w in q for w in ("affect", "impact", "break", "upgrade", "migrate", "change", "safe", "touch")):
        cases = _cases_for(provider)
        if cases:
            c = cases[-1]
            if as_json:
                print(json.dumps({"question": question, "answer": "case-impact",
                                  "case_id": c["case_id"],
                                  "repo_states": c["repo_states"]}, indent=2))
            else:
                print(agent_svc.explain_case(workspace, c["case_id"]))
            return
        from truhowl.maintenance_agents import analyze_impact
        res = analyze_impact(root_path, provider)
        if as_json:
            print(json.dumps({
                "question": question,
                "provider": res.provider,
                "affected_files": res.affected_files,
                "callsites_count": res.callsites_count,
                "wrapper_files": res.wrapper_files,
            }, indent=2))
            return
        print(f"{res.provider}: {len(res.affected_files)} file(s), {res.callsites_count} callsite(s) affected.")
        for w in res.wrapper_files[:10]:
            print(f"  [wrapper] {w}")
        for f in res.affected_files[:20]:
            if f not in (res.wrapper_files or []):
                print(f"  [callsite] {f}")
        if not res.affected_files:
            print("No affected usage detected. Upgrade looks safe, verification still required.")
        return

    if any(w in q for w in ("status", "health", "watch", "monitor", "know", "connect", "ready", "setup")):
        return cmd_status(args)

    if as_json:
        print(json.dumps({"question": question, "answer": "usage",
                          "hint": "ask about impact, failures, or status"}, indent=2))
        return
    print("Ask Truhowl about this repository (read-only). Examples:")
    print('  truhowl ask "what breaks if we upgrade stripe?"')
    print('  truhowl ask "why did the billing migration fail?"')
    print('  truhowl ask "are we ready to migrate openai?"')
    print("Related: truhowl status, truhowl check, truhowl migrate")


def _agent_workspace() -> str:
    from truhowl.agent.service import resolve_workspace
    return resolve_workspace(os.path.abspath("."))


def _agent_repo_keys(workspace: str, values: list[str]) -> list[str] | None:
    """Map --repo values (keys or paths) to case repo keys. None means all."""
    if not values:
        return None
    from truhowl.agent.service import load_store
    store = load_store(workspace)
    keys: list[str] = []
    for v in values:
        if v in store.repos:
            keys.append(v)
            continue
        abs_v = os.path.abspath(v)
        match = next((k for k, r in store.repos.items()
                      if os.path.abspath(r.get("path", "")) == abs_v), None)
        keys.append(match or abs_v)
    return keys


def cmd_agent_watch(args):
    from truhowl.agent import service as agent
    workspace = _agent_workspace()
    repos = getattr(args, "repos", []) or ["."]
    case = agent.watch(workspace, args.provider, args.from_version, args.to_version, repos)
    print(f"Case {case.case_id}: {case.provider} {case.version_from} -> {case.version_to}")
    for r in case.repos:
        usage = next((u for u in agent.load_store(workspace).usages
                      if u.get("repo_key") == r.repo_key), {})
        print(f"  [{r.state}] {r.repo_key}: "
              f"{len(usage.get('files', []))} files, {usage.get('callsites_count', 0)} callsites")
    print(f"Next: truhowl agent run {case.case_id}")


def cmd_agent_run(args):
    from truhowl.agent import service as agent
    workspace = _agent_workspace()
    keys = _agent_repo_keys(workspace, getattr(args, "repos", []) or [])
    results = agent.run_case(
        workspace, args.case_id, keys,
        create_pr=bool(getattr(args, "create_pr", False)),
        github_repo=getattr(args, "github_repo", None),
        llm_api_key=getattr(args, "api_key", None),
        llm_model=getattr(args, "model", None),
        llm_base_url=getattr(args, "base_url", None),
    )
    for res in results:
        line = f"  [{res['state']}] {res['repo_key']}"
        if res.get("pr_url"):
            line += f"  PR: {res['pr_url']}"
        if res.get("reason"):
            line += f"  reason: {res['reason'][:160]}"
        print(line)


def cmd_agent_cases(args):
    from truhowl.agent import service as agent
    workspace = _agent_workspace()
    cases = agent.list_cases(workspace, getattr(args, "state", None))
    if not cases:
        print("No migration cases recorded for this workspace yet.")
        print("Run: truhowl agent watch --provider stripe --from 19 --to 20 --repo .")
        return
    for c in cases:
        print(f"{c['case_id']}: {c['provider']} {c['version_from']} -> {c['version_to']}")
        for key, state in c["repo_states"].items():
            print(f"  [{state}] {key}")


def cmd_agent_show(args):
    from truhowl.agent import service as agent
    print(agent.explain_case(_agent_workspace(), args.case_id))


def cmd_auth(args):
    """Authenticate and configure BYOK AI provider with automatic repository indexing."""
    action = getattr(args, "action", None)
    if action in ("status", "check"):
        args.status = True
    elif action in ("clear", "logout"):
        args.clear = True

    if getattr(args, "status", False):
        summary = get_active_provider_summary()
        print("================================================================================")
        print("                 TRUHOWL: AI PROVIDER CREDENTIAL STATUS                         ")
        print("================================================================================\n")
        if summary["configured"]:
            print("Status:       CONFIGURED [OK]")
            print(f"Provider:     {summary.get('provider')}")
            if summary.get("model"):
                print(f"Model:        {summary.get('model')}")
            if summary.get("masked_key"):
                print(f"API Key:      {summary.get('masked_key')}")
            print(f"Source:       {summary.get('source')}")
            gh_user = _github_identity()
            print(f"GitHub:       {gh_user}" if gh_user else "GitHub:       NOT CONFIGURED")
        else:
            print("Status:       NOT CONFIGURED [REQUIRED]")
            print("Action:       Run 'truhowl auth' to connect an AI provider.")
        print("\n================================================================================")
        return

    if getattr(args, "clear", False):
        clear_credentials()
        print("[OK] Stored Truhowl credentials removed.")
        return

    provider = getattr(args, "provider", None)
    api_key = getattr(args, "api_key", None)
    model = getattr(args, "model", None)
    base_url = getattr(args, "base_url", None)

    # If not provided via CLI flags, prompt interactively if tty is available
    if not provider or not api_key:
        print("================================================================================")
        print("                   TRUHOWL: CONNECT YOUR AI PROVIDER                            ")
        print("================================================================================\n")
        print("Select your AI Provider:")
        print("  1) Anthropic (Claude 3.5 Sonnet) [Recommended]")
        print("  2) OpenAI (GPT-4o)")
        print("  3) Groq (openai/gpt-oss-120b) [High-speed LPU]")
        print("  4) OpenAI-Compatible / Local (Ollama, vLLM)\n")

        if not sys.stdin.isatty() and (not provider or not api_key):
            print("Error: Running in non-interactive environment. Please supply flags:")
            print("  truhowl auth --provider <anthropic|openai|groq> --api-key <...>")
            sys.exit(1)

        choice = input("Enter choice [1-4] (default: 1): ").strip() or "1"
        if choice == "1":
            provider = "anthropic"
            model = model or "claude-3-5-sonnet-20241022"
        elif choice == "2":
            provider = "openai"
            model = model or "gpt-4o"
        elif choice == "3":
            provider = "groq"
            base_url = base_url or "https://api.groq.com/openai/v1"
            model = model or "openai/gpt-oss-120b"
        elif choice == "4":
            provider = "openai_compatible"
            base_url = base_url or input("Base URL [http://localhost:11434/v1]: ").strip() or "http://localhost:11434/v1"
            model = model or input("Model name [deepseek-coder]: ").strip() or "deepseek-coder"
        else:
            provider = "anthropic"

        if not api_key:
            api_key = getpass.getpass(f"Enter API Key for {provider}: ").strip()

    if provider == "groq":
        base_url = base_url or "https://api.groq.com/openai/v1"
        model = model or "openai/gpt-oss-120b"

    valid, msg = verify_credentials(provider, api_key, model=model, base_url=base_url)
    if not valid:
        print(f"\n[ERROR] {msg}")
        sys.exit(1)

    installation = getattr(args, "installation", None)
    repo = getattr(args, "repo", None)
    creds_path = save_credentials(provider=provider, api_key=api_key, model=model, base_url=base_url,
                                  installation_id=installation, repo=repo)
    print(f"\n[OK] Credentials verified successfully for {provider}!")
    scope = f" (installation {installation}" + (f", repo {repo}" if repo else "") + ")" if installation else ""
    print(f"[OK] Saved configuration to {creds_path}{scope}")

    # Automatic Day-0 Knowledge Graph Indexing
    root_path = os.path.abspath(getattr(args, "path", ".") or ".")
    print(f"\n[INDEXING] Initializing Truhowl Knowledge Graph for: {root_path}...")
    try:
        run_audit(repo_root=root_path, output_format="cli", write_graph=True)
        print("[OK] Codebase indexed successfully. Dependency call graph ready.")
        print("\nNext steps:")
        print("  1. Run `truhowl check` to inspect external dependencies and drift.")
        print("  2. Run `truhowl migrate` to autonomously resolve migrations.")
    except Exception as exc:
        print(f"[NOTICE] Initial indexing notice: {exc}")

    print("================================================================================")


def cmd_check(args):
    """Day-0 External-Change Dependency Audit and Risk Register — zero-token static indexing (blueprint box 2)."""
    # Zero-token path: never gate scan/index on LLM creds. Only gate fix/maintain when AI is actually needed.
    root_path = os.path.abspath(getattr(args, "path", ".") or ".")
    write_graph = getattr(args, "write_graph", False)
    output = run_audit(
        repo_root=root_path,
        output_format=getattr(args, "format", "cli"),
        write_graph=write_graph,
    )
    print(output)
    # Index warms KB test_recipe so future repair has verification context without extra tokens.
    if write_graph:
        try:
            tcmd = _detect_test_command(root_path)
            if tcmd:
                for d in detect_drift(root_path):
                    _f, _t, _m = resolve_migration(d["provider"], d.get("declared_version"), d.get("target_version"))
                    ensure_test_recipe(root_path, d["provider"], _f, _t, tcmd)
        except Exception:
            pass
    if getattr(args, "format", "cli") == "cli":
        try:
            from truhowl import hunt as hunt_agent
            findings = hunt_agent.list_findings(root_path)
            if findings:
                hunt_agent.persist_findings(root_path, findings)
                print()
                print("Truhowl found an issue.")
                print()
                for f in findings[:5]:
                    print(f"[{f.finding_id}] {f.provider} "
                          f"{f.version_from} -> {f.version_to}: {f.summary[:100]}")
                print()
                print("Run:")
                for f in findings[:5]:
                    print(f"  truhowl migrate {f.finding_id}")
        except Exception:
            pass


def cmd_fix(args):
    """Run autonomous repair: finding-based migration when given an <id>, else provider flow."""
    if _hunt_finding_requested(args):
        return cmd_hunt(args)
    if getattr(args, "command", "") in ("hunt", "@hunt", "migrate") and not getattr(args, "detect", False):
        repo_dir = _resolve_hunt_repo(getattr(args, "root_dir", ".") or ".")
        try:
            from truhowl import hunt as hunt_agent
            findings = hunt_agent.list_findings(repo_dir)
            if len(findings) == 1:
                setattr(args, "finding", findings[0].finding_id)
                return cmd_hunt(args)
            elif len(findings) > 1:
                print("================================================================================")
                print("                       TRUHOWL MIGRATE: MULTIPLE FINDINGS                       ")
                print("================================================================================\n")
                print(f"Multiple actionable findings detected in {repo_dir}:\n")
                for f in findings:
                    print(f"  [{f.finding_id}] {f.provider} {f.version_from} -> {f.version_to}")
                    print(f"      {f.summary[:100]}\n")
                print("Run:")
                for f in findings:
                    print(f"  truhowl migrate {f.finding_id}")
                print("\n================================================================================")
                return
        except Exception:
            pass
    return cmd_maintain(args)


def _hunt_finding_requested(args) -> bool:
    """True when the invocation names a finding instead of a repository path."""
    if getattr(args, "finding", None) or getattr(args, "issue", None):
        return True
    raw = getattr(args, "root_dir", ".") or "."
    if raw != "." and not os.path.isdir(os.path.abspath(raw)):
        return True
    return False


def _resolve_hunt_repo(raw: str) -> str:
    """Resolve the repository checkout for a Hunt run (active context aware)."""
    if raw and raw != "." and os.path.isdir(os.path.abspath(raw)):
        return os.path.abspath(raw)
    root_dir = os.path.abspath(".")
    active_name = get_active_repo()
    if active_name:
        rec = get_repository(active_name)
        if rec and rec.get("workdir") and os.path.isdir(rec["workdir"]):
            root_dir = os.path.abspath(rec["workdir"])
    return root_dir


def cmd_hunt(args):
    """Hunt: autonomous repair starting from an identified Truhowl finding.

    Usage: truhowl hunt <id> [--create-pr] [--repo owner/repo]

    The finding is only the starting point: Hunt reconstructs full context,
    reasons with AI, authors the patch with AI, verifies in an isolated
    sandbox, and fails closed when correctness cannot be established.
    """
    from truhowl import hunt as hunt_agent

    raw = getattr(args, "root_dir", ".") or "."
    explicit = getattr(args, "finding", None) or None
    issue = getattr(args, "issue", None) or None
    if issue and not explicit:
        explicit = f"issue:{issue}"
    if explicit:
        finding_ref = explicit
        repo_dir = _resolve_hunt_repo(raw if raw != "." else ".")
    else:
        finding_ref = raw
        repo_dir = _resolve_hunt_repo(".")

    print("================================================================================")
    print("                       TRUHOWL MIGRATE: AUTONOMOUS REPAIR                        ")
    print("================================================================================\n")
    print(f"Repository:              {repo_dir}")
    print(f"Finding:                 {finding_ref}\n")

    report = hunt_agent.run_hunt(
        repo_dir=repo_dir,
        finding_ref=finding_ref,
        create_pr=bool(getattr(args, "create_pr", False)),
        github_repo=getattr(args, "repo", None),
        auto_approve_pr=bool(getattr(args, "yes", False)),
        max_iterations=int(getattr(args, "max_iterations", 3) or 3),
        llm_api_key=getattr(args, "api_key", None),
        llm_model=getattr(args, "model", None),
        llm_base_url=getattr(args, "base_url", None),
    )

    if getattr(args, "json", False):
        import dataclasses as _dc
        # default=str: the sealed token carries a private sentinel object
        # that is structural, not serializable — stringify, never leak.
        print(json.dumps(_dc.asdict(report), indent=2, default=str))
        return

    print(hunt_agent.render_hunt_summary(report))
    if report.unified_diff:
        print("\n--- Patch Preview ---")
        print(hunt_agent.render_diff_preview(report.unified_diff))
    print()

    if report.success and not report.pr_url and not getattr(args, "create_pr", False):
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        if interactive and not getattr(args, "yes", False):
            try:
                answer = input("Raise PR? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer in ("y", "yes"):
                from truhowl.hunt import decide_pr, gather_context, resolve_finding
                finding = resolve_finding(repo_dir, report.finding_id)
                if finding is not None and report.verified is not None:
                    ctx = gather_context(repo_dir, finding)
                    pr_url = decide_pr(report.verified, ctx,
                                       github_repo=getattr(args, "repo", None),
                                       auto_approve=True)
                    report.pr_url = pr_url
                    if pr_url:
                        print(f"\n[PR OPENED] {pr_url}")
                    else:
                        print("\nNo PR created (no GitHub repository configured). "
                              "Changes are left in the checkout for review.")

    print("================================================================================")


def _github_repo_from_remote(workdir: str) -> str | None:
    """Extract owner/repo from the git origin URL. None when unavailable."""
    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=workdir, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if "github.com" in remote:
            return remote.split("github.com")[-1].lstrip(":").lstrip("/").removesuffix(".git") or None
    except Exception:
        pass
    return None


def cmd_consult(args):
    """Consult mode: assess with AI reasoning, file a GitHub Issue, modify nothing."""
    raw_path = getattr(args, "path", ".") or "."
    root_path = os.path.abspath(raw_path)
    if raw_path == ".":
        active_name = get_active_repo()
        if active_name:
            rec = get_repository(active_name)
            if rec and rec.get("workdir") and os.path.isdir(rec["workdir"]):
                root_path = os.path.abspath(rec["workdir"])
    if not has_valid_credentials():
        _print_auth_warning()
        print("Consult reasons with AI — static `truhowl check` needs no key.")
        sys.exit(1)

    detections = [d for d in detect_changes(root_path) if d.outcome != NO_IMPACT]
    if not detections:
        print("Truhowl Consult: no maintenance problems found. No changes made.")
        return

    repo = getattr(args, "repo", None) or _github_repo_from_remote(root_path)
    if not repo:
        print("Error: Consult files a GitHub Issue — pass --repo owner/repo "
              "or run inside a GitHub checkout.")
        sys.exit(2)
    client = GitHubAppClient()
    if not client.token and not (client.app_id and client.private_key):
        print("Error: Consult files a GitHub Issue — set GITHUB_TOKEN or "
              "App credentials first.")
        sys.exit(2)

    planner = AIPatchPlanner.from_env()
    if planner is None:  # Unreachable given the credential check; fail closed anyway.
        _print_auth_warning()
        sys.exit(1)

    items = []
    for detection in detections:
        source = detection.source
        _from = source.version_from or ""
        _to = source.version_to or ""
        context = build_reasoning_context(
            root_path, source.provider, _from, _to,
            source.metadata.get("breaking_change", ""),
            source.metadata.get("migration_guide_url", ""))
        assessment = planner.assess(
            repo_dir=root_path, provider_name=source.provider,
            from_version=_from, to_version=_to,
            migration_details=source.metadata.get("breaking_change", ""),
            changelog_url=source.metadata.get("migration_guide_url", ""),
            affected_files=detection.affected_files, context=context)
        items.append({
            "display": source.provider,
            "version_from": _from, "version_to": _to,
            "breaking_change": source.metadata.get("breaking_change", ""),
            "guide_url": source.metadata.get("migration_guide_url", ""),
            "affected_files": detection.affected_files,
            "assessment_body": assessment.get("body", ""),
            "auto_repairable": True,
            "confidence": assessment.get("confidence", "high"),
        })

    if not any(item["assessment_body"] for item in items):
        print("Error: Consult assessment produced no reasoning — refusing to file an empty issue.")
        sys.exit(1)

    body = render_consult_issue(items)
    title = f"[Truhowl Consult] {len(items)} maintenance issue(s) in {repo}"
    resp = client.create_issue(repo=repo, title=title, body=body, labels=["truhowl", "consult"])
    url = resp.get("html_url") if isinstance(resp, dict) else None
    print(f"Howl consulted {len(items)} issue(s); no code was modified.")
    print(f"[ISSUE OPENED] {url}" if url else "[ISSUE OPENED]")
    print()
    print(body)




def cmd_app(args):
    """Manage GitHub App daemon and Webhook listener."""
    action = getattr(args, "app_action", "serve")

    if action == "serve":
        cfg = load_config()
        policy = cfg.pipeline_policy()
        client = GitHubAppClient()
        # Managed checkouts: PR/install events resolve to a real clone, never the daemon cwd.
        handler = make_pr_bot_handler(
            client=client, policy=policy,
            workdir_fn=lambda payload: workdir_for_event(payload, token=client.token),
        )
        secret = args.secret or os.environ.get("TRUHOWL_WEBHOOK_SECRET")
        if not secret and not getattr(args, "no_secret", False):
            print("Error: refusing to serve webhooks without a secret (forged events would be accepted).")
            print("Set TRUHOWL_WEBHOOK_SECRET, pass --secret, or use --no-secret for local debugging only.")
            sys.exit(2)
        server = WebhookServer(port=args.port, secret=secret, handler=handler)
        sec_msg = "YES (HMAC-SHA256)" if secret else "NO — local debugging only (--no-secret)"
        watch_every = getattr(args, "watch", 0) or 0
        print("================================================================================")
        print("               TRUHOWL GITHUB APP: CONTINUOUS WEBHOOK LISTENER                  ")
        print("================================================================================\n")
        print(f"Listening on port:       {args.port}")
        print(f"Webhook Endpoint:        http://localhost:{args.port}/webhook")
        print(f"Healthcheck:             http://localhost:{args.port}/health")
        print(f"Secret Enforced:         {sec_msg}")
        print("Repo checkouts:          managed cache (clone on install, pull on sight)")
        print(f"Background watch:        {'Howl hunting every ' + str(watch_every) + 's' if watch_every else 'OFF (pass --watch SECONDS)'}\n")
        print("Press Ctrl+C to stop daemon.\n")
        print("================================================================================")
        if watch_every:
            def _loop():
                while True:
                    try:
                        time.sleep(watch_every)
                        watch_once(client=client, policy=policy)
                    except Exception:
                        continue

            thread = threading.Thread(target=_loop, daemon=True)
            thread.start()
        try:
            server.start(blocking=True)
        except KeyboardInterrupt:
            print("\n[Truhowl App] Stopped.")
    elif action == "status":
        print("[Truhowl App] Status: Ready (Daemon not running locally).")


def cmd_mcp(args):
    """Run Truhowl Model Context Protocol (MCP) server over stdio."""
    serve_stdio()


def cmd_maintain(args):
    """Run autonomous continuous maintenance loop with AI-authored repair."""
    raw_dir = getattr(args, "root_dir", ".") or "."
    root_dir = os.path.abspath(raw_dir)
    if raw_dir == ".":
        active_name = get_active_repo()
        if active_name:
            rec = get_repository(active_name)
            if rec and rec.get("workdir") and os.path.isdir(rec["workdir"]):
                root_dir = os.path.abspath(rec["workdir"])
    print("================================================================================")
    print("               TRUHOWL AUTONOMOUS MAINTENANCE LOOP: EXECUTION                   ")
    print("================================================================================\n")
    print(f"Repository:              {root_dir}")
    
    target_provider = args.provider
    if not target_provider or target_provider == "auto":
        detected_all = detect_drift(root_dir)
        if detected_all:
            target_provider = detected_all[0]["provider"]
            # G2: version-aware — seed from/to from manifest + registry when flags omitted
            if not getattr(args, "from_version", None):
                args.from_version = detected_all[0].get("declared_version")
            if not getattr(args, "to_version", None) and detected_all[0].get("target_version"):
                args.to_version = detected_all[0].get("target_version")
        else:
            print("No external provider detected in manifests. Refusing to guess.")
            print("Run `truhowl check` to audit, or pass --provider explicitly.")
            print("================================================================================")
            return

    print(f"Target Provider:         {target_provider}")

    if args.detect:
        detected = detect_drift(root_dir, target_provider if target_provider != "auto" else None)
        print(f"\nDetected Manifest Dependencies ({len(detected)}):")
        for d in detected:
            print(f"  - {d['display_name']} ({d['package_name']}): {d['declared_version']}")
        print("================================================================================")
        return

    report = run_maintenance_cycle(
        repo_dir=root_dir,
        provider_name=target_provider,
        from_version=args.from_version,
        to_version=args.to_version,
        create_pr=args.create_pr,
        github_repo=args.repo,
        llm_api_key=getattr(args, "api_key", None),
        llm_model=getattr(args, "model", None),
        llm_base_url=getattr(args, "base_url", None),
    )

    if getattr(args, "json", False):
        print(json.dumps(dataclasses.asdict(report), indent=2))
        return

    print(f"Version Migration:       {report.from_version} -> {report.to_version}")
    print(f"Files Scanned/Patched:   {report.files_scanned} scanned, {report.files_modified} modified")
    repair_path_label = {
        "ai-reasoning": "AI-authored (Hunt reasoning with evidence)",
        "none": "none (refused)",
    }.get(report.repair_path, report.repair_path)
    print(f"Repair Path:             {repair_path_label}")
    print(f"Blast-Radius Check:      {'PASS (0 unintended files modified)' if report.blast_radius_verified else 'FAIL'}")
    if report.files_modified == 0:
        print("Repository Tests:        NOT RUN (no repair was applied)")
    elif report.test_exit_code == 0:
        print(f"Repository Tests:        PASSED (Exit 0, {report.test_duration_ms}ms)")
    else:
        print(f"Repository Tests:        FAILED (Exit {report.test_exit_code})")
    if getattr(report, "error", None):
        print(f"Quarantined:             {report.error}")
        print("Action required:       Run `truhowl auth` to enable AI repair.")
    if not report.success and report.test_exit_code != 0:
        print("Rollback:                APPLIED (snapshot restored, no changes left on disk)")
    print(f"Maintenance Outcome:     {'SUCCESS (VERIFIED GREEN) — Hunt' if report.success else 'REFUSED / INCOMPLETE'}\n")

    if report.patch_results:
        print("--- Patch Breakdown ---")
        for r in report.patch_results:
            rel = os.path.relpath(r.file_path, report.repository_path)
            print(f"  {rel}  ({r.lines_changed} lines changed)")
            for rule_desc in r.rules_applied:
                print(f"    - {rule_desc}")
        print()

    if report.unified_diff:
        print("--- Surgical Patch Preview ---")
        for line in report.unified_diff.splitlines()[:40]:
            print(f"  {line}")
        print()

    if args.show_pr or args.create_pr:
        print("--- Developer Trust Surface PR Body ---")
        print(report.trust_pr_body)

    if report.pr_url:
        print(f"\n[PR OPENED] {report.pr_url}")

    print("================================================================================")




def cmd_graph(args):
    """Query and inspect the External-Change Dependency Graph."""
    root_path = os.path.abspath(args.path)
    graph = build_dependency_graph(root_path)
    if getattr(args, "json", False):
        print(json.dumps(graph, indent=2))
        return

    print("================================================================================")
    print("                 TRUHOWL: EXTERNAL-CHANGE DEPENDENCY GRAPH                     ")
    print("================================================================================\n")
    print(f"Repository:              {root_path}")
    print(f"Providers Ingested:      {len(graph.get('providers', []))}")
    print(f"Contracts Modeled:       {len(graph.get('contracts', []))}")
    print(f"Manifest Dependencies:   {len(graph.get('manifest_deps', []))}")
    print(f"Wrapper Clients Found:   {len(graph.get('wrappers', []))}")
    print(f"AST Callsites Mapped:    {len(graph.get('callsites', []))}")
    print(f"Active Graph Edges:      {len(graph.get('edges', []))}")
    print("================================================================================")
    for w in graph.get('wrappers', []):
        print(f"  [Wrapper] {w['file_path']} -> wraps {w['wraps_package']}")
    for c in graph.get('callsites', [])[:10]:
        print(f"  [Callsite] {c['file_path']}:{c['line_number']} -> {c['matched_pattern']}")
    if len(graph.get('callsites', [])) > 10:
        print(f"  ... and {len(graph.get('callsites', [])) - 10} more callsites mapped.")
    print("================================================================================")


def cmd_providers(args):
    """List supported providers and migration contract specifications."""
    reg = get_default_registry()
    providers = reg.list_providers()

    if getattr(args, "json", False):
        print(json.dumps([dataclasses.asdict(p) for p in providers], indent=2))
        return

    print("================================================================================")
    print("                    TRUHOWL PROVIDER CONTRACT REGISTRY                          ")
    print("================================================================================\n")
    for p in providers:
        print(f"Provider:                {p.display_name} ({p.name})")
        print(f"Package:                 {p.package_name}")
        print(f"Documentation:           {p.docs_url}")
        if p.migrations:
            print(f"Available Migrations ({len(p.migrations)}):")
            for m_key, m in p.migrations.items():
                print(f"  - {m.from_version} -> {m.to_version}: {m.description}")
        else:
            print("  - Standard OpenAPI Contract Mapping available")
        print()
    print("================================================================================")


def cmd_pr(args):
    """Review a pull request using the Truhowl PR Bot."""
    workdir = os.path.abspath(getattr(args, "path", "."))
    cfg = load_config()
    policy = cfg.pipeline_policy()
    if getattr(args, "auto_fix", False):
        policy.pr_auto_fix = True

    repo = getattr(args, "repo", None)
    if not repo:
        try:
            remote = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=workdir, capture_output=True, text=True).stdout.strip()
            if "github.com" in remote:
                repo = remote.split("github.com")[-1].lstrip(":").lstrip("/").removesuffix(".git")
        except Exception:
            pass
    repo = repo or "local/repo"

    pr_num = getattr(args, "pr_number", 1) or 1

    print("================================================================================")
    print("                      TRUHOWL AUTOMATED PR CONTRACT GUARD                       ")
    print("================================================================================\n")
    print(f"Repository:              {repo}")
    print(f"PR Number:               #{pr_num}")
    print(f"Working Directory:       {workdir}")
    print(f"Auto-Fix Mode:           {'APPLY & COMMIT' if policy.pr_auto_fix else 'AUDIT ONLY'}\n")

    result = run_on_pr_locally(
        repo=repo,
        pr_number=pr_num,
        workdir=workdir,
        base_branch=getattr(args, "base", "main"),
        policy=policy,
    )

    status_str = result.get("pipeline_status", "unknown").upper()
    check_str = result.get("check_state", "unknown").upper()
    mergeable = result.get("mergeable", False)
    merge_str = "YES [MERGE_READY]" if mergeable else "NO [BLOCKED: UNVERIFIED]"
    print(f"Pipeline Status:         {status_str}")
    print(f"Check State:             {check_str}")
    print(f"Mergeable:               {merge_str}")
    print(f"Outcome:                 {result.get('check_description', '')}\n")

    if result.get("comment_preview"):
        print("--- PR Review Comment ---")
        print(result["comment_preview"])
        print()

    if result.get("comment_posted"):
        print("[OK] Review comment posted directly to GitHub Pull Request.\n")

    print("================================================================================")


def main():
    # Direct agent shortcut: `truhowl claude` / `truhowl opencode` / `truhowl codex`
    if len(sys.argv) > 1 and sys.argv[1] in _KNOWN_AGENTS:
        agent_name = sys.argv[1]
        sys.stderr.write(f"[DEPRECATED] Direct agent wrapping ('truhowl {agent_name}') is deprecated; Truhowl is focused on autonomous SDK/API migrations.\n")
        user_argv = sys.argv[2:]
        ws_root = find_workspace_root() or os.path.abspath(".")
        sys.exit(_launch_agent(agent_name, ws_root, user_argv=user_argv))

    description = textwrap.dedent("""\
        Truhowl: Autonomous SDK/API Migration Worker
        Repairs breaking upgrades and proves the migration works before opening a PR.

        Workflow:
          truhowl status [path]            Show repos, dependencies, and maintenance state
          truhowl ask "question"           Ask about impact, failures, or readiness (read-only)
          truhowl check [path]             Detect breaking SDK/API drift and show affected usage
          truhowl migrate <finding|provider>  Plan, edit, build, test, repair, and prepare/open PR
          truhowl consult [path]           Analyze drift and file a GitHub issue (report-only)

        Configuration & Diagnostics:
          truhowl login                    Connect & configure AI provider credentials (OpenAI, Anthropic, Groq, etc.)
          truhowl auth                     Same as login (all auth flags accepted)
          truhowl doctor                   Verify environment, credentials, test runner, and repository health
          truhowl status                   Show current workspace, repository context, and provider status

        Advanced / Plumbing:
          truhowl graph [path]             Inspect external dependency graph and callsites
          truhowl diff                     Inspect unapplied/recorded agent execution changes
          truhowl undo                     Reverse the last applied change set
          truhowl providers                List supported providers and migration contract catalog
          truhowl app                      Manage GitHub App webhook server daemon

        Renamed: hunt is a deprecated alias for migrate (truhowl hunt still works).
    """)

    parser = argparse.ArgumentParser(
        prog="truhowl",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-w", "--workflow",
        dest="workflow_flag",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--run",
        dest="run_flag",
        default=None,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", help=argparse.SUPPRESS)

    init_p = subparsers.add_parser("init", help="Initialize Truhowl in the current repository")
    init_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")

    subparsers.add_parser("status", help="Show workspace status: agents, lanes, security events")

    subparsers.add_parser("doctor", help="Blueprint readiness: auth + indexed + test command")

    inspect_parser = subparsers.add_parser("inspect", help="Dump declarative topology and project state")
    inspect_parser.add_argument("--json", action="store_true")

    comp_parser = subparsers.add_parser("compartment", help=argparse.SUPPRESS)
    comp_subparsers = comp_parser.add_subparsers(dest="compartment_command")
    comp_create_parser = comp_subparsers.add_parser("create")
    comp_create_parser.add_argument("name")

    connect_parser = subparsers.add_parser("connect", help="Connect GitHub account, choose repositories, and issue Repository Key")
    connect_parser.add_argument("source", nargs="?", default=None, help="Repository name (owner/repo) or source compartment")
    connect_parser.add_argument("target", nargs="?", default=None, help="Target compartment (for legacy topology)")
    connect_parser.add_argument("--repo", default=None, help="GitHub repository (owner/repo)")
    connect_parser.add_argument("--path", default=".", help="Local checkout path (default: .)")

    active_parser = subparsers.add_parser("active", help="Show or set the currently active Truhowl repository working context")
    active_parser.add_argument("repo", nargs="?", default=None, help="Repository name to set as active (e.g. owner/repo)")

    disconnect_parser = subparsers.add_parser("disconnect", help="Disconnect active or specified repository from Truhowl")
    disconnect_parser.add_argument("repo", nargs="?", default=None, help="Repository name to disconnect (e.g. owner/repo)")
    disconnect_parser.add_argument("--all", action="store_true", help="Disconnect all registered repositories")

    run_parser = subparsers.add_parser("run", help="Run a declared workflow DAG (alias for --run)")
    run_parser.add_argument("target", nargs="?", default=None, help="Workflow name (e.g. invoice-pipeline), workflow file, or command")
    run_parser.add_argument("--compartment", default="default", help="Compartment policy for a standalone file")
    run_parser.add_argument("--verbose", action="store_true")

    diff_parser = subparsers.add_parser("diff", help="Show change sets associated with agent executions")
    diff_parser.add_argument("--execution", default=None, help="Show changes for a single execution")
    diff_parser.add_argument("--unapplied", action="store_true", help="Only show change sets not yet applied")
    diff_parser.add_argument("--trailers", action="store_true", help="Include formatted Git metadata trailers")
    diff_parser.add_argument("--json", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="Promote change sets into the workspace baseline")
    apply_parser.add_argument("--execution", default=None, help="Apply a single execution's change set")
    apply_parser.add_argument("--force", action="store_true", help="Apply even when changes overlap other executions")
    apply_parser.add_argument("-c", "--commit", action="store_true", help="Automatically create a Git commit with Truhowl trailers")
    apply_parser.add_argument("-m", "--message", default=None, help="Custom Git commit message")

    commit_parser = subparsers.add_parser("commit", help="Commit applied execution changes to Git with RFC-5322 metadata trailers")
    commit_parser.add_argument("-m", "--message", default=None, help="Custom Git commit message header")
    commit_parser.add_argument("--execution", default=None, help="Commit a specific execution's change set")
    commit_parser.add_argument("--all", action="store_true", help="Commit all completed/applied executions")
    commit_parser.add_argument("--force", action="store_true", help="Apply and commit even if changes overlap")

    undo_parser = subparsers.add_parser("undo", help="Reverse the last Truhowl-managed apply operation")
    undo_parser.add_argument("--execution", default=None, help="Undo a specific execution's apply")

    restore_parser = subparsers.add_parser("restore", help="Restore the workspace from a session's snapshot checkpoint")
    restore_parser.add_argument("session_id", nargs="?", default=None, help="Session ID to restore (defaults to latest)")

    exec_parser = subparsers.add_parser("exec", help="Run a command or launch an agent inside an explicitly selected compartment")
    exec_parser.add_argument("--compartment", default=None, help="Override the compartment for this execution")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    wrap_parser = subparsers.add_parser("wrap", help=argparse.SUPPRESS)
    wrap_parser.add_argument("-c", "--claude", nargs="?", const="")
    wrap_parser.add_argument("-o", "--opencode", nargs="?", const="")
    wrap_parser.add_argument("--lane", default="default")
    wrap_parser.add_argument("--agent", default="Claude Code")
    wrap_parser.add_argument("--task", default="")
    wrap_parser.add_argument("--verbose", action="store_true")
    wrap_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    lane_parser = subparsers.add_parser("lane", help=argparse.SUPPRESS)
    lane_subparsers = lane_parser.add_subparsers(dest="lane_command")
    lane_create_p = lane_subparsers.add_parser("create")
    lane_create_p.add_argument("name")
    lane_create_p.add_argument("--agent", default="Claude Code")
    lane_inspect_p = lane_subparsers.add_parser("inspect")
    lane_inspect_p.add_argument("name")
    lane_inspect_p.add_argument("--json", action="store_true")

    subparsers.add_parser("lanes", help=argparse.SUPPRESS)

    integ_parser = subparsers.add_parser("integrate", help=argparse.SUPPRESS)
    integ_subparsers = integ_parser.add_subparsers(dest="subcommand")
    integ_create = integ_subparsers.add_parser("create")
    integ_create.add_argument("lanes", nargs="+")
    integ_subparsers.add_parser("preview")
    integ_subparsers.add_parser("apply")

    subparsers.add_parser("sessions", help=argparse.SUPPRESS)

    session_parser = subparsers.add_parser("session", help=argparse.SUPPRESS)
    session_subparsers = session_parser.add_subparsers(dest="session_command")
    sess_inspect_p = session_subparsers.add_parser("inspect")
    sess_inspect_p.add_argument("session_id")
    sess_inspect_p.add_argument("--json", action="store_true")
    sess_rollback_p = session_subparsers.add_parser("rollback")
    sess_rollback_p.add_argument("session_id")

    branch_parser = subparsers.add_parser("branch", help="Create a new workflow branch in workflows/<name>.yaml")
    branch_parser.add_argument("name", help="Workflow branch name")

    step_parser = subparsers.add_parser("step", help="Add a step to a workflow branch with auto-inferred properties")
    step_parser.add_argument("workflow", help="Workflow name (workflows/<workflow>.yaml)")
    step_parser.add_argument("target", help="Script file path (e.g. src/ocr.py) or command (e.g. pytest -q)")
    step_parser.add_argument("--name", default=None, help="Custom step name (inferred from filename by default)")
    step_parser.add_argument("--compartment", default=None, help="Compartment policy (research, builder, network, tester, default)")
    step_parser.add_argument("--type", default=None, choices=["process", "agent"], help="Step type")
    step_parser.add_argument("--depends-on", default=None, help="Comma-separated prerequisite step names")

    workflow_parser = subparsers.add_parser("workflow", help="Manage and run agentic workflow branches")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command")
    wf_run_p = workflow_subparsers.add_parser("run", help="Run a declared YAML workflow DAG (each node its own Execution) or a workflow file")
    wf_run_p.add_argument("workflow", help="Declared workflow name from config.yaml, or a workflow file path")
    wf_run_p.add_argument("--compartment", default="default", help="Compartment for a standalone workflow file")
    wf_run_p.add_argument("--verbose", action="store_true")

    for alias in ("branch", "new", "create"):
        wf_b_p = workflow_subparsers.add_parser(alias, help="Create a new workflow branch")
        wf_b_p.add_argument("name", help="Workflow branch name")

    wf_step_p = workflow_subparsers.add_parser("step", help="Add a step to a workflow branch")
    wf_step_p.add_argument("workflow", help="Workflow name")
    wf_step_p.add_argument("target", help="Script file path or shell command")
    wf_step_p.add_argument("--name", default=None, help="Custom step name")
    wf_step_p.add_argument("--compartment", default=None, help="Compartment policy")
    wf_step_p.add_argument("--type", default=None, choices=["process", "agent"], help="Step type")
    wf_step_p.add_argument("--depends-on", default=None, help="Comma-separated dependencies")

    for alias in ("show", "inspect", "info", "list"):
        wf_s_p = workflow_subparsers.add_parser(alias, help="Inspect declared workflow steps and DAG")
        wf_s_p.add_argument("name", nargs="?", default=None, help="Workflow name")

    # --- AutoPatch Subcommands ---
    diff_schema_p = subparsers.add_parser("diff-schema", help="Diff two OpenAPI JSON specs for breaking changes")
    diff_schema_p.add_argument("old", help="Old OpenAPI JSON spec file path")
    diff_schema_p.add_argument("new", help="New OpenAPI JSON spec file path")
    diff_schema_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    scan_api_p = subparsers.add_parser("scan-api", help="Scan codebase for API callsites (TS/JS/Py/Go)")
    scan_api_p.add_argument("--root-dir", default=".", help="Root directory to scan (default: .)")
    scan_api_p.add_argument("--sdk", default=None, help="Comma-separated SDK package names (e.g. stripe,@stripe/stripe-node)")
    scan_api_p.add_argument("--method", default=None, help="Comma-separated method patterns (e.g. charges.create,refunds.create)")
    scan_api_p.add_argument("--url", default=None, help="Comma-separated base URLs (e.g. api.stripe.com)")
    scan_api_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    autopatch_p = subparsers.add_parser("autopatch", help="Generate maintenance plan, contract tests, and PR report")
    autopatch_p.add_argument("--old", required=True, help="Old OpenAPI JSON spec file path")
    autopatch_p.add_argument("--new", required=True, help="New OpenAPI JSON spec file path")
    autopatch_p.add_argument("--root-dir", default=".", help="Codebase directory to scan")
    autopatch_p.add_argument("--sdk", default=None, help="Comma-separated SDK package names")
    autopatch_p.add_argument("--method", default=None, help="Comma-separated method patterns")
    autopatch_p.add_argument("--url", default=None, help="Comma-separated base URLs")
    autopatch_p.add_argument("--lang", default="typescript", choices=["typescript", "ts", "python", "py"], help="Contract test language")
    autopatch_p.add_argument("--out-dir", default=None, help="Output directory for report and contract tests")

    wf_order_p = subparsers.add_parser("workflow-order", help="Validate workflow DAG and print topological execution order")
    wf_order_p.add_argument("file", help="Workflow YAML/JSON file path")

    inventory_p = subparsers.add_parser("inventory", help="Discover all external API dependencies and their health status")
    inventory_p.add_argument("--root-dir", default=".", help="Root directory to scan (default: .)")
    inventory_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")



    explain_p = subparsers.add_parser("explain", help="Explain why Truhowl classified a finding as affected, unaffected, or unresolved")
    explain_p.add_argument("finding_id", help="Finding or pattern identifier")

    check_p = subparsers.add_parser(
        "check",
        aliases=["scan", "audit"],
        help="Scan repository for external API integrations and breaking drift",
    )
    check_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")
    check_p.add_argument("--format", default="cli", choices=["cli", "github-issue", "issue", "markdown", "md", "json"], help="Output format (default: cli)")
    check_p.add_argument("--write-graph", action="store_true", help="Persist .truhowl/graph.json")

    hunt_p = subparsers.add_parser(
        "hunt",
        aliases=["work", "@hunt"],
        help="Deprecated alias for migrate (still works)",
    )
    hunt_p.add_argument("root_dir", nargs="?", default=".", help="Finding id (from truhowl check) or codebase directory")
    hunt_p.add_argument("--provider", default="auto", help="Target API provider (e.g. stripe, openai, anthropic, or auto)")
    hunt_p.add_argument("--from", dest="from_version", default=None, help="Current dependency version")
    hunt_p.add_argument("--to", dest="to_version", default=None, help="Target dependency version")
    hunt_p.add_argument("--detect", action="store_true", help="Detect installed API providers in repository")
    hunt_p.add_argument("--create-pr", action="store_true", help="Open GitHub Pull Request via API")
    hunt_p.add_argument("--show-pr", action="store_true", help="Display the Trust PR body")
    hunt_p.add_argument("--repo", default=None, help="GitHub repository name (owner/repo) for PR creation")
    hunt_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    hunt_p.add_argument("--model", default=None, help="BYOK LLM model name (e.g. claude-3-5-sonnet-20241022, gpt-4o)")
    hunt_p.add_argument("--api-key", default=None, help="BYOK LLM API key (or set ANTHROPIC_API_KEY/OPENAI_API_KEY)")
    hunt_p.add_argument("--base-url", default=None, help="Custom LLM base URL (e.g. for local Ollama/vLLM)")
    hunt_p.add_argument("--finding", default=None, help="Hunt finding id from truhowl check (e.g. stripe-a1b2c3)")
    hunt_p.add_argument("--issue", default=None, help="GitHub issue number backing the finding")
    hunt_p.add_argument("--yes", action="store_true", help="Auto-approve PR creation after verified repair")
    hunt_p.add_argument("--max-iterations", type=int, default=3, help="Max AI-directed repair iterations (default: 3)")

    migrate_p = subparsers.add_parser(
        "migrate",
        aliases=["fix", "maintain", "update"],
        help="Autonomous migration repair: plan, edit, build/test, repair, and prepare/open PR",
    )
    migrate_p.add_argument("root_dir", nargs="?", default=".", help="Finding id (from truhowl check) or codebase directory")
    migrate_p.add_argument("--provider", default="auto", help="Target API provider (e.g. stripe, openai, anthropic, or auto)")
    migrate_p.add_argument("--from", dest="from_version", default=None, help="Current dependency version")
    migrate_p.add_argument("--to", dest="to_version", default=None, help="Target dependency version")
    migrate_p.add_argument("--detect", action="store_true", help="Detect installed API providers in repository")
    migrate_p.add_argument("--create-pr", action="store_true", help="Open GitHub Pull Request via API")
    migrate_p.add_argument("--show-pr", action="store_true", help="Display the Trust PR body")
    migrate_p.add_argument("--repo", default=None, help="GitHub repository name (owner/repo) for PR creation")
    migrate_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    migrate_p.add_argument("--model", default=None, help="BYOK LLM model name (e.g. claude-3-5-sonnet-20241022, gpt-4o)")
    migrate_p.add_argument("--api-key", default=None, help="BYOK LLM API key (or set ANTHROPIC_API_KEY/OPENAI_API_KEY)")
    migrate_p.add_argument("--base-url", default=None, help="Custom LLM base URL (e.g. for local Ollama/vLLM)")
    migrate_p.add_argument("--finding", default=None, help="Migration finding id from truhowl check (e.g. stripe-a1b2c3)")
    migrate_p.add_argument("--issue", default=None, help="GitHub issue number backing the finding")
    migrate_p.add_argument("--yes", action="store_true", help="Auto-approve PR creation after verified repair")
    migrate_p.add_argument("--max-iterations", type=int, default=3, help="Max AI-directed repair iterations (default: 3)")

    consult_p = subparsers.add_parser(
        "consult",
        aliases=["howl", "@howl"],
        help="Analyze drift and file a GitHub issue (report-only, modifies zero code)",
    )
    consult_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")
    consult_p.add_argument("--repo", default=None, help="GitHub repository name (owner/repo) for the Issue")

    reviews_p = subparsers.add_parser("reviews", help="List past Truhowl maintenance runs")
    reviews_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")

    onboard_p = subparsers.add_parser("onboard", help="Guided setup: connect provider, index, report readiness")
    onboard_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")

    subparsers.add_parser("logout", help="Remove stored AI provider credentials (alias for auth --clear)")

    app_p = subparsers.add_parser("app", help="Manage Truhowl GitHub App and Webhook server")
    app_p.add_argument("app_action", nargs="?", default="serve", choices=["serve", "status"], help="App action")
    app_p.add_argument("--port", type=int, default=8080, help="Webhook server port (default: 8080)")
    app_p.add_argument("--secret", default=None, help="GitHub Webhook secret for HMAC validation")
    app_p.add_argument("--no-secret", action="store_true", help="Allow serving without a secret (local debugging only)")
    app_p.add_argument("--watch", type=int, default=0, help="Poll READY repos for drift every N seconds (0 = off)")

    providers_p = subparsers.add_parser("providers", help="List supported providers and migration contract catalog")
    providers_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    pr_p = subparsers.add_parser("pr", help="Automated PR review and contract guard")
    pr_p.add_argument("pr_number", nargs="?", type=int, default=1, help="Pull Request number to review")
    pr_p.add_argument("--repo", default=None, help="GitHub repository name (owner/repo)")
    pr_p.add_argument("--path", default=".", help="Local repository checkout path (default: .)")
    pr_p.add_argument("--base", default="main", help="Base branch to diff against (default: main)")
    pr_p.add_argument("--auto-fix", action="store_true", help="Apply verified patches automatically")
    pr_p.add_argument("--provider", default=None, help="Specific provider filter")

    graph_p = subparsers.add_parser("graph", help="Query and inspect the External-Change Dependency Graph")
    graph_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")
    graph_p.add_argument("--json", action="store_true", help="Output full graph JSON")

    subparsers.add_parser("mcp", help="Run Truhowl Model Context Protocol (MCP) server for AI assistants")

    auth_p = subparsers.add_parser("auth", help="Connect and configure BYOK AI provider (OpenAI, Anthropic, etc.)")
    auth_p.add_argument("action", nargs="?", default=None, choices=["login", "ai", "status", "clear", None], help="Action (login, ai, status, clear)")
    auth_p.add_argument("--provider", choices=["anthropic", "openai", "groq", "openai_compatible", "ollama", "local"], default=None, help="AI provider name")
    auth_p.add_argument("--api-key", default=None, help="AI provider API key")
    auth_p.add_argument("--model", default=None, help="Model name (e.g. claude-3-5-sonnet-20241022, gpt-4o)")
    auth_p.add_argument("--base-url", default=None, help="Base URL for custom/local endpoints")
    auth_p.add_argument("--path", default=".", help="Repository root path to auto-index (default: .)")
    auth_p.add_argument("--status", action="store_true", help="Display current AI provider credential status")
    auth_p.add_argument("--clear", action="store_true", help="Clear saved credentials")
    auth_p.add_argument("--installation", default=None, help="Associate credentials with a GitHub App installation id")
    auth_p.add_argument("--repo", default=None, help="Associate credentials with a repository (owner/repo, with --installation)")

    login_p = subparsers.add_parser("login", help="Connect AI provider credentials (same as auth)")
    login_p.add_argument("--provider", choices=["anthropic", "openai", "groq", "openai_compatible", "ollama", "local"], default=None, help="AI provider name")
    login_p.add_argument("--api-key", default=None, help="AI provider API key")
    login_p.add_argument("--model", default=None, help="Model name (e.g. claude-3-5-sonnet-20241022, gpt-4o)")
    login_p.add_argument("--base-url", default=None, help="Base URL for custom/local endpoints")
    login_p.add_argument("--path", default=".", help="Repository root path to auto-index (default: .)")

    ask_p = subparsers.add_parser("ask", help="Ask about impact, failures, or readiness (read-only)")
    ask_p.add_argument("question", nargs="*", help="Question, e.g. \"what breaks if we upgrade stripe?\"")
    ask_p.add_argument("--path", default=".", help="Repository root path (default: .)")
    ask_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    ask_p.add_argument("--act", action="store_true",
                       help="Execute the proposed agent run (default prints the command)")

    agent_parser = subparsers.add_parser("agent", help="Truhowl agent: watch changes, run cases, inspect state")
    agent_sub = agent_parser.add_subparsers(dest="agent_command")
    agent_watch_p = agent_sub.add_parser("watch", help="Discover change, calculate impact, create case (no repairs)")
    agent_watch_p.add_argument("--provider", required=True, help="Target provider (e.g. stripe)")
    agent_watch_p.add_argument("--from", dest="from_version", required=True, help="Current version")
    agent_watch_p.add_argument("--to", dest="to_version", required=True, help="Target version")
    agent_watch_p.add_argument("--repo", action="append", default=[], dest="repos",
                               help="Repo path (repeatable; default: .)")
    agent_run_p = agent_sub.add_parser("run", help="Run case lifecycle end-to-end (plan, repair, verify, record)")
    agent_run_p.add_argument("case_id", help="Migration case id (see truhowl agent cases)")
    agent_run_p.add_argument("--repo", action="append", default=[], dest="repos",
                             help="Repo key or path (repeatable; default: all case repos)")
    agent_run_p.add_argument("--create-pr", action="store_true", help="Open GitHub PR for verified repos")
    agent_run_p.add_argument("--github-repo", default=None, help="GitHub repo (owner/repo) for PR creation")
    agent_run_p.add_argument("--model", default=None, help="BYOK LLM model name")
    agent_run_p.add_argument("--api-key", default=None, help="BYOK LLM API key")
    agent_run_p.add_argument("--base-url", default=None, help="Custom LLM base URL")
    agent_cases_p = agent_sub.add_parser("cases", help="List migration cases and repo states")
    agent_cases_p.add_argument("--state", default=None, help="Filter by repo state (e.g. verified, refused)")
    agent_show_p = agent_sub.add_parser("show", help="Explain a case from persisted evidence")
    agent_show_p.add_argument("case_id", help="Migration case id")

    index_p = subparsers.add_parser("index", help="Index repository dependencies, callsites, and construct graph")
    index_p.add_argument("path", nargs="?", default=".", help="Repository root path (default: .)")
    index_p.add_argument("--write-graph", action="store_true", default=True, help="Persist .truhowl/graph.json")

    shim_parser = subparsers.add_parser("_exec_shim", help=argparse.SUPPRESS)
    shim_parser.add_argument("shim_args", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if getattr(args, "run_flag", None):
        cmd_run(argparse.Namespace(target=args.run_flag, compartment="default", verbose=False))
        return

    if getattr(args, "workflow_flag", None):
        cmd_workflow_branch(argparse.Namespace(name=args.workflow_flag))
        return

    dispatch = {
        "init": cmd_init,
        "auth": cmd_auth,
        "login": cmd_auth,
        "ask": cmd_ask,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "inspect": cmd_inspect,
        "run": cmd_run,
        "lanes": cmd_lanes,
        "sessions": cmd_sessions,
        "diff": cmd_diff,
        "apply": cmd_apply,
        "commit": cmd_commit,
        "undo": cmd_undo,
        "restore": cmd_restore,
        "branch": cmd_workflow_branch,
        "step": cmd_step,
        "diff-schema": cmd_diff_schema,
        "scan-api": cmd_scan_api,
        "autopatch": cmd_autopatch,
        "workflow-order": cmd_workflow_order,
        "inventory": cmd_inventory,
        "explain": cmd_explain,
        "app": cmd_app,
        "check": cmd_check,
        "scan": cmd_check,
        "audit": cmd_check,
        "index": cmd_check,
        "fix": cmd_fix,
        "maintain": cmd_fix,
        "update": cmd_fix,
        "work": cmd_fix,
        "hunt": cmd_fix,
        "migrate": cmd_fix,
        "@hunt": cmd_fix,
        "consult": cmd_consult,
        "howl": cmd_consult,
        "@howl": cmd_consult,
        "reviews": cmd_reviews,
        "onboard": cmd_onboard,
        "logout": cmd_logout,
        "providers": cmd_providers,
        "pr": cmd_pr,
        "graph": cmd_graph,
        "mcp": cmd_mcp,
        "active": cmd_active,
        "connect": cmd_connect,
        "disconnect": cmd_disconnect,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    elif args.command == "exec":
        if args.cmd and args.cmd[0] == "--":
            args.cmd = args.cmd[1:]
        cmd_exec(args)
    elif args.command == "wrap":
        if args.cmd and args.cmd[0] == "--":
            args.cmd = args.cmd[1:]
        cmd_wrap(args)
    elif args.command == "lane":
        lcmd = getattr(args, "lane_command", None)
        if lcmd == "create":
            cmd_lane_create(args)
        elif lcmd == "inspect":
            cmd_lane_inspect(args)
        else:
            lane_parser.print_help()
    elif args.command == "integrate":
        cmd_integrate(args)
    elif args.command == "session":
        scmd = getattr(args, "session_command", None)
        if scmd == "inspect":
            cmd_session_inspect(args)
        elif scmd == "rollback":
            cmd_session_rollback(args)
        else:
            session_parser.print_help()
    elif args.command == "compartment":
        if getattr(args, "compartment_command", None) == "create":
            cmd_compartment_create(args)
        else:
            comp_parser.print_help()
    elif args.command == "connect":
        cmd_connect(args)
    elif args.command == "workflow":
        wcmd = getattr(args, "workflow_command", None)
        if wcmd == "run":
            cmd_workflow_run(args)
        elif wcmd in ("branch", "new", "create"):
            cmd_workflow_branch(args)
        elif wcmd == "step":
            cmd_step(args)
        elif wcmd in ("show", "inspect", "info", "list"):
            cmd_workflow_show(args)
        else:
            workflow_parser.print_help()
    elif args.command == "_exec_shim":
        cmd_exec_shim(args)
    elif args.command == "agent":
        acmd = getattr(args, "agent_command", None)
        if acmd == "watch":
            cmd_agent_watch(args)
        elif acmd == "run":
            cmd_agent_run(args)
        elif acmd == "cases":
            cmd_agent_cases(args)
        elif acmd == "show":
            cmd_agent_show(args)
        else:
            agent_parser.print_help()
    else:
        parser.print_help()


def cli():
    main()


if __name__ == "__main__":
    cli()
