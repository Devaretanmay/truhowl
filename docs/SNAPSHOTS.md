# BLAKE3 Snapshot & Differential Rollback Guide

Truhowl provides high-speed, BLAKE3 hash-based workspace snapshotting and differential file restoration to neutralize rogue file modifications and guarantee physical workspace recovery.

---

## 1. How It Works

1. **Pre-Execution Manifest**: Before an agent or workflow step executes, `SnapshotManager` scans the workspace directory (excluding `.git`, `.venv`, `node_modules`, `target`) and computes 16-byte BLAKE3 hashes for every file.
2. **Execution Tracking**: The agent runs inside its isolated kernel compartment.
3. **Differential Restoration (`truhowl undo`)**:
   - Modified files are restored to their exact pre-execution content.
   - Deleted files are recovered.
   - Newly created stray files are cleanly purged.

---

## 2. CLI Usage (`truhowl undo`)

```bash
# Execute an agent
truhowl claude

# Inspect changes
truhowl diff

# Rollback physical files instantly if the agent corrupted code
truhowl undo
```

---

## 3. Python SDK Usage

```python
from truhowl.sandbox.snapshot import SnapshotManager

# Initialize manager for the target worktree
snap = SnapshotManager(
    workdir=".",
    snapshot_dir=".truhowl/snapshots/exec_101"
)

# Take pre-execution snapshot
file_count = snap.snapshot()
print(f"Snapshotted {file_count} files.")

# ... Execute agent or tool step ...

# Instant rollback of any altered files
restored_count = snap.restore()
print(f"Restored {restored_count} changed file(s).")

# Clean up snapshot files
snap.cleanup()
```

---

## 4. Automatic Snapshotting in Workflows & Agent Sessions

When using `truhowl claude`, `truhowl --run <workflow>`, or `AgentTruhowl`, pre-execution snapshots are created automatically. Execution results track all added, modified, and deleted files with full auditability.
