# Execution-Evidence Compression & Failure Summarization

Sandboxed test suites, build runs, and compiler outputs can easily produce tens of thousands of lines of terminal text. Truhowl includes high-speed native Rust compression engines (`src/engines/compression/`) to distill verbose test logs down to high-signal failure traces, stack traces, and verification evidence for LLM maintenance agents and Developer Trust PR receipts without exceeding context limits.

---

## 1. Core Compression Engines

The Truhowl Rust core includes four specialized evidence compression engines:

1. **LogCompressor & Stack Trace Isolator**: Strips noisy repetitive progress loops, polling logs, and build progress bars while preserving critical error tracebacks, panic messages, failing assertion lines, and exit statuses.
2. **SmartCrusher (JSON & Contract Compaction)**: Compacts large OpenAPI schemas, dependency trees, and payload arrays into structural schemas and representative records.
3. **DiffCompressor**: Trims massive multi-file diffs to highlight modified AST nodes while preserving file boundaries and syntax integrity.
4. **TextCrusher**: Extractive summarizer for verbose terminal logs.

---

## 2. Dynamic Content Routing (`route_and_compress`)

Truhowl automatically detects the content type of execution output (build logs, JSON, diffs, raw text) and applies the optimal engine:

```python
from truhowl._core import route_and_compress

# Route and compress execution output (test logs, build traces, diffs)
raw_log = """
==================================== ERRORS ====================================
_______________________ test_stripe_charges_v4_migration _______________________
TypeError: Stripe.Charge.create() missing 1 required positional argument: 'params'
...
=========================== short test summary info ============================
FAILED tests/test_stripe.py::test_stripe_charges_v4_migration - TypeError
"""

compressed_log = route_and_compress(raw_log)

print(f"Raw Log: {len(raw_log)} bytes -> Compressed Evidence: {len(compressed_log)} bytes")
print(compressed_log)
```

---

## 3. Where Compression Is Wired

* **Compartment outputs**: `Box.enable_compression()` (auto-enabled on
  `AgentTruhowl`) compresses large compartment results above the size
  threshold; `compressed_outputs` exposes the distilled text.
* **Hunt verification evidence** uses bounded raw capture instead
  (test output capped per attempt, diff previews capped per PR body),
  so repair evidence is never lossy-compressed before the model or
  the reviewer sees it.
* **Deterministic Compression**: Fast, reproducible Rust execution ensures evidence is compressed identically across runs.
