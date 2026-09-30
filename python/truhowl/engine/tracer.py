"""Execution trace - makes the Truhowl lifecycle visible for development."""

import os
import sys
import time
from functools import cache



@cache
def is_trace_enabled() -> bool:
    return os.environ.get("TRUHOWL_TRACE", "0") in ("1", "true", "yes", "on")


def _colorize(s: str, color: str) -> str:
    if not sys.stderr.isatty():
        return s
    codes = {
        "green": "32", "red": "31", "yellow": "33",
        "cyan": "36", "dim": "2", "bold": "1",
    }
    code = codes.get(color, "0")
    return f"\033[{code}m{s}\033[0m"


class Tracer:
    """Collects and prints structured lifecycle events."""

    def __init__(self, box_id: str, verbose: bool = False):
        self.box_id = box_id
        self.verbose = verbose or is_trace_enabled()
        self._started_at: float = time.time()
        self._entries: list[str] = []

    def emit(self, event: str, **data) -> None:
        if not self.verbose:
            return
        now = time.time()
        elapsed = now - self._started_at
        line = self._format_event(event, elapsed, data)
        if line:
            self._entries.append(line)
            print(line, file=sys.stderr, flush=True)

    def header(self, request: str) -> None:
        if not self.verbose:
            return
        sep = "=" * 55
        ts = time.strftime("%H:%M:%S")
        cid = self.box_id.replace('box_', 'comp_')
        lines = [
            "",
            f"  {_colorize(sep, 'cyan')}",
            f"  {_colorize(f'Truhowl #{cid}', 'bold')}    {_colorize(ts, 'dim')}",
            f"  {_colorize(sep, 'cyan')}",
        ]
        self._entries.extend(lines)
        for line in lines:
            print(line, file=sys.stderr, flush=True)

    def footer(self, status: str, elapsed_total: float) -> None:
        if not self.verbose:
            return
        sep = "=" * 55
        status_color = "green" if status == "success" else "red"
        lines = [
            f"  {_colorize(sep, 'cyan')}",
            f"  Result:  {_colorize(status, status_color)}  |  {_colorize(f'{elapsed_total:.2f}s', 'bold')} elapsed",
            f"  {_colorize(sep, 'cyan')}",
            "",
        ]
        self._entries.extend(lines)
        for line in lines:
            print(line, file=sys.stderr, flush=True)

    def _format_event(self, event: str, elapsed: float, data: dict) -> str | None:
        prefix = f"  {elapsed:>7.3f}s"

        if event == "box.created":
            path = data.get("path", "")
            return f"{prefix}  {_colorize('[ok]', 'green')} Truhowl Created     {_colorize(path, 'dim')}"

        if event == "box.entered":
            sandbox = data.get("sandbox_applied", False)
            sb = _colorize("sandbox on", "green") if sandbox else _colorize("sandbox off", "yellow")
            return f"{prefix}  {_colorize('[ok]', 'green')} Kernel Sandbox Active {sb}"

        if event == "box.destroyed":
            return f"{prefix}  {_colorize('[ok]', 'green')} Truhowl Released"

        if event == "box.insulated":
            profile = data.get("profile", "")
            modules = data.get("modules", 0)
            return f"{prefix}  {_colorize('[ok]', 'green')} Runtime Insulated   profile={profile}, modules={modules}"

        if event == "box.released":
            return f"{prefix}  {_colorize('[ok]', 'green')} Runtime Released"

        if event == "task_profile":
            profile = data.get("profile", "code")
            return f"{prefix}  {_colorize('[ok]', 'yellow')} Task Profile        {_colorize('->', 'dim')} {profile}"

        if event == "compartment_start":
            name = data.get("name", "")
            return f"{prefix}  {_colorize('[>]', 'cyan')} Compartment          {name}"

        if event == "compartment_done":
            name = data.get("name", "")
            elapsed_c = data.get("elapsed", 0)
            return f"{prefix}    {_colorize('[ok]', 'green')} {name}  {_colorize(f'({elapsed_c:.2f}s)', 'dim')}"

        if event == "compartment_failed":
            name = data.get("name", "")
            error = data.get("error", "")
            return f"{prefix}    {_colorize('[x]', 'red')} {name}  {error}"

        return None
