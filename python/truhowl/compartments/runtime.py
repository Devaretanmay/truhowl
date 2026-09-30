"""CompartmentRuntime - manages lifecycle, message routing, and per-compartment policy enforcement."""

import logging
import time
from typing import Any

from .base import Compartment
from .base import CompartmentConfig
from .base import Message
from ..engine.events import emit
from ..sandbox.enforcer import SandboxEnforcer

_logger = logging.getLogger("truhowl.runtime")


class CompartmentContext:
    """What a compartment receives when it runs - messages and shared state."""

    def __init__(
        self,
        name: str,
        config: CompartmentConfig,
        workdir: str,
        box_dir: str,
        messages: list[Message],
        state: dict,
        _box: Any = None,
    ):
        self.name = name
        self.config = config
        self.workdir = workdir
        self.box_dir = box_dir
        self.messages = messages
        self.state = state
        self._box = _box

    def send(self, to: str, data: Any, type: str = "data") -> None:
        """Convenience - runtime routes this to the target compartment."""
        self.state.setdefault("_outbox", []).append(
            Message(from_=self.name, to=to, data=data, type=type)
        )


class CompartmentRuntime:
    """Coordinates compartment lifecycles and enforces per-compartment policies."""

    def __init__(self):
        self._compartments: dict[str, Compartment] = {}
        self._edges: list[tuple[str, str]] = []
        self._message_queue: dict[str, list[Message]] = {}
        self._results: dict[str, Any] = {}

    def add(self, compartment: Compartment) -> "CompartmentRuntime":
        name = compartment.config.name
        if not name or name == "unnamed":
            raise ValueError("Every compartment needs a unique name in its config.")
        if name in self._compartments:
            raise ValueError(f"Compartment '{name}' is already registered.")
        self._compartments[name] = compartment
        return self

    def edge(self, from_name: str, to_name: str) -> "CompartmentRuntime":
        if from_name not in self._compartments:
            raise ValueError(f"Unknown source compartment: '{from_name}'")
        if to_name not in self._compartments:
            raise ValueError(f"Unknown target compartment: '{to_name}'")

        src_cfg = self._compartments[from_name].config
        dst_cfg = self._compartments[to_name].config

        allowed_out = dst_cfg.allow_inbound_from
        if allowed_out != ["*"] and from_name not in allowed_out:
            raise ValueError(
                f"Compartment '{to_name}' does not accept inbound from '{from_name}'. "
                f"allow_inbound_from = {allowed_out}"
            )

        allowed_in = src_cfg.allow_outbound_to
        if allowed_in != ["*"] and to_name not in allowed_in:
            raise ValueError(
                f"Compartment '{from_name}' cannot outbound to '{to_name}'. "
                f"allow_outbound_to = {allowed_in}"
            )

        self._edges.append((from_name, to_name))
        return self

    def run(
        self,
        entry: str | None = None,
        box: Any = None,
        workdir: str = ".",
        box_dir: str = "",
    ) -> dict:
        """Execute compartments.

        Parameters
        ----------
        entry : str, optional
            Name of the starting compartment. If omitted, runs all in
            registration order.
            box : Box, optional
            Kernel sandbox instance. If provided, each compartment's
            policy is pushed to the sandbox, and lifecycle hooks
            (snapshots, compression) run around each compartment.
        workdir, box_dir : str
            Paths exposed to compartments.

        Returns
        -------
        dict
            ``{compartment_name: result, ...}``
        """
        if not self._compartments:
            raise RuntimeError("No compartments registered. Call add() first.")

        names = list(self._compartments.keys())
        start_idx = 0
        if entry:
            if entry not in names:
                raise ValueError(f"Entry compartment '{entry}' not found. Registered: {names}")
            start_idx = names.index(entry)

        self._results = {}
        self._shared_state: dict[str, Any] = {}
        for i in range(start_idx, len(names)):
            name = names[i]
            comp = self._compartments[name]
            cfg = comp.config

            pending = self._message_queue.pop(name, [])
            for msg in pending:
                comp.deliver(msg)

            if box is not None:
                box.apply_policy(cfg)

            # Shared state stays separate so internal keys never leak into results.
            ctx = CompartmentContext(
                name=name,
                config=cfg,
                workdir=workdir,
                box_dir=box_dir,
                messages=comp.receive(),
                state=self._shared_state,
                _box=box,
            )

            _logger.info("Compartment %s start  perms=%s", name, cfg.permissions)
            start = time.time()

            emit("compartment_start", name=name)
            if box:
                box.compartment_started(name)

            # Wrap execution in per-compartment sandbox enforcement
            policy = box._current_policy if box is not None else {}
            enforcer = SandboxEnforcer(policy) if policy else None
            if enforcer:
                enforcer.__enter__()

            # Box hooks run after the enforcer exits: snapshot cleanup and
            # compression touch the real filesystem and must see clean globals.
            outcome = "error"
            try:
                result = comp.run(ctx)

                if ctx.state.get("_outbox"):
                    for msg in ctx.state.pop("_outbox"):
                        self._enqueue(msg)

                self._results[name] = result or {}
                elapsed = round(time.time() - start, 2)
                _logger.info("Compartment %s done  %.2fs", name, elapsed)

                emit("compartment_done", name=name, elapsed=elapsed, result=self._results[name])
                outcome = "done"

            except Exception as exc:
                elapsed = round(time.time() - start, 2)
                _logger.warning("Compartment %s failed after %.2fs: %s", name, elapsed, exc)
                self._results[name] = {"error": str(exc)}
                emit("compartment_failed", name=name, error=str(exc), elapsed=elapsed)
            finally:
                if enforcer:
                    enforcer.__exit__(None, None, None)

            if box:
                if outcome == "done":
                    box.compartment_finished(name, self._results[name])
                else:
                    box.compartment_failed(name)

        return dict(self._results)

    def _enqueue(self, msg: Message) -> None:
        if msg.to not in self._compartments:
            _logger.warning("Dropping message to unknown compartment '%s'", msg.to)
            return
        self._message_queue.setdefault(msg.to, []).append(msg)
        _logger.debug("Routed message: %s -> %s  type=%s", msg.from_, msg.to, msg.type)
