"""SnapshotManager - hash-based filesystem snapshot for rollback on compartment failure."""

import json
import logging
import os
import shutil


from blake3 import blake3

_logger = logging.getLogger("truhowl.snapshot")

_MANIFEST = "_manifest.json"

_DEFAULT_EXCLUDE = frozenset({
    ".git", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".truhowl", ".hg", ".svn",
    "target", "build", "dist", ".next",
})


def _file_hash(path: str) -> str:
    h = blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


class SnapshotManager:
    """Hash-based filesystem snapshot for rollback."""

    def __init__(
        self,
        workdir: str,
        snapshot_dir: str,
        exclude: set[str] | None = None,
    ):
        self._workdir = os.path.abspath(workdir)
        self._snapshot_dir = os.path.abspath(snapshot_dir)
        self._exclude = set(exclude) if exclude is not None else set(_DEFAULT_EXCLUDE)

    def snapshot(self) -> int:
        if os.path.isdir(self._snapshot_dir):
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)
        os.makedirs(self._snapshot_dir, exist_ok=True)

        manifest: dict[str, str] = {}
        count = 0
        for dirpath, dirnames, filenames in os.walk(self._workdir):
            dirnames[:] = [d for d in dirnames if d not in self._exclude]
            for fn in filenames:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, self._workdir)
                dst = os.path.join(self._snapshot_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    h = _file_hash(src)
                    shutil.copy2(src, dst)
                    manifest[rel] = h
                    count += 1
                except (OSError, PermissionError) as exc:
                    _logger.debug("Skipping %s: %s", rel, exc)

        with open(os.path.join(self._snapshot_dir, _MANIFEST), "w") as f:
            json.dump(manifest, f, sort_keys=True)

        _logger.info("Snapshot: %d files from %s", count, self._workdir)
        return count

    def restore(self) -> int:
        manifest_path = os.path.join(self._snapshot_dir, _MANIFEST)
        if not os.path.isfile(manifest_path):
            _logger.warning("No snapshot manifest found at %s", manifest_path)
            return 0

        with open(manifest_path) as f:
            manifest: dict[str, str] = json.load(f)

        count = 0
        for rel, expected_hash in manifest.items():
            current_path = os.path.join(self._workdir, rel)

            # Only restore if hash differs or file is missing
            needs_restore = False
            if not os.path.isfile(current_path):
                needs_restore = True
            else:
                try:
                    current_hash = _file_hash(current_path)
                    if current_hash != expected_hash:
                        needs_restore = True
                except (OSError, PermissionError):
                    needs_restore = True

            if not needs_restore:
                continue

            src = os.path.join(self._snapshot_dir, rel)
            if not os.path.isfile(src):
                continue

            os.makedirs(os.path.dirname(current_path), exist_ok=True)
            try:
                shutil.copy2(src, current_path)
                count += 1
            except (OSError, PermissionError) as exc:
                _logger.warning("Restore failed for %s: %s", rel, exc)

        tracked = set(manifest)
        for dirpath, dirnames, filenames in os.walk(self._workdir, topdown=True, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in self._exclude]
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, self._workdir)
                if rel in tracked:
                    continue
                snapshot_root = os.path.abspath(self._snapshot_dir)
                absolute_path = os.path.abspath(path)
                if absolute_path == snapshot_root or absolute_path.startswith(snapshot_root + os.sep):
                    continue
                try:
                    os.unlink(path)
                    count += 1
                except (OSError, PermissionError) as exc:
                    _logger.warning("Remove generated file failed for %s: %s", rel, exc)

        _logger.info("Restore: %d files changed out of %d tracked", count, len(manifest))
        return count

    def cleanup(self) -> None:
        if os.path.isdir(self._snapshot_dir):
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)
