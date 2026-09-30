"""SandboxEnforcer - Python-level per-compartment permission enforcement."""

import builtins
import logging
import os
import shutil
import subprocess
from typing import Any, Callable

_logger = logging.getLogger("truhowl.enforcer")


def _check(policy: dict, require: list[str]) -> None:
    perms = policy.get("permissions", [])
    missing = [p for p in require if p not in perms]
    if missing:
        raise PermissionError(
            f"compartment '{policy.get('name', '?')}' lacks: "
            f"{missing} (needs: {require})"
        )


class SandboxEnforcer:
    """Context manager that patches I/O functions to enforce a sandbox policy."""

    def __init__(self, policy: dict):
        self._policy = dict(policy)
        self._originals: dict[str, Any] = {}

    def __enter__(self):
        self._patch()
        return self

    def __exit__(self, *args):
        self._unpatch()

    def _patch(self):
        p = self._policy

        orig = builtins.open
        self._save("builtins.open", orig)
        builtins.open = _wrap_open(orig, p)

        for name in ("system", "popen"):
            orig = getattr(os, name)
            self._save(f"os.{name}", orig)
            setattr(os, name, _wrap_exec(orig, p))

        for name in (
            "remove", "unlink", "rmdir", "mkdir", "makedirs",
            "rename", "replace", "symlink", "chmod", "chown",
        ):
            orig = getattr(os, name)
            self._save(f"os.{name}", orig)
            setattr(os, name, _wrap_write(orig, p))

        for name in ("listdir", "scandir", "walk"):
            orig = getattr(os, name)
            self._save(f"os.{name}", orig)
            setattr(os, name, _wrap_read(orig, p))

        orig = os.open
        self._save("os.open", orig)
        os.open = _wrap_os_open(orig, p)

        for name in ("run", "Popen", "call", "check_call", "check_output"):
            orig = getattr(subprocess, name, None)
            if orig is not None:
                self._save(f"subprocess.{name}", orig)
                setattr(subprocess, name, _wrap_exec(orig, p))

        for name in ("copy2", "copy", "copyfile", "copyfileobj", "copystat",
                     "move", "copytree", "rmtree"):
            orig = getattr(shutil, name, None)
            if orig is not None:
                self._save(f"shutil.{name}", orig)
                setattr(shutil, name, _wrap_write(orig, p))

    def _unpatch(self):
        for key, original in self._originals.items():
            mod, name = key.split(".", 1)
            module = {
                "builtins": builtins, "os": os,
                "subprocess": subprocess, "shutil": shutil,
            }.get(mod)
            if module is not None:
                setattr(module, name, original)
        self._originals.clear()

    def _save(self, key: str, original: Any):
        self._originals[key] = original


# Wrappers capture the ORIGINAL function at creation time to prevent recursion.


# Commands blocked regardless of fs_exec permission.
DANGEROUS_COMMANDS: set[str] = {
    "mkfs", "mkfs.ext4", "mkfs.btrfs", "mkfs.xfs", "mkfs.fat", "mkswap",
    "fdisk", "parted", "partprobe", "gdisk", "sfdisk",
    "dd",
    "sudo", "su", "doas", "pkexec", "visudo",
    "shutdown", "reboot", "poweroff", "halt", "init", "systemctl",
    "telinit", "runlevel",
    "passwd", "chpasswd", "usermod", "groupmod", "useradd", "userdel",
    "adduser", "deluser", "addgroup", "delgroup",
    "kexec", "modprobe", "insmod", "rmmod", "depmod",
    "iptables", "ip6tables", "ufw", "firewall-cmd",
    "docker", "podman", "nerdctl", "ctr",
}

# Patterns matched against full command strings (e.g. "rm -rf /").
DANGEROUS_PATTERNS: list[str] = [
    "rm -rf /", "rm -rf ~", "rm -rf .", "rm -rf *",
    "rm -r /", "rm -rf --no-preserve-root",
    "chmod 777", "chmod -R 777", "chmod a+rwx",
    "chown -R", "chown 0:",
    ":(){ :|:& };:",
    ">/dev/sda", ">/dev/sdb", ">/dev/nvme",
    "eval ", "exec ",
    "source /dev", ". /dev",
]


def _extract_cmd_from_args(args: tuple, kwargs: dict) -> str:
    """Extract a human-readable command string from subprocess/os call args."""
    if not args:
        return ""
    cmd = args[0]
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(a) for a in cmd)
    return str(cmd)


def _check_dangerous(cmd_str: str) -> None:
    """Raise PermissionError if the command matches the blocklist."""
    cmd_str = cmd_str.strip()
    if not cmd_str:
        return

    first_token = cmd_str.split()[0].lower() if cmd_str.split() else ""
    base_cmd = first_token.split("/")[-1] if "/" in first_token else first_token
    if base_cmd in DANGEROUS_COMMANDS:
        raise PermissionError(
            f"Command blocked: '{base_cmd}' is not allowed in sandbox mode"
        )

    cmd_lower = cmd_str.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            raise PermissionError(
                f"Command blocked: matches dangerous pattern '{pattern}'"
            )

    # Block piping remote output to a shell interpreter
    pipe_targets = ["| bash", "| sh", "| zsh", "| fish", "| python", "| python3"]
    for target in pipe_targets:
        if target in cmd_lower:
            raise PermissionError(
                f"Command blocked: piping to '{target.strip('| ')}' is not allowed"
            )


def _wrap_open(original: Callable, policy: dict) -> Callable:
    def _wrapped(file, mode="r", *args, **kwargs):
        needs = []
        if "r" in mode:
            needs.append("fs_read")
        if "w" in mode or "a" in mode or "+" in mode or "x" in mode:
            needs.append("fs_write")
        if needs:
            _check(policy, needs)
        return original(file, mode, *args, **kwargs)
    return _wrapped


def _wrap_exec(original: Callable, policy: dict) -> Callable:
    def _wrapped(*args, **kwargs):
        cmd_str = _extract_cmd_from_args(args, kwargs)
        _check_dangerous(cmd_str)
        _check(policy, ["fs_exec"])
        return original(*args, **kwargs)
    return _wrapped


def _wrap_write(original: Callable, policy: dict) -> Callable:
    def _wrapped(*args, **kwargs):
        _check(policy, ["fs_write"])
        return original(*args, **kwargs)
    return _wrapped


def _wrap_read(original: Callable, policy: dict) -> Callable:
    def _wrapped(*args, **kwargs):
        _check(policy, ["fs_read"])
        return original(*args, **kwargs)
    return _wrapped


def _wrap_os_open(original: Callable, policy: dict) -> Callable:
    def _wrapped(path, flags, mode=0o777, *, dir_fd=None):
        needs = []
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        if flags & write_flags:
            needs.append("fs_write")
        if not (flags & write_flags) or (flags & os.O_RDWR):
            needs.append("fs_read")
        if needs:
            _check(policy, needs)
        return original(path, flags, mode, dir_fd=dir_fd)
    return _wrapped
