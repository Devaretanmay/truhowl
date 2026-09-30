# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Repository-level migration planning: one coordinated operation, not N patches.

Why this module exists
----------------------
The repair path used to treat each file as an independent patch request. A
real SDK migration is a single coordinated operation over *migration units*
(import, initialization, method call, request shape, response shape, type,
module system, dependency metadata). Fixing the import but not the constructor,
or the callsite but not the old type, produces a repository that compiles
nowhere — which is exactly how safe refusals were produced.

This module builds a MigrationPlan before any edit:

    authoritative knowledge (truhowl.migration_knowledge)
      + observed usage in this repository (Rust callsite locator + text scan)
      + repository environment (package manager, tsconfig, verification cmds)
        -> MigrationPlan(units, per-file intents, invariants, completeness)

It also owns two deterministic checks that keep a *partial* migration from ever
being called complete:

* check_plan_completeness — before editing: which observed legacy usages does
  the plan not account for? Unaccounted usage means PLAN INCOMPLETE.
* residual_hits — after a candidate passes verification: which observed legacy
  usages are still present in the candidate's own files? Residual usage means
  the migration unit is unfinished, so the candidate is not sealed; the exact
  list is handed back to the model for a full-candidate retry.

Both are measurement. Neither authors, edits, or judges semantics.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from truhowl.migration_knowledge import (
    ALL_UNITS,
    knowledge_for,
    source_authority_rank,
)
from truhowl.providers.registry import SymbolChange

# ── Limits (planning is bounded; this is not a whole-repo database) ───────

MAX_SLICE_FILES = 40
MAX_HITS_PER_TOKEN = 25
MAX_SCAN_BYTES = 400_000

_SCAN_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go")

_MANIFESTS = frozenset({
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod",
})

_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".next", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".truhowl", "coverage", ".mypy_cache",
    ".ruff_cache", ".pytest_cache",
})

# Tokens too generic to assert migration completeness against. Requiring one of
# these to disappear would refuse correct repairs, so they are reported as
# non-detectable and left to compiler/test evidence instead.
_GENERIC_TOKENS = frozenset({
    "data", "error", "user", "session", "client", "config", "configuration",
    "id", "url", "default", "value", "result", "args", "options", "params",
    "request", "response", "completion", "messages", "model", "amount",
    "create", "update", "list", "get", "send", "run", "call", "caller", "token",
    "promise", "value", "success", "context", "error", "options", "payload",
})

_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_IMPORT_BINDING_RE = re.compile(r"^\s*import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\b")
_REQUIRE_DESTRUCTURE_RE = re.compile(r"^\s*(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\b")
_REQUIRE_DEFAULT_RE = re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\b")


# ── Detection specs (what the plan is allowed to require) ─────────────────

@dataclass(frozen=True)
class Detection:
    """A legacy usage the plan can observe in this repository.

    ``mode`` decides how presence is matched so a *fixed* form never trips the
    check:

    * identifier — word-boundary identifier/member path (``OpenAIApi``,
      ``auth.user``). Never matches inside the replacement when the
      replacement's identifier differs.
    * module — exact quoted module specifier, so ``'aws-sdk'`` does not match
      the migrated ``'@aws-sdk/client-s3'``.
    * binding — a local imported name that must stop being used.
    """

    old_symbol: str
    new_symbol: str
    token: str
    mode: str
    unit: str
    change_type: str
    source_url: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_symbol": self.old_symbol, "new_symbol": self.new_symbol,
            "token": self.token, "mode": self.mode, "unit": self.unit,
            "change_type": self.change_type, "source_url": self.source_url,
        }


@dataclass
class NonDetectable:
    """An old symbol we refuse to assert on, with the reason why."""

    old_symbol: str
    unit: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"old_symbol": self.old_symbol, "unit": self.unit, "reason": self.reason}


def _is_specific(token: str) -> bool:
    """Is this token specific enough to require its disappearance?

    Class-like tokens (``Configuration``) and member paths (``subscriptions.del``)
    are specific. Plain lowercase generic words are not, however common they are
    in this catalog: asserting on those would refuse correct repairs.
    """
    if not token or len(token) < 3:
        return False
    class_like = token[0].isupper() or any(c.isupper() for c in token)
    if not class_like and token.lower() in _GENERIC_TOKENS:
        return False
    if "." in token:
        return True
    if class_like:
        return True
    return len(token) >= 6


def detection_from_change(change: SymbolChange) -> Detection | NonDetectable:
    """Derive an observable detection from one knowledge claim, or explain why not.

    Only identifier-shaped, member-path, module-specifier and import-binding
    usages are asserted. Anything else (response envelopes such as ``.data``,
    prose-level request-shape guidance) is reported as non-detectable and left
    to compiler/test evidence.
    """
    old = (change.old_symbol or "").strip()
    if not old:
        return NonDetectable(old_symbol=old, unit=change.unit, reason="empty symbol")
    if change.change_type == "signature":
        return NonDetectable(old_symbol=old, unit=change.unit,
                             reason="signature change: no removable symbol")

    # 1. Import / require shapes -> binding or module detection.
    if old.startswith("import ") or "require(" in old:
        quoted = _QUOTED_RE.search(old)
        module = quoted.group(1) if quoted else ""
        binding = _IMPORT_BINDING_RE.match(old)
        if binding:
            name = binding.group(1)
            if _is_specific(name):
                return Detection(old, change.new_symbol, name, "identifier", change.unit,
                                 change.change_type, change.source_url, change.note)
            return NonDetectable(old, change.unit, f"import binding {name!r} too generic")
        destructured = _REQUIRE_DESTRUCTURE_RE.match(old)
        if destructured:
            names = [n.strip().split(":")[0].strip() for n in destructured.group(1).split(",")]
            for name in names:
                if _is_specific(name):
                    return Detection(old, change.new_symbol, name, "identifier", change.unit,
                                     change.change_type, change.source_url, change.note)
            return NonDetectable(old, change.unit, "destructured require binding too generic")
        default_require = _REQUIRE_DEFAULT_RE.match(old)
        if default_require:
            name = default_require.group(1)
            if _is_specific(name):
                return Detection(old, change.new_symbol, name, "identifier", change.unit,
                                 change.change_type, change.source_url, change.note)
            return NonDetectable(old, change.unit, f"require binding {name!r} too generic")
        # Module specifiers are matched inside quotes exactly, so they need no
        # length heuristic — but a replacement that keeps the same module
        # (``require('stripe').default`` -> ``require('stripe')``) must not be
        # asserted, or the correct repair would be refused.
        if module and len(module) >= 4:
            if re.search(r"['\"]" + re.escape(module) + r"['\"]", change.new_symbol or ""):
                return NonDetectable(old, change.unit,
                                     f"replacement still requires {module!r}: access-shape change")
            return Detection(old, change.new_symbol, module, "module", change.unit,
                             change.change_type, change.source_url, change.note)
        return NonDetectable(old, change.unit, "module specifier not specific enough")

    # 2. Member-path / identifier shapes.
    token = old
    if token.startswith("new "):
        token = token[4:].strip()
    if "(" in token:
        token = token.split("(", 1)[0].strip()
    token = token.strip().strip(".").strip()
    if not token:
        return NonDetectable(old, change.unit, "no identifier in symbol")
    if "." in token:
        token = ".".join(token.split(".")[-2:])
    if not _is_specific(token):
        return NonDetectable(old, change.unit, f"token {token!r} too generic to assert on")
    # Call-shape change: if the replacement still refers to the same identifier
    # (``Stripe('key') -> new Stripe('key')``), the identifier must NOT be
    # required to disappear. Asserting it would refuse the correct repair.
    new = change.new_symbol or ""
    if re.search(r"(?<![\w$])" + re.escape(token) + r"(?![\w$])", new):
        return NonDetectable(old, change.unit,
                             f"replacement still uses {token!r}: call-shape change, not a rename")
    mode = "identifier"
    # A module-style token (scoped package) is matched inside quotes exactly.
    if token.startswith("@") or (change.unit == "module_system" and "." not in token):
        mode = "module"
    return Detection(old, change.new_symbol, token, mode, change.unit,
                     change.change_type, change.source_url, change.note)


def detections_for(knowledge: Any) -> tuple[list[Detection], list[NonDetectable]]:
    """Split a knowledge record into assertable detections and explicit skips."""
    detections: list[Detection] = []
    skipped: list[NonDetectable] = []
    seen: set[tuple[str, str]] = set()
    for change in getattr(knowledge, "symbol_changes", []) or []:
        if change.change_type == "added":
            continue
        derived = detection_from_change(change)
        if isinstance(derived, NonDetectable):
            skipped.append(derived)
            continue
        key = (derived.token, derived.mode)
        if key in seen:
            continue
        seen.add(key)
        detections.append(derived)
    return detections, skipped


# ── Observation: where does this repository still use the legacy shape? ───

@dataclass
class SymbolHit:
    file: str
    line: int
    snippet: str
    token: str
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "snippet": self.snippet,
                "token": self.token, "unit": self.unit}

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.unit}] {self.snippet}"


def _strip_line_comment(line: str) -> str:
    """Best-effort: drop //, #, /*… comment content so comments never assert."""
    for marker in ("//", "#", "/*", "*"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line


def _without_string_literals(line: str) -> str:
    """Blank out string/template literal contents, preserving length.

    A symbol inside a string is data, not a usage: test harnesses that compare
    source text (``src.includes('getCurrentHub')``) must never be mistaken for
    un-migrated callsites. Module specifiers are matched in a separate mode that
    deliberately keeps quotes intact.
    """
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in line:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            out.append(" ")
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def _identifier_pattern(token: str) -> re.Pattern[str]:
    """Identifier or member path, bounded by non-identifier characters.

    A preceding ``.`` is allowed on purpose: legacy member paths are almost
    always reached through a receiver (``supabase.auth.user``,
    ``Sentry.getCurrentHub``), and requiring the token to start a line would
    observe nothing. The trailing boundary still keeps ``ConfigurationStore``
    from matching ``Configuration``.
    """
    return re.compile(r"(?<![\w$])" + re.escape(token) + r"(?![\w$])")


def _module_pattern(module: str) -> re.Pattern[str]:
    """Module specifier inside an actual import/require context.

    The context prefix matters: a harness that inspects
    ``pkg.dependencies['aws-sdk']`` mentions the old package name as data and
    must not be mistaken for un-migrated code, or a correct migration would be
    refused for a file it was right to leave alone.
    """
    return re.compile(
        r"(?:require\s*\(|from\s+|import\s*\(|import\s+)[^'\"\n]*['\"]"
        + re.escape(module) + r"['\"]"
    )


def scan_for_detections(
    repo_dir: str,
    detections: list[Detection],
    files: list[str] | None = None,
) -> tuple[dict[str, list[SymbolHit]], list[str]]:
    """Find observed legacy usage. Returns (hits by token, files actually scanned).

    Only the migration slice is scanned: whole-repository text search would
    both cost too much and assert against unrelated code.
    """
    if not detections:
        return {}, []

    by_mode: list[tuple[Detection, re.Pattern[str]]] = []
    for det in detections:
        pattern = _module_pattern(det.token) if det.mode == "module" else _identifier_pattern(det.token)
        by_mode.append((det, pattern))

    if files is None:
        files = _walk_slice_files(repo_dir)

    hits: dict[str, list[SymbolHit]] = {}
    scanned: list[str] = []
    for rel in files[:MAX_SLICE_FILES]:
        abs_p = os.path.join(repo_dir, rel)
        try:
            if not os.path.isfile(abs_p) or os.path.getsize(abs_p) > MAX_SCAN_BYTES:
                continue
            with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        scanned.append(rel)
        for det, pattern in by_mode:
            bucket = hits.setdefault(det.token, [])
            if len(bucket) >= MAX_HITS_PER_TOKEN:
                continue
            for i, raw_line in enumerate(text.splitlines(), start=1):
                if det.mode == "module":
                    if not pattern.search(raw_line):
                        continue
                else:
                    code = _without_string_literals(_strip_line_comment(raw_line))
                    if not pattern.search(code):
                        continue
                bucket.append(SymbolHit(file=rel, line=i,
                                        snippet=raw_line.strip()[:200],
                                        token=det.token, unit=det.unit))
                if len(bucket) >= MAX_HITS_PER_TOKEN:
                    break
    return hits, scanned


def _walk_slice_files(repo_dir: str, extra: list[str] | None = None) -> list[str]:
    """Bounded candidate file list for the migration slice."""
    out: list[str] = []
    for path in extra or []:
        rel = _rel(repo_dir, path)
        if rel and rel not in out:
            out.append(rel)
    for dirpath, dirnames, filenames in os.walk(repo_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(_SCAN_EXTENSIONS):
                rel = _rel(repo_dir, os.path.join(dirpath, name))
                if rel and rel not in out:
                    out.append(rel)
                    if len(out) >= MAX_SLICE_FILES * 3:
                        return out
    return out


def _rel(repo_dir: str, path: str) -> str:
    """Normalize to a repo-relative path, resolving relative inputs against the repo.

    Relative inputs are joined to the repository root, not the process CWD:
    callers legitimately pass "package.json", and resolving that against the
    CWD silently drops the manifest from the plan.
    """
    if not path:
        return ""
    base = os.path.abspath(repo_dir)
    abs_p = path if os.path.isabs(path) else os.path.join(base, path)
    try:
        rel = os.path.relpath(os.path.abspath(abs_p), base)
    except ValueError:
        return ""
    return "" if rel.startswith("..") else rel.replace(os.sep, "/")


def discover_migration_slice(
    repo_dir: str,
    provider: str,
    seed_files: list[str] | None = None,
    reverse_deps: bool = True,
) -> list[str]:
    """Files this migration plausibly spans, before any edit happens.

    Seeds: the finding's affected files, files the callsite locator proves use
    the SDK, and wrapper files from the dependency graph. Then one bounded
    reverse-dependency pass: files that import a seed file carry the same
    client type across the boundary, which is the multi-file atomicity problem
    (fix the client, miss the consumer) this pass exists to close.
    """
    seeds: list[str] = []
    for path in seed_files or []:
        rel = _rel(repo_dir, path)
        if rel and rel not in seeds:
            seeds.append(rel)

    try:
        from truhowl.autopatch import ScanConfig, scan_callsites
        from truhowl.providers.registry import get_default_registry
        spec = get_default_registry().get(provider)
        names = [provider] + ([spec.package_name] if spec and spec.package_name else [])
        res = scan_callsites(repo_dir, ScanConfig(sdk_names=list(dict.fromkeys(names)))) or {}
        for c in res.get("callsites", []) or []:
            rel = _rel(repo_dir, str(c.get("file_path", "")))
            if rel and rel not in seeds and len(seeds) < MAX_SLICE_FILES:
                seeds.append(rel)
    except Exception:
        pass

    try:
        from truhowl.graph import build_dependency_graph
        graph = build_dependency_graph(repo_dir) or {}
        for w in (graph.get("wrappers", []) or [])[:10]:
            rel = _rel(repo_dir, w.get("wrapper_file") or w.get("file_path") or "")
            if rel and rel not in seeds and len(seeds) < MAX_SLICE_FILES:
                seeds.append(rel)
    except Exception:
        pass

    if reverse_deps and seeds:
        seeds.extend(_importers_of(repo_dir, seeds))
    return list(dict.fromkeys(seeds))[:MAX_SLICE_FILES]


def _importers_of(repo_dir: str, seeds: list[str]) -> list[str]:
    """Files importing a seed module. Bounded, extension-aware, best-effort."""
    stems: list[str] = []
    for seed in seeds:
        base = os.path.basename(seed)
        stem = base.rsplit(".", 1)[0]
        if stem and stem not in ("index",) and len(stem) >= 4:
            stems.append(stem)
    if not stems:
        return []
    pattern = re.compile(r"(?:from|require\s*\()\s*['\"][^'\"]*(?:" +
                         "|".join(re.escape(s) for s in stems) + r")['\"]")
    out: list[str] = []
    for rel in _walk_slice_files(repo_dir):
        if rel in seeds:
            continue
        try:
            with open(os.path.join(repo_dir, rel), "r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_SCAN_BYTES)
        except OSError:
            continue
        if pattern.search(text):
            out.append(rel)
            if len(out) >= MAX_SLICE_FILES:
                break
    return out


# ── The plan ──────────────────────────────────────────────────────────────

@dataclass
class MigrationUnit:
    """One coordinated unit of the migration, with its observed usage."""

    id: str
    symbols_old: list[str] = field(default_factory=list)
    symbols_new: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    observed: list[SymbolHit] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)

    @property
    def has_observed_usage(self) -> bool:
        return bool(self.observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbols_old": self.symbols_old,
            "symbols_new": self.symbols_new,
            "required_changes": self.required_changes,
            "affected_files": self.affected_files,
            "observed_usage": [h.to_dict() for h in self.observed],
            "source_urls": self.source_urls,
        }


@dataclass
class FileIntent:
    file: str
    units: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"units": self.units, "intents": self.intents, "evidence": self.evidence}


@dataclass
class PlanCompleteness:
    """Pre-edit completeness verdict for a plan."""

    complete: bool
    covered: list[str] = field(default_factory=list)
    uncovered: list[NonDetectable] = field(default_factory=list)
    uncovered_observed: list[SymbolHit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "covered_symbols": self.covered,
            "uncovered_claims": [u.to_dict() for u in self.uncovered],
            "uncovered_observed_usage": [h.to_dict() for h in self.uncovered_observed],
            "notes": self.notes,
        }


@dataclass
class MigrationPlan:
    provider: str
    from_version: str
    to_version: str
    summary: str = ""
    guide_url: str = ""
    units: list[MigrationUnit] = field(default_factory=list)
    files: dict[str, FileIntent] = field(default_factory=dict)
    verification: dict[str, str] = field(default_factory=dict)
    invariants: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    completeness: PlanCompleteness | None = None
    slice_files: list[str] = field(default_factory=list)
    observed: dict[str, list[SymbolHit]] = field(default_factory=dict)
    knowledge_available: bool = True

    # ── derived views ──
    @property
    def affected_files(self) -> list[str]:
        """The planned file set: what a correct candidate is allowed to touch."""
        return sorted(self.files.keys())

    def units_for_file(self, rel: str) -> list[str]:
        fi = self.files.get(rel)
        return list(fi.units) if fi else []

    def old_tokens(self) -> list[str]:
        out: list[str] = []
        for unit in self.units:
            for det in unit.detections:
                if det.token not in out:
                    out.append(det.token)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "summary": self.summary,
            "guide_url": self.guide_url,
            "units": [u.to_dict() for u in self.units],
            "files": {k: v.to_dict() for k, v in self.files.items()},
            "verification": self.verification,
            "invariants": self.invariants,
            "sources": self.sources,
            "completeness": self.completeness.to_dict() if self.completeness else None,
            "slice_files": self.slice_files,
        }


# ── Verification command detection ────────────────────────────────────────

_COMPILE_SCRIPTS = ("type-check", "typecheck", "tsc", "build", "compile", "lint:types")
_BEHAVIORAL_SCRIPTS = ("test", "test:unit", "test:ci", "vitest", "jest")


def declaring_manifest(repo_dir: str, provider: str) -> str:
    """The manifest file that declares this provider's dependency, if any."""
    try:
        from truhowl.drift import detect_drift
        for entry in detect_drift(repo_dir, None) or []:
            names = {str(entry.get("provider", "")).lower(), str(entry.get("package_name", "")).lower()}
            if provider.lower() in names or provider.lower() in {n for n in names if n}:
                rel = _rel(repo_dir, str(entry.get("manifest_path", "")))
                if rel and os.path.basename(rel) in _MANIFESTS:
                    return rel
    except Exception:
        pass
    pkg = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as f:
                data = json.load(f) or {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                if provider in (data.get(section) or {}):
                    return "package.json"
        except Exception:
            pass
    return ""


def detect_verification_commands(repo_dir: str) -> dict[str, str]:
    """Compile-tier and behavioral-tier commands the repository itself declares."""
    out: dict[str, str] = {"compile": "", "behavioral": ""}
    pkg_path = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                scripts = (json.load(f) or {}).get("scripts", {}) or {}
            for name in _COMPILE_SCRIPTS:
                if name in scripts:
                    out["compile"] = f"npm run {name}"
                    break
            for name in _BEHAVIORAL_SCRIPTS:
                if name in scripts:
                    out["behavioral"] = "npm test" if name == "test" else f"npm run {name}"
                    break
        except Exception:
            pass
    if not out["behavioral"]:
        try:
            from truhowl.test_runner import _detect_test_command
            out["behavioral"] = _detect_test_command(repo_dir) or ""
        except Exception:
            pass
    if not out["compile"] and os.path.isfile(os.path.join(repo_dir, "tsconfig.json")):
        out["compile"] = "tsc --noEmit"
    return out


# ── Plan construction ────────────────────────────────────────────────────

def build_migration_plan(
    repo_dir: str,
    provider: str,
    from_version: str,
    to_version: str,
    summary: str = "",
    guide_url: str = "",
    affected_files: list[str] | None = None,
    must_not_change: list[str] | None = None,
) -> MigrationPlan:
    """Build the repository-level plan for one provider version pair.

    Deterministic evidence only: authoritative knowledge, observed usage in the
    migration slice, and the repository's own verification commands. The plan
    describes *what must change*; it never writes an edit.
    """
    repo_dir = os.path.abspath(repo_dir)
    knowledge = knowledge_for(provider, from_version, to_version)
    detections, non_detectable = detections_for(knowledge) if knowledge else ([], [])

    slice_files = discover_migration_slice(repo_dir, provider, seed_files=affected_files)
    observed, scanned = scan_for_detections(repo_dir, detections, files=slice_files)

    units: dict[str, MigrationUnit] = {}
    for det in detections:
        unit = units.setdefault(det.unit or "method_call", MigrationUnit(id=det.unit or "method_call"))
        if det.old_symbol not in unit.symbols_old:
            unit.symbols_old.append(det.old_symbol)
        if det.new_symbol and det.new_symbol not in unit.symbols_new:
            unit.symbols_new.append(det.new_symbol)
        if det.source_url and det.source_url not in unit.source_urls:
            unit.source_urls.append(det.source_url)
        unit.detections.append(det)
        change = _change_for(knowledge, det.old_symbol)
        label = _intent_label(det, change)
        if label and label not in unit.required_changes:
            unit.required_changes.append(label)

    for token, hits in observed.items():
        for hit in hits:
            unit = units.setdefault(hit.unit or "method_call", MigrationUnit(id=hit.unit or "method_call"))
            unit.observed.append(hit)
            if hit.file not in unit.affected_files:
                unit.affected_files.append(hit.file)

    files: dict[str, FileIntent] = {}
    for unit in units.values():
        for hit in unit.observed:
            fi = files.setdefault(hit.file, FileIntent(file=hit.file))
            if unit.id not in fi.units:
                fi.units.append(unit.id)
            intent = _intent_label_for_unit(unit)
            if intent and intent not in fi.intents:
                fi.intents.append(intent)
            fi.evidence.append(str(hit))

    # Dependency metadata is its own unit: leaving the manifest on the old range
    # is how a fully migrated call site reverts on the next install. The manifest
    # that declares the dependency is planned even when the finding never listed
    # it — the unit graph requires it, and many repositories only touch source.
    manifest_files = [
        m for m in (_rel(repo_dir, rel) for rel in (affected_files or []))
        if m and os.path.basename(m) in _MANIFESTS
    ]
    declaring = declaring_manifest(repo_dir, provider)
    if declaring and declaring not in manifest_files:
        manifest_files.append(declaring)
    if manifest_files:
        package_name = provider
        try:
            from truhowl.providers.registry import get_default_registry
            spec = get_default_registry().get(provider)
            if spec and spec.package_name:
                package_name = spec.package_name
        except Exception:
            pass
        unit = units.get("dependency_metadata") or MigrationUnit(id="dependency_metadata")
        units["dependency_metadata"] = unit
        bump = f"update {package_name} to the migrated version in the manifest"
        if bump not in unit.required_changes:
            unit.required_changes.append(bump)
        if package_name not in unit.symbols_old:
            unit.symbols_old.append(package_name)
        if f"{package_name}@{from_version}" not in unit.symbols_new:
            unit.symbols_new.append(f"{package_name}@{to_version}")
        for manifest in manifest_files:
            unit.affected_files.append(manifest)
            fi = files.setdefault(manifest, FileIntent(file=manifest))
            if "dependency_metadata" not in fi.units:
                fi.units.append("dependency_metadata")
            if bump not in fi.intents:
                fi.intents.append(bump)

    sources = []
    for src in getattr(knowledge, "sources", []) or []:
        sources.append({
            "kind": src.kind, "url": src.url, "title": src.title, "note": src.note,
            "authority_rank": source_authority_rank(src.kind),
        })
    sources.sort(key=lambda s: s["authority_rank"])

    invariants = [
        "preserve existing behavior and public function signatures unless the migration requires the change",
        "preserve request semantics: do not convert a prompt-completion call into a chat call",
        "do not modify files outside the planned file set",
        "do not weaken, delete, or skip tests to make verification pass",
        "do not add @ts-ignore, eslint-disable, or `any` casts to silence migration errors",
    ]
    if must_not_change:
        invariants.append("must not change: " + ", ".join(sorted(set(must_not_change))[:20]))
    for note in getattr(knowledge, "behavior_notes", []) or []:
        invariants.append(note)

    fallback_summary = ""
    if knowledge:
        notes = getattr(knowledge, "behavior_notes", []) or []
        fallback_summary = notes[0] if notes else ""
    plan = MigrationPlan(
        provider=provider,
        from_version=from_version,
        to_version=to_version,
        summary=summary or fallback_summary,
        guide_url=guide_url,
        units=[units[u] for u in ALL_UNITS if u in units] + [units[u] for u in units if u not in ALL_UNITS],
        files=files,
        verification=detect_verification_commands(repo_dir),
        invariants=invariants,
        sources=sources,
        slice_files=scanned or slice_files,
        observed=observed,
        knowledge_available=knowledge is not None,
    )
    plan.completeness = check_plan_completeness(plan, non_detectable)
    return plan


def _change_for(knowledge: Any, old_symbol: str) -> SymbolChange | None:
    for change in getattr(knowledge, "symbol_changes", []) or []:
        if change.old_symbol == old_symbol:
            return change
    return None


def _intent_label(det: Detection, change: SymbolChange | None) -> str:
    if change is None:
        return f"migrate {det.old_symbol}"
    if det.change_type == "removed" and not det.new_symbol:
        return f"remove usage of {change.old_symbol}"
    if det.new_symbol:
        return f"{change.change_type}: {change.old_symbol} -> {det.new_symbol}"
    return f"{change.change_type}: {change.old_symbol}"


def _intent_label_for_unit(unit: MigrationUnit) -> str:
    if unit.required_changes:
        return "; ".join(unit.required_changes[:4])
    return f"migrate {unit.id} usage"


def unique_observed(hits: list[SymbolHit]) -> list[SymbolHit]:
    """One entry per file:line, so two symbols on one line are shown once."""
    seen: set[tuple[str, int]] = set()
    out: list[SymbolHit] = []
    for hit in hits:
        key = (hit.file, hit.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def check_plan_completeness(plan: MigrationPlan, non_detectable: list[NonDetectable] | None = None) -> PlanCompleteness:
    """Does the plan account for every legacy usage we actually observed?

    A coverage gap does not silently drop the usage: the plan is marked
    incomplete and the gap is named, so the reasoner is told to resolve it from
    compiler evidence rather than the migration being declared finished.
    """
    covered = sorted(plan.old_tokens())
    uncovered = list(non_detectable or [])
    uncovered_observed: list[SymbolHit] = []

    known_old_symbols = {u for unit in plan.units for u in unit.symbols_old}
    for token, hits in plan.observed.items():
        if token in covered or not hits:
            continue
        if token in known_old_symbols:
            continue
        uncovered.append(NonDetectable(old_symbol=token, unit=hits[0].unit,
                                       reason="observed legacy usage with no authoritative mapping"))
        uncovered_observed.extend(hits[:5])

    notes: list[str] = []
    if not plan.knowledge_available:
        notes.append(
            "no authoritative symbol-level knowledge on record for this version pair; "
            "the migration must be reasoned from the repository's compiler and test evidence"
        )
    for unit in plan.units:
        if unit.id == "dependency_metadata":
            continue
        if not unit.has_observed_usage:
            notes.append(
                f"unit {unit.id}: no legacy usage observed in the migration slice "
                f"(declared because the vendor change requires it)"
            )
    complete = not uncovered_observed
    return PlanCompleteness(complete=complete, covered=covered, uncovered=uncovered,
                            uncovered_observed=uncovered_observed, notes=notes)


# ── Briefing (what the model is told before editing) ──────────────────────

def render_plan_briefing(plan: MigrationPlan) -> str:
    """Render the plan as a coordinated-operation briefing.

    Deliberately not an edit recipe: it names the migration units, the observed
    usage that proves them, the authoritative mapping, and what must not change.
    """
    lines: list[str] = [
        f"MIGRATION PLAN: {plan.provider} {plan.from_version} -> {plan.to_version}",
        "This is ONE coordinated migration. Every migration unit below must land in the "
        "same candidate; a candidate that fixes some units and not others is not a repair.",
    ]
    if plan.summary:
        lines.append(f"Change: {plan.summary}")
    if plan.guide_url:
        lines.append(f"Vendor guide: {plan.guide_url}")
    if plan.verification:
        compile_cmd = plan.verification.get("compile") or "(none declared)"
        behavior_cmd = plan.verification.get("behavioral") or "(none detected)"
        lines.append(f"Verification: compile tier `{compile_cmd}`, behavioral tier `{behavior_cmd}`")

    lines.append("")
    lines.append("Migration units:")
    for unit in plan.units:
        marks = "OBSERVED" if unit.has_observed_usage else "declared, not observed"
        lines.append(f"- [{unit.id}] ({marks})")
        for old, new in zip(unit.symbols_old, unit.symbols_new or [""] * len(unit.symbols_old)):
            if new:
                lines.append(f"    {old}  ->  {new}")
            else:
                lines.append(f"    {old}  ->  (removed)")
        for change in unit.required_changes:
            lines.append(f"    required: {change}")
        for hit in unique_observed(unit.observed)[:6]:
            lines.append(f"    observed: {hit}")
        if unit.source_urls:
            lines.append(f"    source: {unit.source_urls[0]}")

    if plan.files:
        lines.append("")
        lines.append("Planned file set (files this migration may touch):")
        for rel, fi in sorted(plan.files.items()):
            lines.append(f"- {rel}: units={fi.units or ['(unknown)']}")

    if plan.completeness and not plan.completeness.complete:
        lines.append("")
        lines.append("PLAN INCOMPLETE — observed legacy usage with no authoritative mapping:")
        for item in plan.completeness.uncovered_observed[:10]:
            lines.append(f"- {item} (reason from compiler evidence; do not leave it migrated partially)")
    if plan.completeness and plan.completeness.uncovered:
        lines.append("")
        lines.append("Claims intentionally not asserted (too generic or signature-level):")
        for item in plan.completeness.uncovered[:10]:
            lines.append(f"- {item.old_symbol} ({item.unit}): {item.reason}")

    if plan.invariants:
        lines.append("")
        lines.append("Invariants:")
        lines.extend(f"- {inv}" for inv in plan.invariants[:20])
    return "\n".join(lines)


# ── Residual-drift check (post-candidate, pre-seal) ───────────────────────

@dataclass
class ResidualHit:
    file: str
    line: int
    snippet: str
    old_symbol: str
    new_symbol: str
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "snippet": self.snippet,
                "old_symbol": self.old_symbol, "new_symbol": self.new_symbol, "unit": self.unit}

    def __str__(self) -> str:
        target = f" -> {self.new_symbol}" if self.new_symbol else " (removed)"
        return f"{self.file}:{self.line} [{self.unit}] {self.old_symbol}{target}: {self.snippet}"


def residual_hits(
    sandbox_dir: str,
    plan: MigrationPlan,
    changed_files: list[str] | None = None,
) -> list[ResidualHit]:
    """Legacy usage the candidate's OWN files still contain.

    Scoped to symbols we observed in the repository before the repair and told
    the model about, and only matched against files the candidate changed, so
    untouched legacy usage elsewhere (or a symbol we never saw) can never turn
    a correct repair into a refusal.
    """
    if not plan or not plan.observed:
        return []
    files = [f for f in (changed_files if changed_files is not None else plan.affected_files) if f]
    if not files:
        return []

    detections: list[Detection] = []
    for unit in plan.units:
        for det in unit.detections:
            if det.token in plan.observed:
                detections.append(det)
    if not detections:
        return []

    hits, _scanned = scan_for_detections(sandbox_dir, detections, files=files)
    out: list[ResidualHit] = []
    for det in detections:
        for hit in hits.get(det.token, []):
            # A hit on the replacement itself is not residue.
            if det.new_symbol and det.new_symbol.strip() == hit.snippet.strip():
                continue
            out.append(ResidualHit(file=hit.file, line=hit.line, snippet=hit.snippet,
                                   old_symbol=det.old_symbol, new_symbol=det.new_symbol,
                                   unit=det.unit))
    # One report per line: overlapping detections for the same usage (for
    # example ``getCurrentHub`` and ``Sentry.getCurrentHub``) are one finding.
    best: dict[tuple[str, int], ResidualHit] = {}
    unique: list[ResidualHit] = []
    for hit in out:
        key = (hit.file, hit.line)
        existing = best.get(key)
        if existing is None or len(hit.old_symbol) > len(existing.old_symbol):
            best[key] = hit
    for hit in best.values():
        unique.append(hit)
    unique.sort(key=lambda h: (h.file, h.line))
    return unique


def render_residual_briefing(hits: list[ResidualHit], plan: MigrationPlan) -> str:
    """Hand a partial migration back with the exact remaining units named."""
    lines = [
        "MIGRATION INCOMPLETE (fail-closed): the candidate still contains legacy usage "
        "that this migration must remove. Verification only proves what the repository's "
        "own commands load, so these were not caught by the test run.",
        "",
        "Still present in the candidate's changed files:",
    ]
    for hit in hits[:20]:
        lines.append(f"- {hit}")
    lines.append("")
    lines.append("The next attempt starts from the ORIGINAL migration baseline. Return a COMPLETE "
                 "replacement candidate covering every migration unit:")
    for unit in plan.units:
        if unit.id == "dependency_metadata":
            continue
        lines.append(f"- [{unit.id}] {', '.join(unit.required_changes[:3]) or 'migrate usage'}")
    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────

def persist_plan(plan: MigrationPlan, repo_dir: str, finding_id: str) -> str:
    """Write the plan into the finding's audit directory for review."""
    path = os.path.join(os.path.abspath(repo_dir), ".truhowl", "hunt", finding_id, "migration_plan.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict(), f, indent=2)
            f.write("\n")
    except OSError:
        return ""
    return path


def plan_file_set(plan: MigrationPlan | None) -> list[str]:
    """Safe accessor for the planned file set."""
    return list(plan.affected_files) if plan else []


__all__ = [
    "Detection",
    "NonDetectable",
    "SymbolHit",
    "MigrationUnit",
    "FileIntent",
    "PlanCompleteness",
    "MigrationPlan",
    "ResidualHit",
    "build_migration_plan",
    "check_plan_completeness",
    "detections_for",
    "detection_from_change",
    "discover_migration_slice",
    "detect_verification_commands",
    "scan_for_detections",
    "residual_hits",
    "render_plan_briefing",
    "render_residual_briefing",
    "declaring_manifest",
    "unique_observed",
    "persist_plan",
    "plan_file_set",
    "MAX_SLICE_FILES",
]
