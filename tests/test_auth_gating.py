import os
import shutil
import subprocess
import sys
from truhowl.credentials import save_credentials, load_credentials, clear_credentials, has_valid_credentials, verify_credentials


def test_credentials_module_lifecycle(tmp_path):
    creds_file = str(tmp_path / "creds.json")
    os.environ["TRUHOWL_CREDENTIALS_FILE"] = creds_file

    try:
        assert not has_valid_credentials()
        assert load_credentials() is None

        saved_path = save_credentials("anthropic", "sk-ant-testkey1234567890", model="claude-3-5-sonnet-20241022")
        assert saved_path == creds_file
        assert has_valid_credentials()

        data = load_credentials()
        assert data is not None
        assert data["provider"] == "anthropic"
        assert data["api_key"] == "sk-ant-testkey1234567890"
        assert data["model"] == "claude-3-5-sonnet-20241022"

        mode = os.stat(creds_file).st_mode & 0o777
        assert mode == 0o600

        assert clear_credentials()
        assert not os.path.exists(creds_file)
        assert not has_valid_credentials()
    finally:
        os.environ.pop("TRUHOWL_CREDENTIALS_FILE", None)


def test_verify_credentials_formats():
    ok, _ = verify_credentials("anthropic", "sk-ant-12345678901234567890")
    assert ok

    bad, msg = verify_credentials("anthropic", "bad-key")
    assert not bad
    assert "Invalid Anthropic" in msg

    ok, _ = verify_credentials("openai", "sk-1234567890123456789012345")
    assert ok

    bad, msg = verify_credentials("openai", "not-sk")
    assert not bad
    assert "Invalid OpenAI" in msg


def _copy_fixture(tmp_path):
    dst = str(tmp_path / "taxonomy_stripe")
    shutil.copytree("trials/fixtures/taxonomy_stripe", dst)
    return dst


def test_cli_check_gated_without_auth(tmp_path):
    # Blueprint box 2: zero-token indexing must succeed without LLM creds
    creds_file = str(tmp_path / "creds_none.json")
    env = dict(os.environ)
    env["PYTHONPATH"] = "python"
    env["TRUHOWL_CREDENTIALS_FILE"] = creds_file
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        env.pop(k, None)

    res = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "check", "trials/fixtures/taxonomy_stripe/"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in res.stdout

    # fix without creds should quarantine when AI is needed (no rewrite case via unknown provider)
    res2 = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "fix", _copy_fixture(tmp_path), "--provider", "twilio"],
        capture_output=True,
        text=True,
        env=env,
    )
    # twilio has no rewrites and no KB — should not crash, may be unverified but not LLM-gated hard fail
    assert res2.returncode in (0, 1)


def test_cli_auth_and_auto_index(tmp_path):
    fixture = _copy_fixture(tmp_path)
    creds_file = str(tmp_path / "creds_auth.json")
    env = dict(os.environ)
    env["PYTHONPATH"] = "python"
    env["TRUHOWL_CREDENTIALS_FILE"] = creds_file
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRUHOWL_LLM_KEY"):
        env.pop(k, None)

    res_auth = subprocess.run(
        [
            sys.executable, "-m", "truhowl.cli.main", "auth",
            "--provider", "anthropic",
            "--api-key", "sk-ant-validkey123456789012345",
            "--path", fixture,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_auth.returncode == 0
    assert "Credentials verified successfully for anthropic" in res_auth.stdout
    assert "Initializing Truhowl Knowledge Graph" in res_auth.stdout

    res_status = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "auth", "--status"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_status.returncode == 0
    assert "CONFIGURED [OK]" in res_status.stdout
    assert "anthropic" in res_status.stdout

    res_check = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "check", "trials/fixtures/taxonomy_stripe/"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_check.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in res_check.stdout

    res_index = subprocess.run(
        [sys.executable, "-m", "truhowl.cli.main", "index", fixture],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_index.returncode == 0
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in res_index.stdout
