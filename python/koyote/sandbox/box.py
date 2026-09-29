"""Box - standalone kernel-level sandbox. No AI awareness.

Task-profile classification and insulation (snapshots, credential proxy,
output compression) are managed directly by the sandbox container.
Normal compartments insulate nothing by default - enable each explicitly.
AgentKoyote sets ``auto_modules=True`` to enable all three.
"""

import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..engine.events import emit
from .compression import OutputCompressor
from .proxy import CredentialProxy, RouteConfig
from .snapshot import SnapshotManager
from .task_profile import classify as classify_profile

from koyote._core import sandbox_apply as _core_sandbox_apply, sandbox_check_supported as _core_sandbox_check_supported

_logger = logging.getLogger("koyote.box")

_CORE = (_core_sandbox_apply, _core_sandbox_check_supported)


def _get_core():
    return _CORE


STATE_CREATED = "created"
STATE_READY = "ready"
STATE_RUNNING = "running"
STATE_DESTROYED = "destroyed"

@dataclass
class BoxConfig:
    block_network: bool = True
    credential_rules: list = field(default_factory=list)
    snapshot_base: str = ""
    auto_modules: bool = False


class Box:
    KOYOTE_DIR = ".koyote"

    def __init__(self, workdir: str = ".", config: BoxConfig | None = None):
        self.workdir = os.path.abspath(workdir)
        self.box_id = f"box_{uuid.uuid4().hex[:8]}"
        self.box_dir = os.path.join(self.workdir, self.KOYOTE_DIR, "boxes", self.box_id)
        self.config = config or BoxConfig()
        self._state = STATE_CREATED
        self._started_at: float | None = None
        self._sandbox_applied = False
        self._current_policy: dict = {}
        self.task_profile: str = ""
        self._snapshot_enabled = False
        self._snapshot: SnapshotManager | None = None
        self.credential_proxy: CredentialProxy | None = None
        self.compressor: OutputCompressor | None = None
        emit("box.created", box_id=self.box_id, path=self.box_dir)

    def enable_snapshot(self) -> "Box":
        """Opt-in to pre-compartment filesystem snapshots with rollback."""
        self._snapshot_enabled = True
        return self

    def enable_credential_proxy(self) -> "Box":
        """Opt-in to the credential-injecting HTTP proxy for the box lifetime."""
        if self.credential_proxy is not None:
            return self
        routes = []
        for rd in self.config.credential_rules:
            if isinstance(rd, RouteConfig):
                routes.append(rd)
            elif isinstance(rd, dict):
                routes.append(RouteConfig(**rd))
        if not routes:
            return self
        self.credential_proxy = CredentialProxy(routes=routes)
        self.credential_proxy.start()
        self.credential_proxy.set_env()
        _logger.info(
            "Credential proxy active - %d route(s), proxy at %s",
            len(routes), self.credential_proxy.proxy_url,
        )
        return self

    def enable_compression(self) -> "Box":
        """Opt-in to output compression for compartment results."""
        if self.compressor is None:
            self.compressor = OutputCompressor()
        return self

    def insulate(self, task_request: str) -> None:
        """Classify the task profile and enable insulation for the box.

        Boxes with ``auto_modules`` enable snapshots, the credential proxy,
        and compression; otherwise only explicitly enabled insulation runs.
        """
        task_profile = classify_profile(task_request)
        self.task_profile = task_profile
        emit("task_profile", profile=task_profile)
        if self.config.auto_modules:
            self.enable_snapshot().enable_credential_proxy().enable_compression()
        enabled = (
            (1 if self._snapshot_enabled else 0)
            + (1 if self.credential_proxy is not None else 0)
            + (1 if self.compressor is not None else 0)
        )
        _logger.info("Box insulated (profile=%s, task=%s, modules=%d)",
                     task_profile, task_request[:60], enabled)
        emit("box.insulated", profile=task_profile, modules=enabled)

    def release(self) -> None:
        if self.credential_proxy is not None:
            self.credential_proxy.restore_env()
            self.credential_proxy.stop()
            self.credential_proxy = None
        if self.compressor is not None:
            self.compressor.log_totals()
            self.compressor = None
        self._snapshot = None
        _logger.info("Box released")
        emit("box.released")

    def compartment_started(self, name: str) -> None:
        if not self._snapshot_enabled or not self.config.snapshot_base:
            return
        self._snapshot = SnapshotManager(
            workdir=self.workdir,
            snapshot_dir=os.path.join(self.config.snapshot_base, self.box_id),
        )
        count = self._snapshot.snapshot()
        if count > 0:
            _logger.info("Snapshot taken for '%s': %d files", name, count)

    def compartment_finished(self, name: str, result: Any) -> None:
        if self._snapshot is not None:
            self._snapshot.cleanup()
            self._snapshot = None
        if self.compressor is not None:
            self.compressor.record(name, result)

    def compartment_failed(self, name: str) -> None:
        if self._snapshot is None:
            return
        count = self._snapshot.restore()
        if count > 0:
            _logger.info("Rolled back '%s': %d files restored", name, count)
        self._snapshot.cleanup()
        self._snapshot = None

    @property
    def compressed_outputs(self) -> dict[str, str]:
        if self.compressor is None:
            return {}
        return self.compressor.compressed_outputs

    def enter(
        self,
        block_network: bool | None = None,
        sandbox: bool | None = None,
        enforce: bool = False,
    ) -> bool:
        if self._state != STATE_CREATED:
            raise RuntimeError(f"Cannot enter from state: {self._state}")
        self._state = STATE_READY
        os.makedirs(self.box_dir, exist_ok=True)
        if block_network is not None:
            self.config.block_network = block_network
        self._sandbox_applied = False
        core = _get_core()
        if sandbox is None or sandbox:
            if len(core) >= 2:
                apply_fn, check_supported_fn = core[0], core[1]
                try:
                    supported_info = check_supported_fn()
                    if isinstance(supported_info, dict):
                        supported = str(supported_info.get("supported", "false")).lower() == "true"
                    else:
                        supported = bool(supported_info)
                    if not supported:
                        if enforce:
                            raise RuntimeError(
                                f"Enforced sandboxing requested, but platform is unsupported ({supported_info}). Fail-closed."
                            )
                        _logger.warning("Sandbox not available on this platform")
                    else:
                        applied = apply_fn(self.workdir, self.config.block_network)
                        self._sandbox_applied = applied is not False
                        if self._sandbox_applied:
                            _logger.info("Sandbox applied (network_blocked=%s)", self.config.block_network)
                        else:
                            if enforce:
                                raise RuntimeError("Enforced sandboxing failed to apply. Fail-closed.")
                            _logger.warning("Sandbox could not be applied")
                except Exception as e:
                    if enforce:
                        raise RuntimeError(f"Enforced sandboxing failed: {e}") from e
                    _logger.warning("Sandbox unavailable, continuing without: %s", e)
        self._state = STATE_RUNNING
        self._started_at = time.time()
        emit("box.entered", box_id=self.box_id, sandbox_applied=self._sandbox_applied)
        return self._sandbox_applied

    def run_command(
        self,
        cmd: str | list[str],
        timeout: int = 120,
        enforce_sandbox: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a command confined in the sandbox without restricting the orchestrator."""
        from ..test_runner import run_sandboxed_command
        return run_sandboxed_command(
            cmd,
            cwd=self.workdir,
            timeout=timeout,
            block_network=self.config.block_network,
            enforce=enforce_sandbox or (self._sandbox_applied and enforce_sandbox),
            extra_env=extra_env,
        )

    def apply_policy(self, config) -> None:
        """Records the current compartment's policy so the SandboxEnforcer can read it."""
        self._current_policy = {
            "name": config.name,
            "permissions": list(config.permissions),
            "timeout_s": config.timeout_s,
        }
        _logger.debug(
            "Policy for compartment '%s': %s",
            config.name, config.permissions,
        )

    def exit(self) -> None:
        if self._state not in (STATE_RUNNING, STATE_READY):
            raise RuntimeError(f"Cannot exit from state: {self._state}")
        if os.path.isdir(self.box_dir):
            shutil.rmtree(self.box_dir, ignore_errors=True)
        self._state = STATE_DESTROYED
        self._started_at = None
        self._current_policy = {}
        _logger.info("Box %s destroyed", self.box_id)
        emit("box.destroyed", box_id=self.box_id)

    @property
    def state(self) -> str:
        return self._state

    @property
    def elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return round(time.time() - self._started_at, 2)

    @property
    def is_active(self) -> bool:
        return self._state == STATE_RUNNING
