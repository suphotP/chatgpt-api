import os
import stat

from cryptography.fernet import Fernet

from chatgpt_api.providers.chatgpt.crypto import (
    clear_runtime_passphrase,
    decrypt_text,
    encrypt_text,
    is_encrypted,
    key_file_path,
    load_secrets_key,
    reencrypt_file,
    set_runtime_passphrase,
)
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


def test_encrypt_round_trip():
    key = Fernet.generate_key()
    token = encrypt_text("hello secret", key)

    assert is_encrypted(token)
    assert decrypt_text(token, key) == "hello secret"


def test_decrypt_passthrough_for_plaintext():
    assert is_encrypted("plain text capture") is False
    assert decrypt_text("plain text capture", b"unused-key") == "plain text capture"


def test_load_secrets_key_creates_owner_only_key_file(tmp_path):
    accounts_dir = tmp_path / "accounts"

    key_one = load_secrets_key(accounts_dir)
    key_two = load_secrets_key(accounts_dir)

    assert key_one == key_two
    key_path = accounts_dir / ".master.key"
    assert key_path.exists()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600


def test_load_secrets_key_from_passphrase_is_deterministic(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    monkeypatch.setenv("CHATGPT_SECRETS_PASSPHRASE", "correct horse battery staple")

    key_one = load_secrets_key(accounts_dir)
    key_two = load_secrets_key(accounts_dir)

    assert key_one == key_two
    assert not (accounts_dir / ".master.key").exists()
    assert (accounts_dir / ".master.salt").exists()


def test_captured_request_from_file_decrypts_transparently(tmp_path):
    accounts_dir = tmp_path / "secrets" / "accounts"
    account_dir = accounts_dir / "pro-main"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"

    plaintext = "URL: https://chatgpt.com\nStatus: 200\n\nAuthorization: Bearer fake-token\n"
    key = load_secrets_key(accounts_dir)
    capture_path.write_text(encrypt_text(plaintext, key), encoding="utf-8")

    capture = CapturedRequest.from_file(capture_path)

    assert capture.url == "https://chatgpt.com"
    assert capture.headers["authorization"] == "Bearer fake-token"


def test_captured_request_from_file_still_reads_plaintext(tmp_path):
    accounts_dir = tmp_path / "secrets" / "accounts"
    account_dir = accounts_dir / "legacy"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"
    capture_path.write_text("URL: https://chatgpt.com\nStatus: 200\n", encoding="utf-8")

    capture = CapturedRequest.from_file(capture_path)

    assert capture.url == "https://chatgpt.com"


def test_runtime_passphrase_takes_priority_over_env(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    monkeypatch.setenv("CHATGPT_SECRETS_PASSPHRASE", "env-passphrase")
    try:
        set_runtime_passphrase("runtime-passphrase")
        runtime_key = load_secrets_key(accounts_dir)
        clear_runtime_passphrase()
        env_key = load_secrets_key(accounts_dir)
        assert runtime_key != env_key
    finally:
        clear_runtime_passphrase()


def test_reencrypt_file_round_trips_under_new_key(tmp_path):
    capture_path = tmp_path / "chatgpt-request.txt"
    old_key = load_secrets_key(tmp_path / "old")
    new_key = load_secrets_key(tmp_path / "new")
    capture_path.write_text(encrypt_text("super secret", old_key), encoding="utf-8")

    assert reencrypt_file(capture_path, old_key, new_key) is True
    assert decrypt_text(capture_path.read_text(encoding="utf-8"), new_key) == "super secret"


def test_reencrypt_file_is_noop_for_missing_or_plaintext(tmp_path):
    missing = tmp_path / "missing.txt"
    assert reencrypt_file(missing, b"a", b"b") is False

    plaintext = tmp_path / "plain.txt"
    plaintext.write_text("not encrypted", encoding="utf-8")
    assert reencrypt_file(plaintext, b"a", b"b") is False
    assert plaintext.read_text(encoding="utf-8") == "not encrypted"


def test_key_file_path_matches_what_load_secrets_key_creates(tmp_path):
    accounts_dir = tmp_path / "accounts"
    load_secrets_key(accounts_dir)

    assert key_file_path(accounts_dir).exists()
    assert key_file_path(accounts_dir) == accounts_dir / ".master.key"
