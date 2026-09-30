"""Simple pub-sub event bus for Truhowl lifecycle events."""

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


@dataclass
class Event:
    name: str
    data: dict
    timestamp: float


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def emit(self, event: str, **data) -> None:
        evt = Event(name=event, data=data, timestamp=time.time())
        for handler in self._handlers[event]:
            handler(evt)

    def on(self, event: str, handler: Callable) -> None:
        self._handlers[event].append(handler)

    def remove(self, event: str, handler: Callable) -> None:
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass


event_bus = EventBus()
emit = event_bus.emit
