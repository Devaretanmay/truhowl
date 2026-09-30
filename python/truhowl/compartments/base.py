"""Compartment - the fundamental execution unit inside a Truhowl."""

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Message:
    from_: str
    to: str
    data: Any
    type: str = "data"
    timestamp: float = field(default_factory=time.time)


@dataclass
class CompartmentConfig:
    """Policy that defines what a compartment can access and how it runs."""

    name: str = "unnamed"
    description: str = ""

    permissions: list[str] = field(default_factory=lambda: ["fs_read"])

    timeout_s: int = 300

    allow_inbound_from: list[str] = field(default_factory=lambda: ["*"])
    allow_outbound_to: list[str] = field(default_factory=lambda: ["*"])


class Compartment:
    """A named unit of isolated execution with its own permissions and behaviour."""

    def __init__(
        self,
        name: str | None = None,
        fn: Callable | None = None,
        config: CompartmentConfig | None = None,
    ):
        if name:
            if config:
                config.name = name
            else:
                config = CompartmentConfig(name=name)
        self.config = config or CompartmentConfig()
        self._fn = fn
        self._inbox: list[Message] = []


    def deliver(self, msg: Message) -> None:
        self._inbox.append(msg)

    def run(self, ctx: Any) -> Any:
        """Execute this compartment's logic.

        Override in subclasses, or pass ``fn`` to the constructor.
        """
        if self._fn is not None:
            return self._fn(ctx)
        raise NotImplementedError(
            f"Compartment '{self.config.name}' has no run logic. "
            "Either subclass it or pass a callable as fn=."
        )


    def receive(self) -> list[Message]:
        """Read all pending messages from other compartments."""
        msgs = list(self._inbox)
        self._inbox.clear()
        return msgs

    def __repr__(self) -> str:
        return f"<Compartment '{self.config.name}': {self.config.permissions}>"
