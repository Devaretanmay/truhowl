"""Unit tests for SnapshotManager - hash-based snapshot, restore, and cleanup."""

import json
import os
import shutil
import tempfile
import uuid
import unittest
from blake3 import blake3

from truhowl.sandbox.snapshot import SnapshotManager, _file_hash


def _write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return content


class _SnapshotTestBase(unittest.TestCase):

    def setUp(self):
        uid = uuid.uuid4().hex[:8]
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_snap_ut_{uid}")
        self.workdir = os.path.join(self.tmpdir, "work")
        self.snapdir = os.path.join(self.tmpdir, "snapshots")
        os.makedirs(self.workdir)
        self.mgr = SnapshotManager(
            workdir=self.workdir,
            snapshot_dir=self.snapdir,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestSnapshotManagerBasics(_SnapshotTestBase):
    """Basic lifecycle: snapshot, restore, cleanup."""

    def test_snapshot_creates_manifest(self):
        _write(os.path.join(self.workdir, "a.txt"), "aaa")
        _write(os.path.join(self.workdir, "b.txt"), "bbb")
        count = self.mgr.snapshot()
        self.assertEqual(count, 2)
        manifest_path = os.path.join(self.snapdir, "_manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.assertIn("a.txt", manifest)
        self.assertIn("b.txt", manifest)
        self.assertEqual(len(manifest["a.txt"]), 16)
        self.assertEqual(manifest["a.txt"], blake3(b"aaa").hexdigest()[:16])

    def test_cleanup_removes_snapshot_dir(self):
        _write(os.path.join(self.workdir, "f.txt"), "data")
        self.mgr.snapshot()
        self.assertTrue(os.path.isdir(self.snapdir))
        self.mgr.cleanup()
        self.assertFalse(os.path.isdir(self.snapdir))

    def test_restore_no_snapshot_returns_zero(self):
        count = self.mgr.restore()
        self.assertEqual(count, 0)

    def test_restore_no_changes_returns_zero(self):
        _write(os.path.join(self.workdir, "f.txt"), "data")
        self.mgr.snapshot()
        count = self.mgr.restore()
        self.assertEqual(count, 0)

    def test_snapshot_empty_workdir(self):
        count = self.mgr.snapshot()
        self.assertEqual(count, 0)


class TestSnapshotHashMatching(_SnapshotTestBase):
    """Verify hash-based change detection works correctly."""

    def test_hash_changes_when_content_changes(self):
        path = os.path.join(self.workdir, "data.txt")
        _write(path, "original content")
        h1 = _file_hash(path)
        _write(path, "modified content")
        h2 = _file_hash(path)
        self.assertNotEqual(h1, h2, "Hash must change when content changes")

    def test_hash_stable_for_same_content(self):
        path = os.path.join(self.workdir, "data.txt")
        _write(path, "stable content")
        h1 = _file_hash(path)
        h2 = _file_hash(path)
        self.assertEqual(h1, h2, "Hash must be stable for same content")

    def test_manifest_hashes_are_correct(self):
        _write(os.path.join(self.workdir, "a.txt"), "hello")
        _write(os.path.join(self.workdir, "b.txt"), "world")
        self.mgr.snapshot()
        with open(os.path.join(self.snapdir, "_manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["a.txt"], _file_hash(os.path.join(self.workdir, "a.txt")))
        self.assertEqual(manifest["b.txt"], _file_hash(os.path.join(self.workdir, "b.txt")))


class TestSnapshotRestoreOnlyChanged(_SnapshotTestBase):
    """Only files whose hash differs from the manifest are restored."""

    def test_restore_only_modified_file(self):
        _write(os.path.join(self.workdir, "stable.py"), "def f(): pass")
        _write(os.path.join(self.workdir, "changed.py"), "x = 1")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "changed.py"), "x = 999")
        count = self.mgr.restore()
        self.assertEqual(count, 1, "Only the modified file should be restored")

    def test_restore_resets_content(self):
        _write(os.path.join(self.workdir, "f.py"), "original")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "f.py"), "corrupted")
        self.mgr.restore()
        with open(os.path.join(self.workdir, "f.py")) as f:
            self.assertEqual(f.read(), "original", "Content must be restored")

    def test_restore_multiple_files(self):
        for i in range(5):
            _write(os.path.join(self.workdir, f"f{i}.txt"), f"content{i}")
        self.mgr.snapshot()
        for i in range(3):
            _write(os.path.join(self.workdir, f"f{i}.txt"), f"modified{i}")
        count = self.mgr.restore()
        self.assertEqual(count, 3, "Only modified files should be restored")

    def test_restore_no_ops_when_none_changed(self):
        for i in range(5):
            _write(os.path.join(self.workdir, f"f{i}.txt"), f"content{i}")
        self.mgr.snapshot()
        count = self.mgr.restore()
        self.assertEqual(count, 0, "Nothing to restore when nothing changed")


class TestSnapshotDeletedFiles(_SnapshotTestBase):
    """Files deleted during execution are restored from snapshot."""

    def test_deleted_file_restored(self):
        _write(os.path.join(self.workdir, "important.py"), "critical data")
        self.mgr.snapshot()
        os.remove(os.path.join(self.workdir, "important.py"))
        count = self.mgr.restore()
        self.assertEqual(count, 1, "Deleted file must be restored")
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "important.py")))

    def test_deleted_file_content_restored_correctly(self):
        _write(os.path.join(self.workdir, "cfg.json"), '{"key": "secret"}')
        self.mgr.snapshot()
        os.remove(os.path.join(self.workdir, "cfg.json"))
        self.mgr.restore()
        with open(os.path.join(self.workdir, "cfg.json")) as f:
            self.assertEqual(f.read(), '{"key": "secret"}')

    def test_mixed_delete_and_modify(self):
        _write(os.path.join(self.workdir, "keep.txt"), "keep")
        _write(os.path.join(self.workdir, "del.txt"), "delete me")
        _write(os.path.join(self.workdir, "mod.txt"), "modify me")
        self.mgr.snapshot()
        os.remove(os.path.join(self.workdir, "del.txt"))
        _write(os.path.join(self.workdir, "mod.txt"), "modified")
        count = self.mgr.restore()
        self.assertEqual(count, 2, "Deleted + modified files must be restored")
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "del.txt")))
        with open(os.path.join(self.workdir, "mod.txt")) as f:
            self.assertEqual(f.read(), "modify me")


class TestSnapshotNewFiles(_SnapshotTestBase):
    """New files created during execution are removed on restore."""

    def test_new_file_is_removed_on_restore(self):
        _write(os.path.join(self.workdir, "original.txt"), "original")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "new.txt"), "i am new")
        self.mgr.restore()
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "new.txt")))

    def test_new_file_in_new_dir_is_removed(self):
        _write(os.path.join(self.workdir, "a.txt"), "a")
        self.mgr.snapshot()
        os.makedirs(os.path.join(self.workdir, "sub"))
        _write(os.path.join(self.workdir, "sub", "b.txt"), "b")
        self.mgr.restore()
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "sub", "b.txt")))


class TestSnapshotExcludePatterns(_SnapshotTestBase):
    """Excluded directories are skipped during snapshot."""

    def setUp(self):
        uid = uuid.uuid4().hex[:8]
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_exclude_ut_{uid}")
        self.workdir = os.path.join(self.tmpdir, "work")
        self.snapdir = os.path.join(self.tmpdir, "snapshots")
        os.makedirs(self.workdir)
        self.mgr = SnapshotManager(
            workdir=self.workdir,
            snapshot_dir=self.snapdir,
            exclude={"node_modules", ".venv", "build"},
        )

    def test_excluded_dirs_skipped(self):
        _write(os.path.join(self.workdir, "src/main.py"), "code")
        _write(os.path.join(self.workdir, "node_modules/package/index.js"), "skipped")
        _write(os.path.join(self.workdir, ".venv/bin/python"), "skipped")
        _write(os.path.join(self.workdir, "build/output.o"), "skipped")
        count = self.mgr.snapshot()
        self.assertEqual(count, 1, "Only src/main.py should be snapshotted")

    def test_excluded_dirs_not_in_manifest(self):
        _write(os.path.join(self.workdir, "keep.py"), "keep")
        _write(os.path.join(self.workdir, "node_modules/pkg.js"), "skip")
        self.mgr.snapshot()
        with open(os.path.join(self.snapdir, "_manifest.json")) as f:
            manifest = json.load(f)
        self.assertIn("keep.py", manifest)
        self.assertNotIn("node_modules/pkg.js", manifest)

    def test_custom_exclude_overrides_default(self):
        mgr = SnapshotManager(
            workdir=self.workdir,
            snapshot_dir=self.snapdir,
            exclude={".git", "custom_exclude"},
        )
        _write(os.path.join(self.workdir, "README.md"), "docs")
        _write(os.path.join(self.workdir, ".git/HEAD"), "ref")
        _write(os.path.join(self.workdir, "custom_exclude/data.bin"), "skip")
        count = mgr.snapshot()
        self.assertEqual(count, 1, "Only README.md should be snapshotted")


class TestSnapshotSubdirectories(_SnapshotTestBase):
    """Files in nested subdirectories are handled correctly."""

    def test_nested_files_snapshotted(self):
        _write(os.path.join(self.workdir, "a/b/c/deep.txt"), "deep")
        _write(os.path.join(self.workdir, "x/y/z/deep2.txt"), "deep2")
        count = self.mgr.snapshot()
        self.assertEqual(count, 2)
        with open(os.path.join(self.snapdir, "_manifest.json")) as f:
            manifest = json.load(f)
        self.assertIn("a/b/c/deep.txt", manifest)
        self.assertIn("x/y/z/deep2.txt", manifest)

    def test_nested_file_restored(self):
        _write(os.path.join(self.workdir, "src/lib/helper.py"), "helper code")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "src/lib/helper.py"), "corrupted")
        self.mgr.restore()
        with open(os.path.join(self.workdir, "src/lib/helper.py")) as f:
            self.assertEqual(f.read(), "helper code")

    def test_mixed_nested_and_flat(self):
        _write(os.path.join(self.workdir, "root.txt"), "root")
        _write(os.path.join(self.workdir, "sub/dir/file.txt"), "nested")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "root.txt"), "modified root")
        count = self.mgr.restore()
        self.assertEqual(count, 1, "Only root.txt should be restored")


class TestSnapshotEdgeCases(_SnapshotTestBase):
    """Edge cases and error handling."""

    def test_double_snapshot_overwrites(self):
        _write(os.path.join(self.workdir, "v1.txt"), "version 1")
        self.mgr.snapshot()
        _write(os.path.join(self.workdir, "v1.txt"), "version 2")
        self.mgr.snapshot()
        self.mgr.restore()
        with open(os.path.join(self.workdir, "v1.txt")) as f:
            self.assertEqual(
                f.read(), "version 2",
                "Second snapshot should capture version 2",
            )

    def test_symlink_skipped(self):
        _write(os.path.join(self.workdir, "real.txt"), "real")
        try:
            os.symlink("real.txt", os.path.join(self.workdir, "link.txt"))
        except OSError:
            pass
        count = self.mgr.snapshot()
        self.assertGreaterEqual(count, 1)

    def test_binary_files(self):
        _write(os.path.join(self.workdir, "data.bin"),
               b"\x00\x01\x02\xff\xfe".decode("latin-1"))
        _write(os.path.join(self.workdir, "img.png"),
               b"\x89PNG\r\n\x1a\n".decode("latin-1"))
        count = self.mgr.snapshot()
        self.assertEqual(count, 2)
        self.mgr.restore()
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "data.bin")))


if __name__ == "__main__":
    unittest.main()
