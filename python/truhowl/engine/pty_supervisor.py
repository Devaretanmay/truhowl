from __future__ import annotations
from dataclasses import dataclass
import logging
import os
import shutil
import subprocess
from typing import Sequence

_logger = logging.getLogger("truhowl.process_runner")


@dataclass
class CaptureResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and self.error is None


class PtySupervisor:
    def __init__(
        self,
        workdir: str = ".",
        compartment_policy: dict | None = None,
        extra_env: dict | None = None,
    ) -> None:
        self.workdir = os.path.abspath(workdir)
        self.compartment_policy = compartment_policy or {}
        self.extra_env = extra_env or {}

    def attach(self, argv: Sequence[str]) -> int:
        if not argv:
            raise ValueError("argv must not be empty")
        binary = self._resolve(argv[0])
        child_env = self._build_env()
        proc = subprocess.run(
            [binary] + list(argv[1:]),
            cwd=self.workdir,
            env=child_env,
        )
        return proc.returncode

    def capture(self, argv: Sequence[str], timeout_s: int = 300) -> CaptureResult:
        if not argv:
            raise ValueError("argv must not be empty")
        binary = self._resolve(argv[0])
        child_env = self._build_env()
        try:
            proc = subprocess.run(
                [binary] + list(argv[1:]),
                cwd=self.workdir,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return CaptureResult(
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired:
            return CaptureResult(
                returncode=-1,
                error=f"Process timed out after {timeout_s}s",
            )
        except Exception as e:
            return CaptureResult(
                returncode=-1,
                error=str(e),
            )

    def _resolve(self, binary_name: str) -> str:
        if os.path.isabs(binary_name) or os.sep in binary_name:
            if os.path.isfile(binary_name) and os.access(binary_name, os.X_OK):
                return binary_name
            raise FileNotFoundError(f"Binary not found: {binary_name}")

        truhowl_base = os.path.abspath(os.path.join(self.workdir, ".truhowl"))
        path_dirs = os.environ.get("PATH", "").split(os.path.pathsep)
        for d in path_dirs:
            if not d:
                continue
            abs_d = os.path.abspath(d)
            if abs_d == truhowl_base or abs_d.startswith(truhowl_base + os.sep):
                continue
            candidate = os.path.join(d, binary_name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        resolved = shutil.which(binary_name)
        if resolved and os.path.isfile(resolved):
            return resolved
        raise FileNotFoundError(f"Could not resolve binary on PATH: {binary_name}")

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        return env
