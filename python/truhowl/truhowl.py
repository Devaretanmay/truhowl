"""Truhowl - kernel-level sandbox with an insulated runtime for isolated execution.

Two entry points:

* :class:`Truhowl` : a normal outer compartment. A kernel sandbox plus a runtime that
  runs inner compartments you register. Nothing is predefined: no compartments,
  no insulation. Enable snapshots, the credential proxy, or compression explicitly.
* :class:`AgentTruhowl` : an agent outer compartment (agent-oriented container). It
  enables all insulation (credential proxy, snapshots, compression) the moment
  it runs.

Inner compartments are always yours: create them, wire them, and drop them into
either outer compartment.
"""

import logging
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .sandbox.box import Box, BoxConfig
from .sandbox.proxy import RouteConfig
from .compartments import Compartment, CompartmentRuntime
from .engine.events import event_bus
from .engine.tracer import Tracer, is_trace_enabled

_logger = logging.getLogger("truhowl")


@dataclass
class TruhowlConfig:
    workdir: str = "."
    credential_rules: list[RouteConfig] = field(default_factory=list)
    sandbox: bool = False
    block_network: bool = False
    auto_modules: bool = False


@dataclass
class TruhowlResult:
    status: str = "success"
    summary: str = ""
    elapsed_s: float = 0.0
    compartments_completed: list[str] = field(default_factory=list)
    output: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Truhowl:
    """A kernel-level sandbox. A normal outer compartment with nothing predefined."""

    TRUHOWL_DIR = ".truhowl"

    def __init__(
        self,
        workdir: str = ".",
        config: TruhowlConfig | None = None,
        verbose: bool = False,
    ):
        if config is not None:
            self.config = config
        else:
            self.config = TruhowlConfig(workdir=workdir)
        self.workdir = os.path.abspath(self.config.workdir)
        self._snapshot_base = os.path.join(self.workdir, self.TRUHOWL_DIR, "snapshots")
        self._box = Box(workdir=self.workdir, config=BoxConfig(
            credential_rules=self.config.credential_rules,
            snapshot_base=self._snapshot_base,
            auto_modules=self.config.auto_modules,
        ))
        self.box_id = self._box.box_id
        self.box_dir = self._box.box_dir
        self._runtime = CompartmentRuntime()
        self._started_at: float = 0.0
        self._tracer: Tracer | None = None
        self.verbose = verbose or is_trace_enabled()

    def add(self, compartment: Compartment) -> "Truhowl":
        """Register a compartment.

        Compartments run in registration order unless an entry point is
        specified in :meth:`run`.
        """
        self._runtime.add(compartment)
        return self

    def edge(self, from_name: str, to_name: str) -> "Truhowl":
        """Define a message path between two compartments: ``from_name -> to_name``.

        Messages sent by ``from_name`` are routed to ``to_name``.
        """
        self._runtime.edge(from_name, to_name)
        return self

    def enable_snapshot(self) -> "Truhowl":
        """Opt-in to pre-compartment filesystem snapshots with rollback."""
        self._box.enable_snapshot()
        return self

    def enable_credential_proxy(self) -> "Truhowl":
        """Opt-in to the credential-injecting HTTP proxy for the run."""
        self._box.enable_credential_proxy()
        return self

    def enable_compression(self) -> "Truhowl":
        """Opt-in to output compression for compartment results."""
        self._box.enable_compression()
        return self

    def run(
        self,
        entry: str | None = None,
        request: str = "",
    ) -> TruhowlResult:
        """Execute all registered inner compartments inside this outer compartment.

        The lifecycle is:

        1. **Sandbox entered** - sandbox environment is created
        2. **Sandbox insulated** - enabled insulation wires up for the task
        3. **Compartments execute** - each runs with its own policy
        4. **Cleanup** - insulation released, sandbox destroyed

        Parameters
        ----------
        entry : str, optional
            Name of the compartment to start from. If omitted, all
            compartments run in registration order.
        request : str, optional
            Human-readable task description for logging and trace
            headers. Also used to classify the task profile
            (e.g. "fix bug" -> debugging profile).

        Returns
        -------
        TruhowlResult
        """
        task_desc = request or entry or "truhowl.run()"
        self._started_at = time.time()
        _logger.info("Truhowl %s running: %s", self.box_id, task_desc[:80])

        if self.verbose:
            self._tracer = Tracer(self.box_id, verbose=True)
            self._tracer.header(task_desc)
            self._wire_tracer_events()

        self._box.enter(
            block_network=self.config.block_network,
            sandbox=self.config.sandbox,
        )

        self._box.insulate(task_desc)

        raw_results = self._runtime.run(
            entry=entry,
            box=self._box,
            workdir=self.workdir,
            box_dir=self.box_dir,
        )

        compressed = self._read_compressed_outputs()
        for name, compressed_val in compressed.items():
            if name in raw_results:
                if isinstance(raw_results[name], dict):
                    raw_results[name]["_compressed"] = compressed_val
                else:
                    raw_results[name] = {
                        "result": raw_results[name],
                        "_compressed": compressed_val,
                    }

        elapsed = round(time.time() - self._started_at, 2)

        try:
            self._box.release()
        except Exception as exc:
            _logger.debug("Error during box release: %s", exc)
        try:
            self._box.exit()
        except Exception as exc:
            _logger.debug("Error during box exit: %s", exc)

        if self._tracer:
            status = "error" if any(
                isinstance(v, dict) and "error" in v for v in raw_results.values()
            ) else "success"
            self._tracer.footer(status, elapsed)
            self._unwire_tracer_events()

        return self._build_result(raw_results, elapsed)

    def _wire_tracer_events(self) -> None:
        t = self._tracer
        if not t:
            return
        self._tracer_unsubscribe = []

        def handler(evt):
            t.emit(evt.name, **evt.data)

        for event_name in [
            "box.created", "box.entered", "box.destroyed",
            "box.insulated", "box.released",
            "task_profile",
            "compartment_start", "compartment_done", "compartment_failed",
        ]:
            event_bus.on(event_name, handler)
            self._tracer_unsubscribe.append((event_name, handler))

    def _unwire_tracer_events(self) -> None:
        if not hasattr(self, "_tracer_unsubscribe"):
            return
        for event_name, handler in self._tracer_unsubscribe:
            event_bus.remove(event_name, handler)
        self._tracer_unsubscribe.clear()

    def _read_compressed_outputs(self) -> dict[str, str]:
        """Read compressed outputs from the box compressor (if enabled)."""
        return self._box.compressed_outputs

    def _build_result(self, raw: dict[str, Any], elapsed: float) -> TruhowlResult:
        errors: list[str] = []
        completed: list[str] = []
        status = "success"

        for name, result in raw.items():
            if isinstance(result, dict) and "error" in result:
                errors.append(str(result["error"]))
                status = "error"
            else:
                completed.append(name)

        summary = "Task completed"
        if errors:
            summary = errors[-1]

        return TruhowlResult(
            status=status,
            summary=summary,
            elapsed_s=elapsed,
            compartments_completed=completed,
            output=dict(raw),
            errors=errors,
        )


class AgentTruhowl(Truhowl):
    """An agent compartment (pro outer compartment): enables all insulation.

    Credential proxy, snapshots, and output compression all activate
    automatically when the compartment runs. Inner compartments are still yours to define.
    """

    def __init__(
        self,
        workdir: str = ".",
        config: TruhowlConfig | None = None,
        verbose: bool = False,
    ):
        if config is not None:
            config = replace(config, auto_modules=True)
        else:
            config = TruhowlConfig(workdir=workdir, auto_modules=True)
        super().__init__(workdir=workdir, config=config, verbose=verbose)


# Deprecated Koyote-era aliases (remove after transition).
Koyote = Truhowl
AgentKoyote = AgentTruhowl
KoyoteConfig = TruhowlConfig
KoyoteResult = TruhowlResult
