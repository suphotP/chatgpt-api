import argparse
from pathlib import Path

import chatgpt_api.cli as cli
from chatgpt_api.cli import main
from chatgpt_api.providers.chatgpt.crypto import (
    clear_runtime_passphrase,
    decrypt_text,
    encrypt_text,
    is_encrypted,
    key_file_path,
    load_auto_secrets_key,
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
    exit_code = main(["server", "command", "--preset", "local", "--api-key", "local-dev-key"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "python3 -m chatgpt_api server start" in output
    assert "--accounts" not in output
    assert "local-dev-key" in output


def test_server_command_keeps_explicit_account_pool(capsys):
    exit_code = main(["server", "command", "--preset", "local", "--accounts", "go,plus-main", "--api-key", "local-dev-key"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "--accounts go,plus-main" in output


def test_server_command_accepts_launch_overrides(capsys):
    exit_code = main(
        [
            "server",
            "command",
            "--preset",
            "local",
            "--api-key",
            "local-dev-key",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
            "--public-base-url",
            "http://127.0.0.1:8010/v1",
            "--account-strategy",
            "random",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "--account-strategy random" in output
    assert "--port 8010" in output
    assert "--public-base-url http://127.0.0.1:8010/v1" in output


def test_server_accounts_auto_discovers_saved_captures(tmp_path):
    for name in ("go", "plus-main"):
        capture = tmp_path / name / "chatgpt-request.txt"
        capture.parent.mkdir()
        capture.write_text("URL: https://chatgpt.com/backend-api/f/conversation\n", encoding="utf-8")

    args = argparse.Namespace(accounts="", account="", accounts_dir=tmp_path)

    assert cli._server_accounts_from_args(args) == ("go", "plus-main")
    assert cli._primary_account_from_args(args, ("go", "plus-main")) == "go"


def test_server_accounts_can_be_pinned_to_skip_broken_profiles(tmp_path):
    for name in ("broken-old", "plus"):
        capture = tmp_path / name / "chatgpt-request.txt"
        capture.parent.mkdir()
        capture.write_text("URL: https://chatgpt.com/backend-api/f/conversation\n", encoding="utf-8")

    args = argparse.Namespace(accounts="plus", account="", accounts_dir=tmp_path)

    assert cli._server_accounts_from_args(args) == ("plus",)


def test_server_command_docker_accounts_mount_is_writable(capsys):
    exit_code = main(["server", "command", "--preset", "docker"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "/data/secrets/accounts:ro" not in output
    assert "-v \"$PWD/secrets/accounts:/data/secrets/accounts\"" in output


def test_api_chat_posts_route_override_to_bridge_router(monkeypatch, capsys):
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": body["model"],
            "chatgpt_account": "pro",
            "choices": [{"message": {"content": "ok from router"}}],
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(
        [
            "api",
            "chat",
            "--message",
            "hello",
            "--model",
            "auto",
            "--account",
            "pro",
            "--temporary-chat",
            "--agent-mode",
            "opencode",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls["method"] == "POST"
    assert calls["path"] == "/chat/completions"
    assert calls["body"]["chatgpt_account"] == "pro"
    assert calls["body"]["metadata"]["chatgpt_temporary_chat"] is True
    assert calls["body"]["metadata"]["agent_mode"] == "opencode"
    assert "ok from router" in output


def test_api_image_saves_locally_without_sending_host_output_path(tmp_path, monkeypatch, capsys):
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "id": "img_test",
            "model": body["model"],
            "chatgpt_account": "pro",
            "data": [
                {
                    "b64_json": "cG5nLWJ5dGVz",
                    "mime_type": "image/png",
                    "path": "/data/outputs/generated.png",
                }
            ],
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)
    output_dir = tmp_path / "local-images"

    exit_code = main(
        [
            "api",
            "image",
            "--prompt",
            "make one image",
            "--account",
            "pro",
            "--output-dir",
            str(output_dir),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls["method"] == "POST"
    assert calls["path"] == "/images/generations"
    assert "output_dir" not in calls["body"]
    assert "output_path" not in calls["body"]
    assert calls["body"]["chatgpt_account"] == "pro"
    saved = output_dir / "generated.png"
    assert saved.read_bytes() == b"png-bytes"
    assert f"local_path[1]={saved.resolve()}" in output


def test_api_edit_saves_locally_without_sending_host_output_path(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "id": "edit_test",
            "model": body["model"],
            "data": [
                {
                    "b64_json": "ZWRpdC1ieXRlcw==",
                    "mime_type": "image/png",
                    "path": "/data/outputs/edited.png",
                }
            ],
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)
    output_path = tmp_path / "edited-local.png"

    exit_code = main(
        [
            "api",
            "edit",
            "--prompt",
            "edit image",
            "--input-image",
            str(source),
            "--output-path",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert calls["method"] == "POST"
    assert calls["path"] == "/images/edits"
    assert "output_dir" not in calls["body"]
    assert "output_path" not in calls["body"]
    assert calls["body"]["input_images"][0]["data_url"].startswith("data:image/png;base64,")
    assert output_path.read_bytes() == b"edit-bytes"


def test_api_research_generates_cancelable_operation_id(monkeypatch, capsys):
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "id": "chatcmpl_research",
            "model": body["model"],
            "chatgpt_account": "pro",
            "chatgpt_operation_id": body["chatgpt_operation_id"],
            "chatgpt_research_report_path": "/tmp/report.md",
            "chatgpt_research_report_download_url": "http://127.0.0.1:8000/v1/chatgpt/files/file/report.md",
            "choices": [{"message": {"content": "Deep Research complete."}}],
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(
        [
            "api",
            "research",
            "--prompt",
            "research this",
            "--operation-id",
            "chatgptop_test",
            "--accounts",
            "free,pro",
            "--account-strategy",
            "failover",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls["method"] == "POST"
    assert calls["path"] == "/chat/completions"
    assert calls["body"]["deep_research"] is True
    assert calls["body"]["chatgpt_operation_id"] == "chatgptop_test"
    assert calls["body"]["chatgpt_accounts"] == ["free", "pro"]
    assert calls["body"]["chatgpt_account_strategy"] == "failover"
    assert "api operation --operation-id chatgptop_test" in output
    assert "api cancel --operation-id chatgptop_test" in output
    assert "deep_research_ready=yes" in output


def test_api_research_prints_cancelled_without_provider_error(monkeypatch, capsys):
    async def fake_api_request_json(args, method, path, body):
        raise cli.ProviderError("API POST /chat/completions failed: HTTP 499: ChatGPT Deep Research cancelled")

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(
        [
            "api",
            "research",
            "--prompt",
            "research this",
            "--operation-id",
            "chatgptop_cancelled",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "Research Cancelled" in captured.out
    assert "operation_id=chatgptop_cancelled" in captured.out
    assert "status=cancelled" in captured.out
    assert "provider error" not in captured.err


def test_api_operation_gets_operation_id(monkeypatch, capsys):
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "object": "chatgpt.operation",
            "operation": {
                "id": "chatgptop_test",
                "kind": "research",
                "account": "pro",
                "provider_selected": True,
                "conversation_id": "conversation-1",
                "deep_research_message_id": "message-1",
                "deep_research_session_id": "session-1",
                "deep_research_ready": True,
                "cancel_requested": False,
                "completed": False,
            },
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(["api", "operation", "--operation-id", "chatgptop_test"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls["method"] == "GET"
    assert calls["path"] == "/chatgpt/operations/chatgptop_test"
    assert calls["body"] is None
    assert "Operation Status" in output
    assert "deep_research_ready=yes" in output
    assert "deep_research_session_id=session-1" in output


def test_api_cancel_posts_operation_id(monkeypatch, capsys):
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {
            "status": "ok",
            "operation": {
                "id": "chatgptop_test",
                "kind": "research",
                "cancel_requested": True,
                "completed": False,
            },
        }

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(["api", "cancel", "--operation-id", "chatgptop_test"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls["method"] == "POST"
    assert calls["path"] == "/chatgpt/operations/chatgptop_test/cancel"
    assert calls["body"] == {}
    assert "cancel_requested=yes" in output


def test_api_vision_sends_data_url_input_image(tmp_path, monkeypatch):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    calls = {}

    async def fake_api_request_json(args, method, path, body):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = body
        return {"text": "seen", "mode": "describe", "choices": [{"message": {"content": "seen"}}]}

    monkeypatch.setattr(cli, "_api_request_json", fake_api_request_json)

    exit_code = main(["api", "vision", "--mode", "describe", "--input-image", str(image_path)])

    assert exit_code == 0
    assert calls["path"] == "/chatgpt/vision"
    assert calls["body"]["input_images"][0]["name"] == "sample.png"
    assert calls["body"]["input_images"][0]["data_url"].startswith("data:image/png;base64,")


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


def test_secrets_rotate_encrypts_legacy_plaintext_capture(tmp_path, capsys):
    accounts_dir = tmp_path / "accounts"
    account_dir = accounts_dir / "legacy"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"
    capture_path.write_text("URL: https://chatgpt.com\n", encoding="utf-8")

    exit_code = main(["secrets", "rotate", "--accounts-dir", str(accounts_dir)])

    output = capsys.readouterr().out
    on_disk = capture_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert "legacy: encrypted" in output
    assert is_encrypted(on_disk)
    assert decrypt_text(on_disk, load_secrets_key(accounts_dir)) == "URL: https://chatgpt.com\n"


def test_secrets_rotate_to_auto_key_file_ignores_env_passphrase(tmp_path, capsys, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    account_dir = accounts_dir / "pro"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"
    monkeypatch.setenv("CHATGPT_SECRETS_PASSPHRASE", "old-passphrase")
    old_key = load_secrets_key(accounts_dir)
    capture_path.write_text(encrypt_text("URL: https://chatgpt.com\n", old_key), encoding="utf-8")

    exit_code = main(["secrets", "rotate", "--accounts-dir", str(accounts_dir)])

    output = capsys.readouterr().out
    new_key = load_auto_secrets_key(accounts_dir)
    assert exit_code == 0
    assert "pro: rotated" in output
    assert key_file_path(accounts_dir).exists()
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
