"""At-rest encryption for local account capture files.

Capture files hold live session credentials (cookies, bearer tokens, sentinel
proof headers). Encrypting them on disk protects against the case the README
itself warns about: the file getting copied into a backup, synced to the
cloud, or pasted somewhere by mistake, without anyone needing the key.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENC_PREFIX = "enc:v1:"

_MASTER_KEY_FILE = ".master.key"
_MASTER_SALT_FILE = ".master.salt"
_PBKDF2_ITERATIONS = 600_000
_PASSPHRASE_ENV_VAR = "CHATGPT_SECRETS_PASSPHRASE"

# Set once at process start by an interactive prompt (see `cli.py
# --secrets-passphrase-prompt`). Never written to disk or to the environment,
# so a cold copy of the machine's disk carries no usable key material.
_runtime_passphrase: str | None = None


def set_runtime_passphrase(passphrase: str) -> None:
    global _runtime_passphrase
    _runtime_passphrase = passphrase


def clear_runtime_passphrase() -> None:
    global _runtime_passphrase
    _runtime_passphrase = None


def is_encrypted(data: str) -> bool:
    return data.startswith(ENC_PREFIX)


def encrypt_text(plaintext: str, key: bytes) -> str:
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return ENC_PREFIX + token.decode("ascii")


def decrypt_text(data: str, key: bytes) -> str:
    if not is_encrypted(data):
        return data
    token = data[len(ENC_PREFIX) :].encode("ascii")
    try:
        return Fernet(key).decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "could not decrypt account capture: wrong CHATGPT_SECRETS_PASSPHRASE "
            "or a corrupted/missing master key file"
        ) from exc


def load_secrets_key(accounts_dir: Path) -> bytes:
    """Return the Fernet key used to encrypt captures under ``accounts_dir``.

    Key material is chosen in this order:

    1. A passphrase set in-memory via ``set_runtime_passphrase`` (interactive
       prompt at server start). Never touches disk or the environment, so a
       cold copy of this machine's disk (lost laptop, leaked backup, stolen
       drive) carries no usable key.
    2. ``CHATGPT_SECRETS_PASSPHRASE`` in the environment. Convenient for
       Docker/headless setups, but weaker than (1): the passphrase can end up
       in shell history, `.env` files, or process listings.
    3. A random key auto-generated on first use and stored next to the
       accounts directory with owner-only (0600) permissions. Zero-config
       default; protects against the capture file leaking on its own, but not
       against a copy of the whole accounts directory.
    """

    if _runtime_passphrase:
        return _derive_key_from_passphrase(_runtime_passphrase, _get_or_create_salt(accounts_dir))
    passphrase = os.environ.get(_PASSPHRASE_ENV_VAR, "").strip()
    if passphrase:
        return _derive_key_from_passphrase(passphrase, _get_or_create_salt(accounts_dir))
    return _get_or_create_key_file(accounts_dir)


def _derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _get_or_create_salt(accounts_dir: Path) -> bytes:
    accounts_dir.mkdir(parents=True, exist_ok=True)
    salt_path = accounts_dir / _MASTER_SALT_FILE
    if salt_path.exists():
        return salt_path.read_bytes()
    salt = os.urandom(16)
    salt_path.write_bytes(salt)
    return salt


def _get_or_create_key_file(accounts_dir: Path) -> bytes:
    accounts_dir.mkdir(parents=True, exist_ok=True)
    key_path = accounts_dir / _MASTER_KEY_FILE
    if key_path.exists():
        return key_path.read_bytes().strip()

    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def key_file_path(accounts_dir: Path) -> Path:
    return accounts_dir / _MASTER_KEY_FILE


def reencrypt_file(path: Path, old_key: bytes, new_key: bytes) -> bool:
    """Re-encrypt one capture file in place under new key material.

    Returns False (no-op) if the file is missing or already plaintext, so
    callers can migrate a mixed directory of encrypted and legacy plaintext
    captures without special-casing each one.
    """

    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if not is_encrypted(text):
        return False
    plaintext = decrypt_text(text, old_key)
    path.write_text(encrypt_text(plaintext, new_key), encoding="utf-8")
    return True
