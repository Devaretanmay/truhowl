import ast
import json
import logging
import os
import shutil
import subprocess

from blake3 import blake3

_logger = logging.getLogger("truhowl.test_runner")


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


# ── Fail-closed candidate syntax gate ─────────────────────────────────────
#
# Verification runs the repository's own test command. That command only
# proves the files it actually loads: a candidate that leaves a changed
# source file unparseable can still exit 0 when no test imports it. Sealing
# such a candidate would be a false verification, so every changed file is
# parsed first and an unparseable change fails closed before the test run.
#
# This is deterministic measurement only — it never decides or authors a
# repair, it only refuses to call unparseable output verified.

# Extensions we can parse deterministically with a trusted, non-bespoke tool.
_SYNTAX_GATE_EXTENSIONS = frozenset({".py", ".pyi", ".json", ".js", ".cjs", ".mjs"})

# Extensions the migration surface cares about but that we deliberately do
# NOT gate locally, with the reason recorded in the evidence output. TypeScript
# needs the repository's own project context (tsc is run by the repo's
# verification command when it exists); gating it per-file would invent
# errors tsc would not report.
_SYNTAX_GATE_SKIPPED = {
    ".ts": "typescript: checked by the repository's own compile/test step",
    ".tsx": "typescript: checked by the repository's own compile/test step",
    ".rs": "rust: checked by cargo",
    ".go": "go: checked by go build/test",
}

_SYNTAX_GATE_DIRS = frozenset({
    ".git", "node_modules", ".next", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".truhowl", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "coverage",
})


def _git_status_changed_files(repo_dir: str) -> list[str]:
    """Repo-relative paths the sandbox worktree differs from its baseline in.

    Untracked files count: a candidate may add a file, and a new file with a
    syntax error is exactly the kind of change that must not be sealed.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--", "."],
            cwd=repo_dir, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:  # rename/copy: judge the destination
            path = path.split(" -> ", 1)[1]
        if path.startswith("./"):
            path = path[2:]
        if path:
            out.append(path)
    return out


def _path_is_gateable(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    if any(p in _SYNTAX_GATE_DIRS for p in parts[:-1]):
        return False
    return os.path.splitext(parts[-1])[1].lower() in _SYNTAX_GATE_EXTENSIONS


def _parse_error_for(path: str, rel: str) -> str:
    """Return a normalized parse error for one file, or '' when it parses."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        return f"unreadable: {exc}"
    if ext in (".py", ".pyi"):
        try:
            ast.parse(text, filename=rel)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else 0
            return f"SyntaxError: {exc.msg} (line {line})"
        except (ValueError, MemoryError, RecursionError) as exc:
            return f"ParseError: {exc}"
        return ""
    if ext == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError) as exc:
            return f"JSONDecodeError: {exc}"
        return ""
    # JavaScript: use the runtime's own parser rather than a bespoke one.
    node = shutil.which("node")
    if not node:
        return ""
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True,
                              text=True, timeout=60)
    except Exception:
        return ""
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " ".join(detail[:4])[:400] or "parse error"
        return f"SyntaxError: {tail}"
    return ""


def check_candidate_syntax(
    repo_dir: str,
    changed_files: list[str] | None = None,
) -> tuple[int, str, str]:
    """Parse every changed source file. Returns (exit_code, command, output).

    ``changed_files`` is the candidate's own footprint (the caller already
    measured it); when omitted the sandbox worktree is interrogated directly.
    exit_code 0 means every changed, parseable file parsed. Anything else is
    a fail-closed refusal, and the output is written so the existing
    structured-failure parser can hand it back to the repair reasoner.
    """
    if changed_files is None:
        changed_files = _git_status_changed_files(repo_dir)
    rel_paths = sorted({str(p).replace("\\", "/").lstrip("./") for p in changed_files if p})
    targets = [r for r in rel_paths if _path_is_gateable(r)]
    if not targets:
        return 0, "", ""

    failures: list[tuple[str, str]] = []
    checked: list[str] = []
    for rel in targets:
        abs_p = os.path.join(repo_dir, rel)
        if not os.path.isfile(abs_p):
            continue
        checked.append(rel)
        err = _parse_error_for(abs_p, rel)
        if err:
            failures.append((rel, err))

    if not failures:
        return 0, "", ""

    lines = [
        f"SYNTAX GATE FAILED (fail-closed): {len(failures)} of {len(checked)} "
        f"changed file(s) do not parse. A repair whose changed files are not "
        "parseable cannot be verified.",
    ]
    for rel, err in failures:
        lines.append(f"--- {rel} ---")
        lines.append(err)
    lines.append(
        "Fix the parse error in the changed files and re-emit the COMPLETE "
        "migration; do not remove or weaken the affected logic."
    )
    output = "\n".join(lines)[:6000]
    return 1, "syntax-gate", output


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
    from truhowl._core import sandbox_apply, sandbox_check_supported

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
