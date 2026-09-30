# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Repository environment & compiler context extraction.

Provides compiler options, package configuration, and import style evidence
to AI planners and reasoners so that code generation conforms to repository-
specific TypeScript, module-mode, and package manager constraints.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("truhowl.env_context")


@dataclass
class CompilerOptions:
    module: str = ""
    target: str = ""
    module_resolution: str = ""
    es_module_interop: bool | None = None
    allow_synthetic_default_imports: bool | None = None
    strict: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "target": self.target,
            "module_resolution": self.module_resolution,
            "es_module_interop": self.es_module_interop,
            "allow_synthetic_default_imports": self.allow_synthetic_default_imports,
            "strict": self.strict,
            "raw": self.raw,
        }


@dataclass
class RepoEnvironmentContext:
    ecosystem: str = ""  # "javascript", "typescript", "python", "rust", "generic"
    package_type: str = ""  # "module", "commonjs", or ""
    package_manager: str = ""  # "npm", "pnpm", "yarn", "bun", "cargo", "pip", "poetry", etc.
    typescript_version: str = ""
    has_typescript: bool = False
    compiler_options: CompilerOptions = field(default_factory=CompilerOptions)
    dominant_import_style: str = ""  # e.g. "ESM (import { X } from 'Y')", "CommonJS (require)"
    target_pkg_import: str = ""  # specific import statement observed for target package
    import_stats: dict[str, int] = field(default_factory=dict)
    guidance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "package_type": self.package_type,
            "package_manager": self.package_manager,
            "typescript_version": self.typescript_version,
            "has_typescript": self.has_typescript,
            "compiler_options": self.compiler_options.to_dict(),
            "dominant_import_style": self.dominant_import_style,
            "target_pkg_import": self.target_pkg_import,
            "import_stats": self.import_stats,
            "guidance": self.guidance,
        }


def strip_json_comments(text: str) -> str:
    """Strip single-line and multi-line comments and trailing commas from JSONC."""
    pattern = re.compile(
        r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*.*?\*/',
        re.MULTILINE | re.DOTALL,
    )

    def replacer(match: re.Match) -> str:
        s = match.group(1)
        if s is not None:
            return s
        return ""

    clean = pattern.sub(replacer, text)
    clean = re.sub(r',\s*([}\]])', r'\1', clean)
    return clean


def parse_jsonc(content: str) -> dict[str, Any]:
    """Parse JSON with comments and trailing commas, failing closed to empty dict."""
    try:
        return json.loads(content)
    except Exception:
        pass
    try:
        cleaned = strip_json_comments(content)
        return json.loads(cleaned)
    except Exception:
        return {}


def extract_tsconfig(repo_dir: str) -> CompilerOptions:
    """Extract compilerOptions from tsconfig.json (and extended configs if any)."""
    ts_path = os.path.join(repo_dir, "tsconfig.json")
    if not os.path.isfile(ts_path):
        return CompilerOptions()

    opts_dict: dict[str, Any] = {}
    try:
        with open(ts_path, "r", encoding="utf-8", errors="replace") as f:
            data = parse_jsonc(f.read())

        # Check if extends another config (e.g. ./tsconfig.base.json)
        extends_rel = data.get("extends")
        if isinstance(extends_rel, str) and extends_rel.startswith("."):
            ext_path = os.path.normpath(os.path.join(repo_dir, extends_rel))
            if not ext_path.endswith(".json"):
                ext_path += ".json"
            if os.path.isfile(ext_path):
                with open(ext_path, "r", encoding="utf-8", errors="replace") as ef:
                    ext_data = parse_jsonc(ef.read())
                    opts_dict.update(ext_data.get("compilerOptions", {}) or {})

        opts_dict.update(data.get("compilerOptions", {}) or {})
    except Exception as e:
        _logger.debug("Failed to read tsconfig.json in %s: %s", repo_dir, e)
        return CompilerOptions()

    def _get_bool(val: Any) -> bool | None:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "true"
        return None

    return CompilerOptions(
        module=str(opts_dict.get("module", "")).lower(),
        target=str(opts_dict.get("target", "")).lower(),
        module_resolution=str(opts_dict.get("moduleResolution", "")).lower(),
        es_module_interop=_get_bool(opts_dict.get("esModuleInterop")),
        allow_synthetic_default_imports=_get_bool(opts_dict.get("allowSyntheticDefaultImports")),
        strict=_get_bool(opts_dict.get("strict")),
        raw={k: v for k, v in opts_dict.items() if isinstance(v, (str, int, bool))},
    )


def extract_package_json(repo_dir: str) -> tuple[str, str, str, bool]:
    """Extract (package_type, package_manager, typescript_version, has_typescript)."""
    pkg_path = os.path.join(repo_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return ("", "", "", False)

    try:
        with open(pkg_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        data = {}

    # 1. Package type: "module" vs "commonjs" (Node defaults to commonjs)
    pkg_type_val = str(data.get("type", "")).strip().lower()
    package_type = "module" if pkg_type_val == "module" else "commonjs"

    # 2. Package manager
    pkg_manager = ""
    pm_field = data.get("packageManager", "")
    if isinstance(pm_field, str) and pm_field.strip():
        pkg_manager = pm_field.split("@")[0].strip().lower()

    if not pkg_manager:
        if os.path.isfile(os.path.join(repo_dir, "pnpm-lock.yaml")):
            pkg_manager = "pnpm"
        elif os.path.isfile(os.path.join(repo_dir, "yarn.lock")):
            pkg_manager = "yarn"
        elif os.path.isfile(os.path.join(repo_dir, "bun.lockb")) or os.path.isfile(os.path.join(repo_dir, "bun.lock")):
            pkg_manager = "bun"
        elif os.path.isfile(os.path.join(repo_dir, "package-lock.json")):
            pkg_manager = "npm"
        else:
            pkg_manager = "npm"

    # 3. TypeScript version
    deps = data.get("dependencies", {}) or {}
    dev_deps = data.get("devDependencies", {}) or {}
    ts_version = dev_deps.get("typescript") or deps.get("typescript") or ""
    has_typescript = bool(ts_version) or os.path.isfile(os.path.join(repo_dir, "tsconfig.json"))

    return (package_type, pkg_manager, str(ts_version), has_typescript)


_RE_REQUIRE = re.compile(r'(?:const|let|var)\s+.*?=\s*require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)')
_RE_IMPORT_NAMED = re.compile(r'import\s+(?:type\s+)?\{([^}]+)\}\s+from\s*[\'"]([^\'"]+)[\'"]')
_RE_IMPORT_DEFAULT = re.compile(r'import\s+(?:type\s+)?([A-Za-z0-9_$]+)\s+from\s*[\'"]([^\'"]+)[\'"]')
_RE_IMPORT_STAR = re.compile(r'import\s+\*\s+as\s+([A-Za-z0-9_$]+)\s+from\s*[\'"]([^\'"]+)[\'"]')
_RE_IMPORT_EQUALS = re.compile(r'import\s+([A-Za-z0-9_$]+)\s*=\s*require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)')
_RE_CJS_EXPORT = re.compile(r'\b(?:module\.exports|exports\.[A-Za-z0-9_$]+)\s*=')
_RE_ESM_EXPORT = re.compile(r'\bexport\s+(?:default|const|let|var|function|class|type|interface|\{)\b')


def _find_js_ts_files(repo_dir: str, relevant_files: list[str] | None = None, limit: int = 20) -> list[str]:
    """Find a candidate sample of JS/TS files to inspect for import conventions."""
    valid_exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    candidates: list[str] = []

    if relevant_files:
        for f in relevant_files:
            abs_p = f if os.path.isabs(f) else os.path.join(repo_dir, f)
            if os.path.isfile(abs_p) and any(abs_p.endswith(ext) for ext in valid_exts):
                candidates.append(abs_p)

    if candidates:
        return candidates[:limit]

    # Otherwise scan repo tree up to limit
    skip_dirs = {"node_modules", ".git", ".next", "dist", "build", "coverage", ".truhowl", ".yarn"}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            if any(file.endswith(ext) for ext in valid_exts):
                candidates.append(os.path.join(root, file))
                if len(candidates) >= limit:
                    return candidates
    return candidates


def analyze_import_styles(
    repo_dir: str,
    relevant_files: list[str] | None = None,
    target_pkg: str = "",
) -> tuple[str, str, dict[str, int]]:
    """Analyze existing import styles across relevant files in the repository.

    Returns (dominant_import_style, target_pkg_import, import_stats).
    """
    files = _find_js_ts_files(repo_dir, relevant_files=relevant_files)
    stats: dict[str, int] = {
        "require": 0,
        "named_import": 0,
        "default_import": 0,
        "star_import": 0,
        "import_equals": 0,
        "esm_export": 0,
        "cjs_export": 0,
    }
    target_pkg_import = ""
    target_norm = target_pkg.strip().lower()
    target_pattern = None
    if target_norm:
        target_pattern = re.compile(
            r'(import\s+(?:type\s+)?[\w\s{},*$=]+from\s*[\'"]' + re.escape(target_norm) + r'(?:/[^\'"]*)?[\'"]'
            r'|import\s+[\w\s]+=\s*require\s*\(\s*[\'"]' + re.escape(target_norm) + r'(?:/[^\'"]*)?[\'"]\s*\)'
            r'|(?:const|let|var)\s+[\w\s{},:]+=\s*require\s*\(\s*[\'"]' + re.escape(target_norm) + r'(?:/[^\'"]*)?[\'"]\s*\))',
            re.MULTILINE,
        )

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(50000)
        except Exception:
            continue

        if target_pattern and not target_pkg_import:
            m = target_pattern.search(content)
            if m:
                cleaned_import = " ".join(m.group(0).split()).rstrip(";")
                cleaned_import = re.sub(r',\s*\}', ' }', cleaned_import)
                target_pkg_import = cleaned_import

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "/*", "*")):
                continue

            if _RE_REQUIRE.search(stripped):
                stats["require"] += 1
            if _RE_IMPORT_NAMED.search(stripped):
                stats["named_import"] += 1
            elif _RE_IMPORT_DEFAULT.search(stripped) and not stripped.startswith("import type "):
                stats["default_import"] += 1
            if _RE_IMPORT_STAR.search(stripped):
                stats["star_import"] += 1
            if _RE_IMPORT_EQUALS.search(stripped):
                stats["import_equals"] += 1
            if _RE_ESM_EXPORT.search(stripped):
                stats["esm_export"] += 1
            if _RE_CJS_EXPORT.search(stripped):
                stats["cjs_export"] += 1

    total_esm = stats["named_import"] + stats["default_import"] + stats["star_import"] + stats["esm_export"]
    total_cjs = stats["require"] + stats["cjs_export"] + stats["import_equals"]

    if total_esm == 0 and total_cjs == 0:
        dominant = "unknown"
    elif total_esm > 0 and total_cjs == 0:
        if stats["named_import"] >= stats["default_import"] * 2:
            dominant = "ESM with named imports (import { X } from '...')"
        else:
            dominant = "ESM (import X from '...')"
    elif total_cjs > 0 and total_esm == 0:
        dominant = "CommonJS (require / module.exports)"
    else:
        if total_esm > total_cjs * 3:
            dominant = "predominantly ESM (import / export)"
        elif total_cjs > total_esm * 3:
            dominant = "predominantly CommonJS (require / module.exports)"
        else:
            dominant = "mixed (both ESM import and CommonJS require present)"

    return (dominant, target_pkg_import, stats)


def extract_repo_environment(
    repo_dir: str,
    relevant_files: list[str] | None = None,
    target_pkg: str = "",
) -> RepoEnvironmentContext:
    """Extract complete repository environment, compiler options, and import conventions."""
    repo_dir = os.path.abspath(repo_dir)

    # 1. Package manifest
    package_type, package_manager, ts_version, has_ts_pkg = extract_package_json(repo_dir)

    # 2. TypeScript compiler options
    compiler_opts = extract_tsconfig(repo_dir)
    has_ts = has_ts_pkg or os.path.isfile(os.path.join(repo_dir, "tsconfig.json"))

    # If no tsconfig or package.json, check for python/rust
    ecosystem = "generic"
    if has_ts:
        ecosystem = "typescript"
    elif os.path.isfile(os.path.join(repo_dir, "package.json")):
        ecosystem = "javascript"
    elif os.path.isfile(os.path.join(repo_dir, "Cargo.toml")):
        ecosystem = "rust"
        package_manager = "cargo"
    elif (
        os.path.isfile(os.path.join(repo_dir, "pyproject.toml"))
        or os.path.isfile(os.path.join(repo_dir, "requirements.txt"))
        or os.path.isfile(os.path.join(repo_dir, "setup.py"))
    ):
        ecosystem = "python"
        if os.path.isfile(os.path.join(repo_dir, "poetry.lock")):
            package_manager = "poetry"
        elif os.path.isfile(os.path.join(repo_dir, "Pipfile.lock")):
            package_manager = "pipenv"
        else:
            package_manager = "pip"

    # 3. Import style analysis
    dominant_import, target_import, stats = analyze_import_styles(
        repo_dir, relevant_files=relevant_files, target_pkg=target_pkg
    )

    # 4. Generate compiler and environment guidance rules
    guidance: list[str] = []

    if has_ts:
        # Crucial invariant check: esModuleInterop & allowSyntheticDefaultImports
        interop = compiler_opts.es_module_interop
        synth = compiler_opts.allow_synthetic_default_imports
        if interop is False or (interop is None and not synth):
            guidance.append(
                "CRITICAL COMPILER RULE: 'esModuleInterop' is false/disabled in tsconfig.json. "
                "Default imports of CommonJS modules (e.g. `import Stripe from 'stripe'`) will fail "
                "TypeScript compilation with TS2351 ('This expression is not constructable') or TS1259. "
                "You MUST use named imports (`import { Stripe } from 'stripe'`), namespace imports "
                "(`import * as Stripe from 'stripe'`), or `import Stripe = require('stripe')`."
            )
        elif interop is True:
            guidance.append(
                "'esModuleInterop' is enabled in tsconfig.json. Default imports (`import X from 'pkg'`) "
                "are permitted for CommonJS packages."
            )

        if compiler_opts.module_resolution in ("nodenext", "node16"):
            guidance.append(
                f"Module resolution is '{compiler_opts.module_resolution}'. Relative module imports must "
                "specify file extensions (e.g. `import { foo } from './foo.js'`)."
            )

        if compiler_opts.strict is True:
            guidance.append(
                "'strict' mode is enabled in tsconfig.json. All types, parameters, and return types must be fully sound."
            )

    if package_type == "module":
        guidance.append(
            "Package format is ECMAScript Module ('type': 'module' in package.json). "
            "Do NOT use CommonJS require() or module.exports in code files."
        )
    elif package_type == "commonjs":
        guidance.append(
            "Package format is CommonJS (default). Ensure imports/exports match the repository's module resolution."
        )

    if target_import:
        guidance.append(
            f"Observed existing import for '{target_pkg}': `{target_import}`. Preserve this import style to avoid compiler errors."
        )
        if "{" in target_import and "}" in target_import:
            guidance.append(
                f"Target package '{target_pkg}' uses NAMED import syntax (`{target_import}`). "
                f"Do NOT convert to default import (`import {target_pkg.capitalize()} from '{target_pkg}'`), "
                "which causes TS2351 non-constructable errors."
            )
        elif "require(" in target_import:
            guidance.append(
                f"Target package '{target_pkg}' uses CommonJS require syntax (`{target_import}`). "
                "Do NOT introduce ESM import statements in CommonJS files."
            )
    elif dominant_import and dominant_import != "unknown":
        guidance.append(f"Observed codebase import style: {dominant_import}.")

    return RepoEnvironmentContext(
        ecosystem=ecosystem,
        package_type=package_type,
        package_manager=package_manager,
        typescript_version=ts_version,
        has_typescript=has_ts,
        compiler_options=compiler_opts,
        dominant_import_style=dominant_import,
        target_pkg_import=target_import,
        import_stats=stats,
        guidance=guidance,
    )


def format_repo_environment(env: RepoEnvironmentContext) -> str:
    """Format RepoEnvironmentContext into clear, compiler-accurate guidance for LLMs."""
    sections: list[str] = []

    # Ecosystem and Package Manager
    eco_desc = env.ecosystem.capitalize()
    if env.has_typescript:
        eco_desc = "TypeScript / Node.js"
    elif env.ecosystem == "javascript":
        eco_desc = "JavaScript / Node.js"
    sections.append(f"- Ecosystem: {eco_desc}")

    if env.package_type:
        pkg_type_label = "ECMAScript Module ('type': 'module')" if env.package_type == "module" else "CommonJS"
        sections.append(f"- Package Type: {pkg_type_label}")

    if env.package_manager:
        sections.append(f"- Package Manager: {env.package_manager}")

    if env.typescript_version:
        sections.append(f"- TypeScript Version: {env.typescript_version}")

    # Compiler options
    opts = env.compiler_options
    opts_lines = []
    if opts.module:
        opts_lines.append(f"  * module: {opts.module}")
    if opts.target:
        opts_lines.append(f"  * target: {opts.target}")
    if opts.module_resolution:
        opts_lines.append(f"  * moduleResolution: {opts.module_resolution}")
    if opts.es_module_interop is not None:
        opts_lines.append(f"  * esModuleInterop: {str(opts.es_module_interop).lower()}")
    if opts.allow_synthetic_default_imports is not None:
        opts_lines.append(f"  * allowSyntheticDefaultImports: {str(opts.allow_synthetic_default_imports).lower()}")
    if opts.strict is not None:
        opts_lines.append(f"  * strict: {str(opts.strict).lower()}")

    if opts_lines:
        sections.append("- Compiler Options (tsconfig.json):\n" + "\n".join(opts_lines))

    # Observed import style
    if env.dominant_import_style and env.dominant_import_style != "unknown":
        sections.append(f"- Observed Codebase Import Style: {env.dominant_import_style}")

    if env.target_pkg_import:
        sections.append(f"- Target Package Import Pattern: `{env.target_pkg_import}`")

    # Compiler rules and guidance
    if env.guidance:
        sections.append("- Compilation Rules & Constraints:\n" + "\n".join(f"  * {g}" for g in env.guidance))

    return "\n".join(sections)


def format_repo_environment_dict(env_dict: dict[str, Any]) -> str:
    """Format an environment dictionary (from to_dict) into prompt text."""
    opts_dict = env_dict.get("compiler_options", {}) or {}
    opts = CompilerOptions(
        module=opts_dict.get("module", ""),
        target=opts_dict.get("target", ""),
        module_resolution=opts_dict.get("module_resolution", ""),
        es_module_interop=opts_dict.get("es_module_interop"),
        allow_synthetic_default_imports=opts_dict.get("allow_synthetic_default_imports"),
        strict=opts_dict.get("strict"),
        raw=opts_dict.get("raw", {}) or {},
    )
    env = RepoEnvironmentContext(
        ecosystem=env_dict.get("ecosystem", ""),
        package_type=env_dict.get("package_type", ""),
        package_manager=env_dict.get("package_manager", ""),
        typescript_version=env_dict.get("typescript_version", ""),
        has_typescript=bool(env_dict.get("has_typescript", False)),
        compiler_options=opts,
        dominant_import_style=env_dict.get("dominant_import_style", ""),
        target_pkg_import=env_dict.get("target_pkg_import", ""),
        import_stats=env_dict.get("import_stats", {}) or {},
        guidance=env_dict.get("guidance", []) or [],
    )
    return format_repo_environment(env)
