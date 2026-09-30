# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Regression tests verifying fail-closed trust and verification invariants."""

import os
import unittest
from unittest.mock import MagicMock, patch

from truhowl.engine.execution import Execution, ExecutionKind
from truhowl.pipeline import AnalysisResult, PipelinePolicy, TriggerContext, verify_fixes
from truhowl.sandbox.box import Box
from truhowl.sandbox.proxy import CredentialProxy
from truhowl.test_runner import run_sandboxed_command


class TestFailClosedVerification(unittest.TestCase):
    def test_missing_test_command_never_marks_verified_true(self):
        """Invariant: No test command must NEVER mean verified = True."""
        ctx = TriggerContext(
            event_id="test_ev",
            event_type="push",
            repository="owner/repo",
            ref="main",
            sha="abc1234",
            workdir="/tmp/fake-test-repo",
        )
        analysis = AnalysisResult(
            context=ctx,
            modified_files=["src/index.ts"],
            unified_diffs=["--- a/src/index.ts\n+++ b/src/index.ts\n@@ -1 +1 @@\n-old\n+new"],
        )
        policy = PipelinePolicy()

        with patch("truhowl.pipeline._detect_test_command", return_value=""):
            result = verify_fixes(ctx, analysis, policy)

        self.assertFalse(result.verified, "Missing test command must fail closed (verified=False)")
        self.assertEqual(result.test_command, "")
        self.assertEqual(result.test_exit_code, -1)
        self.assertEqual(result.modified_files, [], "Unverified modified files must be cleared")

    def test_failing_test_command_marks_verified_false(self):
        """Failing test command must result in verified = False."""
        ctx = TriggerContext(
            event_id="test_ev",
            event_type="push",
            repository="owner/repo",
            ref="main",
            sha="abc1234",
            workdir="/tmp/fake-test-repo",
        )
        analysis = AnalysisResult(
            context=ctx,
            modified_files=["src/index.ts"],
            unified_diffs=["diff"],
        )
        mock_proc = MagicMock(returncode=1)
        policy = PipelinePolicy()

        with patch("truhowl.pipeline._detect_test_command", return_value="npm test"), \
             patch("truhowl.pipeline._run_tests", return_value=mock_proc):
            result = verify_fixes(ctx, analysis, policy)

        self.assertFalse(result.verified)
        self.assertEqual(result.test_exit_code, 1)

    def test_passing_test_command_marks_verified_true(self):
        """Exit code 0 marks verified = True."""
        ctx = TriggerContext(
            event_id="test_ev",
            event_type="push",
            repository="owner/repo",
            ref="main",
            sha="abc1234",
            workdir="/tmp/fake-test-repo",
        )
        analysis = AnalysisResult(
            context=ctx,
            modified_files=["src/index.ts"],
            unified_diffs=["diff"],
        )
        mock_proc = MagicMock(returncode=0)
        policy = PipelinePolicy()

        with patch("truhowl.pipeline._detect_test_command", return_value="npm test"), \
             patch("truhowl.pipeline._run_tests", return_value=mock_proc):
            result = verify_fixes(ctx, analysis, policy)

        self.assertTrue(result.verified)
        self.assertEqual(result.test_exit_code, 0)
        self.assertEqual(len(result.modified_files), 1)


class TestSandboxProvenance(unittest.TestCase):
    def test_unsandboxed_execution_emits_sandbox_none(self):
        """Unsandboxed execution must emit Agent-Sandbox: none per SPEC.md."""
        ex = Execution(
            execution_id="exec_test_unsandboxed",
            kind=ExecutionKind.PROCESS,
            command=["pytest"],
            policy={"sandbox": False, "isolation": "git-worktree"},
        )
        trailers = ex.git_trailers()
        self.assertIn("Agent-Sandbox: none", trailers)
        self.assertIn("Execution-Isolation: git-worktree", trailers)

    def test_sandboxed_clean_emits_clean(self):
        """Clean sandboxed execution emits Agent-Sandbox: clean."""
        ex = Execution(
            execution_id="exec_test_sandboxed",
            kind=ExecutionKind.PROCESS,
            command=["pytest"],
            policy={"sandbox": True},
        )
        trailers = ex.git_trailers()
        self.assertIn("Agent-Sandbox: clean", trailers)

    def test_sandboxed_with_violations_emits_blocked(self):
        """Policy violations emit Agent-Sandbox: blocked."""
        ex = Execution(
            execution_id="exec_test_blocked",
            kind=ExecutionKind.PROCESS,
            command=["pytest"],
            policy={"sandbox": True},
        )
        ex.emit("fs.denied", {"path": "/etc/shadow"})
        trailers = ex.git_trailers()
        self.assertIn("Agent-Sandbox: blocked", trailers)


class TestChildProcessSandboxEnforcement(unittest.TestCase):
    def test_enforced_sandbox_fails_closed_when_unsupported(self):
        """Enforced sandboxing must raise RuntimeError if platform is unsupported."""
        with patch("truhowl._core.sandbox_check_supported", return_value={"supported": "false", "platform": "mock", "details": "none"}):
            with self.assertRaises(RuntimeError) as ctx:
                run_sandboxed_command(["echo", "hi"], cwd=".", enforce=True)
            self.assertIn("Fail-closed", str(ctx.exception))

    def test_box_enter_enforce_fails_closed_when_unsupported(self):
        """Box.enter(enforce=True) raises RuntimeError if sandbox unsupported."""
        b = Box(workdir="/tmp")
        with patch("truhowl.sandbox.box._CORE", (lambda *a: False, lambda: {"supported": "false"})):
            with self.assertRaises(RuntimeError):
                b.enter(sandbox=True, enforce=True)


class TestCredentialProxyTruth(unittest.TestCase):
    def test_proxy_set_env_does_not_set_https_proxy_by_default(self):
        """CredentialProxy must not set HTTPS_PROXY by default because CONNECT is unsupported."""
        p = CredentialProxy(routes=[])
        p.port = 8888
        p.set_env()
        try:
            self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:8888")
            self.assertIsNone(os.environ.get("HTTPS_PROXY"))
        finally:
            p.restore_env()


if __name__ == "__main__":
    unittest.main()
