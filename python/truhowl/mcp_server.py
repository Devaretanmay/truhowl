# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

"""
Truhowl MCP (Model Context Protocol) Server.

Exposes Truhowl's autonomous software maintenance engine to AI assistants
(Cursor, Claude Code, Windsurf, Copilot) using the standard Model Context Protocol.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from truhowl.audit import render_audit_cli
from truhowl.graph import audit_dependency_graph, build_dependency_graph
from truhowl.maintenance import detect_drift, run_maintenance_cycle
from truhowl.providers.registry import get_default_registry


TOOLS: list[dict[str, Any]] = [
    {
        "name": "truhowl_audit",
        "description": "Scan a codebase for external API/SDK dependencies, deprecation deadlines, and contract drift.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local repository root (default: current directory).",
                    "default": ".",
                }
            },
        },
    },
    {
        "name": "truhowl_analyze_dependency",
        "description": "Locate exact AST callsites across a codebase affected by an external API or SDK breaking change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local repository root.",
                    "default": ".",
                },
                "provider": {
                    "type": "string",
                    "description": "Target API provider (e.g. stripe, openai, clerk, aws-sdk, sentry).",
                },
            },
            "required": ["provider"],
        },
    },
    {
        "name": "truhowl_repair_and_verify",
        "description": "AI-guided repair of breaking contract changes: reason over repo context, apply surgical edits, run native tests in sandbox, generate BLAKE3 receipts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to local repository root.",
                    "default": ".",
                },
                "provider": {
                    "type": "string",
                    "description": "Target API provider.",
                },
                "from_version": {
                    "type": "string",
                    "description": "Current dependency version.",
                },
                "to_version": {
                    "type": "string",
                    "description": "Target dependency version.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview repair without writing files.",
                    "default": False,
                },
            },
            "required": ["provider"],
        },
    },
    {
        "name": "truhowl_explain_drift",
        "description": "Explain why a callsite is breaking based on upstream API schemas, contracts, and vendor deprecation notices.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Provider identifier.",
                },
            },
            "required": ["provider"],
        },
    },
]


def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_path = os.path.abspath(arguments.get("repo_path", "."))

    if name == "truhowl_audit":
        summary = audit_dependency_graph(repo_path)
        cli_text = render_audit_cli(summary)
        return {
            "content": [
                {"type": "text", "text": cli_text},
            ],
            "raw_data": summary,
        }

    elif name == "truhowl_analyze_dependency":
        # kind/identity accepted for future contract types; provider remains the wired path.
        provider = (arguments.get("provider") or arguments.get("identity") or "").lower()
        detected = detect_drift(repo_path, provider if provider else None)
        graph = build_dependency_graph(repo_path)
        matching_nodes = [
            n for n in graph.get("nodes", [])
            if any(call.get("provider", "").lower() == provider for call in n.get("callsites", []))
        ]
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Found {len(matching_nodes)} files with callsites touching {provider}.",
                }
            ],
            "detected_drift": detected,
            "affected_files": [n.get("file_path") for n in matching_nodes],
        }

    elif name == "truhowl_repair_and_verify":
        provider = arguments.get("provider", "")
        from_ver = arguments.get("from_version")
        to_ver = arguments.get("to_version")
        report = run_maintenance_cycle(
            repo_dir=repo_path,
            provider_name=provider,
            from_version=from_ver,
            to_version=to_ver,
            create_pr=False,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Truhowl Repair Status: {'SUCCESS [GREEN]' if report.success else 'FAILED'}\n"
                        f"Files Scanned: {report.files_scanned} | Modified: {report.files_modified}\n"
                        f"Blast Radius Verified: {report.blast_radius_verified}\n"
                        f"Test Duration: {report.test_duration_ms}ms (Exit: {report.test_exit_code})\n\n"
                        f"Unified Diff:\n{report.unified_diff or 'None'}"
                    ),
                }
            ],
            "report": {
                "success": report.success,
                "files_modified": report.files_modified,
                "unintended_files_modified": report.unintended_files_modified,
                "blast_radius_verified": report.blast_radius_verified,
                "test_exit_code": report.test_exit_code,
                "test_duration_ms": report.test_duration_ms,
            },
        }

    elif name == "truhowl_explain_drift":
        provider = arguments.get("provider", "").lower()
        registry = get_default_registry()
        spec = registry.get(provider)
        if not spec:
            return {
                "content": [
                    {"type": "text", "text": f"No registered contract specifications for provider '{provider}'."}
                ]
            }

        migrations = spec.migrations
        explanation = [f"Provider: {spec.display_name} ({spec.package_name})", f"Documentation: {spec.docs_url}\n"]
        for mig_id, mig in migrations.items():
            explanation.append(f"Migration: {mig_id}")
            explanation.append(f"Changelog: {mig.changelog_url}")
            explanation.append(f"Description: {mig.description}")
            explanation.append(f"Breaking changes: {mig.breaking_changes_count}")
            explanation.append("Rewrites registered:")
            for rw in mig.rewrites:
                explanation.append(f"  - Pattern: {rw.pattern} -> {rw.replacement} ({rw.description})")

        return {
            "content": [
                {"type": "text", "text": "\n".join(explanation)}
            ]
        }

    raise ValueError(f"Unknown tool: {name}")


def serve_stdio():
    """Run MCP server over standard input/output (stdio JSON-RPC)."""
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line.strip())
            msg_id = req.get("id")
            method = req.get("method")

            if method == "initialize":
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {
                            "name": "truhowl-mcp-server",
                            "version": "1.1.0",
                        },
                        "capabilities": {
                            "tools": {},
                        },
                    },
                }
            elif method == "tools/list":
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": TOOLS,
                    },
                }
            elif method == "tools/call":
                params = req.get("params", {})
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                tool_result = handle_tool_call(tool_name, arguments)
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": tool_result,
                }
            else:
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()
        except Exception as e:
            err_res = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(e)},
            }
            sys.stdout.write(json.dumps(err_res) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
