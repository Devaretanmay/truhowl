import json
import logging
import os
import shutil
import subprocess

from blake3 import blake3

_logger = logging.getLogger("koyote.test_runner")


def _detect_test_command(repo_dir: str) -> str:
    if os.path.exists(os.path.join(repo_dir, "test", "run.js")):
        return "node test/run.js"
    pkg_json_path = os.path.join(repo_dir, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path) as f:
                data = json.load(f)
            scripts = data.get("scripts", {})
            for candidate in ("test", "test:unit", "test:ci", "type-check", "build"):
                if candidate in scripts:
                    return f"npm run {candidate}" if candidate != "test" else "npm test"
        except Exception as e:
            _logger.warning("Failed to parse %s: %s", pkg_json_path, e)
    if os.path.exists(os.path.join(repo_dir, "pytest.ini")) or os.path.exists(os.path.join(repo_dir, "tests")):
        return "pytest -q"
    if os.path.exists(os.path.join(repo_dir, "Cargo.toml")):
        return "cargo test"
    return ""


def _compute_lockfile_hash(repo_dir: str) -> str:
    candidates = (
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lockb",
        "Cargo.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package.json",
        "Cargo.toml",
    )
    for c in candidates:
        fp = os.path.join(repo_dir, c)
        if os.path.isfile(fp):
            try:
                with open(fp, "rb") as f:
                    return blake3(f.read()).hexdigest()
            except Exception as e:
                _logger.warning("Failed to hash lockfile %s: %s", fp, e)
    return blake3(repo_dir.encode("utf-8")).hexdigest()


def _run_install(repo_dir: str, timeout: int = 120) -> subprocess.CompletedProcess:
    # multi-ecosystem: prefer the manifest actually present
    if os.path.exists(os.path.join(repo_dir, "Cargo.toml")) and shutil.which("cargo"):
        return subprocess.run(["cargo", "fetch"], cwd=repo_dir, capture_output=True, text=True, timeout=timeout)
    if (os.path.exists(os.path.join(repo_dir, "requirements.txt")) or os.path.exists(os.path.join(repo_dir, "pyproject.toml"))) and shutil.which("pip"):
        req = os.path.join(repo_dir, "requirements.txt")
        cmd = ["pip", "install", "-r", req] if os.path.exists(req) else ["pip", "install", "-e", "."]
        try:
            return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            _logger.warning("Failed to run pip install: %s", e)
    if shutil.which("pnpm") and os.path.exists(os.path.join(repo_dir, "pnpm-lock.yaml")):
        cmd = ["pnpm", "install", "--frozen-lockfile=false"]
    elif shutil.which("yarn") and os.path.exists(os.path.join(repo_dir, "yarn.lock")):
        cmd = ["yarn", "install"]
    elif shutil.which("npm") and os.path.exists(os.path.join(repo_dir, "package.json")):
        cmd = ["npm", "install"]
    else:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="no install needed", stderr="")
    return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout)


def run_sandboxed_command(
    cmd: str | list[str],
    cwd: str,
    timeout: int = 120,
    block_network: bool = False,
    enforce: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Execute a child process with kernel sandbox confinement.

    The sandbox restriction is applied via preexec_fn in the child process
    after fork and before exec, ensuring the calling orchestrator remains outside
    the irreversible sandbox.
    """
    from koyote._core import sandbox_apply, sandbox_check_supported

    supported_info = sandbox_check_supported()
    is_supported = str(supported_info.get("supported", "false")).lower() == "true"

    if enforce and not is_supported:
        raise RuntimeError(
            f"Enforced sandboxing requested, but platform '{supported_info.get('platform')}' "
            f"does not support kernel sandboxing ({supported_info.get('details')}). Fail-closed."
        )

    preexec = None
    if is_supported:
        abs_cwd = os.path.abspath(cwd)

        def _preexec():
            applied = sandbox_apply(abs_cwd, block_network)
            if not applied and enforce:
                raise RuntimeError("Failed to apply kernel sandbox in child process")

        preexec = _preexec

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    use_shell = isinstance(cmd, str)
    return subprocess.run(
        cmd,
        shell=use_shell,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        preexec_fn=preexec,
        env=env,
    )


def _run_tests(
    repo_dir: str,
    test_cmd: str,
    timeout: int = 120,
    sandbox: bool = False,
    block_network: bool = False,
    enforce_sandbox: bool = False,
) -> subprocess.CompletedProcess:
    if sandbox:
        return run_sandboxed_command(
            test_cmd,
            cwd=repo_dir,
            timeout=timeout,
            block_network=block_network,
            enforce=enforce_sandbox,
        )
    return subprocess.run(
        test_cmd,
        shell=True,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
