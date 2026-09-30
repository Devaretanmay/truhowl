"""Box lifecycle tests - standalone, no AI imports."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from truhowl.sandbox.box import Box


class TestBoxLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(self.tmpdir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_state_is_created(self):
        b = Box(workdir=self.tmpdir)
        self.assertEqual(b.state, "created")
        self.assertFalse(b.is_active)

    def test_enter_creates_workspace(self):
        b = Box(workdir=self.tmpdir)
        self.assertFalse(os.path.exists(b.box_dir))
        b.enter(block_network=False, sandbox=False)
        self.assertEqual(b.state, "running")
        self.assertTrue(os.path.isdir(b.box_dir))
        b.exit()

    def test_exit_destroys_workspace(self):
        b = Box(workdir=self.tmpdir)
        b.enter(block_network=False, sandbox=False)
        box_dir = b.box_dir
        b.exit()
        self.assertEqual(b.state, "destroyed")
        self.assertFalse(os.path.exists(box_dir))

    def test_double_exit_raises(self):
        b = Box(workdir=self.tmpdir)
        b.enter(block_network=False, sandbox=False)
        b.exit()
        with self.assertRaises(RuntimeError):
            b.exit()

    def test_enter_after_exit_raises(self):
        b = Box(workdir=self.tmpdir)
        b.enter(block_network=False, sandbox=False)
        b.exit()
        with self.assertRaises(RuntimeError):
            b.enter(sandbox=False)

    def test_unsupported_native_sandbox_is_not_reported_as_applied(self):
        b = Box(workdir=self.tmpdir)
        def apply_fn(*_args):
            return False

        def check_fn():
            return {"supported": "false", "platform": "test", "details": "unsupported"}
        with patch("truhowl.sandbox.box._get_core", return_value=(apply_fn, check_fn)):
            self.assertFalse(b.enter(sandbox=True))
        b.exit()
