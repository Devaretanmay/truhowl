import os
import subprocess
import sys

def main():
    print("=== 0. LINT (ruff, entire tree) ===")
    res_lint = subprocess.run(["ruff", "check", "python", "tests"],
                              capture_output=True, text=True)
    if res_lint.returncode != 0:
        print("Ruff found violations:")
        print(res_lint.stdout or res_lint.stderr)
        sys.exit(1)
    print("PASS: ruff clean across python/ and tests/.")

    print("\n=== 1. RUNNING RUST NATIVE TESTS ===")
    res_rust = subprocess.run(["cargo", "test", "--lib"], capture_output=True, text=True)
    if res_rust.returncode != 0:
        print("Rust tests failed!")
        print(res_rust.stderr or res_rust.stdout)
        sys.exit(1)
    else:
        lines = res_rust.stdout.strip().split("\n")
        summary = [line for line in lines if "test result:" in line]
        print("Rust test suite passed:", summary[-1] if summary else "OK")

    print("\n=== 2. RUNNING PYTHON TEST SUITE ===")
    env = dict(os.environ)
    has_local_core = os.path.exists("python/truhowl") and any(f.startswith("_core") for f in os.listdir("python/truhowl"))
    if has_local_core:
        env["PYTHONPATH"] = os.path.abspath("python")
    res_py = subprocess.run(["pytest", "tests/", "-q"], capture_output=True, text=True, env=env)
    if res_py.returncode != 0:
        print("Pytest failed!")
        print(res_py.stderr or res_py.stdout)
        sys.exit(1)
    else:
        print("Python test suite passed:", res_py.stdout.strip().split("\n")[-1])

    print("\n============================================================")
    print("      ALL CODEBASE HYGIENE CHECKS & TEST SUITES PASSED      ")
    print("============================================================")

if __name__ == "__main__":
    main()
