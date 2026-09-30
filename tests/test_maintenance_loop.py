import json
import os
import shutil
from unittest.mock import MagicMock, patch

from truhowl.maintenance import detect_drift, run_maintenance_cycle
from truhowl.patch_writer import PatchResult


def test_detect_drift_in_fixture():
    fixture_dir = "trials/fixtures/taxonomy_stripe"
    detected = detect_drift(fixture_dir)
    assert len(detected) >= 1
    stripe_dep = next((d for d in detected if d["provider"] == "stripe"), None)
    assert stripe_dep is not None
    assert stripe_dep["declared_version"] == "^11.18.0"


def test_run_maintenance_cycle_taxonomy_quarantine_without_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    fixture_dir = "trials/fixtures/taxonomy_stripe"
    target_dir = str(tmp_path / "taxonomy_stripe")
    shutil.copytree(fixture_dir, target_dir)

    report = run_maintenance_cycle(
        repo_dir=target_dir,
        provider_name="stripe",
        from_version="11.18.0",
        to_version="22.0.0",
        create_pr=False,
    )

    assert report.provider_name == "stripe"
    assert report.from_version == "11.18.0"
    assert report.to_version == "22.0.0"
    assert report.success is False
    assert report.repair_path == "none"
    assert "no_credentials_for_ai" in (report.error or "")
    assert "repair quarantined" in report.trust_pr_body


def test_run_maintenance_cycle_ai_authored_with_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")
    fixture_dir = "trials/fixtures/taxonomy_stripe"
    target_dir = str(tmp_path / "taxonomy_stripe_ai")
    shutil.copytree(fixture_dir, target_dir)

    mock_planner = MagicMock()
    mock_planner.plan_and_apply.return_value = [
        PatchResult(
            file_path=os.path.join(target_dir, "src", "billing.ts"),
            success=True,
            lines_changed=4,
            unified_diff="--- a\n+++ b",
            rules_applied=["AI-authored Stripe migration"],
        )
    ]
    with patch("truhowl.ai_planner.AIPatchPlanner.from_env", return_value=mock_planner):
        report = run_maintenance_cycle(
            repo_dir=target_dir,
            provider_name="stripe",
            from_version="11.18.0",
            to_version="22.0.0",
            create_pr=False,
        )
        assert report.repair_path == "ai-reasoning"


def test_quarantine_reports_no_path(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"dependencies": {"twilio": "^1.0.0"}}))
    monkeypatch.setenv("TRUHOWL_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    report = run_maintenance_cycle(str(repo), "twilio", from_version="1.0", to_version="2.0")
    assert not report.success
    assert report.repair_path == "none"
    assert report.error
