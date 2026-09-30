# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Structured test and compiler failure feedback parser.

Extracts failing files, line numbers, error codes, failure messages, and
stack traces from test runner / compiler outputs (pytest, jest, tsc, cargo, go).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class StructuredFailure:
    failing_files: list[str] = field(default_factory=list)
    failing_lines: list[int] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    primary_message: str = ""
    diagnostic_snippet: str = ""
    raw_output: str = ""

    def format_for_model(self, max_snippet_lines: int = 35) -> str:
        """Render a concise, high-signal diagnostic block for repair reasoning."""
        lines = ["--- STRUCTURED VERIFICATION / TEST FAILURE ---"]
        if self.failing_files:
            targets = [
                f"{f}:{line_no}" if line_no > 0 else f
                for f, line_no in zip(self.failing_files, self.failing_lines)
            ]
            lines.append(f"Failing Target(s): {', '.join(targets[:5])}")
        if self.error_types:
            lines.append(f"Error Type(s): {', '.join(self.error_types[:3])}")
        if self.primary_message:
            lines.append(f"Failure Summary: {self.primary_message}")
        if self.diagnostic_snippet:
            lines.append("Diagnostic Details / Stack Trace:")
            snippet_lines = self.diagnostic_snippet.strip().splitlines()[:max_snippet_lines]
            lines.extend(snippet_lines)
        lines.append("----------------------------------------------")
        return "\n".join(lines)


# Matchers for Python tracebacks, pytest failures, TS errors, Rust compiler errors, etc.
_PY_TRACEBACK_FILE_RE = re.compile(r'File\s+["\']([^"\']+)["\'],\s+line\s+(\d+)', re.IGNORECASE)
_PY_ERROR_RE = re.compile(r"^(?:E\s+|>\s+)?([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning)):\s*(.*)$", re.MULTILINE)
_PYTEST_FAIL_RE = re.compile(r"FAILED\s+([^\s:]+)(?:::([^\s]+))?", re.MULTILINE)
_TS_ERROR_RE = re.compile(r"([a-zA-Z0-9_./\\-]+\.(?:ts|tsx|js|jsx))\s*\((\d+),\s*(\d+)\):\s*error\s*(TS\d+):\s*(.*)")
_RUST_ERROR_RE = re.compile(r"error(?:\[(E\d+)\])?:\s*(.*)\n\s*-->\s*([a-zA-Z0-9_./\\-]+):(\d+):(\d+)")


def extract_structured_test_feedback(output: str, exit_code: int = 1) -> StructuredFailure:
    """Parse raw build/test stdout and stderr into a structured failure record."""
    if not output:
        return StructuredFailure(raw_output=output, primary_message=f"Process exited with code {exit_code}")

    failing_files: list[str] = []
    failing_lines: list[int] = []
    error_types: list[str] = []
    primary_message = ""

    # 1. Pytest / Python tracebacks
    for match in _PY_TRACEBACK_FILE_RE.finditer(output):
        fpath, line_str = match.group(1), match.group(2)
        if not fpath.startswith("<") and fpath not in failing_files:
            failing_files.append(fpath)
            failing_lines.append(int(line_str))

    for match in _PY_ERROR_RE.finditer(output):
        err_type, msg = match.group(1), match.group(2).strip()
        if err_type not in error_types:
            error_types.append(err_type)
        if not primary_message:
            primary_message = f"{err_type}: {msg}"

    for match in _PYTEST_FAIL_RE.finditer(output):
        fpath = match.group(1)
        if fpath not in failing_files:
            failing_files.append(fpath)
            failing_lines.append(0)

    # 2. TypeScript / JavaScript compiler
    for match in _TS_ERROR_RE.finditer(output):
        fpath, line_str, _col, code, msg = match.groups()
        if fpath not in failing_files:
            failing_files.append(fpath)
            failing_lines.append(int(line_str))
        if code not in error_types:
            error_types.append(code)
        if not primary_message:
            primary_message = f"{code}: {msg.strip()}"

    # 3. Rust compiler
    for match in _RUST_ERROR_RE.finditer(output):
        code, msg, fpath, line_str, _col = match.groups()
        if fpath not in failing_files:
            failing_files.append(fpath)
            failing_lines.append(int(line_str))
        if code and code not in error_types:
            error_types.append(code)
        if not primary_message:
            primary_message = msg.strip()

    # If no primary message was found by regex, pick the first error-looking line
    if not primary_message:
        for line in reversed(output.splitlines()):
            sline = line.strip()
            if sline.startswith(("E   ", "FAIL:", "Error:", "FAILED")):
                primary_message = sline
                break
        if not primary_message:
            primary_message = f"Test/verification failure (exit code {exit_code})"

    # Select the most informative snippet (tail lines or failure block)
    lines = output.splitlines()
    if len(lines) <= 40:
        diagnostic_snippet = "\n".join(lines)
    else:
        start_idx = len(lines) - 35
        for i, line in enumerate(lines):
            if any(k in line for k in ("FAILURES", "=== FAILURES ===", "Traceback (most recent call last):")):
                start_idx = i
                break
        diagnostic_snippet = "\n".join(lines[start_idx : start_idx + 40])

    return StructuredFailure(
        failing_files=failing_files,
        failing_lines=failing_lines,
        error_types=error_types,
        primary_message=primary_message,
        diagnostic_snippet=diagnostic_snippet,
        raw_output=output,
    )
