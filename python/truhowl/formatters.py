
import os
import shutil
import subprocess



def run_style_formatter(repo_dir: str, modified_files: list[str]) -> None:
    """Run local repository code formatters (Prettier, Biome, Ruff) to match team style."""
    if not modified_files:
        return
    if os.path.exists(os.path.join(repo_dir, ".prettierrc")) or os.path.exists(os.path.join(repo_dir, "package.json")):
        if shutil.which("npx"):
            for f in modified_files:
                rel_f = os.path.relpath(f, repo_dir) if os.path.isabs(f) else f
                subprocess.run(["npx", "prettier", "--write", rel_f], cwd=repo_dir, capture_output=True)
    if os.path.exists(os.path.join(repo_dir, "pyproject.toml")) or os.path.exists(os.path.join(repo_dir, "ruff.toml")):
        if shutil.which("ruff"):
            for f in modified_files:
                if f.endswith(".py"):
                    rel_f = os.path.relpath(f, repo_dir) if os.path.isabs(f) else f
                    subprocess.run(["ruff", "format", rel_f], cwd=repo_dir, capture_output=True)
