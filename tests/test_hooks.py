"""Tests for the execution hooks (truhowl.hooks).

All hooks are exercised with ``sandbox=False`` because the kernel sandbox
(Landlock / Seatbelt) is irreversible per process: applying it inside pytest
would isolate the shared test process. The Python-level SandboxEnforcer still
runs inside every compartment, so permission checks and execution behaviour
are tested for real.
"""

import os
import shutil
import tempfile
import unittest

from truhowl.hooks.base import (
    DEFAULT_PERMISSIONS,
    VALID_PERMISSIONS,
    ExecutionResult,
    SandboxRunner,
    diff_trees,
    index_workdir,
    validate_permissions,
)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="truhowl_hooks_test_")
        self.workdir = os.path.join(self.base, "run")
        os.makedirs(self.workdir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _write(self, rel, content):
        path = os.path.join(self.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        return path


class TestPermissions(TempCase):
    def test_valid_permissions_dedup(self):
        self.assertEqual(
            validate_permissions(["fs_read", "fs_read", "fs_exec"]),
            ("fs_read", "fs_exec"),
        )

    def test_defaults_within_vocabulary(self):
        self.assertLessEqual(set(DEFAULT_PERMISSIONS), set(VALID_PERMISSIONS))

    def test_unknown_permission_raises(self):
        with self.assertRaises(ValueError) as exc:
            validate_permissions(["fs_read", "banana"])
        self.assertIn("banana", str(exc.exception))


class TestDiffs(TempCase):
    def test_modify_add_delete(self):
        self._write("a.txt", "one")
        self._write("b.txt", "two")
        before = index_workdir(self.workdir)
        self._write("a.txt", "two")
        os.remove(os.path.join(self.workdir, "b.txt"))
        self._write("new.txt", "n")
        after = index_workdir(self.workdir)
        statuses = {d["path"]: d["status"] for d in diff_trees(before, after)}
        self.assertEqual(statuses["a.txt"], "modified")
        self.assertEqual(statuses["b.txt"], "deleted")
        self.assertEqual(statuses["new.txt"], "added")

    def test_excluded_dirs_not_indexed(self):
        self._write(".venv/lib/x.py", "ignored")
        self._write("node_modules/pkg.js", "ignored")
        self._write("keep.txt", "k")
        index = index_workdir(self.workdir)
        self.assertNotIn(".venv/lib/x.py", index)
        self.assertNotIn("node_modules/pkg.js", index)
        self.assertIn("keep.txt", index)

    def test_execution_result_dict(self):
        r = ExecutionResult(returncode=1, stdout="o", stderr="e")
        self.assertEqual(r.as_dict()["returncode"], 1)
        self.assertEqual(r.output, "o\ne")
        self.assertFalse(r.success)
        self.assertTrue(ExecutionResult().success)


class TestSandboxRunner(TempCase):
    def test_shell_capture(self):
        res = SandboxRunner(workdir=self.workdir, sandbox=False).run(
            "echo hi from hook", snapshot=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("hi from hook", res.stdout)
        self.assertTrue(res.success)

    def test_env_passthrough(self):
        res = SandboxRunner(workdir=self.workdir, sandbox=False).run(
            "echo $MY_VAR", snapshot=False, env={"MY_VAR": "custom-data"},
        )
        self.assertIn("custom-data", res.stdout)

    def test_timeout_reported(self):
        # NOTE: when the whole pytest process is kernel-sandboxed by
        # test_e2e_full_suite.py, the CLI ``sleep`` binary cannot be exec'd
        # at all; the box surfaces that as a non-zero return code instead of
        # a TimeoutExpired. The invariant we pin here is "a too-long command
        # must not return 0":
        res = SandboxRunner(workdir=self.workdir, sandbox=False).run(
            "sleep 3", timeout_s=1, snapshot=False,
        )
        self.assertEqual(res.returncode, -1)
        sanity = (res.error or "") + res.stderr
        self.assertTrue(
            "timed out" in sanity or res.returncode != 0 and bool(res.error)
        )

    def test_run_code_and_diffs(self):
        code = "open('created_by_agent.txt', 'w').write('x')\nprint('OUT')\n"
        res = SandboxRunner(workdir=self.workdir, sandbox=False).run_code(code)
        self.assertEqual(res.returncode, 0)
        self.assertIn("OUT", res.stdout)
        self.assertTrue(
            any(d["path"] == "created_by_agent.txt" and d["status"] == "added" for d in res.diffs)
        )

    def test_run_code_unsupported_language(self):
        res = SandboxRunner(workdir=self.workdir, sandbox=False).run_code(
            "print(1)", language="ruby",
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("unsupported", res.stderr)


if __name__ == "__main__":
    unittest.main()