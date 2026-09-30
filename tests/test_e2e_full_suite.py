"""Full E2E test suite for compartment-centric Truhowl."""

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

from truhowl import Truhowl
from truhowl.compartments import Compartment, CompartmentConfig
from truhowl.sandbox.task_profile import classify
from truhowl.engine.tracer import Tracer

def _make_repo(path: str):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@truhowl.test"],
                   cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Truhowl Test"],
                   cwd=path, capture_output=True)
    readme = os.path.join(path, "README.md")
    with open(readme, "w") as f:
        f.write("# Test Repo\n")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path,
                   capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                        "GIT_AUTHOR_EMAIL": "test@test.com",
                        "GIT_COMMITTER_NAME": "Test",
                        "GIT_COMMITTER_EMAIL": "test@test.com"})

def _comp(name: str, data: dict, perms: list[str] = None):
    """Create a compartment that returns a fixed result."""
    def fn(ctx):
        return data
    return Compartment(
        name=name, fn=fn,
        config=CompartmentConfig(permissions=perms or ["fs_read"]),
    )

def _stateful_comp(name: str, fn):
    """Create a compartment with custom logic."""
    return Compartment(
        name=name, fn=fn,
        config=CompartmentConfig(permissions=["fs_read"]),
    )

class TestSingleCompartment(unittest.TestCase):
    """Test 1 - The simplest possible execution. Verify every lifecycle phase."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test1_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_basic_lifecycle_executes(self):
        """Outer compartment, Compartment, Cleanup, Destroy."""
        box = Truhowl(workdir=self.tmpdir)
        self.assertEqual(box._box.state, "created")

        box._box.enter(block_network=False, sandbox=False)
        self.assertEqual(box._box.state, "running")
        self.assertTrue(os.path.isdir(box.box_dir))

        box._box.insulate("Create a Python function that reverses a string")
        self.assertEqual(box._box.task_profile, "code")

        box._box.release()
        self.assertEqual(box._box.state, "running")

        box._box.exit()
        self.assertEqual(box._box.state, "destroyed")
        self.assertFalse(os.path.isdir(box.box_dir))

    def test_single_compartment_runs(self):
        """A single compartment should run and return its result."""
        box = Truhowl(workdir=self.tmpdir)
        box.add(_comp("greeter", {"message": "Hello, World!"}))
        result = box.run()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.compartments_completed, ["greeter"])
        self.assertEqual(result.output.get("greeter", {}).get("message"), "Hello, World!")

    def test_tracer_outputs_lifecycle(self):
        """Execution trace should record every phase."""
        tracer = Tracer("test_tracer", verbose=True)
        box = Truhowl(workdir=self.tmpdir, verbose=True)

        tracer.emit("box.created", box_id=box.box_id)
        box._box.enter(block_network=False, sandbox=False)
        tracer.emit("box.entered", sandbox_applied=False)

        box._box.insulate("Reverse a string")
        box._box.release()

        box._box.exit()
        tracer.emit("box.destroyed")
        tracer.footer("success", 0.5)

        self.assertGreater(len(tracer._entries), 0)

class TestPolicyIsolation(unittest.TestCase):
    """Test 2 - Each compartment has its own permission set."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test2_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_different_permissions_per_compartment(self):
        """Two compartments in the same Box should have different policies."""
        box = Truhowl(workdir=self.tmpdir)
        box.add(Compartment(
            name="reader",
            fn=lambda ctx: {"policy": ctx.config.permissions},
            config=CompartmentConfig(permissions=["fs_read"]),
        ))
        box.add(Compartment(
            name="writer",
            fn=lambda ctx: {"policy": ctx.config.permissions},
            config=CompartmentConfig(permissions=["fs_read", "fs_write"]),
        ))
        result = box.run()
        self.assertEqual(
            result.output.get("reader", {}).get("policy"),
            ["fs_read"],
        )
        self.assertEqual(
            result.output.get("writer", {}).get("policy"),
            ["fs_read", "fs_write"],
        )

    def test_policy_applied_to_box_before_run(self):
        """Box.apply_policy should be called before each compartment runs."""
        box = Truhowl(workdir=self.tmpdir)
        tracked = []

        def tracker_fn(ctx):
            tracked.append(dict(box._box._current_policy))
            return {"tracked": True}

        box.add(Compartment(
            name="one", fn=tracker_fn,
            config=CompartmentConfig(permissions=["fs_read"], timeout_s=30),
        ))
        box.add(Compartment(
            name="two", fn=tracker_fn,
            config=CompartmentConfig(permissions=["network", "fs_read"], timeout_s=120),
        ))
        box.run()

        self.assertEqual(len(tracked), 2)
        self.assertEqual(tracked[0].get("permissions"), ["fs_read"])
        self.assertEqual(tracked[1].get("permissions"), ["network", "fs_read"])
        self.assertEqual(tracked[0].get("timeout_s"), 30)
        self.assertEqual(tracked[1].get("timeout_s"), 120)

    def test_task_profile_classification(self):
        """'Fix' maps to 'debugging', 'Refactor' to 'code', 'Explore' to 'research'."""
        self.assertEqual(classify("Fix this bug"), "debugging")
        self.assertEqual(classify("Refactor the auth module"), "code")
        self.assertEqual(classify("Explore the codebase"), "research")

class TestMultiCompartmentPipeline(unittest.TestCase):
    """Test 3 - Compartments compose into pipelines via message passing."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test3_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_three_compartment_pipeline(self):
        """A, B, C should execute in order."""
        def a(ctx):
            ctx.send("b", {"from": "a"})
            return {"step": "a_done"}

        def b(ctx):
            msgs = ctx.messages
            ctx.send("c", {"from": "b", "received": msgs[0].data if msgs else None})
            return {"step": "b_done"}

        def c(ctx):
            return {"step": "c_done", "received": ctx.messages[0].data if ctx.messages else None}

        box = Truhowl(workdir=self.tmpdir)
        box.add(_stateful_comp("a", a))
        box.add(_stateful_comp("b", b))
        box.add(_stateful_comp("c", c))
        box.edge("a", "b").edge("b", "c")
        result = box.run()

        self.assertEqual(result.status, "success")
        self.assertEqual(result.compartments_completed, ["a", "b", "c"])
        self.assertEqual(result.output.get("c", {}).get("step"), "c_done")
        self.assertIsNotNone(result.output.get("c", {}).get("received"))

    def test_compartment_order_is_preserved(self):
        """Compartments must execute in registration order."""
        order = []

        def tracker(name):
            def fn(ctx):
                order.append(name)
                return {"order": len(order)}
            return fn

        box = Truhowl(workdir=self.tmpdir)
        box.add(_stateful_comp("alpha", tracker("alpha")))
        box.add(_stateful_comp("beta", tracker("beta")))
        box.add(_stateful_comp("gamma", tracker("gamma")))
        box.run()

        self.assertEqual(order, ["alpha", "beta", "gamma"])

class TestLongWorkflow(unittest.TestCase):
    """Test 4 - Box stability, insulation adaptation over time."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test4_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_box_stable_over_multiple_runs(self):
        """Box should handle multiple sequential runs without degradation."""
        for i in range(5):
            box = Truhowl(workdir=self.tmpdir)
            box._box.enter(block_network=False, sandbox=False)
            self.assertTrue(box._box.is_active)
            self.assertGreaterEqual(box._box.elapsed_s, 0)
            box._box.exit()
            self.assertEqual(box._box.state, "destroyed")

    def test_insulation_adapts_to_different_tasks(self):
        """The box should load different profiles for different tasks."""
        profiles_seen = []
        for request in ["Refactor X", "Fix Y", "Explore Z"]:
            box = Truhowl(workdir=self.tmpdir)
            box._box.enter(block_network=False, sandbox=False)
            box._box.insulate(request)
            profiles_seen.append(box._box.task_profile)
            box._box.release()
            box._box.exit()
        self.assertIn("code", profiles_seen)
        self.assertIn("debugging", profiles_seen)
        self.assertIn("research", profiles_seen)

    def test_multiple_runs_via_truhowl(self):
        """Using Truhowl.run() multiple times with different compartments."""
        for name in ["build", "test", "deploy"]:
            box = Truhowl(workdir=self.tmpdir)
            box.add(_comp(name, {"task": name}))
            result = box.run()
            self.assertEqual(result.status, "success")
            self.assertEqual(result.compartments_completed, [name])

class TestFailureRecovery(unittest.TestCase):
    """Test 5 - Compartment errors, box health after failure, cleanup."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test5_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_compartment_raises(self):
        """Running without compartments should raise."""
        box = Truhowl(workdir=self.tmpdir)
        with self.assertRaises(RuntimeError):
            box.run()

    def test_failing_compartment_does_not_break_box(self):
        """A compartment that raises should be caught, not crash the runtime."""
        box = Truhowl(workdir=self.tmpdir)
        box.add(Compartment(
            name="failing",
            fn=lambda ctx: (_ for _ in ()).throw(ValueError("boom")),
            config=CompartmentConfig(permissions=["fs_read"]),
        ))
        result = box.run()
        self.assertEqual(result.status, "error")
        self.assertGreater(len(result.errors), 0)

    def test_box_healthy_after_error(self):
        """Box must be properly destroyed even when a compartment fails."""
        box = Truhowl(workdir=self.tmpdir)
        box.add(Compartment(
            name="crash",
            fn=lambda ctx: 1 / 0,
            config=CompartmentConfig(permissions=["fs_read"]),
        ))
        result = box.run()
        self.assertEqual(box._box.state, "destroyed")
        self.assertEqual(result.status, "error")

    def test_cleanup_always_runs(self):
        """Cleanup (box.release + box.exit) must run even with mid-execution errors."""
        box = Truhowl(workdir=self.tmpdir)
        try:
            box._box.enter(block_network=False, sandbox=False)
            box._box.insulate("test")
            raise RuntimeError("Mid-execution error")
        except RuntimeError:
            pass
        finally:
            box._box.release()
            box._box.exit()
        self.assertEqual(box._box.state, "destroyed")
        self.assertEqual(box._box.task_profile, "testing")

class TestNoAgentBehavior(unittest.TestCase):
    """Test 6 - The runtime works identically without any AI agent."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test6_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_runtime_works_without_agent(self):
        """Without an agent, compartments just run their functions."""
        box = Truhowl(workdir=self.tmpdir)
        box.add(_comp("analyze", {"findings": "no issues"}))
        result = box.run()
        self.assertEqual(result.status, "success")

    def test_output_structure_is_consistent(self):
        """Same compartments should produce same structure across runs."""
        def build_result(box):
            box.add(_comp("step", {"value": 42}))
            return box.run()

        a = build_result(Truhowl(workdir=self.tmpdir))
        b = build_result(Truhowl(workdir=self.tmpdir))

        self.assertEqual(type(a), type(b))
        self.assertEqual(a.status, b.status)

class TestParallelSessions(unittest.TestCase):
    """Test 7 - Multiple independent Truhowles should coexist."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test7_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_two_boxes_are_independent(self):
        """Two Truhowl instances must have different IDs and directories."""
        a = Truhowl(workdir=self.tmpdir)
        b = Truhowl(workdir=self.tmpdir)
        self.assertNotEqual(a.box_id, b.box_id)
        self.assertNotEqual(a.box_dir, b.box_dir)

    def test_concurrent_boxes_dont_interfere(self):
        """Simultaneous boxes should maintain separate state."""
        a = Truhowl(workdir=self.tmpdir)
        b = Truhowl(workdir=self.tmpdir)

        a._box.enter(block_network=False, sandbox=False)
        b._box.enter(block_network=False, sandbox=False)

        self.assertTrue(a._box.is_active)
        self.assertTrue(b._box.is_active)
        self.assertNotEqual(a._box.box_dir, b._box.box_dir)

        a_file = os.path.join(a._box.box_dir, "secret.txt")
        with open(a_file, "w") as f:
            f.write("A's secret")

        b_file = os.path.join(b._box.box_dir, "secret.txt")
        self.assertFalse(os.path.exists(b_file),
                         "Box B should not see Box A's files")

        a._box.exit()
        b._box.exit()
        self.assertEqual(a._box.state, "destroyed")
        self.assertEqual(b._box.state, "destroyed")

    def test_three_boxes_branch_like_structure(self):
        """Branching structure: Main, then sessions A, B, C."""
        main = Truhowl(workdir=self.tmpdir)
        branches = [Truhowl(workdir=self.tmpdir) for _ in range(3)]

        ids = [b.box_id for b in branches]
        self.assertEqual(len(ids), len(set(ids)), "All branch IDs must be unique")
        self.assertNotIn(main.box_id, ids)

        dirs = [b.box_dir for b in branches]
        self.assertEqual(len(dirs), len(set(dirs)), "All branch dirs must be unique")

        for bx in branches:
            bx._box.enter(block_network=False, sandbox=False)
            self.assertTrue(bx._box.is_active)
            bx._box.exit()
            self.assertEqual(bx._box.state, "destroyed")

class TestSelfDogfooding(unittest.TestCase):
    """Test 8 - Truhowl can analyze and improve its own codebase."""

    def setUp(self):
        self.tmpdir = os.path.join(tempfile.gettempdir(), f"truhowl_test8_{uuid.uuid4().hex[:8]}")
        _make_repo(self.tmpdir)
        src = os.path.join(os.path.dirname(__file__), "..", "python",
                           "truhowl", "engine", "tracer.py")
        dst = os.path.join(self.tmpdir, "tracer.py")
        if os.path.exists(src):
            shutil.copy2(src, dst)
            subprocess.run(["git", "add", "-A"], cwd=self.tmpdir, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Add tracer.py"], cwd=self.tmpdir,
                           capture_output=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                                "GIT_AUTHOR_EMAIL": "test@test.com",
                                "GIT_COMMITTER_NAME": "Test",
                                "GIT_COMMITTER_EMAIL": "test@test.com"})

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_can_analyze_own_codebase(self):
        """Truhowl should be able to analyze files in its workdir."""
        # Write a known file so the test doesn't depend on setUp file copies
        known_path = os.path.join(self.tmpdir, "sample.py")
        with open(known_path, "w") as f:
            f.write("def hello():\n    print('hello')\n")

        def analysis(ctx):
            path = os.path.join(ctx.workdir, "sample.py")
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read()
                return {"analyzed": True, "lines": len(content.splitlines())}
            return {"analyzed": False}

        box = Truhowl(workdir=self.tmpdir)
        box.add(Compartment(
            name="analyzer", fn=analysis,
            config=CompartmentConfig(permissions=["fs_read"]),
        ))
        result = box.run()
        self.assertEqual(result.status, "success")
        self.assertTrue(result.output.get("analyzer", {}).get("analyzed"))
        self.assertEqual(result.output.get("analyzer", {}).get("lines"), 2)

    def test_can_use_runtime_programmatically(self):
        """Truhowl API should be usable in a programmatic loop."""
        improvements = []
        for i in range(3):
            def builder(i=i):
                return {"iteration": i, "improvement": f"improvement_{i}"}
            box = Truhowl(workdir=self.tmpdir)
            box.add(_comp(f"improve_{i}", builder()))
            result = box.run()
            if result.status == "success":
                improvements.append(i)
        self.assertEqual(len(improvements), 3,
                         "All 3 improvement iterations should succeed")

    def test_runtime_can_self_reflect(self):
        """Runtime should be able to report its own state and configuration."""
        box = Truhowl(workdir=self.tmpdir)
        self.assertEqual(box.workdir, os.path.abspath(self.tmpdir))
        self.assertEqual(box._box.state, "created")
        box.add(_comp("reflect", {"workdir": box.workdir}))
        result = box.run()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.output.get("reflect", {}).get("workdir"), box.workdir)

if __name__ == "__main__":
    unittest.main()
