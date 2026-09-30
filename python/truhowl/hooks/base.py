"""Shared execution primitives for the framework integration hooks.

Every hook (LangChain, CrewAI, AutoGen, data-science, CLI agents) runs user
or LLM-authored code inside a :class:`truhowl.AgentTruhowl` compartment.
This module centralises the pieces they all share:

* :class:`ExecutionResult` : the structured outcome returned to agent context
  (stdout, stderr, exit code, elapsed time and BLAKE3 file diffs).
* :class:`SandboxRunner` : one-shot "run a shell command / code snippet inside
  a kernel-enforced compartment" helper used by every hook underneath.
* :data:`VALID_PERMISSIONS` : the permission vocabulary recognised by the
  sandbox policy layer.

The native ``_core`` module is optional: when it is not built, ``sandbox`` is
a no-op at the kernel level but the Python-level
:class:`truhowl.sandbox.enforcer.SandboxEnforcer` still enforces the
compartment permission set. Hooks therefore behave identically whether or not
the Rust core is present.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from ..truhowl import AgentTruhowl, TruhowlConfig
from ..compartments import Compartment, CompartmentConfig
from ..sandbox.proxy import RouteConfig
from ..sandbox.snapshot import _DEFAULT_EXCLUDE as _DIFF_EXCLUDE, _file_hash

_logger = logging.getLogger("truhowl.hooks")

#: The only permission tokens recognised by the sandbox policy layer.
VALID_PERMISSIONS: frozenset[str] = frozenset(
    {"fs_read", "fs_write", "fs_exec", "network", "gpu", "sys_info"}
)

#: Default permissions for general-purpose code execution.
DEFAULT_PERMISSIONS: tuple[str, ...] = ("fs_read", "fs_exec")


def validate_permissions(permissions: Sequence[str]) -> tuple[str, ...]:
    """Validate a permission list against :data:`VALID_PERMISSIONS`.

    Parameters
    ----------
    permissions
        The requested permission tokens.

    Returns
    -------
    tuple[str, ...]
        The validated permissions, deduplicated and order-preserving.

    Raises
    ------
    ValueError
        If any token is not a known sandbox permission.
    """
    unknown = sorted(set(permissions) - VALID_PERMISSIONS)
    if unknown:
        raise ValueError(
            f"Unknown sandbox permission(s): {unknown}. "
            f"Valid permissions: {sorted(VALID_PERMISSIONS)}"
        )
    return tuple(dict.fromkeys(permissions))


def index_workdir(workdir: str) -> dict[str, str]:
    """Hash every tracked file under ``workdir`` relative to its root.

    Parameters
    ----------
    workdir
        The directory to index.

    Returns
    -------
    dict[str, str]
        Mapping of ``relative_path -> content_hash`` for files not excluded
        by :data:`_DIFF_EXCLUDE`.
    """
    index: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(workdir):
        dirnames[:] = [d for d in dirnames if d not in _DIFF_EXCLUDE]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, workdir)
            try:
                index[rel] = _file_hash(full)
            except (OSError, PermissionError) as exc:
                _logger.debug("Skipping %s while indexing: %s", rel, exc)
    return index


def diff_trees(before: Mapping[str, str], after: Mapping[str, str]) -> list[dict[str, str]]:
    """Compare two workdir indexes and describe what changed.

    Parameters
    ----------
    before
        Index captured before execution.
    after
        Index captured after execution.

    Returns
    -------
    list[dict[str, str]]
        One entry per changed path with keys ``path`` and ``status`` in
        ``{"added", "modified", "deleted"}``.
    """
    diffs: list[dict[str, str]] = []
    all_paths = set(before) | set(after)
    for path in sorted(all_paths):
        if path not in before:
            diffs.append({"path": path, "status": "added"})
        elif path not in after:
            diffs.append({"path": path, "status": "deleted"})
        elif before[path] != after[path]:
            diffs.append({"path": path, "status": "modified"})
    return diffs


@dataclass
class ExecutionResult:
    """Structured outcome of running code inside a Truhowl compartment.

    Attributes
    ----------
    returncode
        The subprocess exit code (``0`` means success).
    stdout
        Captured standard output, trailing newline stripped.
    stderr
        Captured standard error, trailing newline stripped.
    diffs
        BLAKE3 file diffs detected between before and after the run.
    elapsed_s
        Wall-clock duration of the run in seconds.
    error
        Raised exception message when the compartment itself failed.
    timed_out
        True when the subprocess hit the configured timeout.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    diffs: list[dict[str, str]] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None
    timed_out: bool = False

    @property
    def output(self) -> str:
        """Combined stdout plus stderr, mirroring a terminal capture."""
        parts = [self.stdout]
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts).rstrip("\n")

    @property
    def success(self) -> bool:
        """True when the run exited cleanly with no compartment error."""
        return self.returncode == 0 and self.error is None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view safe to embed in agent state / tool results."""
        return asdict(self)


class SandboxRunner:
    """Execute shell commands or code snippets inside an AgentTruhowl.

    A fresh :class:`truhowl.AgentTruhowl` is created per :meth:`run`
    call, the workdir is BLAKE3-hashed before and after, and the subprocess
    runs with the kernel sandbox (Landlock/Seatbelt) applied when available.

    Parameters
    ----------
    workdir
        Worktree granted to the sandbox and used as the subprocess cwd.
    credential_rules
        Proxy routes that inject credentials from the environment (see
        :class:`truhowl.sandbox.proxy.RouteConfig`).
    sandbox
        Apply the kernel-enforced sandbox. Disable only for tests / debug.
    block_network
        Deny outbound network by default.
    verbose
        Enable the lifecycle tracer.
    """

    def __init__(
        self,
        workdir: str = ".",
        credential_rules: Sequence[RouteConfig] | None = None,
        sandbox: bool = True,
        block_network: bool = True,
        verbose: bool = False,
    ) -> None:
        self.workdir = os.path.abspath(workdir)
        self.credential_rules = list(credential_rules or [])
        self.sandbox = sandbox
        self.block_network = block_network
        self.verbose = verbose

    def _box(self) -> AgentTruhowl:
        return AgentTruhowl(config=TruhowlConfig(
            workdir=self.workdir,
            credential_rules=list(self.credential_rules),
            sandbox=self.sandbox,
            block_network=self.block_network,
        ))

    def run(
        self,
        command: str,
        *,
        permissions: Sequence[str] = DEFAULT_PERMISSIONS,
        timeout_s: int = 300,
        env: Mapping[str, str] | None = None,
        name: str = "hook",
        snapshot: bool = True,
        cwd: str | None = None,
    ) -> ExecutionResult:
        """Run a shell ``command`` inside a sandboxed compartment.

        Parameters
        ----------
        command
            The shell command string to execute.
        permissions
            Compartment permissions (validated against
            :data:`VALID_PERMISSIONS`).
        timeout_s
            Hard timeout for the subprocess; a :class:`TimeoutError` is
            returned in ``error`` on expiry.
        env
            Optional extra environment variables for the subprocess.
        name
            Compartment name.
        snapshot
            Compute BLAKE3 file diffs across the run.
        cwd
            Subprocess working directory (defaults to ``self.workdir``).

        Returns
        -------
        ExecutionResult
        """
        perms = validate_permissions(permissions)
        run_env = dict(os.environ)
        if env:
            run_env.update(env)

        capture: dict[str, Any] = {}

        def _execute(ctx: Any) -> dict[str, Any]:
            started = time.time()
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    cwd=cwd or self.workdir,
                    env=run_env,
                )
                capture.update({
                    "returncode": proc.returncode,
                    "stdout": (proc.stdout or "").rstrip("\n"),
                    "stderr": (proc.stderr or "").rstrip("\n"),
                })
            except subprocess.TimeoutExpired as exc:
                capture["timed_out"] = True
                capture["error"] = (
                    f"command timed out after {timeout_s}s"
                    f"{': ' + exc.stdout.decode() if exc.stdout else ''}"
                )
                capture["returncode"] = -1
            except Exception as exc:  # noqa: BLE001 - surfaced to the agent
                capture["error"] = str(exc)
                capture["returncode"] = -1
            capture["elapsed_s"] = round(time.time() - started, 2)
            return dict(capture)

        before = index_workdir(self.workdir) if snapshot else {}
        box = self._box()
        box.add(Compartment(
            name=name,
            fn=_execute,
            config=CompartmentConfig(permissions=list(perms), timeout_s=timeout_s),
        ))
        result = box.run(entry=name)
        after = index_workdir(self.workdir) if snapshot else {}

        raw = result.output.get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        diffs = diff_trees(before, after) if snapshot else []
        return ExecutionResult(
            returncode=int(raw.get("returncode", 0)),
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            diffs=diffs,
            elapsed_s=float(raw.get("elapsed_s", 0.0)),
            error=raw.get("error") or raw.get("_error"),
            timed_out=bool(raw.get("timed_out", False)),
        )

    def run_code(
        self,
        code: str,
        *,
        language: str = "python",
        permissions: Sequence[str] = ("fs_read", "fs_write", "fs_exec"),
        timeout_s: int = 300,
        env: Mapping[str, str] | None = None,
        name: str = "hook-code",
    ) -> ExecutionResult:
        """Write ``code`` to an isolated temp file and execute it.

        The snippet is written under ``.truhowl/tmp`` (outside the diff
        index and inside the worktree grant) and executed with the active
        interpreter, so it never shares the parent process or its globals.

        Parameters
        ----------
        code
            Source code to execute.
        language
            Execution engine. Only ``"python"`` is supported today.
        permissions, timeout_s, env, name
            Forwarded to :meth:`run`.

        Returns
        -------
        ExecutionResult
        """
        if language != "python":
            return ExecutionResult(
                returncode=2,
                stderr=f"unsupported language for sandbox execution: {language!r}",
            )

        tmp_root = os.path.join(self.workdir, ".truhowl", "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        script_path = os.path.join(tmp_root, f"{name.replace(' ', '_')}.py")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(code)

        return self.run(
            f"{_python_interpreter()} {shlex.quote(script_path)}",
            permissions=permissions,
            timeout_s=timeout_s,
            env=env,
            name=name,
        )

def _python_interpreter() -> str:
    """The Python executable that should run sandboxed snippets."""
    return shutil.which("python3") or "python"


__all__ = [
    "VALID_PERMISSIONS",
    "DEFAULT_PERMISSIONS",
    "ExecutionResult",
    "SandboxRunner",
    "validate_permissions",
    "index_workdir",
    "diff_trees",
]
