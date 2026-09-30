"""Unit tests for Virtual Agent Lanes & Integration Engine."""

import shutil
import tempfile

from truhowl.engine.lane import LaneManager, LaneStatus
from truhowl.engine.integration import IntegrationEngine

def test_lane_manager_lifecycle():
    tmp_dir = tempfile.mkdtemp()
    try:
        mgr = LaneManager(workdir=tmp_dir)
        lane = mgr.create_lane(name="auth-fix", agent_id="Claude Code")

        assert lane.name == "auth-fix"
        assert lane.lane_id == "auth-fix"
        assert lane.agent_id == "Claude Code"
        assert lane.status == LaneStatus.CREATED

        mgr.record_diff("auth-fix", [{"path": "src/auth.py", "status": "modified"}])
        updated = mgr.get_lane("auth-fix")

        assert updated is not None
        assert updated.status == LaneStatus.COMPLETED
        assert len(updated.changes) == 1

        all_lanes = mgr.list_lanes()
        assert len(all_lanes) >= 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_integration_engine_candidate_and_conflicts():
    tmp_dir = tempfile.mkdtemp()
    try:
        lane_mgr = LaneManager(workdir=tmp_dir)

        lane_mgr.create_lane(name="auth-fix", agent_id="Claude Code")
        lane_mgr.record_diff("auth-fix", [{"path": "src/auth.py", "status": "modified"}])

        lane_mgr.create_lane(name="logging", agent_id="OpenCode")
        lane_mgr.record_diff("logging", [{"path": "src/logger.py", "status": "added"}])

        eng = IntegrationEngine(workdir=tmp_dir)
        cand = eng.create_candidate(["auth-fix", "logging"])

        assert len(cand.changes) == 2
        assert len(cand.conflicts) == 0

        preview_text = eng.preview()
        assert "auth-fix" in preview_text
        assert "logging" in preview_text
        assert "src/auth.py" in preview_text

        assert eng.apply() is True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
