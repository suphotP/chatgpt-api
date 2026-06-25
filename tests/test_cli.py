from pathlib import Path

import chatgpt_api.cli as cli
from chatgpt_api.cli import main
from chatgpt_api.providers.chatgpt.crypto import (
    clear_runtime_passphrase,
    decrypt_text,
    encrypt_text,
    key_file_path,
    load_secrets_key,
    set_runtime_passphrase,
)


def test_inspect_capture_redacts_secret_headers(tmp_path, capsys):
    capture_path = tmp_path / "request.txt"
    capture_path.write_text(
        """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer fake-token
Cookie: oai-did=device-1
Accept: text/event-stream
""",
        encoding="utf-8",
    )

    exit_code = main(["inspect-capture", str(capture_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "authorization: <redacted>" in output
    assert "cookie: <redacted>" in output
    assert "fake-token" not in output


def test_inspect_capture_from_account_profile(tmp_path, capsys):
    capture_path = tmp_path / "pro" / "chatgpt-request.txt"
    capture_path.parent.mkdir()
    capture_path.write_text(
        """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer fake-token
Request Data: {"action":"next"}
""",
        encoding="utf-8",
    )

    exit_code = main(["inspect-capture", "--account", "pro", "--accounts-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "request_json=yes" in output
    assert "fake-token" not in output


def test_accounts_lists_profiles(tmp_path, capsys):
    (tmp_path / "free").mkdir()
    (tmp_path / "free" / "chatgpt-request.txt").write_text("URL: https://chatgpt.com\n", encoding="utf-8")

    exit_code = main(["accounts", "--accounts-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "free: capture=yes" in output


def test_doctor_json_reports_missing_setup_without_crashing(tmp_path, capsys):
    exit_code = main(
        [
            "doctor",
            "--json",
            "--accounts-dir",
            str(tmp_path),
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--api-key",
            "local-dev-key",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert '"object": "chatgpt.doctor"' in output
    assert '"account_profiles"' in output
    assert "local-dev-key" not in output


def test_server_command_prints_start_command(capsys):
    exit_code = main(["server", "command", "--preset", "local", "--accounts", "free,pro", "--api-key", "local-dev-key"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "python3 -m chatgpt_api server start" in output
    assert "--accounts free,pro" in output
    assert "local-dev-key" in output


def test_account_info_from_account_profile(tmp_path, capsys):
    capture_path = tmp_path / "pro" / "chatgpt-request.txt"
    capture_path.parent.mkdir()
    capture_path.write_text(
        """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer header.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsib3BlbmFpX3BsYW5fdHlwZSI6InBybyIsImNoYXRncHRfcGxhbl90eXBlIjoicHJvIn0sImh0dHBzOi8vYXBpLm9wZW5haS5jb20vcHJvZmlsZSI6eyJlbWFpbCI6InByb0BleGFtcGxlLmNvbSJ9fQ.sig
Cookie: oai-last-model-config=%7B%22model%22%3A%22gpt-5-5-thinking%22%2C%22effort%22%3A%22extended%22%7D
Request Data: {"action":"next","model":"gpt-5-5-thinking","thinking_effort":"extended"}
""",
        encoding="utf-8",
    )
    (tmp_path / "pro" / "settings.json").write_text(
        """
{
  "settings": {
    "last_used_model_config": {
      "juices": {"web": {"gpt-5-5-thinking": "max"}},
      "slugs": {"web": "gpt-5-5"}
    },
    "wingman_thinking_effort": "instant"
  },
  "available_options": {"backend_reasoning_effort": ["instant", "medium", "high"]}
}
""",
        encoding="utf-8",
    )

    exit_code = main(["account-info", "--account", "pro", "--accounts-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "plan=pro" in output
    assert "observed_models=gpt-5-5-thinking" in output
    assert "observed_efforts=extended, max, instant, medium, high" in output
    assert "settings_available_reasoning_efforts=instant, medium, high" in output
    assert "pro@example.com" not in output


def test_secrets_rotate_switches_key_file_to_passphrase(tmp_path, capsys, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    account_dir = accounts_dir / "pro"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"

    old_key = load_secrets_key(accounts_dir)
    capture_path.write_text(encrypt_text("URL: https://chatgpt.com\n", old_key), encoding="utf-8")
    assert key_file_path(accounts_dir).exists()

    responses = iter(["new-passphrase", "new-passphrase"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(responses))

    exit_code = main(["secrets", "rotate", "--accounts-dir", str(accounts_dir), "--to-passphrase-prompt"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "pro: rotated" in output
    assert "rotated 1 of 1" in output
    assert not key_file_path(accounts_dir).exists()

    set_runtime_passphrase("new-passphrase")
    new_key = load_secrets_key(accounts_dir)
    clear_runtime_passphrase()
    assert decrypt_text(capture_path.read_text(encoding="utf-8"), new_key) == "URL: https://chatgpt.com\n"


def test_secrets_rotate_rejects_mismatched_new_passphrase(tmp_path, capsys, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    account_dir = accounts_dir / "pro"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"
    old_key = load_secrets_key(accounts_dir)
    capture_path.write_text(encrypt_text("URL: https://chatgpt.com\n", old_key), encoding="utf-8")

    responses = iter(["one", "two"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(responses))

    exit_code = main(["secrets", "rotate", "--accounts-dir", str(accounts_dir), "--to-passphrase-prompt"])

    assert exit_code == 2
    assert "did not match" in capsys.readouterr().err
    assert key_file_path(accounts_dir).exists()


def test_account_capabilities_from_account_profile(tmp_path, capsys):
    capture_path = tmp_path / "pro" / "chatgpt-request.txt"
    capture_path.parent.mkdir()
    capture_path.write_text(
        """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer header.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsib3BlbmFpX3BsYW5fdHlwZSI6InBybyIsImNoYXRncHRfcGxhbl90eXBlIjoicHJvIn19.sig
Request Data: {"action":"next","model":"gpt-5-5"}
""",
        encoding="utf-8",
    )
    (tmp_path / "pro" / "settings.json").write_text(
        """
{
  "settings": {
    "last_used_model_config": {
      "juices": {"web": {"gpt-5-5-pro": "extended", "gpt-5-5-thinking": "max"}},
      "slugs": {"web": "gpt-5-5"}
    }
  },
  "available_options": {"backend_reasoning_effort": ["instant", "medium", "high"]}
}
""",
        encoding="utf-8",
    )

    exit_code = main(["account-capabilities", "--account", "pro", "--accounts-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "plan=pro" in output
    assert "supported_models=gpt-5-5, gpt-5-5-thinking, gpt-5-5-pro" in output
    assert "default_model=gpt-5-5" in output
    assert "thinking_model=gpt-5-5-thinking" in output
    assert "thinking_efforts=standard, extended, max" in output
    assert "pro_model=gpt-5-5-pro" in output
    assert "pro_efforts=standard, extended" in output
    assert "backend_reasoning_efforts=instant, medium, high" in output
    assert "extra_observed_models=-" in output
