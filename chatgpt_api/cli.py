"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

from chatgpt_api.api.openai_compat import OpenAICompatConfig, run_server
from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.registry import default_registry
from chatgpt_api.core.types import ChatRequest, ContentPart, ImageInput, ImageRequest, Message
from chatgpt_api.providers import register_builtin_providers
from chatgpt_api.providers.chatgpt.account_info import (
    detect_account_info,
    infer_account_capabilities,
    load_settings_file,
)
from chatgpt_api.providers.chatgpt.accounts import (
    accounts_dir_from_env,
    list_account_profiles,
    resolve_account_capture_path,
    resolve_account_settings_path,
)
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.crypto import (
    clear_runtime_passphrase,
    key_file_path,
    load_secrets_key,
    reencrypt_file,
    set_runtime_passphrase,
)
from chatgpt_api.providers.chatgpt.models import parse_model_picker
from chatgpt_api.providers.chatgpt.proof import decode_proof_config, generate_proof_token
from chatgpt_api.providers.chatgpt.provider import ChatGPTProvider
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest
from chatgpt_api.providers.chatgpt.timezone import local_timezone_payload
from chatgpt_api.providers.chatgpt.transport import ChatGPTEndpoints, ChatGPTWebTransport, _websocket_url_headers


def main(argv: list[str] | None = None) -> int:
    register_builtin_providers()
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        argv = ["menu"]
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatgpt-api")
    parser.add_argument("--provider", default=os.environ.get("CHAT_PROVIDER", "chatgpt"))
    subparsers = parser.add_subparsers(required=True)

    doctor = subparsers.add_parser("doctor", help="Check local setup, account captures, Docker files, and API health")
    doctor.add_argument("--base-url", default=_admin_base_url_default())
    doctor.add_argument("--api-key", default=_admin_api_key_default())
    doctor.add_argument("--accounts-dir", type=Path, default=_accounts_dir_default())
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    menu = subparsers.add_parser("menu", help="Interactive control-plane menu for common admin tasks")
    menu.add_argument("--base-url", default=_admin_base_url_default())
    menu.add_argument("--api-key", default=_admin_api_key_default())
    menu.set_defaults(func=cmd_menu)

    providers = subparsers.add_parser("providers", help="List registered providers")
    providers.set_defaults(func=cmd_providers)

    accounts = subparsers.add_parser("accounts", help="List local ChatGPT account profiles")
    accounts.add_argument("--accounts-dir", type=Path, default=None)
    accounts.set_defaults(func=cmd_accounts)

    account_info = subparsers.add_parser("account-info", help="Detect account plan and observed modes from a capture")
    account_info.add_argument("path", type=Path, nargs="?")
    account_info.add_argument("--account", default=None)
    account_info.add_argument("--accounts-dir", type=Path, default=None)
    account_info.add_argument("--settings", type=Path, default=None)
    account_info.add_argument("--no-settings", action="store_true")
    account_info.add_argument("--json", action="store_true")
    account_info.set_defaults(func=cmd_account_info)

    account_capabilities = subparsers.add_parser(
        "account-capabilities",
        help="Infer supported ChatGPT models and thinking efforts for an account",
    )
    account_capabilities.add_argument("path", type=Path, nargs="?")
    account_capabilities.add_argument("--account", default=None)
    account_capabilities.add_argument("--accounts-dir", type=Path, default=None)
    account_capabilities.add_argument("--settings", type=Path, default=None)
    account_capabilities.add_argument("--no-settings", action="store_true")
    account_capabilities.add_argument("--json", action="store_true")
    account_capabilities.set_defaults(func=cmd_account_capabilities)

    account_models = subparsers.add_parser("account-models", help="Fetch ChatGPT model picker metadata")
    account_models.add_argument("path", type=Path, nargs="?")
    account_models.add_argument("--account", default=None)
    account_models.add_argument("--accounts-dir", type=Path, default=None)
    account_models.add_argument("--impersonate", default="safari18_4")
    account_models.add_argument("--json", action="store_true")
    account_models.set_defaults(func=cmd_account_models)

    account_check = subparsers.add_parser("account-check", help="Check whether a ChatGPT capture still authenticates")
    account_check.add_argument("path", type=Path, nargs="?")
    account_check.add_argument("--account", default=None)
    account_check.add_argument("--accounts-dir", type=Path, default=None)
    account_check.add_argument("--impersonate", default="safari18_4")
    account_check.set_defaults(func=cmd_account_check)

    account_limits = subparsers.add_parser("account-limits", help="Fetch ChatGPT live conversation init limits")
    account_limits.add_argument("path", type=Path, nargs="?")
    account_limits.add_argument("--account", default=None)
    account_limits.add_argument("--accounts-dir", type=Path, default=None)
    account_limits.add_argument("--requested-default-model", default=None)
    account_limits.add_argument("--conversation-id", default=None)
    account_limits.add_argument("--conversation-origin", default=None)
    account_limits.add_argument("--impersonate", default="safari18_4")
    account_limits.add_argument("--json", action="store_true")
    account_limits.set_defaults(func=cmd_account_limits)

    secrets = subparsers.add_parser("secrets", help="Manage local account-secrets encryption")
    secrets_subparsers = secrets.add_subparsers(required=True)
    secrets_rotate = secrets_subparsers.add_parser(
        "rotate",
        help="Re-encrypt stored account captures under new key material, for example when switching to a passphrase",
    )
    secrets_rotate.add_argument("--accounts-dir", type=Path, default=_accounts_dir_default())
    secrets_rotate.add_argument(
        "--from-passphrase-prompt",
        action="store_true",
        help="Prompt for the current passphrase used to decrypt existing captures",
    )
    secrets_rotate.add_argument(
        "--to-passphrase-prompt",
        action="store_true",
        help="Prompt for the new passphrase; omit to rotate to a fresh auto-generated key file instead",
    )
    secrets_rotate.set_defaults(func=cmd_secrets_rotate)

    admin = subparsers.add_parser("admin", help="Manage a running Bridge API from CLI or Docker")
    admin.add_argument("--base-url", default=os.environ.get("CHATGPT_ADMIN_BASE_URL") or os.environ.get("CHATGPT_BASE_URL") or "http://127.0.0.1:8000/v1")
    admin.add_argument("--api-key", default=os.environ.get("CHATGPT_API_KEY", "local-dev-key"))
    admin_subparsers = admin.add_subparsers(required=True)

    admin_commands: list[argparse.ArgumentParser] = []

    admin_status = admin_subparsers.add_parser("status", help="Show server status and runtime settings")
    admin_commands.append(admin_status)
    admin_status.add_argument("--json", action="store_true")
    admin_status.set_defaults(func=cmd_admin_status)

    admin_usage = admin_subparsers.add_parser("usage", help="Show live per-account usage and limits")
    admin_commands.append(admin_usage)
    admin_usage.add_argument("--json", action="store_true")
    admin_usage.set_defaults(func=cmd_admin_usage)

    admin_models = admin_subparsers.add_parser("models", help="Show models currently exposed by the running API")
    admin_commands.append(admin_models)
    admin_models.add_argument("--json", action="store_true")
    admin_models.set_defaults(func=cmd_admin_models)

    admin_capacity = admin_subparsers.add_parser("capacity", help="Show chat/image/research capacity across routed accounts")
    admin_commands.append(admin_capacity)
    admin_capacity.add_argument("--json", action="store_true")
    admin_capacity.set_defaults(func=cmd_admin_capacity)

    admin_settings = admin_subparsers.add_parser("settings", help="Show persisted Bridge API settings")
    admin_commands.append(admin_settings)
    admin_settings.add_argument("--json", action="store_true")
    admin_settings.set_defaults(func=cmd_admin_settings)

    admin_limits = admin_subparsers.add_parser("set-limits", help="Persist local concurrency limits")
    admin_commands.append(admin_limits)
    admin_limits.add_argument("--chat", default=None, help="Example: free=1,go=2,plus=3,pro=4,work-pro=2")
    admin_limits.add_argument("--upload", default=None, help="Example: free=1,go=1,plus=1,pro=1")
    admin_limits.add_argument("--image", default=None, help="Example: free=1,go=1,plus=2,pro=3")
    admin_limits.add_argument("--research", default=None, help="Example: free=1,go=1,plus=2,pro=2")
    admin_limits.add_argument("--json", action="store_true")
    admin_limits.set_defaults(func=cmd_admin_set_limits)

    admin_reset = admin_subparsers.add_parser("reset-settings", help="Reset persisted Bridge API settings to defaults")
    admin_commands.append(admin_reset)
    admin_reset.add_argument("--json", action="store_true")
    admin_reset.set_defaults(func=cmd_admin_reset_settings)

    admin_accounts = admin_subparsers.add_parser("accounts", help="List configured and stored accounts")
    admin_commands.append(admin_accounts)
    admin_accounts.add_argument("--json", action="store_true")
    admin_accounts.set_defaults(func=cmd_admin_accounts)

    admin_check_accounts = admin_subparsers.add_parser("check-accounts", help="Live-check one or all accounts")
    admin_commands.append(admin_check_accounts)
    admin_check_accounts.add_argument(
        "--account",
        default="all",
        metavar="ACCOUNT_NAME",
        help="Local account alias to check, such as pro-main, or all.",
    )
    admin_check_accounts.add_argument("--json", action="store_true")
    admin_check_accounts.set_defaults(func=cmd_admin_check_accounts)

    admin_account = admin_subparsers.add_parser("account", help="Add, update, verify, list, or delete accounts")
    admin_commands.append(admin_account)
    account_subparsers = admin_account.add_subparsers(required=True)

    account_list = account_subparsers.add_parser("list", help="List saved accounts")
    account_list.add_argument("--json", action="store_true")
    account_list.set_defaults(func=cmd_admin_account_list)

    account_verify = account_subparsers.add_parser("verify", help="Live-check one account or all accounts")
    account_verify.add_argument(
        "--account",
        default="all",
        metavar="ACCOUNT_NAME",
        help="Local account alias to check, such as pro-main, or all.",
    )
    account_verify.add_argument("--json", action="store_true")
    account_verify.set_defaults(func=cmd_admin_account_verify)

    account_add = account_subparsers.add_parser("add", help="Add an account from a copied request capture")
    account_add.add_argument(
        "--account",
        metavar="ACCOUNT_NAME",
        help="Local account alias to create, such as pro-main. This is not the ChatGPT plan name.",
    )
    account_add.add_argument("--capture-file", type=Path, metavar="PATH")
    account_add.add_argument("--paste", action="store_true", help="Paste capture text interactively instead of reading a file")
    account_add.add_argument("--no-live-verify", action="store_true", help="Skip the post-save live account probe")
    account_add.add_argument("--json", action="store_true")
    account_add.set_defaults(func=cmd_admin_account_save, account_action="add")

    account_update = account_subparsers.add_parser("update", help="Replace an existing account capture after validation")
    account_update.add_argument(
        "--account",
        metavar="ACCOUNT_NAME",
        help="Local account alias to refresh, such as pro-main. This is not the ChatGPT plan name.",
    )
    account_update.add_argument("--capture-file", type=Path, metavar="PATH")
    account_update.add_argument("--paste", action="store_true", help="Paste capture text interactively instead of reading a file")
    account_update.add_argument("--no-live-verify", action="store_true", help="Skip the post-save live account probe")
    account_update.add_argument("--json", action="store_true")
    account_update.set_defaults(func=cmd_admin_account_save, account_action="update")

    account_delete = account_subparsers.add_parser("delete", help="Delete an account capture/settings pair")
    account_delete.add_argument(
        "--account",
        metavar="ACCOUNT_NAME",
        help="Local account alias to delete, such as old-free-main.",
    )
    account_delete.add_argument("--keep-capture", action="store_true")
    account_delete.add_argument("--keep-settings", action="store_true")
    account_delete.add_argument("--json", action="store_true")
    account_delete.set_defaults(func=cmd_admin_account_delete)

    for account_command in [account_list, account_verify, account_add, account_update, account_delete]:
        account_command.add_argument("--base-url", default=argparse.SUPPRESS)
        account_command.add_argument("--api-key", default=argparse.SUPPRESS)

    admin_save_capture = admin_subparsers.add_parser("save-capture", help="Add or refresh an account from copied request details")
    admin_commands.append(admin_save_capture)
    admin_save_capture.add_argument(
        "--account",
        metavar="ACCOUNT_NAME",
        help="Local account alias to create or refresh, such as pro-main. This is not the ChatGPT plan name.",
    )
    admin_save_capture.add_argument("--capture-file", type=Path, metavar="PATH")
    admin_save_capture.add_argument("--paste", action="store_true", help="Paste capture text interactively instead of reading a file")
    admin_save_capture.add_argument("--no-live-verify", action="store_true", help="Skip the post-save live account probe")
    admin_save_capture.add_argument("--json", action="store_true")
    admin_save_capture.set_defaults(func=cmd_admin_save_capture)

    admin_delete_account = admin_subparsers.add_parser("delete-account", help="Delete an account capture/settings pair")
    admin_commands.append(admin_delete_account)
    admin_delete_account.add_argument(
        "--account",
        metavar="ACCOUNT_NAME",
        help="Local account alias to delete, such as old-free-main.",
    )
    admin_delete_account.add_argument("--keep-capture", action="store_true")
    admin_delete_account.add_argument("--keep-settings", action="store_true")
    admin_delete_account.add_argument("--json", action="store_true")
    admin_delete_account.set_defaults(func=cmd_admin_delete_account)

    admin_artifacts = admin_subparsers.add_parser("artifacts", help="List saved images/research files")
    admin_commands.append(admin_artifacts)
    admin_artifacts.add_argument("--limit", type=int, default=100)
    admin_artifacts.add_argument("--json", action="store_true")
    admin_artifacts.set_defaults(func=cmd_admin_artifacts)

    admin_delete_artifact = admin_subparsers.add_parser("delete-artifact", help="Delete artifact metadata and optionally the file")
    admin_commands.append(admin_delete_artifact)
    admin_delete_artifact.add_argument("--file-id", required=True)
    admin_delete_artifact.add_argument("--delete-file", action="store_true")
    admin_delete_artifact.add_argument("--json", action="store_true")
    admin_delete_artifact.set_defaults(func=cmd_admin_delete_artifact)

    admin_opencode = admin_subparsers.add_parser("opencode", help="Inject/eject/show opencode consumer config")
    admin_commands.append(admin_opencode)
    admin_opencode.add_argument("action", choices=["status", "inject", "eject"])
    admin_opencode.add_argument("--model", default="chatgpt-web/auto@optimized")
    admin_opencode.add_argument("--config-path", default=None)
    admin_opencode.add_argument("--json", action="store_true")
    admin_opencode.set_defaults(func=cmd_admin_opencode)

    admin_test_chat = admin_subparsers.add_parser("test-chat", help="Send a short API smoke-test chat")
    admin_commands.append(admin_test_chat)
    admin_test_chat.add_argument("--message", "-m", default="Say hello in one short sentence.")
    admin_test_chat.add_argument("--model", default="auto")
    admin_test_chat.add_argument("--json", action="store_true")
    admin_test_chat.set_defaults(func=cmd_admin_test_chat)

    admin_test_image = admin_subparsers.add_parser("test-image", help="Generate one image through the running API")
    admin_commands.append(admin_test_image)
    admin_test_image.add_argument("--prompt", "-p", required=True)
    admin_test_image.add_argument("--model", default="auto")
    admin_test_image.add_argument("--output-dir", default=None)
    admin_test_image.add_argument("--json", action="store_true")
    admin_test_image.set_defaults(func=cmd_admin_test_image)

    admin_presets = admin_subparsers.add_parser("presets", help="Print launch presets for local, LAN, and Docker")
    admin_commands.append(admin_presets)
    admin_presets.add_argument("--accounts", default="free-main,pro-main")
    admin_presets.add_argument("--api-key", default=os.environ.get("CHATGPT_API_KEY", "local-dev-key"))
    admin_presets.add_argument("--lan-host", default="0.0.0.0")
    admin_presets.add_argument("--lan-base-url", default="http://192.168.1.203:8000/v1")
    admin_presets.set_defaults(func=cmd_admin_presets)

    for admin_command in admin_commands:
        if admin_command is admin_presets:
            continue
        admin_command.add_argument("--base-url", default=argparse.SUPPRESS)
        admin_command.add_argument("--api-key", default=argparse.SUPPRESS)

    chat = subparsers.add_parser("chat", help="Send a chat message")
    chat.add_argument("--model", default=None)
    chat.add_argument("--message", "-m", required=True)
    chat.add_argument("--system", default=None)
    chat.add_argument("--conversation-id", default=None)
    chat.add_argument("--parent-message-id", default=None)
    chat.add_argument("--action", choices=["next", "continue", "variant"], default="next")
    chat.add_argument("--variant-purpose", default=None)
    chat.add_argument("--thinking-effort", default=None)
    chat.add_argument("--account", default=None)
    chat.add_argument("--accounts-dir", type=Path, default=None)
    chat.add_argument("--capture", type=Path, default=None)
    chat.add_argument("--use-captured-payload", action="store_true")
    chat.add_argument("--no-refresh-web-tokens", action="store_true")
    chat.add_argument("--impersonate", default="safari18_4")
    chat.add_argument("--max-events", type=int, default=None)
    chat.set_defaults(func=cmd_chat)

    image = subparsers.add_parser("image", help="Generate or edit an image")
    image.add_argument("--model", default=None)
    image.add_argument("--prompt", "-p", required=True)
    image.add_argument("--input-image", type=Path, action="append", default=[])
    image.add_argument("--aspect-ratio", choices=["auto", "1:1", "3:4", "9:16", "4:3", "16:9"], default="auto")
    image.add_argument("--out", type=Path, default=None)
    image.add_argument("--account", default=None)
    image.add_argument("--accounts-dir", type=Path, default=None)
    image.add_argument("--capture", type=Path, default=None)
    image.add_argument("--no-refresh-web-tokens", action="store_true")
    image.add_argument("--impersonate", default="safari18_4")
    image.set_defaults(func=cmd_image)

    vision = subparsers.add_parser("vision", help="OCR or describe up to 10 input images")
    vision.add_argument("--model", default=None)
    vision.add_argument("--mode", choices=["ocr", "describe", "custom"], default="custom")
    vision.add_argument("--prompt", "-p", default=None)
    vision.add_argument("--input-image", type=Path, action="append", default=[], required=True)
    vision.add_argument("--temporary-chat", action=argparse.BooleanOptionalAction, default=True)
    vision.add_argument("--account", default=None)
    vision.add_argument("--accounts-dir", type=Path, default=None)
    vision.add_argument("--capture", type=Path, default=None)
    vision.add_argument("--no-refresh-web-tokens", action="store_true")
    vision.add_argument("--impersonate", default="safari18_4")
    vision.set_defaults(func=cmd_vision)

    capture = subparsers.add_parser("inspect-capture", help="Inspect a copied ChatGPT request capture")
    capture.add_argument("path", type=Path, nargs="?")
    capture.add_argument("--account", default=None)
    capture.add_argument("--accounts-dir", type=Path, default=None)
    capture.set_defaults(func=cmd_inspect_capture)

    probe = subparsers.add_parser("probe-capture", help="Replay a copied ChatGPT request capture")
    probe.add_argument("path", type=Path, nargs="?")
    probe.add_argument("--account", default=None)
    probe.add_argument("--accounts-dir", type=Path, default=None)
    probe.add_argument("--message", default="hello from chatgpt-api probe")
    probe.add_argument("--model", default="auto")
    probe.add_argument("--conversation-id", default=None)
    probe.add_argument("--transport", choices=["curl_cffi", "httpx"], default="curl_cffi")
    probe.add_argument("--impersonate", default="chrome")
    probe.add_argument("--refresh-web-tokens", action="store_true")
    probe.add_argument("--timeout", type=float, default=60.0)
    probe.add_argument("--max-events", type=int, default=12)
    probe.set_defaults(func=cmd_probe_capture)

    server = subparsers.add_parser("server", help="Start or print commands for the main Bridge API server")
    server_subparsers = server.add_subparsers(required=True)

    server_start = server_subparsers.add_parser("start", help="Start the main Bridge API server")
    _add_serve_arguments(server_start)
    server_start.set_defaults(func=cmd_serve)

    server_command = server_subparsers.add_parser("command", help="Print a copy-paste server start command")
    server_command.add_argument("--preset", choices=["local", "lan", "docker"], default="local")
    server_command.add_argument("--accounts", default=os.environ.get("CHATGPT_ACCOUNTS", "free-main,pro-main"))
    server_command.add_argument("--api-key", default=os.environ.get("CHATGPT_API_KEY", "local-dev-key"))
    server_command.add_argument("--lan-base-url", default=os.environ.get("CHATGPT_PUBLIC_BASE_URL", "http://192.168.1.203:8000/v1"))
    server_command.set_defaults(func=cmd_server_command)

    serve = subparsers.add_parser("serve", help="Run the local ChatGPT Web bridge API server")
    _add_serve_arguments(serve)
    serve.set_defaults(func=cmd_serve)

    return parser


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for server settings before launch instead of relying only on flags/env vars.",
    )
    parser.add_argument("--account", default=os.environ.get("CHATGPT_ACCOUNT", "free"))
    parser.add_argument(
        "--accounts",
        default=os.environ.get("CHATGPT_ACCOUNTS", ""),
        help="Comma-separated local account aliases for multi-account failover/routing, for example free-main,pro-main.",
    )
    parser.add_argument(
        "--account-strategy",
        default=os.environ.get("CHATGPT_ACCOUNT_STRATEGY", "auto"),
        help="Account routing strategy: auto, sticky, failover, round-robin, weighted, quota-aware",
    )
    parser.add_argument("--accounts-dir", type=Path, default=_accounts_dir_default())
    parser.add_argument(
        "--secrets-passphrase-prompt",
        action="store_true",
        help=(
            "Prompt for the account-secrets passphrase at startup instead of using an "
            "auto-generated key file. The passphrase is held in memory only for this "
            "process, so a cold copy of this machine's disk (lost laptop, leaked backup) "
            "carries no usable key. Requires a TTY; not compatible with detached Docker."
        ),
    )
    parser.add_argument("--host", default=os.environ.get("CHATGPT_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHATGPT_API_PORT", "8000")))
    parser.add_argument("--api-key", default=os.environ.get("CHATGPT_API_KEY"))
    parser.add_argument(
        "--image-output-dir",
        type=Path,
        default=Path(os.environ.get("CHATGPT_IMAGE_OUTPUT_DIR", "outputs/chatgpt-images")),
        help="Directory for images generated through chat or /v1/images/generations",
    )
    parser.add_argument(
        "--research-output-dir",
        type=Path,
        default=Path(os.environ.get("CHATGPT_RESEARCH_OUTPUT_DIR", "outputs/chatgpt-research")),
        help="Directory for markdown reports generated by ChatGPT Deep Research",
    )
    parser.add_argument(
        "--admin-db-path",
        type=Path,
        default=Path(os.environ.get("CHATGPT_ADMIN_DB_PATH", "outputs/chatgpt-admin.sqlite")),
        help="SQLite metadata DB for the local bridge admin console",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("CHATGPT_PUBLIC_BASE_URL"),
        help=(
            "Public API base URL used in generated artifact download links. "
            "Use the LAN URL, for example http://192.168.1.203:8000/v1."
        ),
    )
    parser.add_argument("--impersonate", default=os.environ.get("CHATGPT_IMPERSONATE", "safari18_4"))
    parser.add_argument(
        "--web-timeout",
        type=float,
        default=float(os.environ.get("CHATGPT_WEB_TIMEOUT", "5400")),
        help="ChatGPT Web request timeout in seconds. Default is 5400 seconds for long Deep Research runs.",
    )
    parser.add_argument(
        "--chat-concurrency",
        default=os.environ.get("CHATGPT_CHAT_CONCURRENCY"),
        help="Local chat throttle by plan or account alias, for example free=1,go=2,plus=3,pro=4 or pro-main=4,work-pro=2.",
    )
    parser.add_argument(
        "--upload-concurrency",
        default=os.environ.get("CHATGPT_UPLOAD_CONCURRENCY"),
        help="Local upload throttle by plan/account alias, shared by OCR, describe, image edit, and composite. Default is free=1,go=1,plus=1,pro=1.",
    )
    parser.add_argument(
        "--image-concurrency",
        default=os.environ.get("CHATGPT_IMAGE_CONCURRENCY"),
        help="Local image throttle by plan/account alias, for example free=1,go=1,plus=2,pro=3 or pro-main=3.",
    )
    parser.add_argument(
        "--research-concurrency",
        default=os.environ.get("CHATGPT_RESEARCH_CONCURRENCY"),
        help="Local Deep Research throttle by plan/account alias, for example free=1,go=1,plus=2,pro=2 or pro-main=2.",
    )
    parser.add_argument(
        "--agent-mode",
        default=os.environ.get("CHATGPT_AGENT_MODE") or os.environ.get("CHATGPT_AGENT_PROMPT_MODE") or "optimized",
        help="Tool bridge prompt mode: optimized or opencode",
    )
    parser.add_argument(
        "--model-fallback",
        default=os.environ.get("CHATGPT_MODEL_FALLBACK", "auto"),
        help="Fallback ChatGPT model after recoverable model errors. Use 'none' to disable.",
    )
    parser.set_defaults(temporary_chat=_env_bool("CHATGPT_TEMPORARY_CHAT", True))
    parser.add_argument(
        "--temporary-chat",
        dest="temporary_chat",
        action="store_true",
        help="Send chat completions as ChatGPT temporary/private chats (default).",
    )
    parser.add_argument(
        "--normal-chat",
        dest="temporary_chat",
        action="store_false",
        help="Send chat completions as normal ChatGPT chats. Image generation always uses normal mode.",
    )


def _admin_base_url_default() -> str:
    return os.environ.get("CHATGPT_ADMIN_BASE_URL") or os.environ.get("CHATGPT_BASE_URL") or "http://127.0.0.1:8000/v1"


def _admin_api_key_default() -> str:
    return os.environ.get("CHATGPT_API_KEY", "local-dev-key")


def _accounts_dir_default() -> Path | None:
    value = os.environ.get("CHATGPT_ACCOUNTS_DIR", "").strip()
    return Path(value) if value else None


async def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, object]] = []
    accounts_dir = args.accounts_dir
    profiles = list_account_profiles(accounts_dir)

    checks.append(
        {
            "name": "python",
            "ok": sys.version_info >= (3, 11),
            "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
    )
    checks.append(
        {
            "name": "account_profiles",
            "ok": bool(profiles),
            "detail": f"{len(profiles)} profile(s) in {accounts_dir or 'default secrets/accounts'}",
        }
    )
    checks.append({"name": "dockerfile", "ok": Path("Dockerfile").exists(), "detail": "Dockerfile"})
    checks.append({"name": "dockerignore", "ok": Path(".dockerignore").exists(), "detail": ".dockerignore"})

    health = await _probe_json(_health_url(args.base_url), args.api_key, timeout=5.0)
    checks.append(
        {
            "name": "api_health",
            "ok": bool(health.get("ok")),
            "detail": health.get("detail") or health.get("status") or "-",
        }
    )
    models = await _probe_json(_admin_url(args.base_url, "/models"), args.api_key, timeout=5.0)
    model_count = 0
    if isinstance(models.get("payload"), dict) and isinstance(models["payload"].get("data"), list):
        model_count = len(models["payload"]["data"])
    checks.append(
        {
            "name": "api_models",
            "ok": bool(models.get("ok")),
            "detail": f"{model_count} model(s)" if models.get("ok") else models.get("detail") or "-",
        }
    )

    payload = {
        "object": "chatgpt.doctor",
        "ok": all(bool(item["ok"]) for item in checks if item["name"] != "api_health" and item["name"] != "api_models")
        and (bool(health.get("ok")) or not profiles),
        "base_url": args.base_url,
        "api_key": "<set>" if args.api_key else None,
        "accounts_dir": str(accounts_dir) if accounts_dir else None,
        "checks": checks,
        "next": {
            "start_server": "python3 -m chatgpt_api server start --accounts <account-names> --api-key <api-key>",
            "check_capacity": "python3 -m chatgpt_api admin capacity --base-url http://127.0.0.1:8000/v1 --api-key <api-key>",
            "docker": "docker compose up --build",
        },
    }
    if args.json:
        _print_json(payload)
        return 0 if payload["ok"] else 1

    print(_headline("ChatGPT API Doctor"))
    print(f"base_url   {_mono(args.base_url)}")
    print(f"api_key    {'set' if args.api_key else 'not set'}")
    print(f"accounts   {len(profiles)} profile(s)")
    print()
    for check in checks:
        print(f"{_status_word(bool(check['ok'])):<7} {check['name']:<17} {check.get('detail') or '-'}")
    print()
    print("next:")
    print("  python3 -m chatgpt_api server start --accounts <account-names> --api-key local-dev-key")
    print("  example account aliases: free-main,pro-main")
    print("  python3 -m chatgpt_api admin capacity --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key")
    print("  docker compose up --build")
    return 0 if payload["ok"] else 1


ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _validate_account_name(value: str) -> str:
    account = value.strip()
    if not ACCOUNT_NAME_RE.fullmatch(account):
        raise ProviderError(
            "account name must be an ASCII slug: English letters, numbers, dash, or underscore; "
            "examples: free-main, pro-main, free_2"
        )
    return account


def _prompt_text(label: str, *, default: str | None = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if value or allow_empty:
            return value
        print("value is required")


def _prompt_choice(label: str, choices: list[str], *, default: str) -> str:
    choice_text = "/".join(choices)
    while True:
        value = input(f"{label} ({choice_text}) [{default}]: ").strip() or default
        if value in choices:
            return value
        print(f"choose one of: {choice_text}")


def _prompt_menu_choice(max_choice: int) -> str:
    while True:
        value = input(_color("select", "1;36") + f" [1-{max_choice}, q, ?]: ").strip().lower()
        if value in {"q", "quit", "exit"}:
            return "quit"
        if value in {"", "?", "h", "help"}:
            return "help"
        if value.isdigit() and 1 <= int(value) <= max_choice:
            return value
        print(f"enter a number from 1-{max_choice}, q to quit, or ? to redraw the menu")


def _prompt_int(label: str, *, default: int) -> int:
    while True:
        value = input(f"{label} [{default}]: ").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            print("enter a number")


def _prompt_account_name(label: str = "account name/alias", *, default: str | None = None) -> str:
    while True:
        try:
            return _validate_account_name(_prompt_text(label, default=default))
        except ProviderError as exc:
            print(exc)


def _suggest_account_alias(profiles: list[str]) -> str:
    index = len(profiles) + 1
    while True:
        candidate = f"account-{index}"
        if candidate not in profiles:
            return candidate
        index += 1


def _prompt_new_account_alias(profiles: list[str]) -> str | None:
    print("Alias rules: English letters, numbers, dash, or underscore only.")
    print("Examples: free-main, pro-main, work-pro, free_2")
    print("Type `back` to cancel.")
    default = _suggest_account_alias(profiles)
    while True:
        value = input(f"new account alias [{default}]: ").strip()
        if value.lower() in {"back", "cancel"}:
            return None
        if not value:
            value = default
        try:
            account = _validate_account_name(value)
        except ProviderError as exc:
            print(exc)
            continue
        if account in profiles:
            print(f"`{account}` already exists. Use update, or choose a different alias.")
            continue
        return account


def _prompt_accounts_csv(label: str, *, default: str, profiles: list[str] | None = None) -> str:
    if profiles:
        print("available local account aliases:")
        for index, name in enumerate(profiles, start=1):
            print(f"  {index}. {name}")
        print("Use comma-separated aliases or numbers, for example 1,2 or free-main,pro-main.")
    while True:
        value = _prompt_text(label, default=default).strip()
        if not value:
            return ""
        try:
            selected = _resolve_account_selection(value, profiles or [])
            for item in selected:
                _validate_account_name(item)
            return ",".join(selected)
        except ProviderError as exc:
            print(exc)


def _prompt_account_from_list(
    label: str,
    *,
    profiles: list[str] | None = None,
    allow_all: bool = False,
    allow_new: bool = False,
    default: str | None = None,
) -> str:
    choices = list(profiles or [])
    if choices:
        print("available local account aliases:")
        for index, name in enumerate(choices, start=1):
            print(f"  {index}. {name}")
    if allow_all:
        print("  all. all accounts")
    if allow_new:
        print("  new. type a new local alias")
    while True:
        value = _prompt_text(label, default=default).strip()
        if allow_all and value == "all":
            return value
        if allow_new and value == "new":
            return _prompt_account_name("new local account alias")
        try:
            if value.isdigit() and choices:
                index = int(value)
                if 1 <= index <= len(choices):
                    return choices[index - 1]
            return _validate_account_name(value)
        except ProviderError as exc:
            print(exc)


def _resolve_account_selection(raw: str, profiles: list[str]) -> list[str]:
    selected: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item.isdigit() and profiles:
            index = int(item)
            if index < 1 or index > len(profiles):
                raise ProviderError(f"account number {index} is out of range")
            selected.append(profiles[index - 1])
        else:
            selected.append(_validate_account_name(item))
    if not selected:
        raise ProviderError("at least one account alias is required")
    return selected


def _prompt_account_name_or_all(*, default: str = "all") -> str:
    while True:
        value = _prompt_text("account name/alias, or all", default=default).strip()
        if value == "all":
            return value
        try:
            return _validate_account_name(value)
        except ProviderError as exc:
            print(exc)


def _prompt_capture_text(*, default_mode: str = "paste") -> str:
    mode = _prompt_choice("capture source", ["paste", "file"], default=default_mode)
    if mode == "file":
        while True:
            path_text = _prompt_text("capture file path")
            path = Path(path_text).expanduser()
            if path.exists():
                return path.read_text(encoding="utf-8")
            print(f"file not found: {path}")
    print()
    print(_headline("Paste ChatGPT Request Capture"))
    print("Paste all request headers plus Chrome Payload or Safari Request Data.")
    print("Nothing is saved until required fields pass inspection.")
    print("Finish with a line containing only END_CAPTURE.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END_CAPTURE":
            break
        lines.append(line)
    capture_text = "\n".join(lines).strip()
    if not capture_text:
        raise ProviderError("capture text is empty")
    return capture_text


async def cmd_menu(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        print("Interactive menu requires a TTY. Use `python3 -m chatgpt_api admin ...` commands in Docker or CI.", file=sys.stderr)
        return 2
    while True:
        _print_control_menu(args)
        choice = _prompt_menu_choice(15)
        if choice == "quit":
            return 0
        if choice == "help":
            continue
        print()
        try:
            result = await _run_menu_action(args, choice)
        except KeyboardInterrupt:
            print("\ncancelled")
            result = 130
        except ProviderError as exc:
            print(_color(f"error: {exc}", "1;31"))
            result = 2
        if result:
            print(_color(f"action finished with exit code {result}", "1;33"))
        _press_enter()


def _print_control_menu(args: argparse.Namespace) -> None:
    print()
    print(_headline("ChatGPT API Control Center"))
    print(f"API        {_mono(args.base_url)}")
    print(f"Bearer     {'set' if args.api_key else 'not set'}")
    print(f"Shortcut   {_mono('python3 -m chatgpt_api <command>')} works even when {_mono('chatgpt-api')} is not on PATH")
    print()
    _print_menu_group(
        "Observe",
        [
            ("1", "Doctor", "local setup, API health, Docker files"),
            ("2", "Status", "running server, routing, storage"),
            ("3", "Capacity", "parallel slots, quotas, routes"),
            ("4", "Usage", "live per-account limits and reset times"),
            ("5", "Models", "models exposed by the local API"),
        ],
    )
    _print_menu_group(
        "Accounts",
        [
            ("6", "List accounts", "saved aliases and capture paths"),
            ("7", "Verify accounts", "live-check one account or all"),
            ("8", "Add account", "paste Safari/Chrome capture; validates before save"),
            ("9", "Update account", "refresh an expired capture"),
            ("10", "Delete account", "remove capture/settings after confirmation"),
        ],
    )
    _print_menu_group(
        "Use and integrate",
        [
            ("11", "Artifacts", "saved images and research reports"),
            ("12", "Test chat", "send one smoke-test message"),
            ("13", "Test image", "generate one image and print its download path"),
            ("14", "opencode config", "status, inject, or eject consumer config"),
            ("15", "Launch presets", "copy-paste server commands"),
        ],
    )
    print()


def _print_menu_group(title: str, rows: list[tuple[str, str, str]]) -> None:
    print(_color(title.upper(), "1;34"))
    for number, label, description in rows:
        print(f"  {number:>2}. {label:<18} {description}")
    print()


async def _run_menu_action(args: argparse.Namespace, choice: str) -> int:
    if choice == "1":
        return await cmd_doctor(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, accounts_dir=_accounts_dir_default(), json=False))
    if choice == "2":
        return await cmd_admin_status(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, json=False))
    if choice == "3":
        return await cmd_admin_capacity(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, json=False))
    if choice == "4":
        return await cmd_admin_usage(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, json=False))
    if choice == "5":
        return await cmd_admin_models(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, json=False))
    if choice == "6":
        return await cmd_admin_accounts(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, json=False))
    if choice == "7":
        account = await _prompt_admin_account(args, "account alias to verify", allow_all=True, default="all")
        return await cmd_admin_check_accounts(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, account=account, json=False))
    if choice == "8":
        return await _menu_save_account_capture(args, action="add")
    if choice == "9":
        return await _menu_save_account_capture(args, action="update")
    if choice == "10":
        return await _menu_delete_account(args)
    if choice == "11":
        return await cmd_admin_artifacts(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, limit=25, json=False))
    if choice == "12":
        message = _prompt_text("test chat message", default="Say hello in one short sentence.")
        model = _prompt_text("model", default="auto")
        return await cmd_admin_test_chat(argparse.Namespace(base_url=args.base_url, api_key=args.api_key, message=message, model=model, json=False))
    if choice == "13":
        prompt = _prompt_text("image prompt", default="A tiny blue glass app icon, no text.")
        model = _prompt_text("model", default="auto")
        output_dir = _prompt_text("output dir (blank = server default)", default="", allow_empty=True) or None
        return await cmd_admin_test_image(
            argparse.Namespace(base_url=args.base_url, api_key=args.api_key, prompt=prompt, model=model, output_dir=output_dir, json=False)
        )
    if choice == "14":
        action = _prompt_choice("opencode action", ["status", "inject", "eject"], default="status")
        model = _prompt_text("opencode model", default="chatgpt-web/auto@optimized")
        config_path = _prompt_text("opencode config path (blank = default)", default="", allow_empty=True) or None
        return await cmd_admin_opencode(
            argparse.Namespace(base_url=args.base_url, api_key=args.api_key, action=action, model=model, config_path=config_path, json=False)
        )
    if choice == "15":
        return await cmd_admin_presets(
            argparse.Namespace(
                accounts=os.environ.get("CHATGPT_ACCOUNTS", "free-main,pro-main"),
                api_key=args.api_key,
                lan_host="0.0.0.0",
                lan_base_url=os.environ.get("CHATGPT_PUBLIC_BASE_URL", "http://192.168.1.203:8000/v1"),
            )
        )
    return 1


async def _menu_save_account_capture(args: argparse.Namespace, *, action: str) -> int:
    profiles = await _admin_account_aliases(args)
    print(_headline("Account Capture Wizard"))
    print("Use a local alias such as free-main, pro-main, work-pro, or test-free.")
    print("The capture is inspected before save, then live-verified unless you use the non-interactive flags.")
    print()
    if action == "add":
        account = _prompt_new_account_alias(profiles)
        if account is None:
            print("cancelled")
            return 0
    else:
        if not profiles:
            print("No saved accounts exist yet. Choose Add account first.")
            return 1
        account = _prompt_account_from_list("account alias to update", profiles=profiles)
    capture_text = _prompt_capture_text(default_mode="paste")
    return await _save_capture_text_with_verification(
        argparse.Namespace(
            base_url=args.base_url,
            api_key=args.api_key,
            no_live_verify=False,
            json=False,
            account_action=action,
        ),
        account,
        capture_text,
    )


async def _menu_delete_account(args: argparse.Namespace) -> int:
    account = await _prompt_admin_account(args, "account alias to delete")
    print(f"This removes local capture/settings for `{account}`.")
    confirm = input("type DELETE to confirm, anything else to cancel: ").strip()
    if confirm != "DELETE":
        print("cancelled")
        return 0
    return await cmd_admin_delete_account(
        argparse.Namespace(
            base_url=args.base_url,
            api_key=args.api_key,
            account=account,
            keep_capture=False,
            keep_settings=False,
            json=False,
        )
    )


def _press_enter() -> None:
    if sys.stdin.isatty():
        input(_color("\npress Enter to return to the menu...", "2"))


async def cmd_server_command(args: argparse.Namespace) -> int:
    if args.preset == "docker":
        print("docker compose up --build")
        print("docker run --rm -p 8000:8000 --env-file .env -v \"$PWD/secrets/accounts:/data/secrets/accounts:ro\" -v \"$PWD/outputs:/data/outputs\" chatgpt-api:local")
        return 0
    if args.preset == "lan":
        print(
            "CHATGPT_API_KEY={key} python3 -m chatgpt_api server start "
            "--accounts {accounts} --account-strategy failover --host 0.0.0.0 --port 8000 "
            "--public-base-url {base}"
            .format(key=args.api_key, accounts=args.accounts, base=args.lan_base_url)
        )
        return 0
    print(
        "CHATGPT_API_KEY={key} python3 -m chatgpt_api server start "
        "--accounts {accounts} --account-strategy failover --host 127.0.0.1 --port 8000 "
        "--public-base-url http://127.0.0.1:8000/v1"
        .format(key=args.api_key, accounts=args.accounts)
    )
    return 0


async def cmd_providers(args: argparse.Namespace) -> int:
    for name in default_registry.names():
        provider = default_registry.create(name)
        caps = provider.capabilities
        print(
            f"{name}: chat={caps.chat} stream={caps.streaming} "
            f"image_generation={caps.image_generation} image_edit={caps.image_edit} vision={caps.vision}"
        )
    return 0


async def cmd_accounts(args: argparse.Namespace) -> int:
    profiles = list_account_profiles(args.accounts_dir)
    if not profiles:
        print("no account profiles found")
        return 0
    for profile in profiles:
        print(
            f"{profile.name}: capture={'yes' if profile.exists else 'no'} "
            f"settings={'yes' if profile.has_settings else 'no'} path={profile.capture_path}"
        )
    return 0


async def cmd_secrets_rotate(args: argparse.Namespace) -> int:
    accounts_dir = args.accounts_dir or accounts_dir_from_env()

    if args.from_passphrase_prompt:
        set_runtime_passphrase(getpass.getpass("current passphrase: "))
        old_key = load_secrets_key(accounts_dir)
        clear_runtime_passphrase()
    else:
        old_key = load_secrets_key(accounts_dir)

    profiles = list_account_profiles(accounts_dir)
    if not profiles:
        print("no account profiles found")
        return 0

    switching_to_passphrase = bool(args.to_passphrase_prompt)
    if switching_to_passphrase:
        new_passphrase = getpass.getpass("new passphrase: ")
        if not new_passphrase:
            raise ProviderError("new passphrase must not be empty")
        if getpass.getpass("confirm new passphrase: ") != new_passphrase:
            raise ProviderError("passphrases did not match")
        set_runtime_passphrase(new_passphrase)
        new_key = load_secrets_key(accounts_dir)
        clear_runtime_passphrase()
    else:
        key_path = key_file_path(accounts_dir)
        if key_path.exists():
            key_path.unlink()
        new_key = load_secrets_key(accounts_dir)

    rotated = 0
    for profile in profiles:
        if reencrypt_file(profile.capture_path, old_key, new_key):
            rotated += 1
            print(f"{profile.name}: rotated")
        elif profile.exists:
            print(f"{profile.name}: skipped (capture is plaintext; update it once to encrypt)")
        else:
            print(f"{profile.name}: skipped (no capture file)")
    print(f"rotated {rotated} of {len(profiles)} account capture(s)")

    if switching_to_passphrase:
        key_path = key_file_path(accounts_dir)
        if key_path.exists():
            key_path.unlink()
            print(f"removed {key_path}: captures now require the passphrase, not a key file")
    return 0


async def cmd_account_info(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    settings = _load_account_settings(args.settings, args.account, args.accounts_dir, args.no_settings)
    detected = detect_account_info(capture, settings).to_redacted_dict()
    if args.json:
        print(json.dumps(detected, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"capture={path}")
    print(f"plan={detected['plan_type'] or '-'}")
    print(f"plan_bucket={detected['plan_bucket'] or '-'}")
    print(f"email={detected['email'] or '-'}")
    print(f"account_id={detected['account_id'] or '-'}")
    print(f"token_expires_at={detected['token_expires_at'] or '-'}")
    print(f"observed_models={_csv_or_dash(detected['observed_models'])}")
    print(f"observed_efforts={_csv_or_dash(detected['observed_efforts'])}")
    print(f"observed_actions={_csv_or_dash(detected['observed_actions'])}")
    print(f"request_model={detected['request_model'] or '-'}")
    print(f"request_thinking_effort={detected['request_thinking_effort'] or '-'}")
    print(f"settings_default_model_slug={detected['settings_default_model_slug'] or '-'}")
    print(f"settings_available_reasoning_efforts={_csv_or_dash(detected['settings_available_reasoning_efforts'])}")
    print(f"settings_wingman_thinking_effort={detected['settings_wingman_thinking_effort'] or '-'}")
    print(f"settings_last_used_slugs={json.dumps(detected['settings_last_used_slugs'], ensure_ascii=False, sort_keys=True)}")
    print(f"settings_last_used_juices={json.dumps(detected['settings_last_used_juices'], ensure_ascii=False, sort_keys=True)}")
    print("scope=capture-derived; not an exhaustive live capability matrix")
    return 0


async def cmd_account_capabilities(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    settings = _load_account_settings(args.settings, args.account, args.accounts_dir, args.no_settings)
    info = detect_account_info(capture, settings)
    capabilities = infer_account_capabilities(info)
    if args.json:
        print(json.dumps(capabilities, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"capture={path}")
    print(f"plan={capabilities['plan_type'] or '-'}")
    print(f"plan_bucket={capabilities['plan_bucket'] or '-'}")
    print(f"supported_models={_csv_or_dash(capabilities['supported_models'])}")
    print(f"default_model={capabilities['default_model'] or '-'}")
    print(f"thinking_model={capabilities['thinking_model'] or '-'}")
    print(f"thinking_efforts={_csv_or_dash(capabilities['thinking_efforts'])}")
    print(f"pro_model={capabilities['pro_model'] or '-'}")
    print(f"pro_efforts={_csv_or_dash(capabilities['pro_efforts'])}")
    if capabilities.get("auto_only"):
        print("model_policy=auto-only")
        print(f"model_policy_reason={capabilities.get('auto_only_reason') or '-'}")
    print(f"backend_reasoning_efforts={_csv_or_dash(capabilities['backend_reasoning_efforts'])}")
    print(f"extra_observed_models={_csv_or_dash(capabilities['extra_observed_models'])}")
    print(f"scope={capabilities['scope']}")
    return 0


async def cmd_account_models(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    payload = await asyncio.to_thread(_fetch_account_models, capture, args.impersonate)
    picker = parse_model_picker(payload)
    if args.json:
        print(json.dumps(picker.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"capture={path}")
    print(f"default_model_slug={picker.default_model_slug or '-'}")
    print(f"model_picker_version={picker.model_picker_version if picker.model_picker_version is not None else '-'}")
    print(f"models={_csv_or_dash(picker.model_slugs)}")
    for version in picker.versions:
        if not version.enabled:
            continue
        print(f"version={version.id} display={version.display_text} slugs={_csv_or_dash(version.slugs)}")
        for preset in version.presets:
            effort = f" effort={preset.thinking_effort}" if preset.thinking_effort else ""
            print(f"  preset={preset.title} lane={preset.lane} model={preset.model_slug}{effort}")
    return 0


async def cmd_account_check(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    detected = detect_account_info(capture).to_redacted_dict()
    status, auth_ok, detail = await asyncio.to_thread(
        _check_account_auth,
        capture,
        args.impersonate,
    )
    print(f"capture={path}")
    print(f"plan={detected['plan_type'] or '-'}")
    print(f"plan_bucket={detected['plan_bucket'] or '-'}")
    print(f"status={status if status is not None else '-'}")
    print(f"auth_ok={str(auth_ok).lower()}")
    if detail:
        print(f"detail={detail}")
    return 0 if auth_ok else 1


async def cmd_account_limits(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    auth = ChatGPTAuthConfig.from_captured_request(CapturedRequest.from_file(path))
    transport = ChatGPTWebTransport(auth, impersonate=args.impersonate)
    payload = await asyncio.to_thread(
        transport.conversation_init,
        args.requested_default_model,
        args.conversation_id,
        args.conversation_origin,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    timezone_payload = local_timezone_payload()
    print(f"capture={path}")
    print(f"timezone={timezone_payload['timezone']}")
    print(f"timezone_offset_min={timezone_payload['timezone_offset_min']}")
    print(f"default_model_slug={payload.get('default_model_slug') or '-'}")
    print("model_limits:")
    model_limits = payload.get("model_limits")
    if isinstance(model_limits, list) and model_limits:
        for item in model_limits:
            if not isinstance(item, dict):
                continue
            print(
                "  "
                f"model={item.get('model_slug') or '-'} "
                f"using_default={item.get('using_default_model_slug') or '-'} "
                f"resets_after={item.get('resets_after') or '-'} "
                f"description={item.get('description') or '-'}"
            )
    else:
        print("  -")

    print("blocked_features:")
    blocked_features = payload.get("blocked_features")
    if isinstance(blocked_features, list) and blocked_features:
        for item in blocked_features:
            if not isinstance(item, dict):
                continue
            print(
                "  "
                f"name={item.get('name') or '-'} "
                f"resets_after={item.get('resets_after') or '-'} "
                f"description={item.get('description') or '-'}"
            )
    else:
        print("  -")

    print("limits_progress:")
    limits_progress = payload.get("limits_progress")
    if isinstance(limits_progress, list) and limits_progress:
        for item in limits_progress:
            if not isinstance(item, dict):
                continue
            print(
                "  "
                f"feature={item.get('feature_name') or '-'} "
                f"remaining={item.get('remaining') if item.get('remaining') is not None else '-'} "
                f"reset_after={item.get('reset_after') or '-'}"
            )
    else:
        print("  -")
    return 0


async def cmd_admin_status(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "GET", "/chatgpt/admin/status")
    if args.json:
        _print_json(payload)
        return 0
    server = payload.get("server", {})
    routing = payload.get("routing", {})
    storage = payload.get("storage", {})
    print(f"ok={str(payload.get('ok')).lower()}")
    print(f"base_url={server.get('public_base_url') or server.get('base_url') or '-'}")
    print(f"auth={server.get('auth_mode') or '-'}")
    print(f"accounts={_csv_or_dash(routing.get('accounts') or [])}")
    print(f"strategy={routing.get('account_strategy') or '-'}")
    print(f"chat_concurrency={json.dumps(routing.get('account_concurrency') or {}, ensure_ascii=False, sort_keys=True)}")
    print(f"feature_concurrency={json.dumps(routing.get('feature_concurrency') or {}, ensure_ascii=False, sort_keys=True)}")
    print(f"temporary_chat={str(routing.get('temporary_chat')).lower()}")
    print(f"storage_db={storage.get('admin_db_path') or '-'}")
    print(f"artifacts={storage.get('artifact_count') if storage.get('artifact_count') is not None else '-'}")
    return 0


async def cmd_admin_usage(args: argparse.Namespace) -> int:
    if args.json:
        _print_json(await _admin_request(args, "GET", "/chatgpt/usage"))
        return 0
    text = await _admin_request_text(args, "GET", "/chatgpt/usage?format=table")
    print(text.rstrip())
    return 0


async def cmd_admin_models(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "GET", "/models")
    if args.json:
        _print_json(payload)
        return 0
    models = payload.get("data") if isinstance(payload.get("data"), list) else []
    print(_headline("Exposed Models"))
    for item in models:
        if not isinstance(item, dict):
            continue
        print(f"{item.get('id') or '-':<36} {item.get('owned_by') or '-'}")
    return 0


async def cmd_admin_capacity(args: argparse.Namespace) -> int:
    status = await _admin_request(args, "GET", "/chatgpt/admin/status")
    usage = await _admin_request(args, "GET", "/chatgpt/usage")
    models = await _admin_request(args, "GET", "/models")
    payload = _capacity_payload(status, usage, models)
    if args.json:
        _print_json(payload)
        return 0
    _print_capacity(payload)
    return 0


async def cmd_admin_settings(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "GET", "/chatgpt/admin/settings")
    settings = payload.get("settings", payload)
    if args.json:
        _print_json(settings)
        return 0
    _print_settings_summary(settings)
    return 0


async def cmd_admin_set_limits(args: argparse.Namespace) -> int:
    current = await _admin_request(args, "GET", "/chatgpt/admin/settings")
    settings = current.get("settings") if isinstance(current.get("settings"), dict) else {}
    settings = json.loads(json.dumps(settings))
    concurrency = settings.setdefault("concurrency", {})
    for feature, raw in (("chat", args.chat), ("upload", args.upload), ("image", args.image), ("research", args.research)):
        if raw is None:
            continue
        feature_settings = concurrency.setdefault(feature, {"plans": {}, "accounts": {}})
        plan_limits, account_limits = _parse_admin_limit_spec(raw)
        feature_settings.setdefault("plans", {}).update(plan_limits)
        feature_settings.setdefault("accounts", {}).update(account_limits)
    payload = await _admin_request(args, "POST", "/chatgpt/admin/settings/save", {"settings": settings})
    if args.json:
        _print_json(payload)
        return 0
    print("saved=true")
    _print_settings_summary(payload.get("settings", settings))
    return 0


async def cmd_admin_reset_settings(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "POST", "/chatgpt/admin/settings/reset", {})
    if args.json:
        _print_json(payload)
        return 0
    print("reset=true")
    _print_settings_summary(payload.get("settings", {}))
    return 0


async def cmd_admin_accounts(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "GET", "/chatgpt/admin/accounts")
    if args.json:
        _print_json(payload)
        return 0
    for account in payload.get("accounts", []):
        if not isinstance(account, dict):
            continue
        print(
            f"{account.get('account')}: "
            f"configured={'yes' if account.get('configured') else 'no'} "
            f"capture={'yes' if account.get('capture_exists') else 'no'} "
            f"settings={'yes' if account.get('settings_exists') else 'no'} "
            f"plan={account.get('plan_type') or account.get('plan_bucket') or '-'} "
            f"path={account.get('capture_path') or '-'}"
        )
    return 0


async def cmd_admin_check_accounts(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "POST", "/chatgpt/admin/accounts/check", {"account": args.account})
    if args.json:
        _print_json(payload)
        return 0
    for account in payload.get("accounts", []):
        if not isinstance(account, dict):
            continue
        features = account.get("features") if isinstance(account.get("features"), dict) else {}
        research = features.get("deep_research") if isinstance(features.get("deep_research"), dict) else {}
        image = features.get("image_gen") if isinstance(features.get("image_gen"), dict) else {}
        print(
            f"{account.get('account')}: "
            f"ok={'yes' if account.get('ok') else 'no'} "
            f"plan={account.get('plan_type') or account.get('plan_bucket') or '-'} "
            f"default={account.get('default_model_slug') or '-'} "
            f"research={research.get('remaining') if research.get('remaining') is not None else '-'} "
            f"image={image.get('remaining') if image.get('remaining') is not None else '-'} "
            f"error={account.get('error') or '-'}"
        )
    return 0


async def cmd_admin_account_list(args: argparse.Namespace) -> int:
    return await cmd_admin_accounts(args)


async def cmd_admin_account_verify(args: argparse.Namespace) -> int:
    return await cmd_admin_check_accounts(args)


async def cmd_admin_account_save(args: argparse.Namespace) -> int:
    return await _save_capture_with_verification(args)


async def cmd_admin_account_delete(args: argparse.Namespace) -> int:
    return await cmd_admin_delete_account(args)


async def cmd_admin_save_capture(args: argparse.Namespace) -> int:
    return await _save_capture_with_verification(args)


async def _save_capture_with_verification(args: argparse.Namespace) -> int:
    account = _account_name_from_args(args)
    capture_text = _capture_text_from_args(args)
    return await _save_capture_text_with_verification(args, account, capture_text)


async def _save_capture_text_with_verification(args: argparse.Namespace, account: str, capture_text: str) -> int:
    inspect_payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/captures/inspect",
        {"account": account, "capture_text": capture_text},
    )
    failed_checks = _failed_capture_checks(inspect_payload)
    if failed_checks:
        if args.json:
            _print_json(
                {
                    "ok": False,
                    "saved": False,
                    "phase": "inspect",
                    "failed": failed_checks,
                    "inspection": inspect_payload,
                }
            )
        else:
            print("saved=false")
            print("phase=inspect")
            print(f"account={account}")
            print(f"failed={_csv_or_dash(failed_checks)}")
            print(f"missing={_csv_or_dash(inspect_payload.get('missing') or [])}")
            for warning in inspect_payload.get("warnings") or []:
                print(f"warning={warning}")
        return 1

    payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/captures/save",
        {"account": account, "capture_text": capture_text},
    )
    verify_payload: dict[str, object] | None = None
    verify_ok = True
    if not getattr(args, "no_live_verify", False):
        verify_payload = await _admin_request(args, "POST", "/chatgpt/admin/accounts/check", {"account": account})
        verify_ok = _account_verify_ok(verify_payload, account)
    if args.json:
        _print_json(
            {
                "ok": bool(verify_ok),
                "saved": True,
                "inspection": payload.get("inspection"),
                "capture_path": payload.get("capture_path"),
                "live_verify": verify_payload,
            }
        )
        return 0 if verify_ok else 2
    inspection = payload.get("inspection") if isinstance(payload.get("inspection"), dict) else payload
    print("saved=true")
    print(f"account={account}")
    print(f"plan={inspection.get('plan_type') or inspection.get('plan_bucket') or '-'}")
    print(f"capture_path={payload.get('capture_path') or '-'}")
    print(f"missing={_csv_or_dash(inspection.get('missing') or [])}")
    if verify_payload is not None:
        print(f"live_verify={'ok' if verify_ok else 'failed'}")
        for entry in verify_payload.get("accounts", []):
            if isinstance(entry, dict) and entry.get("account") == account and not entry.get("ok"):
                print(f"live_error={entry.get('error') or entry.get('warning') or entry.get('status') or '-'}")
    return 0 if verify_ok else 2


def _account_name_from_args(args: argparse.Namespace) -> str:
    value = getattr(args, "account", None)
    if isinstance(value, str) and value.strip():
        return _validate_account_name(value)
    if sys.stdin.isatty() and not getattr(args, "json", False):
        return _prompt_account_name()
    raise ProviderError(
        "pass --account <account-name>; account names are local aliases like free-main, pro-main, or work-pro, not plan selectors"
    )


def _capture_text_from_args(args: argparse.Namespace) -> str:
    capture_file = getattr(args, "capture_file", None)
    if capture_file is not None:
        return Path(capture_file).expanduser().read_text(encoding="utf-8")
    if getattr(args, "paste", False) or (sys.stdin.isatty() and not getattr(args, "json", False)):
        return _prompt_capture_text()
    raise ProviderError("pass --capture-file <path> or run interactively with --paste")


def _failed_capture_checks(inspection: dict[str, object]) -> list[str]:
    checks = inspection.get("checks")
    if not isinstance(checks, list):
        return ["checks"]
    failed: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("level") in {"required", "recommended"} and not check.get("ok"):
            failed.append(str(check.get("name") or "unknown"))
    return failed


def _account_verify_ok(payload: dict[str, object], account_name: str) -> bool:
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return False
    for account in accounts:
        if isinstance(account, dict) and account.get("account") == account_name:
            return bool(account.get("ok"))
    return False


async def cmd_admin_delete_account(args: argparse.Namespace) -> int:
    account = _account_name_from_args(args)
    payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/accounts/delete",
        {
            "account": account,
            "delete_capture": not args.keep_capture,
            "delete_settings": not args.keep_settings,
        },
    )
    if args.json:
        _print_json(payload)
        return 0
    print(f"deleted={json.dumps(payload.get('deleted') or {}, ensure_ascii=False, sort_keys=True)}")
    print(f"paths={json.dumps(payload.get('paths') or {}, ensure_ascii=False, sort_keys=True)}")
    return 0


async def cmd_admin_artifacts(args: argparse.Namespace) -> int:
    payload = await _admin_request(args, "GET", f"/chatgpt/admin/artifacts?limit={max(1, args.limit)}")
    if args.json:
        _print_json(payload)
        return 0
    for artifact in payload.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        print(
            f"{artifact.get('file_id') or artifact.get('id')}: "
            f"{artifact.get('kind') or '-'} "
            f"{artifact.get('filename') or '-'} "
            f"{artifact.get('bytes') if artifact.get('bytes') is not None else '-'} bytes "
            f"{artifact.get('download_url') or artifact.get('path') or '-'}"
        )
    return 0


async def cmd_admin_delete_artifact(args: argparse.Namespace) -> int:
    payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/artifacts/delete",
        {"file_id": args.file_id, "delete_file": args.delete_file},
    )
    if args.json:
        _print_json(payload)
        return 0
    print(f"deleted={json.dumps(payload.get('deleted') or {}, ensure_ascii=False, sort_keys=True)}")
    return 0


async def cmd_admin_opencode(args: argparse.Namespace) -> int:
    if args.action == "status":
        payload = await _admin_request(args, "GET", "/chatgpt/admin/opencode")
    elif args.action == "inject":
        payload = await _admin_request(
            args,
            "POST",
            "/chatgpt/admin/opencode/inject",
            {
                "base_url": args.base_url,
                "api_key": args.api_key,
                "model": args.model,
                "config_path": args.config_path,
            },
        )
    else:
        payload = await _admin_request(args, "POST", "/chatgpt/admin/opencode/eject", {"config_path": args.config_path})
    if args.json:
        _print_json(payload)
        return 0
    print(f"action={args.action}")
    print(f"config_path={payload.get('config_path') or '-'}")
    print(f"injected={'yes' if payload.get('injected') else 'no'}")
    print(f"model={payload.get('model') or args.model or '-'}")
    print(f"base_url={payload.get('base_url') or args.base_url}")
    return 0


async def cmd_admin_test_chat(args: argparse.Namespace) -> int:
    payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/test/chat",
        {"model": args.model, "message": args.message},
    )
    if args.json:
        _print_json(payload)
        return 0
    print(f"ok={'yes' if payload.get('ok') else 'no'}")
    print(f"latency_ms={payload.get('latency_ms') if payload.get('latency_ms') is not None else '-'}")
    print(payload.get("content") or "")
    return 0


async def cmd_admin_test_image(args: argparse.Namespace) -> int:
    payload = await _admin_request(
        args,
        "POST",
        "/chatgpt/admin/test/image",
        {"model": args.model, "prompt": args.prompt, "output_dir": args.output_dir},
    )
    if args.json:
        _print_json(payload)
        return 0
    print(f"ok={'yes' if payload.get('ok') else 'no'}")
    print(f"latency_ms={payload.get('latency_ms') if payload.get('latency_ms') is not None else '-'}")
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    for item in response.get("data", []):
        if isinstance(item, dict):
            print(item.get("url") or item.get("download_url") or item.get("path") or "-")
    return 0


async def cmd_admin_presets(args: argparse.Namespace) -> int:
    print("Local single-machine dev:")
    print(
        "  "
        "CHATGPT_API_KEY={key} python3 -m chatgpt_api serve "
        "--accounts {accounts} --account-strategy failover --host 127.0.0.1 --port 8000 "
        "--public-base-url http://127.0.0.1:8000/v1"
        .format(key=args.api_key, accounts=args.accounts)
    )
    print("\nLAN API server:")
    print(
        "  "
        "CHATGPT_API_KEY={key} python3 -m chatgpt_api serve "
        "--accounts {accounts} --account-strategy failover --host {host} --port 8000 "
        "--public-base-url {base}"
        .format(key=args.api_key, accounts=args.accounts, host=args.lan_host, base=args.lan_base_url)
    )
    print("\nDocker-style environment:")
    print(
        "  "
        "CHATGPT_API_KEY={key} CHATGPT_ACCOUNTS={accounts} CHATGPT_API_HOST=0.0.0.0 "
        "CHATGPT_API_PORT=8000 CHATGPT_PUBLIC_BASE_URL={base} "
        "CHATGPT_CHAT_CONCURRENCY=free=1,go=2,plus=3,pro=4 "
        "CHATGPT_UPLOAD_CONCURRENCY=free=1,go=1,plus=1,pro=1 "
        "CHATGPT_IMAGE_CONCURRENCY=free=1,go=1,plus=2,pro=3 "
        "CHATGPT_RESEARCH_CONCURRENCY=free=1,go=1,plus=2,pro=2 "
        "python3 -m chatgpt_api serve"
        .format(key=args.api_key, accounts=args.accounts, base=args.lan_base_url)
    )
    print("\nManage while running:")
    print("  python3 -m chatgpt_api admin status --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key")
    print("  python3 -m chatgpt_api admin set-limits --upload pro=1 --image pro=3 --research pro=2 --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key")
    print("  python3 -m chatgpt_api admin opencode inject --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key")
    return 0


async def _admin_request(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    text = await _admin_request_text(args, method, path, body)
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise ProviderError(f"admin API returned non-JSON response: {text[:300]}") from exc
    if not isinstance(payload, dict):
        raise ProviderError("admin API returned an unexpected JSON payload")
    return payload


async def _admin_request_text(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> str:
    url = _admin_url(args.base_url, path)
    headers: dict[str, str] = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.request(method, url, headers=headers, json=body if body is not None else None)
    text = response.text
    if response.status_code >= 400:
        message = text
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict) and error.get("message"):
                    message = str(error["message"])
        except Exception:
            pass
        raise ProviderError(f"admin API {method} {path} failed: HTTP {response.status_code}: {message}")
    return text


async def _admin_account_aliases(args: argparse.Namespace) -> list[str]:
    try:
        payload = await _admin_request(args, "GET", "/chatgpt/admin/accounts")
    except ProviderError:
        return []
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return []
    aliases: list[str] = []
    for item in accounts:
        if isinstance(item, dict) and isinstance(item.get("account"), str):
            aliases.append(item["account"])
    return aliases


async def _prompt_admin_account(
    args: argparse.Namespace,
    label: str,
    *,
    allow_all: bool = False,
    default: str | None = None,
) -> str:
    profiles = await _admin_account_aliases(args)
    return _prompt_account_from_list(label, profiles=profiles, allow_all=allow_all, default=default)


def _admin_url(base_url: str, path: str) -> str:
    base = (base_url or "http://127.0.0.1:8000/v1").rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def _health_url(base_url: str) -> str:
    base = (base_url or "http://127.0.0.1:8000/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/health"


async def _probe_json(url: str, api_key: str | None, *, timeout: float) -> dict[str, object]:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    if response.status_code >= 400:
        detail = response.text[:240].replace("\n", " ")
        return {"ok": False, "status": response.status_code, "detail": detail}
    try:
        payload = response.json()
    except Exception as exc:
        return {"ok": False, "status": response.status_code, "detail": f"non-JSON response: {type(exc).__name__}"}
    return {"ok": True, "status": response.status_code, "payload": payload}


def _capacity_payload(
    status: dict[str, object],
    usage: dict[str, object],
    models: dict[str, object],
) -> dict[str, object]:
    routing = status.get("routing") if isinstance(status.get("routing"), dict) else {}
    storage = status.get("storage") if isinstance(status.get("storage"), dict) else {}
    routes = status.get("routes") if isinstance(status.get("routes"), dict) else {}
    accounts = [str(item) for item in routing.get("accounts", [])] if isinstance(routing.get("accounts"), list) else []
    chat_limits = routing.get("account_concurrency") if isinstance(routing.get("account_concurrency"), dict) else {}
    feature_limits = routing.get("feature_concurrency") if isinstance(routing.get("feature_concurrency"), dict) else {}
    model_ids = [
        str(item.get("id"))
        for item in (models.get("data") if isinstance(models.get("data"), list) else [])
        if isinstance(item, dict) and item.get("id")
    ]
    per_account = []
    for account in accounts:
        upload_limits = feature_limits.get("upload") if isinstance(feature_limits.get("upload"), dict) else {}
        image_limits = feature_limits.get("image") if isinstance(feature_limits.get("image"), dict) else {}
        research_limits = feature_limits.get("research") if isinstance(feature_limits.get("research"), dict) else {}
        per_account.append(
            {
                "account": account,
                "chat_parallel": int(chat_limits.get(account, 1) or 1) if isinstance(chat_limits, dict) else 1,
                "upload_parallel": int(upload_limits.get(account, 0) or 0) if isinstance(upload_limits, dict) else 0,
                "image_parallel": int(image_limits.get(account, 0) or 0) if isinstance(image_limits, dict) else 0,
                "research_parallel": int(research_limits.get(account, 0) or 0) if isinstance(research_limits, dict) else 0,
                "file_upload_quota": _feature_usage_summary(usage, account, "file_upload"),
                "image_quota": _feature_usage_summary(usage, account, "image_gen"),
                "research_quota": _feature_usage_summary(usage, account, "deep_research"),
            }
        )
    return {
        "object": "chatgpt.capacity",
        "ok": bool(status.get("ok", True)),
        "strategy": routing.get("account_strategy"),
        "temporary_chat": routing.get("temporary_chat"),
        "models": model_ids,
        "model_count": len(model_ids),
        "accounts": per_account,
        "totals": {
            "chat_parallel": sum(int(item["chat_parallel"]) for item in per_account),
            "upload_parallel": sum(int(item["upload_parallel"]) for item in per_account),
            "image_parallel": sum(int(item["image_parallel"]) for item in per_account),
            "research_parallel": sum(int(item["research_parallel"]) for item in per_account),
        },
        "storage": {
            "image_output_dir": storage.get("image_output_dir"),
            "research_output_dir": storage.get("research_output_dir"),
            "admin_db_path": storage.get("admin_db_path"),
            "artifact_count": storage.get("artifact_count"),
        },
        "routes": routes,
    }


def _feature_usage_summary(usage: dict[str, object], account: str, feature_name: str) -> dict[str, object]:
    entries = usage.get("accounts") if isinstance(usage.get("accounts"), list) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("account") != account:
            continue
        features = entry.get("features") if isinstance(entry.get("features"), dict) else {}
        feature = features.get(feature_name) if isinstance(features.get(feature_name), dict) else None
        if feature is None:
            return {"reported": False}
        return {
            "reported": True,
            "remaining": feature.get("remaining"),
            "reset_after": feature.get("reset_after") or feature.get("resets_after"),
            "blocked": bool(feature.get("blocked")),
        }
    return {"reported": False}


def _print_capacity(payload: dict[str, object]) -> None:
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    routes = payload.get("routes") if isinstance(payload.get("routes"), dict) else {}
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    print(_headline("Bridge Capacity"))
    print(f"strategy         {payload.get('strategy') or '-'}")
    print(f"models           {payload.get('model_count') or 0}")
    print(f"chat_parallel    {totals.get('chat_parallel') if totals.get('chat_parallel') is not None else '-'}")
    print(f"upload_parallel  {totals.get('upload_parallel') if totals.get('upload_parallel') is not None else '-'}")
    print(f"image_parallel   {totals.get('image_parallel') if totals.get('image_parallel') is not None else '-'}")
    print(f"research_parallel {totals.get('research_parallel') if totals.get('research_parallel') is not None else '-'}")
    print()
    print(f"{'ACCOUNT':<18} {'CHAT':>5} {'UPLOAD':>6} {'IMAGE':>5} {'RESEARCH':>8}  {'FILE QUOTA':<24} {'IMAGE QUOTA':<28} {'RESEARCH QUOTA'}")
    for account in payload.get("accounts", []):
        if not isinstance(account, dict):
            continue
        print(
            f"{account.get('account') or '-':<18} "
            f"{account.get('chat_parallel')!s:>5} "
            f"{account.get('upload_parallel')!s:>6} "
            f"{account.get('image_parallel')!s:>5} "
            f"{account.get('research_parallel')!s:>8}  "
            f"{_quota_text(account.get('file_upload_quota')):<24} "
            f"{_quota_text(account.get('image_quota')):<28} "
            f"{_quota_text(account.get('research_quota'))}"
        )
    print()
    print("routes:")
    for key in ("chat", "images", "usage", "models", "files"):
        if routes.get(key):
            print(f"  {key:<8} {routes[key]}")
    print("storage:")
    for key in ("image_output_dir", "research_output_dir", "admin_db_path", "artifact_count"):
        if storage.get(key) is not None:
            print(f"  {key:<18} {storage[key]}")


def _quota_text(value: object) -> str:
    if not isinstance(value, dict) or not value.get("reported"):
        return "not reported"
    remaining = value.get("remaining")
    reset_after = value.get("reset_after")
    if value.get("blocked"):
        return f"blocked reset={reset_after or '-'}"
    if remaining is None:
        return f"reported reset={reset_after or '-'}"
    return f"{remaining} reset={reset_after or '-'}"


def _headline(text: str) -> str:
    return _color(text, "1;36")


def _mono(text: str) -> str:
    return _color(text, "2")


def _status_word(ok: bool) -> str:
    return _color("OK", "1;32") if ok else _color("FAIL", "1;31")


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def _parse_admin_limit_spec(raw: str) -> tuple[dict[str, int], dict[str, int]]:
    plan_limits: dict[str, int] = {}
    account_limits: dict[str, int] = {}
    plan_names = {"free", "go", "plus", "pro"}
    for item in raw.split(","):
        if not item.strip():
            continue
        if "=" in item:
            key, value = item.split("=", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            raise ProviderError(f"invalid limit item '{item}', expected name=value")
        name = key.strip()
        if not name:
            raise ProviderError(f"invalid empty limit name in '{item}'")
        try:
            limit = int(value.strip())
        except ValueError as exc:
            raise ProviderError(f"invalid limit value in '{item}'") from exc
        if name.lower() in plan_names:
            plan_limits[name.lower()] = limit
        else:
            account_limits[name] = limit
    return plan_limits, account_limits


def _print_settings_summary(settings: dict[str, object]) -> None:
    concurrency = settings.get("concurrency") if isinstance(settings.get("concurrency"), dict) else {}
    for feature in ("chat", "upload", "image", "research"):
        feature_settings = concurrency.get(feature) if isinstance(concurrency, dict) else {}
        if not isinstance(feature_settings, dict):
            feature_settings = {}
        print(f"{feature}:")
        print(f"  plans={json.dumps(feature_settings.get('plans') or {}, ensure_ascii=False, sort_keys=True)}")
        print(f"  accounts={json.dumps(feature_settings.get('accounts') or {}, ensure_ascii=False, sort_keys=True)}")
    warnings = settings.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("warnings:")
        for warning in warnings:
            print(f"  - {warning}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


async def cmd_chat(args: argparse.Namespace) -> int:
    capture_path = _resolve_capture_path(args.capture, args.account, args.accounts_dir)
    provider = _provider_from_chat_args(args, capture_path)
    messages: list[Message] = []
    if args.system:
        messages.append(Message.text("system", args.system))
    messages.append(Message.text("user", args.message))
    metadata = {}
    if capture_path and args.use_captured_payload:
        capture = CapturedRequest.from_file(capture_path)
        if capture.request_json is None:
            print("capture has no JSON Request Data; cannot use captured payload", file=sys.stderr)
            return 2
        metadata["captured_request_json"] = capture.request_json
    request = ChatRequest(
        messages=messages,
        model=args.model,
        conversation_id=args.conversation_id,
        parent_message_id=args.parent_message_id,
        action=args.action,
        variant_purpose=args.variant_purpose,
        thinking_effort=args.thinking_effort,
        stream=True,
        metadata=metadata,
    )
    event_count = 0
    async for delta in provider.stream_chat(request):
        if delta.text:
            print(delta.text, end="", flush=True)
        if delta.raw is not None:
            event_count += 1
            if args.max_events is not None and event_count >= args.max_events:
                break
    print()
    return 0


def _provider_from_chat_args(args: argparse.Namespace, capture_path: Path | None):
    if capture_path is None:
        return default_registry.create(args.provider)
    if args.provider != "chatgpt":
        raise ProviderError("--capture/--account is currently only supported for the chatgpt provider")
    auth = ChatGPTAuthConfig.from_captured_request(CapturedRequest.from_file(capture_path))
    return ChatGPTProvider(
        ChatGPTWebTransport(
            auth,
            refresh_web_tokens=not args.no_refresh_web_tokens,
            impersonate=args.impersonate,
        )
    )


async def cmd_image(args: argparse.Namespace) -> int:
    capture_path = args.capture or _resolve_capture_path(None, args.account, args.accounts_dir, required=False)
    provider = _provider_from_chat_args(args, capture_path)
    input_paths = list(args.input_image or [])
    if len(input_paths) > 10:
        print("at most 10 input images are supported per request", file=sys.stderr)
        return 2
    prompt = _image_edit_prompt_for_cli(args.prompt, args.aspect_ratio) if input_paths else args.prompt
    request = ImageRequest(
        prompt=prompt,
        input_images=[ImageInput(path.read_bytes(), _guess_mime_type(path), path.name) for path in input_paths],
        model=args.model,
        metadata={"aspect_ratio": args.aspect_ratio} if input_paths else {},
    )
    response = await provider.generate_image(request)
    if not response.images:
        print("no image returned", file=sys.stderr)
        return 1

    first = response.images[0]
    if first.data and args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(first.data)
        print(args.out)
    elif first.url:
        print(first.url)
    else:
        print("image response did not contain data or URL", file=sys.stderr)
        return 1
    return 0


async def cmd_vision(args: argparse.Namespace) -> int:
    capture_path = args.capture or _resolve_capture_path(None, args.account, args.accounts_dir, required=False)
    provider = _provider_from_chat_args(args, capture_path)
    input_paths = list(args.input_image or [])
    if not input_paths:
        print("--input-image is required", file=sys.stderr)
        return 2
    if len(input_paths) > 10:
        print("at most 10 input images are supported per request", file=sys.stderr)
        return 2
    prompt = args.prompt or _default_vision_prompt_for_cli(args.mode)
    content = [
        *(ContentPart.image_bytes(path.read_bytes(), _guess_mime_type(path), path.name) for path in input_paths),
        ContentPart.text_part(prompt),
    ]
    chunks: list[str] = []
    async for delta in provider.stream_chat(
        ChatRequest(
            messages=[Message(role="user", content=content)],
            model=args.model,
            metadata={"history_and_training_disabled": args.temporary_chat},
        )
    ):
        if delta.text:
            chunks.append(delta.text)
            print(delta.text, end="", flush=True)
    if chunks:
        print()
        return 0
    print("no vision text returned", file=sys.stderr)
    return 1


async def cmd_inspect_capture(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    print(f"url={capture.url or '-'}")
    print(f"status={capture.status if capture.status is not None else '-'}")
    print(f"headers={len(capture.headers)}")
    print(f"cookies={len(capture.cookies)}")
    print(f"request_json={'yes' if capture.request_json is not None else 'no'}")
    for name, value in capture.redacted_headers().items():
        print(f"{name}: {value}")
    return 0


async def cmd_probe_capture(args: argparse.Namespace) -> int:
    path = _resolve_capture_path(args.path, args.account, args.accounts_dir, required=True)
    capture = CapturedRequest.from_file(path)
    if not capture.url:
        print("capture is missing URL", file=sys.stderr)
        return 2
    request_json = capture.request_json or _probe_payload(
        message=args.message,
        model=args.model,
        conversation_id=args.conversation_id,
    )

    headers = _replay_headers(capture.headers)
    print(f"POST {capture.url}")
    print(f"headers={len(headers)} cookies={len(capture.cookies)} request_json={'captured' if capture.request_json else 'generated'}")
    print(f"transport={args.transport}")

    if args.transport == "curl_cffi":
        return await asyncio.to_thread(
            _probe_capture_curl_cffi,
            capture.url,
            headers,
            request_json,
            args.timeout,
            args.max_events,
            args.impersonate,
            args.refresh_web_tokens,
        )
    return await _probe_capture_httpx(capture.url, headers, request_json, args.timeout, args.max_events)


async def cmd_serve(args: argparse.Namespace) -> int:
    if getattr(args, "interactive", False):
        _apply_interactive_serve_args(args)
    if getattr(args, "secrets_passphrase_prompt", False):
        _prompt_secrets_passphrase()
    run_server(
        OpenAICompatConfig(
            account=args.account,
            accounts=tuple(item.strip() for item in args.accounts.split(",") if item.strip()),
            accounts_dir=args.accounts_dir,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            impersonate=args.impersonate,
            agent_prompt_mode=args.agent_mode,
            account_strategy=args.account_strategy,
            model_fallback=args.model_fallback,
            temporary_chat=args.temporary_chat,
            image_output_dir=args.image_output_dir,
            research_output_dir=args.research_output_dir,
            admin_db_path=args.admin_db_path,
            public_base_url=args.public_base_url,
            web_timeout=args.web_timeout,
            chat_concurrency=args.chat_concurrency,
            upload_concurrency=args.upload_concurrency,
            image_concurrency=args.image_concurrency,
            research_concurrency=args.research_concurrency,
        )
    )
    return 0


def _apply_interactive_serve_args(args: argparse.Namespace) -> None:
    if not sys.stdin.isatty():
        raise ProviderError("--interactive requires a TTY")

    profiles = [profile.name for profile in list_account_profiles(args.accounts_dir)]
    default_accounts = args.accounts or ",".join(profiles) or args.account

    print(_headline("Bridge Server Launch"))
    print("Account values are local aliases stored under secrets/accounts, not ChatGPT plan names.")
    accounts_csv = _prompt_accounts_csv("routed account aliases", default=default_accounts, profiles=profiles)
    args.accounts = accounts_csv
    args.account = accounts_csv.split(",", 1)[0] if accounts_csv else _prompt_account_name("primary account alias", default=args.account)

    args.account_strategy = _prompt_choice(
        "routing strategy",
        ["auto", "sticky", "failover", "round-robin", "weighted", "quota-aware"],
        default=args.account_strategy,
    )
    args.host = _prompt_choice("bind host", ["127.0.0.1", "0.0.0.0"], default=args.host if args.host in {"127.0.0.1", "0.0.0.0"} else "127.0.0.1")
    args.port = _prompt_int("API port", default=args.port)
    args.api_key = _prompt_text("Bearer API key (blank = no auth)", default=args.api_key or "", allow_empty=True) or None
    args.public_base_url = _prompt_text(
        "public API base URL",
        default=args.public_base_url or f"http://127.0.0.1:{args.port}/v1",
    )
    args.agent_mode = _prompt_choice("agent prompt mode", ["optimized", "opencode"], default=args.agent_mode if args.agent_mode in {"optimized", "opencode"} else "optimized")
    args.model_fallback = _prompt_text("model fallback (auto or none)", default=args.model_fallback)
    args.temporary_chat = _prompt_choice(
        "chat privacy",
        ["temporary", "normal"],
        default="temporary" if args.temporary_chat else "normal",
    ) == "temporary"
    args.chat_concurrency = _prompt_text(
        "chat concurrency limits",
        default=args.chat_concurrency or "free=1,go=2,plus=3,pro=4",
        allow_empty=True,
    ) or None
    args.upload_concurrency = _prompt_text(
        "upload concurrency limits",
        default=args.upload_concurrency or "free=1,go=1,plus=1,pro=1",
        allow_empty=True,
    ) or None
    args.image_concurrency = _prompt_text(
        "image concurrency limits",
        default=args.image_concurrency or "free=1,go=1,plus=2,pro=3",
        allow_empty=True,
    ) or None
    args.research_concurrency = _prompt_text(
        "research concurrency limits",
        default=args.research_concurrency or "free=1,go=1,plus=2,pro=2",
        allow_empty=True,
    ) or None
    args.web_timeout = float(_prompt_text("web timeout seconds", default=str(int(args.web_timeout))))
    args.image_output_dir = Path(_prompt_text("image output dir", default=str(args.image_output_dir)))
    args.research_output_dir = Path(_prompt_text("research output dir", default=str(args.research_output_dir)))
    args.admin_db_path = Path(_prompt_text("admin DB path", default=str(args.admin_db_path)))


def _prompt_secrets_passphrase() -> None:
    if not sys.stdin.isatty():
        raise ProviderError("--secrets-passphrase-prompt requires a TTY")
    passphrase = getpass.getpass("account secrets passphrase: ")
    if not passphrase:
        raise ProviderError("account secrets passphrase must not be empty")
    if getpass.getpass("confirm passphrase: ") != passphrase:
        raise ProviderError("passphrases did not match")
    set_runtime_passphrase(passphrase)


async def _probe_capture_httpx(
    url: str,
    headers: dict[str, str],
    request_json: dict,
    timeout: float,
    max_events: int,
) -> int:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            content=json.dumps(request_json, separators=(",", ":")).encode("utf-8"),
        ) as response:
            print(f"status={response.status_code}")
            print(f"content-type={response.headers.get('content-type', '-')}")
            if response.headers.get("x-oai-request-id"):
                print(f"x-oai-request-id={response.headers['x-oai-request-id']}")
            if response.status_code >= 400:
                body = await response.aread()
                print(body.decode("utf-8", errors="replace")[:800], file=sys.stderr)
                return 1

            event_count = 0
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    print("event=[DONE]")
                    return 0
                event_count += 1
                print(_summarize_sse_payload(payload))
                if event_count >= max_events:
                    print(f"stopped_after={event_count}")
                    return 0
    return 0


def _resolve_capture_path(
    capture_path: Path | None,
    account: str | None,
    accounts_dir: Path | None,
    required: bool = False,
) -> Path | None:
    if capture_path is not None and account:
        raise ProviderError("use either --capture/path or --account, not both")
    if capture_path is not None:
        return capture_path
    account = account or os.environ.get("CHATGPT_ACCOUNT")
    if account:
        try:
            return resolve_account_capture_path(account, accounts_dir)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
    if required:
        raise ProviderError("pass a capture path or --account")
    return None


def _guess_mime_type(path: Path | None) -> str:
    if path is None:
        return "image/png"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "image/png"


def _image_edit_prompt_for_cli(prompt: str, aspect_ratio: str) -> str:
    parts = [
        prompt.strip(),
        "Use the attached image input as visual reference. If multiple images are attached, combine or reconcile them into one new output image according to the prompt.",
        "Return exactly one edited/generated image.",
    ]
    if aspect_ratio != "auto":
        parts.append(f"Make the aspect ratio {aspect_ratio}.")
    return "\n\n".join(parts)


def _default_vision_prompt_for_cli(mode: str) -> str:
    if mode == "ocr":
        return "OCR the image. Extract all visible text faithfully, preserve reading order, and briefly note uncertain text."
    if mode == "describe":
        return "Describe the image with enough context for a developer or creative tool to understand the subject, layout, text, style, and important details."
    return "Analyze the attached image."


def _csv_or_dash(values: list[str]) -> str:
    return ", ".join(values) if values else "-"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"1", "true", "yes", "on", "temporary", "incognito", "private"}:
        return True
    if normalized in {"0", "false", "no", "off", "normal", "regular", "default"}:
        return False
    return default


def _load_account_settings(
    settings_path: Path | None,
    account: str | None,
    accounts_dir: Path | None,
    no_settings: bool,
) -> dict[str, object]:
    if no_settings:
        return {}
    path = settings_path
    selected_account = account or os.environ.get("CHATGPT_ACCOUNT")
    if path is None and selected_account:
        try:
            candidate = resolve_account_settings_path(selected_account, accounts_dir)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        if candidate.exists():
            path = candidate
    if path is None or not path.exists():
        return {}
    return load_settings_file(str(path))


def _check_account_auth(capture: CapturedRequest, impersonate: str) -> tuple[int | None, bool, str | None]:
    try:
        from curl_cffi import requests
    except ImportError:
        return None, False, "curl_cffi is not installed"
    auth = ChatGPTAuthConfig.from_captured_request(capture)
    headers = _websocket_url_headers(auth.request_headers())
    try:
        response = requests.get(
            "https://chatgpt.com/backend-api/celsius/ws/user",
            headers=headers,
            impersonate=impersonate,
            timeout=30,
        )
    except Exception as exc:
        return None, False, type(exc).__name__
    if response.status_code != 200:
        return response.status_code, False, _status_detail(response.status_code)
    try:
        websocket_url = response.json().get("websocket_url")
    except Exception:
        websocket_url = None
    return response.status_code, isinstance(websocket_url, str) and bool(websocket_url), None


def _fetch_account_models(capture: CapturedRequest, impersonate: str) -> dict[str, object]:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise ProviderError("curl_cffi is not installed") from exc
    auth = ChatGPTAuthConfig.from_captured_request(capture)
    headers = _json_get_headers(auth.request_headers())
    endpoints = ChatGPTEndpoints()
    response = requests.get(
        f"{endpoints.base_url}/backend-api/models",
        headers=headers,
        impersonate=impersonate,
        timeout=30,
    )
    if response.status_code >= 400:
        raise ProviderError(f"ChatGPT models request failed: {response.status_code}")
    try:
        parsed = response.json()
    except Exception as exc:
        raise ProviderError("ChatGPT models response was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("ChatGPT models response had an unexpected shape")
    return parsed


def _json_get_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {"content-length", "content-type", "accept-encoding", "host"}
    refreshed = {name: value for name, value in headers.items() if name not in blocked}
    refreshed["accept"] = "application/json"
    return refreshed


def _status_detail(status_code: int) -> str:
    if status_code in {401, 403}:
        return "capture no longer authenticates"
    if status_code == 429:
        return "rate limited"
    return "unexpected response"


def _probe_capture_curl_cffi(
    url: str,
    headers: dict[str, str],
    request_json: dict,
    timeout: float,
    max_events: int,
    impersonate: str,
    refresh_web_tokens: bool,
) -> int:
    try:
        from curl_cffi import requests
    except ImportError:
        print("curl_cffi is not installed; use --transport httpx or install curl_cffi", file=sys.stderr)
        return 2

    if refresh_web_tokens:
        refreshed = _refresh_chatgpt_web_tokens(
            requests=requests,
            conversation_url=url,
            headers=headers,
            request_json=request_json,
            timeout=timeout,
            impersonate=impersonate,
        )
        if refreshed is None:
            return 1
        headers = refreshed

    response = requests.post(
        url,
        headers=headers,
        data=json.dumps(request_json, separators=(",", ":")).encode("utf-8"),
        impersonate=impersonate,
        timeout=timeout,
        stream=True,
    )
    print(f"status={response.status_code}")
    print(f"content-type={response.headers.get('content-type', '-')}")
    if response.headers.get("x-oai-request-id"):
        print(f"x-oai-request-id={response.headers['x-oai-request-id']}")
    if response.status_code >= 400:
        print(response.text[:800], file=sys.stderr)
        return 1

    event_count = 0
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            print("event=[DONE]")
            return 0
        event_count += 1
        print(_summarize_sse_payload(payload))
        if event_count >= max_events:
            print(f"stopped_after={event_count}")
            return 0
    return 0


def _refresh_chatgpt_web_tokens(
    requests,
    conversation_url: str,
    headers: dict[str, str],
    request_json: dict,
    timeout: float,
    impersonate: str,
) -> dict[str, str] | None:
    base_url = conversation_url.split("/backend-api/", 1)[0]
    prepare_url = f"{base_url}/backend-api/f/conversation/prepare"
    requirements_url = f"{base_url}/backend-api/sentinel/chat-requirements"
    refreshed = dict(headers)

    prepare_headers = _json_headers_for_web_token_refresh(headers)
    prepare_response = requests.post(
        prepare_url,
        headers=prepare_headers,
        data=json.dumps(_prepare_payload(request_json), separators=(",", ":")).encode("utf-8"),
        impersonate=impersonate,
        timeout=timeout,
    )
    print(f"prepare_status={prepare_response.status_code}")
    if prepare_response.status_code >= 400:
        print(_safe_body_preview(prepare_response), file=sys.stderr)
        return None
    try:
        prepare_json = prepare_response.json()
    except Exception:
        prepare_json = {}
    conduit_token = prepare_json.get("conduit_token")
    if conduit_token:
        refreshed["x-conduit-token"] = conduit_token
        print("prepare_conduit=yes")
    elif prepare_response.headers.get("x-conduit-token"):
        refreshed["x-conduit-token"] = prepare_response.headers["x-conduit-token"]
        print("prepare_conduit=header")
    else:
        print("prepare_conduit=no")

    requirements_headers = _json_headers_for_web_token_refresh(headers)
    requirements_response = requests.post(
        requirements_url,
        headers=requirements_headers,
        data=b'{"p":null}',
        impersonate=impersonate,
        timeout=timeout,
    )
    print(f"requirements_status={requirements_response.status_code}")
    if requirements_response.status_code >= 400:
        print(_safe_body_preview(requirements_response), file=sys.stderr)
        return None
    try:
        requirements_json = requirements_response.json()
    except Exception:
        requirements_json = {}
    requirements_token = requirements_json.get("token")
    if requirements_token:
        refreshed["openai-sentinel-chat-requirements-token"] = requirements_token
        print("requirements_token=yes")
    else:
        print("requirements_token=no")
    proof_challenge = requirements_json.get("proofofwork")
    if isinstance(proof_challenge, dict) and proof_challenge.get("required"):
        proof_config = decode_proof_config(headers.get("openai-sentinel-proof-token"))
        proof_token = generate_proof_token(
            required=True,
            seed=str(proof_challenge.get("seed") or ""),
            difficulty=str(proof_challenge.get("difficulty") or ""),
            user_agent=headers.get("user-agent"),
            proof_config=proof_config,
        )
        if proof_token:
            refreshed["openai-sentinel-proof-token"] = proof_token
            print("proof_token=yes")
        else:
            print("proof_token=no")
    return refreshed


def _json_headers_for_web_token_refresh(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "accept",
        "content-length",
        "openai-sentinel-chat-requirements-token",
        "x-conduit-token",
        "x-oai-turn-trace-id",
    }
    refreshed = {name: value for name, value in headers.items() if name not in blocked}
    refreshed["accept"] = "application/json"
    refreshed["content-type"] = "application/json"
    return refreshed


def _prepare_payload(request_json: dict) -> dict:
    allowed = {
        "action",
        "conversation_id",
        "parent_message_id",
        "model",
        "client_prepare_state",
        "timezone_offset_min",
        "timezone",
        "variant_purpose",
        "conversation_mode",
        "system_hints",
        "supports_buffering",
        "supported_encodings",
        "thinking_effort",
        "client_contextual_info",
        "paragen_cot_summary_display_override",
        "force_parallel_switch",
        "enable_message_followups",
    }
    return {key: value for key, value in request_json.items() if key in allowed}


def _safe_body_preview(response) -> str:
    try:
        return response.text[:800]
    except Exception:
        return "<failed to read response body>"


def _probe_payload(message: str, model: str, conversation_id: str | None = None) -> dict:
    parent_message_id = str(uuid.uuid4())
    timezone_payload = local_timezone_payload()
    payload = {
        "action": "next",
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [message]},
                "metadata": {"serialization_metadata": {"custom_symbol_offsets": []}},
                "create_time": time.time(),
            }
        ],
        "parent_message_id": parent_message_id,
        "model": model,
        "timezone_offset_min": timezone_payload["timezone_offset_min"],
        "timezone": timezone_payload["timezone"],
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {
            "is_dark_mode": False,
            "time_since_loaded": 20,
            "page_height": 900,
            "page_width": 1440,
            "pixel_ratio": 1,
            "screen_height": 1080,
            "screen_width": 1920,
        },
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


def _replay_headers(headers: dict[str, str]) -> dict[str, str]:
    skip = {"content-length", "host", "accept-encoding"}
    replay = {name: value for name, value in headers.items() if name not in skip}
    replay.setdefault("accept", "text/event-stream")
    replay.setdefault("content-type", "application/json")
    return replay


def _summarize_sse_payload(payload: str) -> str:
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return f"event=raw bytes={len(payload)}"
    if not isinstance(event, dict):
        return f"event={type(event).__name__}"
    event_type = event.get("type") or event.get("event") or "-"
    keys = ",".join(sorted(str(key) for key in event.keys())[:8])
    text = _extract_text_preview(event)
    if text:
        return f"event={event_type} keys={keys} text={text[:160]!r}"
    return f"event={event_type} keys={keys}"


def _extract_text_preview(event: dict) -> str:
    value = event.get("v")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [item.get("v", "") for item in value if isinstance(item, dict) and isinstance(item.get("v"), str)]
        return "".join(parts)
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                return "".join(part for part in parts if isinstance(part, str))
    return ""


def message_with_image(text: str, image_url: str) -> Message:
    return Message(
        role="user",
        content=[ContentPart.image_url(image_url), ContentPart.text_part(text)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
