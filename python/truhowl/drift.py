# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import re
from typing import Any
from truhowl.autopatch import ScanConfig, scan_callsites
from truhowl.change_source import (
    Detection, ChangeSource, NO_IMPACT, IMPACT_AI, IMPACT_QUARANTINE,
)
from truhowl.credentials import has_valid_credentials
from truhowl.intelligence import resolve_migration
from truhowl.knowledge import direct_rewrites_for
from truhowl.providers.registry import get_default_registry

_logger = logging.getLogger("truhowl.drift")


def detect_drift(repo_dir: str, provider_name: str | None = None) -> list[dict[str, Any]]:
    registry = get_default_registry()
    detected = []

    def _add(dep_name: str, version: str, manifest: str):
        p_spec = registry.get(dep_name)
        if p_spec and (provider_name is None or p_spec.name.lower() == provider_name.lower()):
            target = None
            try:
                if p_spec.migrations:
                    target = next(iter(p_spec.migrations.values())).to_version
            except Exception:
                target = None
            detected.append({
                "provider": p_spec.name,
                "display_name": p_spec.display_name,
                "package_name": dep_name,
                "declared_version": version,
                "target_version": target,
                "manifest_path": manifest,
            })

    pkg_json_path = os.path.join(repo_dir, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for dep_name, version in deps.items():
                _add(dep_name, version, "package.json")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "package.json", e, exc_info=True)

    req_path = os.path.join(repo_dir, "requirements.txt")
    if os.path.exists(req_path):
        try:
            with open(req_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip().split("#")[0].strip()
                    if not line or line.startswith("-"):
                        continue
                    name = line.split("==")[0].split(">=")[0].split("~=")[0].split("<")[0].strip().lower()
                    ver = line.split("==")[-1].strip() if "==" in line else "unknown"
                    _add(name, ver, "requirements.txt")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "requirements.txt", e, exc_info=True)

    pyproj = os.path.join(repo_dir, "pyproject.toml")
    if os.path.exists(pyproj):
        try:
            with open(pyproj, "r", encoding="utf-8") as f:
                txt = f.read()
            for m in re.finditer(r'["\']([a-zA-Z0-9_\-]+)["\']\s*=\s*["\']([^"\']+)["\']', txt):
                _add(m.group(1).lower(), m.group(2), "pyproject.toml")
            for m in re.finditer(r'"([a-zA-Z0-9_\-\[\]]+)(?:[><=~!]+[^"]*)?"', txt):
                name = m.group(1).split("[")[0].lower()
                _add(name, "declared", "pyproject.toml")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "pyproject.toml", e, exc_info=True)

    cargo = os.path.join(repo_dir, "Cargo.toml")
    if os.path.exists(cargo):
        try:
            with open(cargo, "r", encoding="utf-8") as f:
                txt = f.read()
            for m in re.finditer(r'^([a-zA-Z0-9_\-]+)\s*=\s*["\']([^"\']+)["\']', txt, re.MULTILINE):
                _add(m.group(1), m.group(2), "Cargo.toml")
            for m in re.finditer(r'^([a-zA-Z0-9_\-]+)\s*=\s*\{[^}]*version\s*=\s*["\']([^"\']+)["\']', txt, re.MULTILINE):
                _add(m.group(1), m.group(2), "Cargo.toml")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "Cargo.toml", e, exc_info=True)

    for lockfile in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        lp = os.path.join(repo_dir, lockfile)
        if os.path.exists(lp):
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read(1_000_000)
                for m in re.finditer(r'"?(@?[a-zA-Z0-9_\-/]+)"?\s*[:@]', txt):
                    name = m.group(1).strip('"').lower()
                    if registry.get(name):
                        _add(name, "locked", lockfile)
                        break
            except Exception as e:
                _logger.debug("Failed to parse %s: %s", lockfile, e, exc_info=True)

    gomod = os.path.join(repo_dir, "go.mod")
    if os.path.exists(gomod):
        try:
            with open(gomod, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(("module ", "go ", "require (", ")")):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        _add(parts[0].lower(), parts[1], "go.mod")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "go.mod", e, exc_info=True)

    gemfile = os.path.join(repo_dir, "Gemfile")
    if os.path.exists(gemfile):
        try:
            with open(gemfile, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            for m in re.finditer(r"gem\s+['\"]([^'\"]+)['\"]\s*(,\s*['\"]([^'\"]+)['\"])?", txt):
                _add(m.group(1).lower(), m.group(3) or "unknown", "Gemfile")
        except Exception as e:
            _logger.debug("Failed to parse %s: %s", "Gemfile", e, exc_info=True)

    seen = set()
    uniq = []
    for d in detected:
        k = (d["provider"], d["package_name"], d["manifest_path"])
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def detect_changes(repo_dir: str, provider_name: str | None = None) -> list[Detection]:
    """Read-only detection: understand change + locate impact, never patch.

    Returns one Detection per detected dependency. Repair strategy is decided
    downstream by TruhowlIntelligence; this function only classifies.
    """
    out: list[Detection] = []
    for d in detect_drift(repo_dir, provider_name):
        source = ChangeSource.sdk(
            d["provider"],
            version_from=d.get("declared_version") or "unknown",
            version_to=d.get("target_version") or "unknown",
            origin="manifest",
            package_name=d.get("package_name", ""),
            manifest_path=d.get("manifest_path", ""),
        )
        try:
            sdk_names = [d["provider"]]
            if d.get("package_name") and d["package_name"] not in sdk_names:
                sdk_names.append(d["package_name"])
            res = scan_callsites(repo_dir, ScanConfig(sdk_names=sdk_names))
            callsites = res.get("callsites", [])
        except Exception:
            callsites = []
        affected = sorted({c.get("file_path", "") for c in callsites if c.get("file_path")})
        if not callsites:
            out.append(Detection(source=source, outcome=NO_IMPACT,
                                 reason="dependency declared but no callsites reference it",
                                 affected_files=[], callsite_count=0,
                                 ai_dependent=False, confidence=0.9))
            continue
        _from, _to, migration = resolve_migration(
            d["provider"], d.get("declared_version"), d.get("target_version"))
        if migration is not None:
            desc = migration.description
            if d["provider"].lower() == "openai":
                contents = " ".join(str(c.get("line_content", "") or "") for c in callsites)
                has_completion = "createCompletion" in contents
                has_chat = "createChatCompletion" in contents
                if has_completion and not has_chat:
                    desc = "OpenAI Node SDK v3 to v4 rewrite: createCompletion -> completions.create (preserve prompt semantics)."
                elif has_chat and not has_completion:
                    desc = "OpenAI Node SDK v3 to v4 rewrite: createChatCompletion -> chat.completions.create."
            source.metadata["breaking_change"] = desc
            source.metadata["migration_guide_url"] = migration.changelog_url
        has_pat = bool(migration and migration.rewrites) or len(direct_rewrites_for(repo_dir, d["provider"], _from, _to)) > 0
        if has_valid_credentials():
            outcome, ai_dep = IMPACT_AI, True
            conf = 0.95 if has_pat else 0.75
            reason = "AI: contract_evidence_ai_reasoning" if has_pat else "AI: novel_drift_ai_reasoning"
        else:
            outcome, ai_dep, conf = IMPACT_QUARANTINE, False, 0.0
            reason = "QUARANTINE: no_credentials_for_ai"
        out.append(Detection(source=source, outcome=outcome,
                             reason=reason,
                             affected_files=affected, callsite_count=len(callsites),
                             ai_dependent=ai_dep, confidence=conf))
    return out
