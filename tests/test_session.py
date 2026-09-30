"""Unit tests for AgentSession primitive and SessionManager."""

import shutil
import tempfile

from truhowl.engine.session import AgentSession, SessionManager, SessionStatus

def test_agent_session_creation():
    session = AgentSession(
        session_id="sess_123",
        agent="Claude Code",
        task="Fix auth bug",
        compartment_id="Builder",
        policy={"permissions": ["fs_read", "fs_write"]},
    )
    session.log_action("READ", "auth.py", status="OK")
    session.log_action("EXECUTE", "pytest", status="BLOCKED_BY_KERNEL", details="network denied")
    session.complete(returncode=0, diffs=[{"path": "auth.py", "status": "modified"}])

    assert session.session_id == "sess_123"
    assert session.agent == "Claude Code"
    assert session.agent_name == "Claude Code"
    assert session.status == SessionStatus.COMPLETED
    assert len(session.actions) == 2
    assert len(session.diffs) == 1

    view = session.format_ascii_view()
    assert "TRUHOWL AGENT SESSION #sess_123" in view


def test_session_manager_lifecycle():
    tmp_dir = tempfile.mkdtemp()
    try:
        mgr = SessionManager(workdir=tmp_dir)
        sess = mgr.create_session(
            agent_name="TestAgent",
            task="UnitTest",
            compartment_name="Tester",
        )
        assert sess.session_id.startswith("sess_")

        sess.log_action("WRITE", "output.txt", status="OK")
        sess.complete(returncode=0)
        mgr.save_session(sess)

        loaded = mgr.get_session(sess.session_id)
        assert loaded is not None
        assert loaded.agent == "TestAgent"
        assert loaded.agent_name == "TestAgent"
        assert loaded.status == SessionStatus.COMPLETED

        all_sessions = mgr.list_sessions()
        assert len(all_sessions) >= 1
        assert all_sessions[0].session_id == sess.session_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
