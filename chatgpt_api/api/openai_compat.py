"""Chat-completions-style HTTP bridge backed by local providers."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.api.http_utils import (
    authorize as _authorize,
    cancel_operation_id_from_path as _cancel_operation_id_from_path,
    operation_id_from_path as _operation_id_from_path,
    query_value as _query_value,
    read_json_body as _read_json_body,
    send_cors_headers as _send_cors_headers,
    send_cors_preflight as _send_cors_preflight,
    send_json as _send_json,
    send_text as _send_text,
)
from chatgpt_api.api.image_inputs import (
    IMAGE_EDIT_ASPECT_RATIO_WARNING,
    MAX_IMAGE_INPUTS_PER_REQUEST,
    default_vision_prompt as _default_vision_prompt,
    image_aspect_ratio_from_body as _image_aspect_ratio_from_body,
    image_edit_prompt as _image_edit_prompt,
    image_input_from_reference as _image_input_from_reference,
    image_inputs_from_body as _image_inputs_from_body,
)
from chatgpt_api.api.prompts import (
    AGENT_PROMPT_MODE_ALIASES,
    AGENT_PROMPT_MODES,
    DEEP_RESEARCH_MODEL_ALIASES,
    DEEP_RESEARCH_SYSTEM_HINT,
    OPTIMIZED_TOOL_BRIDGE_PROMPT,
    TOOL_BRIDGE_PROMPT,
)
from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.types import ChatRequest, ContentPart, ImageRequest, ImageResponse, Message
from chatgpt_api.providers.chatgpt.account_info import detect_account_info, infer_account_capabilities, load_settings_file
from chatgpt_api.providers.chatgpt.accounts import (
    accounts_dir_from_env,
    list_account_profiles,
    resolve_account_capture_path,
    resolve_account_settings_path,
)
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.crypto import encrypt_text, load_secrets_key
from chatgpt_api.providers.chatgpt.provider import ChatGPTProvider
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest, SECRET_HEADER_NAMES
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport



@dataclass(frozen=True, slots=True)
class _DownloadFile:
    path: Path
    filename: str
    content_type: str
    created_at: float


_DOWNLOAD_FILES: dict[str, _DownloadFile] = {}
_DOWNLOAD_FILES_LOCK = threading.Lock()


class AccountRouter:
    def __init__(self, accounts: tuple[str, ...], strategy: str = "auto") -> None:
        if not accounts:
            raise ValueError("at least one account is required")
        self.accounts = accounts
        self.strategy = _normalize_account_strategy(strategy)
        self._lock = threading.Lock()
        self._counter = 0
        self._limiters: dict[str, threading.BoundedSemaphore] = {}
        self._limits: dict[str, int] = {}

    def order(self) -> tuple[str, ...]:
        if len(self.accounts) == 1 or self.strategy in {"auto", "sticky", "failover", "quota-aware"}:
            return self.accounts
        with self._lock:
            index = self._counter
            self._counter += 1
        if self.strategy == "round-robin":
            start = index % len(self.accounts)
            return self.accounts[start:] + self.accounts[:start]
        if self.strategy == "weighted":
            weighted = _weighted_account_sequence(self.accounts)
            start = index % len(weighted)
            return tuple(dict.fromkeys(weighted[start:] + weighted[:start]))
        if self.strategy == "random":
            return tuple(random.sample(self.accounts, len(self.accounts)))
        return self.accounts

    def set_account_limit(self, account: str, limit: int) -> None:
        normalized_limit = max(1, int(limit))
        with self._lock:
            existing = self._limits.get(account)
            if existing == normalized_limit and account in self._limiters:
                return
            self._limits[account] = normalized_limit
            self._limiters[account] = threading.BoundedSemaphore(normalized_limit)
        _set_global_account_limit(account, normalized_limit)

    def account_limit(self, account: str) -> int:
        with self._lock:
            return self._limits.get(account, _default_account_concurrency_limit(account))

    async def acquire(self, account: str) -> "AccountLease":
        with self._lock:
            limiter = self._limiters.get(account)
            if limiter is None:
                limiter = threading.BoundedSemaphore(_default_account_concurrency_limit(account))
                self._limiters[account] = limiter
        await asyncio.to_thread(limiter.acquire)
        return AccountLease(account=account, limiter=limiter)


@dataclass(slots=True)
class AccountLease:
    account: str
    limiter: threading.BoundedSemaphore
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.limiter.release()

    async def __aenter__(self) -> "AccountLease":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class OpenAICompatProviderError(ProviderError):
    def __init__(
        self,
        original: ProviderError,
        requested_model: str,
        provider_model: str,
        init_metadata: dict[str, Any] | None = None,
        account: str | None = None,
        account_attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.original = original
        self.requested_model = requested_model
        self.provider_model = provider_model
        self.init_metadata = init_metadata
        self.account = account
        self.account_attempts = account_attempts or []
        super().__init__(str(original))


class _ClientDisconnected(Exception):
    """Raised when a bridge client closes the SSE connection."""


@dataclass(slots=True)
class _ChatGPTOperation:
    operation_id: str
    kind: str
    created_at: float
    account: str | None = None
    provider: ChatGPTProvider | None = None
    conversation_id: str | None = None
    deep_research_message_id: str | None = None
    deep_research_session_id: str | None = None
    cancel_requested: bool = False
    completed: bool = False
    last_cancel_result: dict[str, Any] | None = None
    last_cancel_error: str | None = None


_CHATGPT_OPERATIONS: dict[str, _ChatGPTOperation] = {}
_CHATGPT_OPERATIONS_LOCK = threading.Lock()
_CHATGPT_OPERATION_TTL_SECONDS = 3600.0


ACCOUNT_STRATEGIES = {"auto", "sticky", "failover", "round-robin", "weighted", "quota-aware", "random"}
ACCOUNT_CONCURRENCY_BY_PLAN = {
    "free": 1,
    "go": 2,
    "plus": 3,
    "pro": 4,
}
BRIDGE_SETTINGS_KEY = "bridge_settings"
CONCURRENCY_FEATURES = ("chat", "upload", "image", "research")
CONCURRENCY_PLAN_DEFAULTS = {
    "chat": {"free": 1, "go": 2, "plus": 3, "pro": 4},
    "upload": {"free": 1, "go": 1, "plus": 1, "pro": 1},
    "image": {"free": 1, "go": 1, "plus": 2, "pro": 3},
    "research": {"free": 1, "go": 1, "plus": 2, "pro": 2},
}
CONCURRENCY_WARNINGS = [
    "These limits are local bridge throttles, not official ChatGPT quotas.",
    "Upload concurrency covers image input jobs such as OCR, describe, image edit, and image composite. One request can attach up to 10 images, but the default upload concurrency is 1 for every plan.",
    "ChatGPT can still apply hidden burst rate limits. On Pro, image quota may show a high daily number, but rapid parallel generations can still trigger a 5-10 minute cooldown.",
    "Deep Research should stay low because each run is long-lived and can consume scarce monthly quota.",
]
USAGE_FEATURE_ALIASES = {
    "deep_research": ("deep_research", "openai_deep_research"),
    "file_upload": ("file_upload", "attachments", "attachment"),
    "image_gen": ("image_gen", "image_generation", "dalle", "gpt_image"),
    "paste_text_to_file": ("paste_text_to_file",),
}
_IMAGE_REQUEST_CACHE_TTL_SECONDS = 45.0
_IMAGE_REQUEST_CACHE_LOCK = threading.Lock()
_ACCOUNT_LIMITER_LOCK = threading.Lock()
_ACCOUNT_LIMITERS: dict[str, threading.BoundedSemaphore] = {}
_ACCOUNT_LIMITS: dict[str, int] = {}
_FEATURE_LIMITER_LOCK = threading.Lock()
_FEATURE_LIMITERS: dict[tuple[str, str], threading.BoundedSemaphore] = {}
_FEATURE_LIMITS: dict[tuple[str, str], int] = {}


@dataclass(slots=True)
class _ImageRequestCacheEntry:
    event: threading.Event
    created_at: float
    response: dict[str, Any] | None = None
    error: BaseException | None = None


_IMAGE_REQUEST_CACHE: dict[str, _ImageRequestCacheEntry] = {}


def _accounts_for_config(config: OpenAICompatConfig) -> tuple[str, ...]:
    accounts = tuple(account.strip() for account in config.accounts if account and account.strip())
    if accounts:
        return tuple(dict.fromkeys(accounts))
    if "," in config.account:
        split_accounts = tuple(item.strip() for item in config.account.split(",") if item.strip())
        if split_accounts:
            return tuple(dict.fromkeys(split_accounts))
    return (config.account,)


def _normalize_account_strategy(strategy: str | None) -> str:
    normalized = (strategy or "auto").strip().lower().replace("_", "-")
    aliases = {
        "default": "auto",
        "limit-aware": "quota-aware",
        "shuffle": "random",
        "rotate": "round-robin",
        "roundrobin": "round-robin",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in ACCOUNT_STRATEGIES:
        raise ValueError(f"unsupported account strategy: {strategy}")
    return normalized


def _weighted_account_sequence(accounts: tuple[str, ...]) -> tuple[str, ...]:
    weighted: list[str] = []
    for account in accounts:
        lowered = account.lower()
        if "pro" in lowered:
            weight = 10
        elif "plus" in lowered:
            weight = 4
        elif "go" in lowered:
            weight = 2
        else:
            weight = 1
        weighted.extend([account] * weight)
    return tuple(weighted or accounts)


def _default_account_concurrency_limit(account: str) -> int:
    return _concurrency_limit_for_plan(_plan_hint_from_account_name(account))


def _concurrency_limit_for_plan(plan_type: str | None) -> int:
    return ACCOUNT_CONCURRENCY_BY_PLAN.get((plan_type or "").strip().lower(), 1)


def _plan_hint_from_account_name(account: str) -> str | None:
    lowered = account.lower()
    for plan in ("pro", "plus", "go", "free"):
        if plan in lowered:
            return plan
    return None


def _configure_account_limits(config: OpenAICompatConfig, router: AccountRouter) -> None:
    for account in router.accounts:
        router.set_account_limit(account, _account_concurrency_limit(config, account))


def _router_for_request(
    config: OpenAICompatConfig,
    default_router: AccountRouter | None,
    body: dict[str, Any],
) -> AccountRouter:
    base_router = default_router or AccountRouter(_accounts_for_config(config), config.account_strategy)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    account = _str_or_none(body.get("chatgpt_account")) or _str_or_none(metadata.get("chatgpt_account"))
    accounts_value = body.get("chatgpt_accounts", metadata.get("chatgpt_accounts"))
    strategy = _str_or_none(body.get("chatgpt_account_strategy")) or _str_or_none(metadata.get("chatgpt_account_strategy"))
    if not account and not accounts_value and not strategy:
        return base_router

    if account and accounts_value:
        raise ValueError("use either chatgpt_account or chatgpt_accounts, not both")
    available = set(_accounts_for_config(config))
    requested_accounts = _request_account_list(account, accounts_value)
    if requested_accounts:
        unknown = [item for item in requested_accounts if item not in available]
        if unknown:
            raise ValueError(f"unknown ChatGPT account alias for this server: {', '.join(unknown)}")
        accounts = tuple(dict.fromkeys(requested_accounts))
    else:
        accounts = base_router.accounts
    router = AccountRouter(accounts, strategy or base_router.strategy)
    _configure_account_limits(config, router)
    return router


def _request_account_list(account: str | None, accounts_value: Any) -> list[str]:
    if account:
        return [account.strip()]
    if accounts_value is None:
        return []
    if isinstance(accounts_value, str):
        return [part.strip() for part in accounts_value.split(",") if part.strip()]
    if isinstance(accounts_value, list):
        return [str(part).strip() for part in accounts_value if str(part).strip()]
    raise ValueError("chatgpt_accounts must be a comma-separated string or list")


def _account_concurrency_limit(config: OpenAICompatConfig, account: str) -> int:
    return max(1, _feature_account_concurrency_limit(config, "chat", account))


def _account_plan_type(config: OpenAICompatConfig, account: str) -> str | None:
    summary = _account_capture_usage_summary(config, account)
    return _str_or_none(summary.get("plan_type")) or _str_or_none(summary.get("plan_bucket")) or _plan_hint_from_account_name(account)


def _default_bridge_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "concurrency": {
            feature: {"plans": dict(plans), "accounts": {}}
            for feature, plans in CONCURRENCY_PLAN_DEFAULTS.items()
        },
        "warnings": list(CONCURRENCY_WARNINGS),
    }


def _bridge_settings(config: OpenAICompatConfig) -> dict[str, Any]:
    stored = _admin_store(config).get_setting(BRIDGE_SETTINGS_KEY, {})
    settings = _normalize_bridge_settings(stored)
    _apply_concurrency_override(settings, "chat", config.chat_concurrency)
    _apply_concurrency_override(settings, "upload", config.upload_concurrency)
    _apply_concurrency_override(settings, "image", config.image_concurrency)
    _apply_concurrency_override(settings, "research", config.research_concurrency)
    return settings


def _normalize_bridge_settings(value: Any) -> dict[str, Any]:
    defaults = _default_bridge_settings()
    if not isinstance(value, dict):
        return defaults
    raw_concurrency = value.get("concurrency") if isinstance(value.get("concurrency"), dict) else {}
    normalized = _default_bridge_settings()
    for feature in CONCURRENCY_FEATURES:
        raw_feature = raw_concurrency.get(feature) if isinstance(raw_concurrency, dict) else {}
        raw_plans = raw_feature.get("plans") if isinstance(raw_feature, dict) else {}
        raw_accounts = raw_feature.get("accounts") if isinstance(raw_feature, dict) else {}
        for plan, default_limit in defaults["concurrency"][feature]["plans"].items():
            normalized["concurrency"][feature]["plans"][plan] = _safe_concurrency_int(
                raw_plans.get(plan) if isinstance(raw_plans, dict) else None,
                default_limit,
                allow_zero=feature != "chat",
            )
        if isinstance(raw_accounts, dict):
            accounts: dict[str, int] = {}
            for account, limit in raw_accounts.items():
                account_name = str(account).strip()
                if not account_name:
                    continue
                accounts[account_name] = _safe_concurrency_int(limit, 1, allow_zero=feature != "chat")
            normalized["concurrency"][feature]["accounts"] = accounts
    normalized["warnings"] = list(CONCURRENCY_WARNINGS)
    return normalized


def _safe_concurrency_int(value: Any, default: int, *, allow_zero: bool) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    lower = 0 if allow_zero else 1
    return max(lower, min(parsed, 32))


def _apply_concurrency_override(settings: dict[str, Any], feature: str, override: str | None) -> None:
    if feature not in CONCURRENCY_FEATURES or not override:
        return
    feature_settings = settings.setdefault("concurrency", {}).setdefault(feature, {"plans": {}, "accounts": {}})
    plans = feature_settings.setdefault("plans", {})
    accounts = feature_settings.setdefault("accounts", {})
    allow_zero = feature != "chat"
    for key, limit in _parse_concurrency_override(override).items():
        if key in {"free", "go", "plus", "pro"}:
            plans[key] = _safe_concurrency_int(limit, plans.get(key, 1), allow_zero=allow_zero)
        else:
            accounts[key] = _safe_concurrency_int(limit, accounts.get(key, 1), allow_zero=allow_zero)


def _parse_concurrency_override(value: str | None) -> dict[str, int]:
    result: dict[str, int] = {}
    if not value:
        return result
    for chunk in re.split(r"[,;\s]+", value):
        if not chunk.strip() or "=" not in chunk:
            continue
        key, _, raw_limit = chunk.partition("=")
        key = key.strip()
        if not key:
            continue
        try:
            result[key] = int(raw_limit.strip())
        except ValueError:
            continue
    return result


def _feature_account_concurrency_limit(config: OpenAICompatConfig, feature: str, account: str) -> int:
    settings = _bridge_settings(config)
    feature_settings = settings.get("concurrency", {}).get(feature, {})
    accounts = feature_settings.get("accounts") if isinstance(feature_settings, dict) else {}
    if isinstance(accounts, dict) and account in accounts:
        return _safe_concurrency_int(accounts.get(account), 1, allow_zero=feature != "chat")
    plan_type = _account_plan_type(config, account)
    plans = feature_settings.get("plans") if isinstance(feature_settings, dict) else {}
    if isinstance(plans, dict):
        default = CONCURRENCY_PLAN_DEFAULTS.get(feature, {}).get((plan_type or "").lower(), 1)
        return _safe_concurrency_int(plans.get((plan_type or "").lower()), default, allow_zero=feature != "chat")
    return CONCURRENCY_PLAN_DEFAULTS.get(feature, {}).get((plan_type or "").lower(), 1)


def _set_global_account_limit(account: str, limit: int) -> None:
    normalized_limit = max(1, int(limit))
    with _ACCOUNT_LIMITER_LOCK:
        existing = _ACCOUNT_LIMITS.get(account)
        if existing == normalized_limit and account in _ACCOUNT_LIMITERS:
            return
        _ACCOUNT_LIMITS[account] = normalized_limit
        _ACCOUNT_LIMITERS[account] = threading.BoundedSemaphore(normalized_limit)


def _global_account_limiter(account: str) -> threading.BoundedSemaphore:
    with _ACCOUNT_LIMITER_LOCK:
        limiter = _ACCOUNT_LIMITERS.get(account)
        if limiter is None:
            limiter = threading.BoundedSemaphore(_default_account_concurrency_limit(account))
            _ACCOUNT_LIMITERS[account] = limiter
        return limiter


def _feature_limiter(feature: str, account: str, limit: int) -> threading.BoundedSemaphore:
    normalized_limit = max(1, int(limit))
    key = (feature, account)
    with _FEATURE_LIMITER_LOCK:
        existing = _FEATURE_LIMITS.get(key)
        if existing == normalized_limit and key in _FEATURE_LIMITERS:
            return _FEATURE_LIMITERS[key]
        _FEATURE_LIMITS[key] = normalized_limit
        limiter = threading.BoundedSemaphore(normalized_limit)
        _FEATURE_LIMITERS[key] = limiter
        return limiter


def _prune_chatgpt_operations(now: float | None = None) -> None:
    cutoff = (now or time.time()) - _CHATGPT_OPERATION_TTL_SECONDS
    with _CHATGPT_OPERATIONS_LOCK:
        stale = [
            operation_id
            for operation_id, operation in _CHATGPT_OPERATIONS.items()
            if operation.created_at < cutoff and operation.completed
        ]
        for operation_id in stale:
            _CHATGPT_OPERATIONS.pop(operation_id, None)


def _create_chatgpt_operation(kind: str, operation_id: str | None = None) -> _ChatGPTOperation:
    _prune_chatgpt_operations()
    operation = _ChatGPTOperation(
        operation_id=operation_id or f"chatgptop_{uuid.uuid4().hex}",
        kind=kind,
        created_at=time.time(),
    )
    with _CHATGPT_OPERATIONS_LOCK:
        _CHATGPT_OPERATIONS[operation.operation_id] = operation
    return operation


def _update_chatgpt_operation(operation_id: str | None, **updates: Any) -> _ChatGPTOperation | None:
    if not operation_id:
        return None
    with _CHATGPT_OPERATIONS_LOCK:
        operation = _CHATGPT_OPERATIONS.get(operation_id)
        if operation is None:
            return None
        for key, value in updates.items():
            if hasattr(operation, key):
                setattr(operation, key, value)
        return operation


def _finish_chatgpt_operation(operation_id: str | None) -> None:
    _update_chatgpt_operation(operation_id, completed=True)


def _chatgpt_operation_cancel_requested(operation_id: str | None) -> bool:
    if not operation_id:
        return False
    with _CHATGPT_OPERATIONS_LOCK:
        operation = _CHATGPT_OPERATIONS.get(operation_id)
        return bool(operation and operation.cancel_requested)


def _chatgpt_operation_payload(operation: _ChatGPTOperation) -> dict[str, Any]:
    deep_research_ready = bool(
        operation.conversation_id and operation.deep_research_message_id and operation.deep_research_session_id
    )
    pending_reason = None
    if operation.kind == "research" and not operation.completed and not deep_research_ready:
        if not operation.provider:
            pending_reason = "provider_not_selected"
        elif not operation.conversation_id:
            pending_reason = "conversation_id_not_available"
        else:
            pending_reason = "deep_research_session_not_available"
    return {
        "id": operation.operation_id,
        "kind": operation.kind,
        "account": operation.account,
        "provider_selected": bool(operation.provider),
        "conversation_id": operation.conversation_id,
        "deep_research_message_id": operation.deep_research_message_id,
        "deep_research_session_id": operation.deep_research_session_id,
        "deep_research_ready": deep_research_ready,
        "pending_reason": pending_reason,
        "cancel_requested": operation.cancel_requested,
        "completed": operation.completed,
        "last_cancel_result": operation.last_cancel_result,
        "last_cancel_error": operation.last_cancel_error,
    }


def _get_chatgpt_operation(operation_id: str) -> tuple[int, dict[str, Any]]:
    _prune_chatgpt_operations()
    with _CHATGPT_OPERATIONS_LOCK:
        operation = _CHATGPT_OPERATIONS.get(operation_id)
        if operation is None:
            return 404, {"error": {"message": "operation not found", "type": "not_found"}}
        return 200, {"object": "chatgpt.operation", "operation": _chatgpt_operation_payload(operation)}


def _stop_chatgpt_operation_by_id(operation_id: str | None) -> _ChatGPTOperation | None:
    if not operation_id:
        return None
    with _CHATGPT_OPERATIONS_LOCK:
        operation = _CHATGPT_OPERATIONS.get(operation_id)
    if operation is None:
        return None
    return _stop_chatgpt_operation(operation)


def _cancel_chatgpt_operation(operation_id: str) -> tuple[int, dict[str, Any]]:
    with _CHATGPT_OPERATIONS_LOCK:
        operation = _CHATGPT_OPERATIONS.get(operation_id)
        if operation is None:
            return 404, {"error": {"message": "operation not found", "type": "not_found"}}
        operation.cancel_requested = True

    result = _stop_chatgpt_operation(operation)
    return 200, {"status": "ok", "operation": _chatgpt_operation_payload(result)}


def _stop_chatgpt_operation(operation: _ChatGPTOperation) -> _ChatGPTOperation:
    cancel_result: dict[str, Any] = {}
    cancel_errors: list[str] = []
    provider = operation.provider
    if provider is None:
        cancel_result["pending"] = "provider_not_selected"
    else:
        if operation.conversation_id:
            try:
                cancel_result["conversation"] = provider.transport.stop_conversation(
                    operation.conversation_id,
                    exclude_async_types=["pro_mode"],
                )
            except ProviderError as exc:
                cancel_errors.append(str(exc))
        else:
            cancel_result["pending"] = "conversation_id_not_available"
        if operation.conversation_id and operation.deep_research_message_id and operation.deep_research_session_id:
            try:
                cancel_result["deep_research"] = provider.transport.stop_deep_research(
                    operation.conversation_id,
                    operation.deep_research_message_id,
                    operation.deep_research_session_id,
                )
            except ProviderError as exc:
                cancel_errors.append(str(exc))
        elif operation.kind == "research":
            cancel_result["deep_research_pending"] = "widget_session_id_not_available"

    with _CHATGPT_OPERATIONS_LOCK:
        current = _CHATGPT_OPERATIONS.get(operation.operation_id)
        if current is None:
            return operation
        current.last_cancel_result = cancel_result
        current.last_cancel_error = "; ".join(cancel_errors) if cancel_errors else None
        return current


async def _with_provider_account_limit(provider: ChatGPTProvider, operation: Any) -> Any:
    account = getattr(provider, "_chatgpt_api_account", None)
    if not isinstance(account, str) or not account:
        return await operation()
    limiter = _global_account_limiter(account)
    await asyncio.to_thread(limiter.acquire)
    try:
        return await operation()
    finally:
        limiter.release()


async def _with_provider_feature_limit(
    config: OpenAICompatConfig,
    provider: ChatGPTProvider,
    feature: str,
    operation: Any,
) -> Any:
    account = getattr(provider, "_chatgpt_api_account", None)
    if not isinstance(account, str) or not account:
        return await operation()
    limit = _feature_account_concurrency_limit(config, feature, account)
    if limit <= 0:
        raise ProviderError(f"ChatGPT {feature} is disabled for account '{account}' by bridge concurrency settings")
    limiter = _feature_limiter(feature, account, limit)
    await asyncio.to_thread(limiter.acquire)
    try:
        return await operation()
    finally:
        limiter.release()


async def _with_provider_feature_limits(
    config: OpenAICompatConfig,
    provider: ChatGPTProvider,
    features: tuple[str, ...],
    operation: Any,
) -> Any:
    ordered_features = tuple(dict.fromkeys(features))

    async def run(index: int) -> Any:
        if index >= len(ordered_features):
            return await operation()
        return await _with_provider_feature_limit(
            config,
            provider,
            ordered_features[index],
            lambda: run(index + 1),
        )

    return await run(0)


def _account_order_for_model(
    config: OpenAICompatConfig,
    router: AccountRouter,
    model_slug: str,
    thinking_effort: str | None,
) -> tuple[str, ...]:
    ordered = router.order()
    if model_slug == "auto":
        return ordered
    supported: list[str] = []
    unknown: list[str] = []
    unsupported: list[str] = []
    for account in ordered:
        support = _account_supports_model(config, account, model_slug, thinking_effort)
        if support is True:
            supported.append(account)
        elif support is False:
            unsupported.append(account)
        else:
            unknown.append(account)
    return tuple(supported + unknown + unsupported) if supported or unknown else ordered


async def _account_order_with_usage_preflight(
    config: OpenAICompatConfig,
    router: AccountRouter,
    model_slug: str,
    thinking_effort: str | None,
    requirements: tuple[tuple[tuple[str, ...], int], ...],
) -> list[tuple[str, dict[str, Any] | None]]:
    ordered = _account_order_for_model(config, router, model_slug, thinking_effort)
    if not requirements or len(ordered) <= 1 or router.strategy == "sticky":
        return [(account, None) for account in ordered]

    ranked: list[tuple[tuple[int, float], int, str, dict[str, Any] | None]] = []
    for index, account in enumerate(ordered):
        provider = _provider_for_account(config, account)
        metadata = await _conversation_init_metadata(provider, model_slug)
        ranked.append((_usage_preflight_rank(metadata, requirements), index, account, metadata))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [(account, metadata) for _rank, _index, account, metadata in ranked]


def _usage_preflight_rank(
    init_metadata: dict[str, Any] | None,
    requirements: tuple[tuple[tuple[str, ...], int], ...],
) -> tuple[int, float]:
    if not isinstance(init_metadata, dict):
        return (1, 0.0)

    score = 0.0
    for names, minimum_remaining in requirements:
        blocked = _matching_feature(init_metadata.get("blocked_features"), *names)
        if blocked is not None:
            return (3, 0.0)
        progress = _matching_feature(init_metadata.get("limits_progress"), *names)
        if progress is None:
            score += -1_000_000_000.0
            continue
        remaining = progress.get("remaining")
        if not isinstance(remaining, (int, float)):
            score += -1_000_000_000.0
            continue
        if remaining < minimum_remaining:
            return (2, float(minimum_remaining - remaining))
        score += -float(remaining)
    return (0, score)


def _upload_usage_requirements(input_image_count: int) -> tuple[tuple[tuple[str, ...], int], ...]:
    if input_image_count <= 0:
        return ()
    return ((USAGE_FEATURE_ALIASES["file_upload"], input_image_count),)


def _image_usage_requirements(input_image_count: int) -> tuple[tuple[tuple[str, ...], int], ...]:
    requirements: list[tuple[tuple[str, ...], int]] = [(USAGE_FEATURE_ALIASES["image_gen"], 1)]
    if input_image_count > 0:
        requirements.append((USAGE_FEATURE_ALIASES["file_upload"], input_image_count))
    return tuple(requirements)


def _research_usage_requirements() -> tuple[tuple[tuple[str, ...], int], ...]:
    return ((USAGE_FEATURE_ALIASES["deep_research"], 1),)


def _account_supports_model(
    config: OpenAICompatConfig,
    account: str,
    model_slug: str,
    thinking_effort: str | None,
) -> bool | None:
    try:
        capture = CapturedRequest.from_file(resolve_account_capture_path(account, config.accounts_dir))
    except Exception:
        return None
    settings_path = resolve_account_settings_path(account, config.accounts_dir)
    settings = load_settings_file(str(settings_path)) if settings_path.exists() else {}
    capabilities = infer_account_capabilities(detect_account_info(capture, settings))
    supported_models = set(capabilities.get("supported_models") or [])
    if model_slug not in supported_models:
        return False
    if model_slug == "gpt-5-5-thinking" and thinking_effort:
        return thinking_effort in set(capabilities.get("thinking_efforts") or [])
    if model_slug == "gpt-5-5-pro" and thinking_effort:
        return thinking_effort in set(capabilities.get("pro_efforts") or [])
    return True


def run_server(config: OpenAICompatConfig) -> None:
    accounts = _accounts_for_config(config)
    router = AccountRouter(accounts, config.account_strategy)
    _configure_account_limits(config, router)
    _admin_store(config)
    server = ThreadingHTTPServer((config.host, config.port), _handler_class(config, router))
    print(f"chatgpt-api bridge server listening on http://{config.host}:{config.port}")
    print(f"accounts={', '.join(accounts)}")
    print(f"account_strategy={router.strategy}")
    print(f"agent_prompt_mode={_normalize_agent_prompt_mode(config.agent_prompt_mode)}")
    print(f"model_fallback={config.model_fallback or 'none'}")
    print(f"temporary_chat={str(config.temporary_chat).lower()}")
    print("admin_api=/v1/chatgpt/admin/*")
    print(f"console={_console_url_for_config(config)}")
    print(f"console_command={_console_command_for_config(config)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_class(config: OpenAICompatConfig, router: AccountRouter | None = None):
    router = router or AccountRouter(_accounts_for_config(config), config.account_strategy)
    _configure_account_limits(config, router)

    class OpenAICompatHandler(BaseHTTPRequestHandler):
        server_version = "chatgpt-api-openai-compat/0.1"

        def do_OPTIONS(self) -> None:  # noqa: N802
            _send_cors_preflight(self)

        def do_HEAD(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            download_id = _download_file_id_from_path(parsed_url.path)
            if download_id:
                _send_download_file(self, download_id, admin_db_path=config.admin_db_path, include_body=False)
                return
            _send_json(self, 404, {"error": {"message": "route not found", "type": "not_found"}})

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            download_id = _download_file_id_from_path(path)
            if download_id:
                _send_download_file(self, download_id, admin_db_path=config.admin_db_path)
                return
            if path == "/admin" or path == "/admin/":
                _send_console_redirect(self, config)
                return
            if path.startswith("/admin/"):
                _send_json(
                    self,
                    404,
                    {
                        "error": {
                            "message": f"console runs on a separate port: {_console_url_for_config(config)}",
                            "type": "not_found",
                        }
                    },
                )
                return
            if not _authorize(self, config.api_key):
                return
            if path == "/health":
                _send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "account": router.accounts[0],
                        "accounts": list(router.accounts),
                        "account_strategy": router.strategy,
                        "account_concurrency": {
                            account: router.account_limit(account) for account in router.accounts
                        },
                        "artifact_downloads": {
                            "route": "/v1/chatgpt/files/{id}/{filename}",
                            "public_base_url": config.public_base_url,
                        },
                    },
                )
                return
            if path == "/v1/models":
                _send_json(self, 200, _models_response(config))
                return
            if path in {"/v1/chatgpt/usage", "/v1/chatgpt/remaining", "/v1/chatgpt/usage/live"}:
                usage = asyncio.run(_account_usage_response(config, router))
                if _query_value(query, "format") in {"table", "text", "markdown", "md"}:
                    _send_text(self, 200, _account_usage_markdown_table(usage))
                else:
                    _send_json(self, 200, usage)
                return
            if path == "/v1/chatgpt/admin/status":
                _send_json(self, 200, _admin_status_response(config, router))
                return
            if path == "/v1/chatgpt/admin/accounts":
                _send_json(self, 200, _admin_accounts_response(config, router))
                return
            if path == "/v1/chatgpt/admin/artifacts":
                _send_json(self, 200, _admin_artifacts_response(config, query))
                return
            if path == "/v1/chatgpt/admin/opencode":
                _send_json(self, 200, _admin_opencode_status(config))
                return
            if path == "/v1/chatgpt/admin/settings":
                _send_json(self, 200, {"object": "chatgpt.admin.settings", "settings": _bridge_settings(config)})
                return
            operation_id = _operation_id_from_path(path)
            if operation_id:
                status, payload = _get_chatgpt_operation(operation_id)
                _send_json(self, status, payload)
                return
            _send_json(self, 404, {"error": {"message": "not found", "type": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            if not _authorize(self, config.api_key):
                return
            path = urlparse(self.path).path
            operation_id = _cancel_operation_id_from_path(path)
            if operation_id:
                status, payload = _cancel_chatgpt_operation(operation_id)
                _send_json(self, status, payload)
                return
            if path.startswith("/v1/chatgpt/admin/"):
                try:
                    body = _read_json_body(self)
                    status, payload = asyncio.run(_admin_post_response(config, router, path, body))
                except ProviderError as exc:
                    status, payload = _provider_error_status_and_payload(exc)
                except ValueError as exc:
                    status, payload = 400, {"error": {"message": str(exc), "type": "invalid_request_error"}}
                _send_json(self, status, payload)
                return
            if path not in {"/v1/chat/completions", "/v1/images/generations", "/v1/images/edits", "/v1/chatgpt/vision"}:
                _send_json(self, 404, {"error": {"message": "not found", "type": "not_found"}})
                return
            try:
                body = _read_json_body(self)
                if path == "/v1/chat/completions" and body.get("stream"):
                    asyncio.run(_chat_completion_stream(config, body, router, self))
                    return
                if path == "/v1/chat/completions":
                    response = asyncio.run(_chat_completion(config, body, router))
                elif path == "/v1/images/generations":
                    response = asyncio.run(_image_generation(config, body, router))
                elif path == "/v1/images/edits":
                    response = asyncio.run(_image_edit(config, body, router))
                else:
                    response = asyncio.run(_vision_request(config, body, router))
            except ProviderError as exc:
                status, payload = _provider_error_status_and_payload(exc)
                _send_json(self, status, payload)
                return
            except _ClientDisconnected:
                return
            except ValueError as exc:
                _send_json(self, 400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
                return
            _send_json(self, 200, response)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return OpenAICompatHandler


async def _chat_completion(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    router: AccountRouter | None = None,
) -> dict[str, Any]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    requested_model = _str_or_none(body.get("model")) or "gpt-5-5"
    model, model_agent_mode = _split_model_agent_mode(requested_model)
    agent_prompt_mode = _resolve_agent_prompt_mode(config, body, model_agent_mode)
    model_slug, thinking_effort = _resolve_model_alias(model, _str_or_none(body.get("thinking_effort")))
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    temporary_chat = _resolve_temporary_chat_mode(config, body)
    router = _router_for_request(config, router, body)
    local_command_response = await _maybe_handle_local_chatgpt_command(config, messages, requested_model, router)
    if local_command_response is not None:
        return local_command_response
    image_response = await _maybe_handle_chat_image_request(config, body, messages, requested_model, router)
    if image_response is not None:
        return image_response
    deep_research_response = await _maybe_handle_deep_research_request(
        config,
        body,
        messages,
        requested_model,
        model_slug,
        router,
    )
    if deep_research_response is not None:
        return deep_research_response
    request_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    requested_operation_id = _str_or_none(body.get("chatgpt_operation_id")) or _str_or_none(
        request_metadata.get("chatgpt_operation_id")
    )
    operation = _create_chatgpt_operation("chat", requested_operation_id) if requested_operation_id else None
    operation_id = operation.operation_id if operation else None
    operation_extra = {"chatgpt_operation_id": operation_id} if operation_id else None
    if not _should_use_agent_bridge(body, tools, model_agent_mode):
        try:
            account, provider, text = await _collect_messages_text_with_accounts(
                config,
                router,
                messages,
                requested_model,
                model_slug,
                thinking_effort,
                temporary_chat,
                operation_id=operation_id,
            )
            if not text:
                raise OpenAICompatProviderError(
                    _empty_response_error(),
                    requested_model,
                    model_slug,
                    await _conversation_init_metadata(provider, model_slug),
                    account=account,
                )
            return _completion_response(requested_model, text, [], account=account, extra=operation_extra)
        finally:
            _finish_chatgpt_operation(operation_id)
    prompt = _build_chat_prompt(messages, tools, body.get("tool_choice"), agent_prompt_mode)
    fallback_model_used: str | None = None
    active_model_slug = model_slug
    active_thinking_effort = thinking_effort
    try:
        account, provider, text = await _collect_prompt_text_with_accounts(
            config,
            router,
            prompt,
            requested_model,
            model_slug,
            thinking_effort,
            temporary_chat,
            operation_id=operation_id,
        )
    except OpenAICompatProviderError as exc:
        fallback_model = _model_fallback_for_config(config, model_slug)
        if fallback_model and _should_try_fallback_model(exc):
            fallback_model_slug, fallback_effort = _resolve_model_alias(fallback_model, None)
            account, provider, text = await _collect_prompt_text_with_accounts(
                config,
                router,
                prompt,
                requested_model,
                fallback_model_slug,
                fallback_effort,
                temporary_chat,
                operation_id=operation_id,
            )
            active_model_slug = fallback_model_slug
            active_thinking_effort = fallback_effort
            fallback_model_used = fallback_model_slug
        else:
            raise
    if not text:
        if _has_successful_tool_result_after_latest_user(messages) or _has_completed_file_action_after_latest_user(messages):
            return _completion_response(requested_model, "Done.", [], account=account, extra=operation_extra)
        raise OpenAICompatProviderError(
            _empty_response_error(),
            requested_model,
            active_model_slug,
            await _conversation_init_metadata(provider, active_model_slug),
            account=account,
        )
    if _has_completed_file_action_after_latest_user(messages) and (
        _response_is_tool_call_json(text) or _response_abandons_workspace_action(text)
    ):
        return _completion_response(
            requested_model,
            "Done.",
            [],
            account=account,
            fallback_model=fallback_model_used,
            extra=operation_extra,
        )
    if _has_successful_tool_result_after_latest_user(messages) and _response_is_tool_call_json(text):
        return _completion_response(
            requested_model,
            "Done.",
            [],
            account=account,
            fallback_model=fallback_model_used,
            extra=operation_extra,
        )
    tool_calls = _filter_repeated_successful_tool_calls(_parse_tool_calls(text, tools), messages)
    text, tool_calls = await _retry_tool_policy_issues(
        provider,
        prompt,
        messages,
        tools,
        text,
        tool_calls,
        requested_model,
        active_model_slug,
        active_thinking_effort,
        temporary_chat,
    )
    text, tool_calls = await _retry_low_quality_tool_calls(
        provider,
        prompt,
        messages,
        tools,
        text,
        tool_calls,
        requested_model,
        active_model_slug,
        active_thinking_effort,
        temporary_chat,
    )
    if not tool_calls and _should_retry_for_missing_tool_call(messages, tools, body.get("tool_choice"), text):
        try:
            text = await _collect_prompt_text(
                provider,
                _build_missing_tool_retry_prompt(prompt, text),
                active_model_slug,
                active_thinking_effort,
                temporary_chat,
                operation_id=operation_id,
            )
        except ProviderError as exc:
            raise OpenAICompatProviderError(
                exc,
                requested_model,
                active_model_slug,
                await _conversation_init_metadata(provider, active_model_slug),
            ) from exc
        if not text:
            raise OpenAICompatProviderError(
                _empty_response_error(),
                requested_model,
                active_model_slug,
                await _conversation_init_metadata(provider, active_model_slug),
            )
        tool_calls = _filter_repeated_successful_tool_calls(_parse_tool_calls(text, tools), messages)
        text, tool_calls = await _retry_tool_policy_issues(
            provider,
            prompt,
            messages,
            tools,
            text,
            tool_calls,
            requested_model,
            active_model_slug,
            active_thinking_effort,
            temporary_chat,
        )
        text, tool_calls = await _retry_low_quality_tool_calls(
            provider,
            prompt,
            messages,
            tools,
            text,
            tool_calls,
            requested_model,
            active_model_slug,
            active_thinking_effort,
            temporary_chat,
        )
    try:
        return _completion_response(
            requested_model,
            text,
            tool_calls,
            account=account,
            fallback_model=fallback_model_used,
            extra=operation_extra,
        )
    finally:
        _finish_chatgpt_operation(operation_id)


async def _chat_completion_stream(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    router: AccountRouter,
    handler: BaseHTTPRequestHandler,
) -> None:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    requested_model = _str_or_none(body.get("model")) or "gpt-5-5"
    model, model_agent_mode = _split_model_agent_mode(requested_model)
    agent_prompt_mode = _resolve_agent_prompt_mode(config, body, model_agent_mode)
    model_slug, thinking_effort = _resolve_model_alias(model, _str_or_none(body.get("thinking_effort")))
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    temporary_chat = _resolve_temporary_chat_mode(config, body)
    router = _router_for_request(config, router, body)

    local_command_response = await _maybe_handle_local_chatgpt_command(config, messages, requested_model, router)
    if local_command_response is not None:
        _send_sse_completion(handler, local_command_response)
        return
    image_response = await _maybe_handle_chat_image_request(config, body, messages, requested_model, router)
    if image_response is not None:
        _send_sse_completion(handler, image_response)
        return
    deep_research_response = await _maybe_handle_deep_research_request(
        config,
        body,
        messages,
        requested_model,
        model_slug,
        router,
    )
    if deep_research_response is not None:
        _send_sse_completion(handler, deep_research_response)
        return

    operation = _create_chatgpt_operation("chat")
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    chunk_base = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": requested_model,
        "chatgpt_operation_id": operation.operation_id,
    }

    try:
        _send_sse_headers(handler, {"X-ChatGPT-Operation-Id": operation.operation_id})
        _write_sse(
            handler,
            {**chunk_base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        )

        def on_conversation_id(conversation_id: str) -> None:
            chunk_base["chatgpt_conversation_id"] = conversation_id

        if not _should_use_agent_bridge(body, tools, model_agent_mode):
            await _stream_messages_text_with_accounts(
                config,
                router,
                messages,
                requested_model,
                model_slug,
                thinking_effort,
                temporary_chat,
                lambda delta: _write_sse_content(handler, chunk_base, delta),
                operation_id=operation.operation_id,
                on_conversation_id=on_conversation_id,
            )
            _write_sse_finish(handler, chunk_base, "stop")
            return

        prompt = _build_chat_prompt(messages, tools, body.get("tool_choice"), agent_prompt_mode)
        streamed = _AgentStreamState(handler, chunk_base)
        text = await _stream_prompt_text_with_accounts(
            config,
            router,
            prompt,
            requested_model,
            model_slug,
            thinking_effort,
            temporary_chat,
            streamed.feed,
            operation_id=operation.operation_id,
            on_conversation_id=on_conversation_id,
        )
        if streamed.started_content:
            streamed.flush_content()
            _write_sse_finish(handler, chunk_base, "stop")
            return

        tool_calls = _filter_repeated_successful_tool_calls(_parse_tool_calls(text, tools), messages)
        if tool_calls:
            _write_sse_tool_calls(handler, chunk_base, tool_calls)
            _write_sse_finish(handler, chunk_base, "tool_calls")
            return

        if text:
            _write_sse_content(handler, chunk_base, text)
        _write_sse_finish(handler, chunk_base, "stop")
    except ProviderError as exc:
        status, payload = _provider_error_status_and_payload(exc)
        message = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else str(exc)
        _write_sse_content(handler, chunk_base, f"ChatGPT provider error ({status}): {message}")
        _write_sse_finish(handler, chunk_base, "stop")
    finally:
        _finish_chatgpt_operation(operation.operation_id)


class _AgentStreamState:
    def __init__(self, handler: BaseHTTPRequestHandler, chunk_base: dict[str, Any]) -> None:
        self.handler = handler
        self.chunk_base = chunk_base
        self.buffer = ""
        self.started_content = False

    def feed(self, delta: str) -> None:
        if not delta:
            return
        if self.started_content:
            _write_sse_content(self.handler, self.chunk_base, delta)
            return

        self.buffer += delta
        stripped = self.buffer.lstrip()
        if not stripped:
            return
        if stripped.startswith("{"):
            return

        self.started_content = True
        _write_sse_content(self.handler, self.chunk_base, self.buffer)
        self.buffer = ""

    def flush_content(self) -> None:
        if self.buffer:
            _write_sse_content(self.handler, self.chunk_base, self.buffer)
            self.buffer = ""


async def _stream_messages_text_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    messages: list[Any],
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    on_delta: Any,
    operation_id: str | None = None,
    on_conversation_id: Any | None = None,
) -> tuple[str, ChatGPTProvider, str]:
    provider_messages = _openai_messages_to_provider_messages(messages)
    input_image_count = _provider_message_image_count(provider_messages)
    attempts: list[dict[str, Any]] = []
    last_error: OpenAICompatProviderError | None = None
    for account, preflight_metadata in await _account_order_with_usage_preflight(
        config,
        router,
        model_slug,
        thinking_effort,
        _upload_usage_requirements(input_image_count),
    ):
        provider = _provider_for_account(config, account)
        init_metadata = preflight_metadata
        if input_image_count:
            if init_metadata is None:
                init_metadata = await _conversation_init_metadata(provider, model_slug)
            preflight_error = _input_upload_preflight_error(init_metadata, input_image_count)
            if preflight_error is not None:
                compat_error = OpenAICompatProviderError(
                    preflight_error,
                    requested_model,
                    model_slug,
                    init_metadata,
                    account=account,
                    account_attempts=attempts.copy(),
                )
                attempts.append(_account_attempt_summary(account, compat_error))
                last_error = compat_error
                if _should_try_next_account(router, compat_error):
                    continue
                raise compat_error from preflight_error
        _update_chatgpt_operation(operation_id, account=account, provider=provider)
        chunks: list[str] = []
        conversation_id: str | None = None

        async def operation() -> None:
            nonlocal conversation_id
            async for delta in provider.stream_chat(
                ChatRequest(
                    messages=provider_messages,
                    model=model_slug,
                    thinking_effort=thinking_effort,
                    stream=True,
                    metadata={"history_and_training_disabled": temporary_chat},
                )
            ):
                if delta.conversation_id:
                    conversation_id = delta.conversation_id
                    _update_chatgpt_operation(operation_id, conversation_id=conversation_id)
                    if on_conversation_id is not None:
                        on_conversation_id(conversation_id)
                    if _chatgpt_operation_cancel_requested(operation_id):
                        _stop_chatgpt_operation_by_id(operation_id)
                        raise _ClientDisconnected()
                if delta.text:
                    chunks.append(delta.text)
                    try:
                        on_delta(delta.text)
                    except _ClientDisconnected:
                        await _stop_chatgpt_conversation_after_disconnect(provider, conversation_id)
                        raise

        try:
            features = ("upload", "chat") if input_image_count else ("chat",)
            await _with_provider_feature_limits(
                config,
                provider,
                features,
                lambda: _with_provider_account_limit(provider, operation),
            )
        except _ClientDisconnected:
            raise
        except ProviderError as exc:
            if chunks:
                raise
            init_metadata = init_metadata or await _conversation_init_metadata(provider, model_slug)
            compat_error = OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from exc
        text = "".join(chunks).strip()
        if text:
            return account, provider, text

        init_metadata = await _conversation_init_metadata(provider, model_slug)
        compat_error = OpenAICompatProviderError(
            _empty_response_error(),
            requested_model,
            model_slug,
            init_metadata,
            account=account,
            account_attempts=attempts.copy(),
        )
        attempts.append(_account_attempt_summary(account, compat_error))
        last_error = compat_error
        if _should_try_next_account(router, compat_error):
            continue
        raise compat_error

    if last_error is not None:
        last_error.account_attempts = attempts
        raise last_error
    raise OpenAICompatProviderError(
        ProviderError("No ChatGPT accounts are configured"),
        requested_model,
        model_slug,
        account_attempts=attempts,
    )


async def _stream_prompt_text_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    prompt: str,
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    on_delta: Any,
    operation_id: str | None = None,
    on_conversation_id: Any | None = None,
) -> str:
    _account, _provider, text = await _stream_messages_text_with_accounts(
        config,
        router,
        [{"role": "user", "content": prompt}],
        requested_model,
        model_slug,
        thinking_effort,
        temporary_chat,
        on_delta,
        operation_id=operation_id,
        on_conversation_id=on_conversation_id,
    )
    return text


async def _stop_chatgpt_conversation_after_disconnect(
    provider: ChatGPTProvider,
    conversation_id: str | None,
) -> None:
    if not conversation_id:
        return
    try:
        await asyncio.to_thread(
            provider.transport.stop_conversation,
            conversation_id,
            exclude_async_types=["pro_mode"],
        )
    except ProviderError:
        return


async def _image_generation(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    router: AccountRouter | None = None,
) -> dict[str, Any]:
    prompt = _str_or_none(body.get("prompt"))
    if not prompt:
        raise ValueError("prompt must be a non-empty string")
    n = body.get("n", 1)
    if not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive integer")
    if n != 1:
        raise ValueError("only n=1 is currently supported for ChatGPT Web image generation")

    requested_model = _str_or_none(body.get("model")) or "auto"
    model, _ = _split_model_agent_mode(requested_model)
    model_slug, _ = _resolve_model_alias(model, None)
    model_slug = _resolve_image_model_alias(model_slug)
    request_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    operation_id = _str_or_none(body.get("chatgpt_operation_id")) or _str_or_none(
        request_metadata.get("chatgpt_operation_id")
    )
    operation = _create_chatgpt_operation("image", operation_id)
    metadata = {
        "size": body.get("size"),
        "quality": body.get("quality"),
        "style": body.get("style"),
        "response_format": body.get("response_format"),
    }
    router = _router_for_request(config, router, body)
    response_format = _str_or_none(body.get("response_format")) or "url"
    output_path = _str_or_none(body.get("output_path")) or _str_or_none(body.get("path"))
    output_dir = _str_or_none(body.get("output_dir"))

    async def produce() -> dict[str, Any]:
        account, image_response = await _generate_image_with_accounts(
            config,
            router,
            ImageRequest(prompt=prompt, model=model_slug, metadata=metadata),
            requested_model,
            model_slug,
            operation_id=operation.operation_id,
        )
        result = _image_generation_response(
            requested_model,
            image_response,
            response_format=response_format,
            account=account,
            output_path=output_path,
            output_dir=output_dir,
            default_output_dir=config.image_output_dir,
            public_base_url=config.public_base_url,
            admin_db_path=config.admin_db_path,
            prompt=prompt,
        )
        result["chatgpt_operation_id"] = operation.operation_id
        return result

    try:
        return await _dedupe_image_request(
            _image_request_cache_key(
                "images",
                config,
                requested_model,
                model_slug,
                prompt,
                response_format=response_format,
                output_path=output_path,
                output_dir=output_dir,
                metadata=metadata,
            ),
            produce,
        )
    finally:
        _finish_chatgpt_operation(operation.operation_id)


async def _image_edit(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    router: AccountRouter | None = None,
) -> dict[str, Any]:
    prompt = _str_or_none(body.get("prompt"))
    if not prompt:
        raise ValueError("prompt must be a non-empty string")
    input_images = _image_inputs_from_body(body, require=True)
    aspect_ratio = _image_aspect_ratio_from_body(body)
    edit_prompt = _image_edit_prompt(prompt, aspect_ratio)

    requested_model = _str_or_none(body.get("model")) or "auto"
    model, _ = _split_model_agent_mode(requested_model)
    model_slug, _ = _resolve_model_alias(model, None)
    model_slug = _resolve_image_model_alias(model_slug)
    request_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    operation_id = _str_or_none(body.get("chatgpt_operation_id")) or _str_or_none(
        request_metadata.get("chatgpt_operation_id")
    )
    operation = _create_chatgpt_operation("image", operation_id)
    metadata = {
        "source": "images_edits",
        "response_format": body.get("response_format"),
        "aspect_ratio": aspect_ratio,
        "input_image_count": len(input_images),
        "aspect_ratio_warning": IMAGE_EDIT_ASPECT_RATIO_WARNING,
    }
    router = _router_for_request(config, router, body)
    response_format = _str_or_none(body.get("response_format")) or "url"
    output_path = _str_or_none(body.get("output_path")) or _str_or_none(body.get("path"))
    output_dir = _str_or_none(body.get("output_dir"))

    async def produce() -> dict[str, Any]:
        account, image_response = await _generate_image_with_accounts(
            config,
            router,
            ImageRequest(
                prompt=edit_prompt,
                input_images=input_images,
                model=model_slug,
                metadata=metadata,
            ),
            requested_model,
            model_slug,
            operation_id=operation.operation_id,
        )
        result = _image_generation_response(
            requested_model,
            image_response,
            response_format=response_format,
            account=account,
            output_path=output_path,
            output_dir=output_dir,
            default_output_dir=config.image_output_dir,
            public_base_url=config.public_base_url,
            admin_db_path=config.admin_db_path,
            prompt=prompt,
        )
        result["chatgpt_operation_id"] = operation.operation_id
        result["input_image_count"] = len(input_images)
        result["aspect_ratio"] = aspect_ratio
        result["warnings"] = [IMAGE_EDIT_ASPECT_RATIO_WARNING]
        return result

    try:
        return await _dedupe_image_request(
            _image_request_cache_key(
                "image-edit",
                config,
                requested_model,
                model_slug,
                prompt,
                response_format=response_format,
                output_path=output_path,
                output_dir=output_dir,
                metadata={
                    **metadata,
                    "input_images": [(image.name, len(image.data), image.mime_type) for image in input_images],
                },
            ),
            produce,
        )
    finally:
        _finish_chatgpt_operation(operation.operation_id)


async def _vision_request(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    router: AccountRouter | None = None,
) -> dict[str, Any]:
    mode = (_str_or_none(body.get("mode")) or "custom").strip().lower()
    if mode not in {"custom", "ocr", "describe"}:
        raise ValueError("mode must be one of: custom, ocr, describe")
    prompt = _str_or_none(body.get("prompt")) or _default_vision_prompt(mode)
    input_images = _image_inputs_from_body(body, require=True)
    requested_model = _str_or_none(body.get("model")) or "auto"
    model, _ = _split_model_agent_mode(requested_model)
    model_slug, thinking_effort = _resolve_model_alias(model, _str_or_none(body.get("thinking_effort")))
    temporary_chat = _resolve_temporary_chat_mode(config, body)
    router = _router_for_request(config, router, body)
    parts = [
        *(ContentPart.image_bytes(image.data, image.mime_type, image.name) for image in input_images),
        ContentPart.text_part(prompt),
    ]
    account, _provider, text = await _collect_messages_text_with_accounts(
        config,
        router,
        [{"role": "user", "content": parts}],
        requested_model,
        model_slug,
        thinking_effort,
        temporary_chat,
    )
    response = _completion_response(requested_model, text, [], account=account)
    response.update(
        {
            "object": "chatgpt.vision",
            "text": text,
            "mode": mode,
            "input_image_count": len(input_images),
            "limits_note": (
                "A single request can attach up to 10 images. The bridge preflights reported "
                "file_upload and image quotas when ChatGPT exposes them, but hidden burst limits may still apply."
            ),
        }
    )
    return response


async def _maybe_handle_chat_image_request(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    messages: list[Any],
    requested_model: str,
    router: AccountRouter,
) -> dict[str, Any] | None:
    if _latest_message_role(messages) != "user":
        return None
    latest = _latest_user_message_text(messages)
    if not _chat_image_intercept_enabled(body, latest):
        return None
    if not _latest_user_requests_image_generation(latest):
        return None
    model, _ = _split_model_agent_mode(requested_model)
    model_slug, _ = _resolve_model_alias(model, None)
    model_slug = _resolve_image_model_alias(model_slug)
    output_path = _image_output_path_from_text(latest)

    async def produce() -> dict[str, Any]:
        account, image_response = await _generate_image_with_accounts(
            config,
            router,
            ImageRequest(prompt=latest, model=model_slug, metadata={"source": "chat_completion_image_intercept"}),
            requested_model,
            model_slug,
        )
        saved_paths = _save_image_response(
            image_response,
            output_path=output_path,
            output_dir=None,
            default_output_dir=config.image_output_dir,
        )
        if not saved_paths:
            raise ProviderError("ChatGPT generated an image but no downloadable image bytes were available")
        assets = [
            _register_download_file(
                path,
                public_base_url=config.public_base_url,
                admin_db_path=config.admin_db_path,
                kind="image",
                account=account,
                prompt=latest,
                metadata={"source": "chat_completion_image_intercept", "requested_model": requested_model},
            )
            for path in saved_paths
        ]
        path_text = "\n".join(str(path) for path in saved_paths)
        link_text = "\n".join(str(asset["download_url"]) for asset in assets)
        return _completion_response(
            requested_model,
            f"Image generated.\nSaved files:\n{path_text}\nDownload links:\n{link_text}",
            [],
            account=account,
            extra={"chatgpt_image_assets": assets},
        )

    return await _dedupe_image_request(
        _image_request_cache_key(
            "chat-image",
            config,
            requested_model,
            model_slug,
            latest,
            output_path=output_path,
            metadata={"source": "chat_completion_image_intercept"},
        ),
        produce,
    )


def _chat_image_intercept_enabled(body: dict[str, Any], latest_user_text: str) -> bool:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    explicit = (
        _bool_or_none(body.get("chatgpt_image_intercept"))
        if "chatgpt_image_intercept" in body
        else _bool_or_none(metadata.get("chatgpt_image_intercept"))
    )
    if explicit is not None:
        return explicit
    return not _looks_like_structured_app_prompt(latest_user_text)


def _looks_like_structured_app_prompt(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    lowered = stripped[:12000].lower()
    return any(
        marker in lowered
        for marker in (
            '"output_contract"',
            '"state_patch"',
            '"current_state"',
            '"recent_transcript"',
            '"image_prompt"',
            '"return valid json only"',
        )
    )


async def _maybe_handle_deep_research_request(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    messages: list[Any],
    requested_model: str,
    model_slug: str,
    router: AccountRouter,
) -> dict[str, Any] | None:
    latest = _latest_user_message_text(messages)
    if not latest or not _request_is_deep_research(body, requested_model):
        return None
    if _latest_message_role(messages) != "user":
        return _completion_response(
            requested_model,
            "Deep Research request already has follow-up messages in the transcript.",
            [],
        )

    request_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    requested_operation_id = _str_or_none(body.get("chatgpt_operation_id")) or _str_or_none(
        request_metadata.get("chatgpt_operation_id")
    )
    operation = _create_chatgpt_operation("research", requested_operation_id)
    try:
        account, _, text, research_metadata = await _collect_deep_research_with_accounts(
            config,
            router,
            latest,
            requested_model,
            model_slug,
            operation_id=operation.operation_id,
        )
        if not text:
            raise OpenAICompatProviderError(
                _empty_response_error(),
                requested_model,
                model_slug,
                account=account,
            )
        report_text = _clean_deep_research_markdown(text)
        report_path = _save_deep_research_report(config, body, latest, report_text)
        report_asset = _register_download_file(
            report_path,
            content_type="text/markdown; charset=utf-8",
            public_base_url=config.public_base_url,
            admin_db_path=config.admin_db_path,
            kind="research",
            account=account,
            prompt=latest,
            metadata=research_metadata,
        )
        return _completion_response(
            requested_model,
            _deep_research_done_message(report_path, str(report_asset["download_url"])),
            [],
            account=account,
            extra={
                "chatgpt_operation_id": operation.operation_id,
                "chatgpt_research_report_path": str(report_path),
                "chatgpt_research_report_download_url": report_asset["download_url"],
                "chatgpt_research_report_file": report_asset,
                "chatgpt_research": research_metadata,
            },
        )
    finally:
        _finish_chatgpt_operation(operation.operation_id)


def _image_request_with_operation_hooks(request: ImageRequest, operation_id: str | None) -> ImageRequest:
    if not operation_id:
        return request

    def on_conversation_id(conversation_id: str) -> None:
        _update_chatgpt_operation(operation_id, conversation_id=conversation_id)
        if _chatgpt_operation_cancel_requested(operation_id):
            _stop_chatgpt_operation_by_id(operation_id)

    metadata = dict(request.metadata)
    metadata["on_conversation_id"] = on_conversation_id
    metadata["cancel_requested"] = lambda: _chatgpt_operation_cancel_requested(operation_id)
    return ImageRequest(
        prompt=request.prompt,
        image=request.image,
        image_mime_type=request.image_mime_type,
        input_images=request.input_images,
        model=request.model,
        metadata=metadata,
    )


async def _generate_image_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    request: ImageRequest,
    requested_model: str,
    model_slug: str,
    operation_id: str | None = None,
) -> tuple[str, ImageResponse]:
    attempts: list[dict[str, Any]] = []
    last_error: OpenAICompatProviderError | None = None
    input_image_count = len(request.input_images) + (1 if request.image is not None else 0)
    for account, init_metadata in await _account_order_with_usage_preflight(
        config,
        router,
        model_slug,
        None,
        _image_usage_requirements(input_image_count),
    ):
        provider = _provider_for_account(config, account)
        if init_metadata is None:
            init_metadata = await _conversation_init_metadata(provider, model_slug)
        preflight_error = _image_request_preflight_error(init_metadata, input_image_count)
        if preflight_error is not None:
            compat_error = OpenAICompatProviderError(
                preflight_error,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from preflight_error
        try:
            _update_chatgpt_operation(operation_id, account=account, provider=provider)
            request_for_account = _image_request_with_operation_hooks(request, operation_id)
            features = ("upload", "image") if input_image_count else ("image",)
            response = await _with_provider_feature_limits(
                config,
                provider,
                features,
                lambda: _with_provider_account_limit(provider, lambda: provider.generate_image(request_for_account)),
            )
        except ProviderError as exc:
            compat_error = OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from exc
        if response.images:
            return account, response
        compat_error = OpenAICompatProviderError(
            ProviderError("ChatGPT image generation returned no images"),
            requested_model,
            model_slug,
            await _conversation_init_metadata(provider, model_slug),
            account=account,
            account_attempts=attempts.copy(),
        )
        attempts.append(_account_attempt_summary(account, compat_error))
        last_error = compat_error
        if _should_try_next_account(router, compat_error):
            continue
        raise compat_error

    if last_error is not None:
        last_error.account_attempts = attempts
        raise last_error
    raise OpenAICompatProviderError(
        ProviderError("No ChatGPT accounts are configured"),
        requested_model,
        model_slug,
        account_attempts=attempts,
    )


def _image_generation_response(
    requested_model: str,
    response: ImageResponse,
    response_format: str,
    account: str | None = None,
    output_path: str | None = None,
    output_dir: str | None = None,
    default_output_dir: Path | None = None,
    public_base_url: str | None = None,
    admin_db_path: Path | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    normalized_format = response_format.strip().lower()
    if normalized_format not in {"url", "b64_json"}:
        raise ValueError("response_format must be either 'url' or 'b64_json'")
    saved_paths = _save_image_response(
        response,
        output_path=output_path,
        output_dir=output_dir,
        default_output_dir=default_output_dir or Path("outputs/chatgpt-images"),
    )
    data: list[dict[str, str]] = []
    for index, image in enumerate(response.images):
        saved_path = saved_paths[index] if index < len(saved_paths) else None
        if normalized_format == "b64_json":
            if image.data is None:
                raise ProviderError("ChatGPT image response did not include image bytes")
            entry = {"b64_json": base64.b64encode(image.data).decode("ascii")}
            if saved_path:
                asset = _register_download_file(
                    saved_path,
                    public_base_url=public_base_url,
                    admin_db_path=admin_db_path,
                    kind="image",
                    account=account,
                    prompt=prompt or response.prompt,
                    metadata={"requested_model": requested_model, "response_format": response_format},
                )
                entry["path"] = str(saved_path)
                entry["download_url"] = str(asset["download_url"])
                entry["file_id"] = str(asset["id"])
            data.append(entry)
            continue
        if saved_path:
            asset = _register_download_file(
                saved_path,
                public_base_url=public_base_url,
                admin_db_path=admin_db_path,
                kind="image",
                account=account,
                prompt=prompt or response.prompt,
                metadata={"requested_model": requested_model, "response_format": response_format},
            )
            data.append(
                {
                    "url": str(asset["download_url"]),
                    "download_url": str(asset["download_url"]),
                    "path": str(saved_path),
                    "filename": str(asset["filename"]),
                    "file_id": str(asset["id"]),
                }
            )
            continue
        if image.url:
            data.append({"url": image.url})
        elif image.data is not None:
            mime_type = image.mime_type or "image/png"
            encoded = base64.b64encode(image.data).decode("ascii")
            data.append({"url": f"data:{mime_type};base64,{encoded}"})
    result: dict[str, Any] = {
        "created": int(time.time()),
        "model": requested_model,
        "data": data,
    }
    if account:
        result["chatgpt_account"] = account
    return result


def _save_image_response(
    response: ImageResponse,
    output_path: str | None,
    output_dir: str | None,
    default_output_dir: Path,
) -> list[Path]:
    saved_paths: list[Path] = []
    for index, image in enumerate(response.images):
        data = _image_asset_bytes(image)
        if data is None:
            continue
        target = _resolve_image_output_path(
            output_path,
            output_dir,
            default_output_dir,
            response.prompt,
            image.mime_type,
            index,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved_paths.append(target)
    return saved_paths


def _image_asset_bytes(image: Any) -> bytes | None:
    data = getattr(image, "data", None)
    if data is not None:
        return data
    url = getattr(image, "url", None)
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    match = re.match(r"^data:[^;]+;base64,(.+)$", url, flags=re.DOTALL)
    if not match:
        return None
    return base64.b64decode(match.group(1))


def _save_deep_research_report(
    config: OpenAICompatConfig,
    body: dict[str, Any],
    prompt: str,
    text: str,
) -> Path:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    output_path = (
        _str_or_none(metadata.get("output_path"))
        or _str_or_none(metadata.get("path"))
        or _str_or_none(body.get("output_path"))
        or _str_or_none(body.get("path"))
    )
    output_dir = _str_or_none(metadata.get("output_dir")) or _str_or_none(body.get("output_dir"))
    target = _resolve_research_output_path(output_path, output_dir, config.research_output_dir, prompt)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _deep_research_done_message(report_path: Path, download_url: str) -> str:
    return f"Deep Research complete.\nSaved report: {report_path}\nDownload link: {download_url}"


def _clean_deep_research_markdown(text: str) -> str:
    cleaned = re.sub(r"\s*cite[^]+", "", text)
    cleaned = re.sub(r"\s*【\d+†[^】]+】", "", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _resolve_research_output_path(
    output_path: str | None,
    output_dir: str | None,
    default_output_dir: Path,
    prompt: str,
) -> Path:
    if output_path:
        path = Path(output_path).expanduser()
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            path = path / _generated_research_filename(prompt)
        return path.resolve()
    directory = Path(output_dir).expanduser() if output_dir else default_output_dir.expanduser()
    return (directory / _generated_research_filename(prompt)).resolve()


def _generated_research_filename(prompt: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9ก-๙]+", "-", prompt.strip().lower()).strip("-")
    slug = slug[:56] or "deep-research"
    return f"{int(time.time())}-{slug}.md"


def _resolve_image_output_path(
    output_path: str | None,
    output_dir: str | None,
    default_output_dir: Path,
    prompt: str,
    mime_type: str | None,
    index: int,
) -> Path:
    extension = _image_extension(mime_type)
    if output_path:
        path = Path(output_path).expanduser()
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            path = path / _generated_image_filename(prompt, extension, index)
        return path.resolve()
    directory = Path(output_dir).expanduser() if output_dir else default_output_dir.expanduser()
    return (directory / _generated_image_filename(prompt, extension, index)).resolve()


def _generated_image_filename(prompt: str, extension: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9ก-๙]+", "-", prompt.strip().lower()).strip("-")
    slug = slug[:48] or "image"
    suffix = f"-{index + 1}" if index else ""
    return f"{int(time.time())}-{slug}{suffix}{extension}"


def _image_extension(mime_type: str | None) -> str:
    normalized = (mime_type or "image/png").split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized, ".png")


def _register_download_file(
    path: Path,
    content_type: str | None = None,
    public_base_url: str | None = None,
    admin_db_path: Path | None = None,
    kind: str = "file",
    account: str | None = None,
    prompt: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    filename = resolved.name or "download"
    file_id = uuid.uuid4().hex
    detected_content_type = content_type or _guess_download_content_type(resolved)
    with _DOWNLOAD_FILES_LOCK:
        _DOWNLOAD_FILES[file_id] = _DownloadFile(
            path=resolved,
            filename=filename,
            content_type=detected_content_type,
            created_at=time.time(),
        )
    relative_url = f"/v1/chatgpt/files/{file_id}/{quote(filename)}"
    asset = {
        "id": file_id,
        "filename": filename,
        "path": str(resolved),
        "download_url": _public_download_url(relative_url, public_base_url),
        "content_type": detected_content_type,
        "bytes": resolved.stat().st_size if resolved.exists() else None,
    }
    if admin_db_path is not None:
        try:
            BridgeAdminStore(admin_db_path).record_artifact(
                asset,
                kind=kind,
                account=account,
                prompt=prompt,
                metadata=metadata,
            )
        except Exception:
            pass
    return asset


def _download_file_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 4 and parts[:3] == ["v1", "chatgpt", "files"]:
        return parts[3]
    return None


def _send_download_file(
    handler: BaseHTTPRequestHandler,
    file_id: str,
    *,
    admin_db_path: Path | None = None,
    include_body: bool = True,
) -> None:
    with _DOWNLOAD_FILES_LOCK:
        entry = _DOWNLOAD_FILES.get(file_id)
    if entry is None and admin_db_path is not None:
        artifact = BridgeAdminStore(admin_db_path).get_artifact(file_id)
        if artifact is not None:
            path = Path(str(artifact.get("path") or "")).expanduser()
            if not path.is_file():
                _send_json(handler, 410, {"error": {"message": "artifact file is no longer available", "type": "gone"}})
                return
            entry = _DownloadFile(
                path=path,
                filename=str(artifact.get("filename") or path.name or "download"),
                content_type=str(artifact.get("content_type") or _guess_download_content_type(path)),
                created_at=time.time(),
            )
            with _DOWNLOAD_FILES_LOCK:
                _DOWNLOAD_FILES[file_id] = entry
    if entry is None:
        _send_json(handler, 404, {"error": {"message": "artifact not found", "type": "not_found"}})
        return
    if not entry.path.is_file():
        _send_json(handler, 410, {"error": {"message": "artifact file is no longer available", "type": "gone"}})
        return
    size = entry.path.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", entry.content_type)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", "private, max-age=31536000")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Disposition", _content_disposition(entry.filename))
    handler.end_headers()
    if include_body:
        handler.wfile.write(entry.path.read_bytes())


def _send_admin_asset(handler: BaseHTTPRequestHandler, path: str) -> None:
    source_root = _project_root() / "apps" / "bridge-console"
    dist_root = source_root / "dist"
    root = dist_root if (dist_root / "index.html").is_file() else source_root
    relative = "index.html" if path in {"/admin", "/admin/"} else path.removeprefix("/admin/").strip("/")
    if not relative:
        relative = "index.html"
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        _send_json(handler, 404, {"error": {"message": "admin asset not found", "type": "not_found"}})
        return
    if not candidate.is_file():
        _send_json(handler, 404, {"error": {"message": "admin asset not found", "type": "not_found"}})
        return
    body = candidate.read_bytes()
    content_type = _guess_download_content_type(candidate)
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store" if candidate.name == "index.html" else "public, max-age=60")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _console_url_for_config(config: OpenAICompatConfig) -> str:
    explicit = os.environ.get("CHATGPT_CONSOLE_URL")
    if explicit:
        return explicit.rstrip("/")
    return "http://127.0.0.1:5174"


def _console_command_for_config(config: OpenAICompatConfig) -> str:
    explicit = os.environ.get("CHATGPT_CONSOLE_COMMAND")
    if explicit:
        return explicit
    return "bun --cwd apps/bridge-console dev"


def _send_console_redirect(handler: BaseHTTPRequestHandler, config: OpenAICompatConfig) -> None:
    body = json.dumps(
        {
            "ok": True,
            "message": "Bridge console runs on a separate port.",
            "console_url": _console_url_for_config(config),
            "start_console": _console_command_for_config(config),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    handler.send_response(308)
    handler.send_header("Location", _console_url_for_config(config))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _admin_status_response(config: OpenAICompatConfig, router: AccountRouter) -> dict[str, Any]:
    db_path = _admin_db_path(config)
    store = _admin_store(config)
    return {
        "object": "chatgpt.admin.status",
        "ok": True,
        "server": {
            "host": config.host,
            "port": config.port,
            "base_url": f"http://{config.host}:{config.port}/v1",
            "public_base_url": config.public_base_url,
            "api_key_required": bool(config.api_key),
            "auth_mode": "bearer" if config.api_key else "none",
            "console_url": _console_url_for_config(config),
            "console_command": _console_command_for_config(config),
        },
        "routing": {
            "account": config.account,
            "accounts": list(router.accounts),
            "account_strategy": router.strategy,
            "account_concurrency": {account: router.account_limit(account) for account in router.accounts},
            "feature_concurrency": {
                feature: {
                    account: _feature_account_concurrency_limit(config, feature, account)
                    for account in router.accounts
                }
                for feature in ("upload", "image", "research")
            },
            "agent_prompt_mode": _normalize_agent_prompt_mode(config.agent_prompt_mode),
            "model_fallback": config.model_fallback or "none",
            "temporary_chat": config.temporary_chat,
        },
        "settings": _bridge_settings(config),
        "storage": {
            "admin_db_path": str(db_path),
            "image_output_dir": str(config.image_output_dir.expanduser().resolve()),
            "research_output_dir": str(config.research_output_dir.expanduser().resolve()),
            "artifact_count": store.artifact_count(),
        },
        "routes": {
            "health": "/health",
            "models": "/v1/models",
            "usage": "/v1/chatgpt/usage",
            "chat": "/v1/chat/completions",
            "images": "/v1/images/generations",
            "image_edits": "/v1/images/edits",
            "vision": "/v1/chatgpt/vision",
            "files": "/v1/chatgpt/files/{id}/{filename}",
            "operation": "/v1/chatgpt/operations/{operation_id}",
            "operation_cancel": "/v1/chatgpt/operations/{operation_id}/cancel",
            "admin_api": "/v1/chatgpt/admin/*",
        },
    }


def _admin_accounts_response(config: OpenAICompatConfig, router: AccountRouter) -> dict[str, Any]:
    configured = set(router.accounts)
    names = _known_admin_account_names(config, router)
    captures_by_name = {entry["account"]: entry for entry in _admin_store(config).list_account_captures()}
    accounts: list[dict[str, Any]] = []
    for name in names:
        capture_path = resolve_account_capture_path(name, config.accounts_dir)
        settings_path = resolve_account_settings_path(name, config.accounts_dir)
        summary = _account_capture_usage_summary(config, name) if capture_path.exists() else {}
        accounts.append(
            {
                "account": name,
                "configured": name in configured,
                "capture_exists": capture_path.exists(),
                "settings_exists": settings_path.exists(),
                "capture_path": str(capture_path),
                "settings_path": str(settings_path),
                "stored": captures_by_name.get(name),
                **summary,
            }
        )
    return {
        "object": "chatgpt.admin.accounts",
        "accounts": accounts,
        "stored_captures": list(captures_by_name.values()),
    }


def _admin_artifacts_response(config: OpenAICompatConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    limit_text = _query_value(query, "limit", "100")
    try:
        limit = int(limit_text)
    except ValueError:
        limit = 100
    return {
        "object": "chatgpt.admin.artifacts",
        "artifacts": _admin_store(config).list_artifacts(limit=limit),
    }


def _admin_opencode_status(config: OpenAICompatConfig) -> dict[str, Any]:
    config_path = _opencode_config_path()
    state_path = _opencode_state_path()
    opencode_config = _read_json_file(config_path)
    provider = opencode_config.get("provider") if isinstance(opencode_config, dict) else {}
    chatgpt_provider = provider.get("chatgpt-web") if isinstance(provider, dict) else None
    options = chatgpt_provider.get("options") if isinstance(chatgpt_provider, dict) else {}
    model = opencode_config.get("model") if isinstance(opencode_config, dict) else None
    backup_path = _opencode_backup_path(config_path)
    return {
        "object": "chatgpt.admin.opencode",
        "config_path": str(config_path),
        "state_path": str(state_path),
        "config_exists": config_path.exists(),
        "state_exists": state_path.exists(),
        "backup_exists": backup_path.exists(),
        "injected": isinstance(chatgpt_provider, dict),
        "model": model,
        "base_url": options.get("baseURL") if isinstance(options, dict) else None,
        "api_key": "<set>" if isinstance(options, dict) and options.get("apiKey") else None,
        "recommended": {
            "base_url": config.public_base_url or f"http://{config.host}:{config.port}/v1",
            "model": "chatgpt-web/auto@optimized",
        },
    }


async def _admin_post_response(
    config: OpenAICompatConfig,
    router: AccountRouter,
    path: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if path == "/v1/chatgpt/admin/captures/inspect":
        return 200, _inspect_account_capture_payload(config, body)
    if path == "/v1/chatgpt/admin/captures/save":
        return _save_account_capture_payload(config, body)
    if path == "/v1/chatgpt/admin/accounts/check":
        return 200, await _admin_accounts_check_payload(config, router, body)
    if path == "/v1/chatgpt/admin/accounts/delete":
        return 200, _admin_account_delete_payload(config, body)
    if path == "/v1/chatgpt/admin/settings/save":
        return 200, _admin_settings_save_payload(config, router, body)
    if path == "/v1/chatgpt/admin/settings/reset":
        return 200, _admin_settings_reset_payload(config, router)
    if path == "/v1/chatgpt/admin/artifacts/delete":
        return 200, _admin_artifact_delete_payload(config, body)
    if path == "/v1/chatgpt/admin/test/chat":
        started = time.time()
        prompt = _str_or_none(body.get("message")) or _str_or_none(body.get("prompt")) or "Say hello in one sentence."
        messages: list[dict[str, Any]] = []
        system = _str_or_none(body.get("system"))
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = await _chat_completion(
            config,
            {
                "model": _str_or_none(body.get("model")) or "auto",
                "messages": messages,
                "stream": False,
                "metadata": {"source": "bridge_console_test_chat"},
            },
            router,
        )
        content = _completion_text_from_response(response)
        return 200, {"ok": True, "latency_ms": round((time.time() - started) * 1000), "content": content, "response": response}
    if path == "/v1/chatgpt/admin/test/image":
        started = time.time()
        prompt = _str_or_none(body.get("prompt"))
        if not prompt:
            raise ValueError("prompt is required")
        response = await _image_generation(
            config,
            {
                "model": _str_or_none(body.get("model")) or "auto",
                "prompt": prompt,
                "response_format": "url",
                "output_dir": _str_or_none(body.get("output_dir")),
                "metadata": {"source": "bridge_console_test_image"},
            },
            router,
        )
        return 200, {"ok": True, "latency_ms": round((time.time() - started) * 1000), "response": response}
    if path == "/v1/chatgpt/admin/opencode/inject":
        return 200, _opencode_inject_payload(config, body)
    if path == "/v1/chatgpt/admin/opencode/eject":
        return 200, _opencode_eject_payload(body)
    raise ValueError(f"unknown admin route: {path}")


async def _admin_accounts_check_payload(
    config: OpenAICompatConfig,
    router: AccountRouter,
    body: dict[str, Any],
) -> dict[str, Any]:
    requested = _str_or_none(body.get("account"))
    if requested and requested.lower() not in {"all", "*"}:
        accounts = [_safe_account_name(requested)]
    else:
        accounts = _known_admin_account_names(config, router)
    checks = await asyncio.gather(*(_account_usage_entry(config, account, probe_chat=True) for account in accounts))
    return {
        "object": "chatgpt.admin.account_check",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounts": checks,
    }


def _admin_account_delete_payload(config: OpenAICompatConfig, body: dict[str, Any]) -> dict[str, Any]:
    account = _safe_account_name(_str_or_none(body.get("account")) or "")
    delete_capture = body.get("delete_capture", True) is not False
    delete_settings = body.get("delete_settings", True) is not False
    capture_path = resolve_account_capture_path(account, config.accounts_dir)
    settings_path = resolve_account_settings_path(account, config.accounts_dir)
    deleted_capture = _unlink_expected_account_file(capture_path) if delete_capture else False
    deleted_settings = _unlink_expected_account_file(settings_path) if delete_settings else False
    deleted_store = _admin_store(config).delete_account_capture(account)
    deleted_directory = _remove_empty_account_dir(capture_path, settings_path)
    return {
        "object": "chatgpt.admin.account_delete",
        "ok": True,
        "account": account,
        "deleted": {
            "capture": deleted_capture,
            "settings": deleted_settings,
            "stored_metadata": deleted_store,
            "empty_directory": deleted_directory,
        },
        "remaining_accounts": _known_admin_account_names(config, AccountRouter(_accounts_for_config(config), config.account_strategy)),
        "paths": {
            "capture": str(capture_path),
            "settings": str(settings_path),
        },
    }


def _admin_settings_save_payload(
    config: OpenAICompatConfig,
    router: AccountRouter,
    body: dict[str, Any],
) -> dict[str, Any]:
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else body
    normalized = _normalize_bridge_settings(settings)
    _admin_store(config).set_setting(BRIDGE_SETTINGS_KEY, normalized)
    _configure_account_limits(config, router)
    return {
        "object": "chatgpt.admin.settings_save",
        "ok": True,
        "settings": _bridge_settings(config),
        "account_concurrency": {account: router.account_limit(account) for account in router.accounts},
        "feature_concurrency": {
            feature: {
                account: _feature_account_concurrency_limit(config, feature, account)
                for account in router.accounts
            }
            for feature in CONCURRENCY_FEATURES
        },
    }


def _admin_settings_reset_payload(config: OpenAICompatConfig, router: AccountRouter) -> dict[str, Any]:
    _admin_store(config).delete_setting(BRIDGE_SETTINGS_KEY)
    _configure_account_limits(config, router)
    return {
        "object": "chatgpt.admin.settings_reset",
        "ok": True,
        "settings": _bridge_settings(config),
        "account_concurrency": {account: router.account_limit(account) for account in router.accounts},
        "feature_concurrency": {
            feature: {
                account: _feature_account_concurrency_limit(config, feature, account)
                for account in router.accounts
            }
            for feature in CONCURRENCY_FEATURES
        },
    }


def _admin_artifact_delete_payload(config: OpenAICompatConfig, body: dict[str, Any]) -> dict[str, Any]:
    file_id = _str_or_none(body.get("file_id"))
    if not file_id:
        raise ValueError("file_id is required")
    delete_file = bool(body.get("delete_file"))
    artifact = _admin_store(config).delete_artifact(file_id)
    if artifact is None:
        raise ValueError("artifact was not found")
    deleted_file = False
    if delete_file:
        path = Path(str(artifact.get("path") or "")).expanduser()
        if path.is_file():
            path.unlink()
            deleted_file = True
    return {
        "object": "chatgpt.admin.artifact_delete",
        "ok": True,
        "file_id": file_id,
        "deleted": {
            "metadata": True,
            "file": deleted_file,
        },
        "artifact": artifact,
    }


def _inspect_account_capture_payload(config: OpenAICompatConfig, body: dict[str, Any]) -> dict[str, Any]:
    account = _safe_account_name(_str_or_none(body.get("account")) or config.account)
    capture_text = _str_or_none(body.get("capture_text")) or _str_or_none(body.get("capture")) or ""
    settings = _settings_from_admin_body(body)
    return _inspect_account_capture(config, account, capture_text, settings)


def _known_admin_account_names(config: OpenAICompatConfig, router: AccountRouter) -> list[str]:
    configured = set(router.accounts)
    stored_names = {entry["account"] for entry in _admin_store(config).list_account_captures()}
    profile_names = {
        profile.name
        for profile in list_account_profiles(config.accounts_dir)
        if profile.exists or profile.has_settings or profile.name in configured or profile.name in stored_names
    }
    return sorted(configured | profile_names | stored_names)


def _unlink_expected_account_file(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return False
    resolved.unlink()
    return True


def _remove_empty_account_dir(*paths: Path) -> bool:
    removed = False
    for path in paths:
        directory = path.expanduser().resolve().parent
        try:
            directory.rmdir()
        except OSError:
            continue
        removed = True
    return removed


def _save_account_capture_payload(config: OpenAICompatConfig, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    account = _safe_account_name(_str_or_none(body.get("account")) or config.account)
    capture_text = _str_or_none(body.get("capture_text")) or _str_or_none(body.get("capture")) or ""
    settings_text = _str_or_none(body.get("settings_text"))
    force = bool(body.get("force"))
    settings = _settings_from_admin_body(body)
    inspection = _inspect_account_capture(config, account, capture_text, settings)
    failed_checks = [
        check["name"]
        for check in inspection.get("checks", [])
        if check.get("level") in {"required", "recommended"} and not check.get("ok")
    ]
    if failed_checks and not force:
        return 400, {
            "error": {
                "message": "capture did not pass validation",
                "type": "invalid_request_error",
                "failed": failed_checks,
                "missing": inspection["missing"],
                "warnings": inspection["warnings"],
            },
            "inspection": inspection,
        }
    capture_path = resolve_account_capture_path(account, config.accounts_dir)
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    accounts_dir = config.accounts_dir if config.accounts_dir is not None else accounts_dir_from_env()
    key = load_secrets_key(accounts_dir)
    capture_path.write_text(encrypt_text(capture_text, key), encoding="utf-8")
    if settings_text:
        settings_path = resolve_account_settings_path(account, config.accounts_dir)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _admin_store(config).record_account_capture(
        account=account,
        capture_path=capture_path,
        inspection=inspection,
    )
    return 200, {"saved": True, "capture_path": str(capture_path), "inspection": inspection}


def _inspect_account_capture(
    config: OpenAICompatConfig,
    account: str,
    capture_text: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    capture: CapturedRequest | None = None
    detected: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}
    try:
        capture = CapturedRequest.from_text(capture_text)
        info = detect_account_info(capture, settings or {})
        detected = info.to_redacted_dict()
        capabilities = infer_account_capabilities(info)
    except Exception as exc:  # noqa: BLE001 - admin screen should show parse failures.
        checks.append(_admin_check("parse", "required", False, _public_status_error(exc)))

    url = capture.url if capture else None
    request_json = capture.request_json if capture else None
    headers = capture.headers if capture else {}
    cookies = capture.cookies if capture else {}
    is_prepare_capture = bool(url and "/backend-api/f/conversation/prepare" in url)
    _add_admin_check(checks, "url", "required", bool(url and "chatgpt.com" in url), url or "missing")
    _add_admin_check(
        checks,
        "authorization",
        "required",
        bool(headers.get("authorization", "").lower().startswith("bearer ")),
        "Bearer token found" if headers.get("authorization") else "missing Authorization header",
    )
    _add_admin_check(checks, "cookie", "required", bool(headers.get("cookie") or cookies), f"{len(cookies)} cookies")
    request_json_ok = isinstance(request_json, dict) or is_prepare_capture
    request_json_detail = (
        "payload parsed"
        if isinstance(request_json, dict)
        else "prepare capture; payload optional"
        if is_prepare_capture
        else "missing Request Data JSON"
    )
    _add_admin_check(checks, "request_json", "required", request_json_ok, request_json_detail)
    _add_admin_check(
        checks,
        "model",
        "recommended",
        bool((isinstance(request_json, dict) and request_json.get("model")) or is_prepare_capture),
        _str_or_none(request_json.get("model"))
        if isinstance(request_json, dict)
        else "prepare capture; inferred at request time"
        if is_prepare_capture
        else "missing",
    )
    _add_admin_check(
        checks,
        "action",
        "recommended",
        bool((isinstance(request_json, dict) and request_json.get("action")) or is_prepare_capture),
        _str_or_none(request_json.get("action"))
        if isinstance(request_json, dict)
        else "prepare capture; inferred at request time"
        if is_prepare_capture
        else "missing",
    )
    for header_name in (
        "openai-sentinel-chat-requirements-token",
        "openai-sentinel-proof-token",
        "openai-sentinel-turnstile-token",
        "x-conduit-token",
        "oai-device-id",
        "oai-session-id",
    ):
        _add_admin_check(
            checks,
            header_name,
            "recommended",
            bool(headers.get(header_name)),
            "present" if headers.get(header_name) else "missing",
        )
    missing = [check["name"] for check in checks if check["level"] == "required" and not check["ok"]]
    warnings = [check["name"] for check in checks if check["level"] == "recommended" and not check["ok"]]
    return {
        "ok": not missing,
        "account": account,
        "missing": missing,
        "warnings": warnings,
        "checks": checks,
        "detected": detected,
        "capabilities": capabilities,
        "preview": {
            "url": url,
            "status": capture.status if capture else None,
            "request_model": request_json.get("model") if isinstance(request_json, dict) else None,
            "request_action": request_json.get("action") if isinstance(request_json, dict) else None,
            "request_thinking_effort": request_json.get("thinking_effort") if isinstance(request_json, dict) else None,
            "headers": _redacted_headers(headers),
            "cookie_count": len(cookies),
            "capture_path": str(resolve_account_capture_path(account, config.accounts_dir)),
        },
    }


def _add_admin_check(checks: list[dict[str, Any]], name: str, level: str, ok: bool, detail: str | None = None) -> None:
    checks.append(_admin_check(name, level, ok, detail))


def _admin_check(name: str, level: str, ok: bool, detail: str | None = None) -> dict[str, Any]:
    return {"name": name, "level": level, "ok": ok, "detail": detail}


def _settings_from_admin_body(body: dict[str, Any]) -> dict[str, Any]:
    settings = body.get("settings")
    if isinstance(settings, dict):
        return settings
    settings_text = _str_or_none(body.get("settings_text"))
    if not settings_text:
        return {}
    parsed = json.loads(settings_text)
    if not isinstance(parsed, dict):
        raise ValueError("settings_text must be a JSON object")
    return parsed


def _redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    return {name: ("<redacted>" if name.lower() in SECRET_HEADER_NAMES else value) for name, value in headers.items()}


def _safe_account_name(value: str) -> str:
    account = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", account):
        raise ValueError("account must use only English letters, numbers, dash, or underscore")
    return account


def _completion_text_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _opencode_inject_payload(config: OpenAICompatConfig, body: dict[str, Any]) -> dict[str, Any]:
    config_path = Path(_str_or_none(body.get("config_path")) or str(_opencode_config_path())).expanduser()
    base_url = _str_or_none(body.get("base_url")) or config.public_base_url or f"http://{config.host}:{config.port}/v1"
    api_key = _str_or_none(body.get("api_key")) or config.api_key or "local-dev-key"
    model = _str_or_none(body.get("model")) or "chatgpt-web/auto@optimized"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _opencode_backup_path(config_path)
    existing = _read_json_file(config_path)
    if config_path.exists() and not backup_path.exists():
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    merged = _opencode_config_with_chatgpt(existing, base_url=base_url, api_key=api_key, model=model)
    _write_json_file(config_path, merged)
    state = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "config_path": str(config_path),
        "source": "bridge-console",
    }
    _write_json_file(_opencode_state_path(), state)
    return {"ok": True, "action": "inject", **_admin_opencode_status(config)}


def _opencode_eject_payload(body: dict[str, Any]) -> dict[str, Any]:
    config_path = Path(_str_or_none(body.get("config_path")) or str(_opencode_config_path())).expanduser()
    backup_path = _opencode_backup_path(config_path)
    if backup_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        return {"ok": True, "action": "restore-backup", "config_path": str(config_path), "backup_path": str(backup_path)}
    existing = _read_json_file(config_path)
    provider = existing.get("provider") if isinstance(existing.get("provider"), dict) else {}
    if isinstance(provider, dict):
        provider.pop("chatgpt-web", None)
        existing["provider"] = provider
    command = existing.get("command") if isinstance(existing.get("command"), dict) else {}
    if isinstance(command, dict):
        for key in ("chatgpt:usage", "chatgpt:remain", "chatgpt:remaining", "chatgpt:research"):
            command.pop(key, None)
        existing["command"] = command
    for key in ("model", "small_model"):
        if isinstance(existing.get(key), str) and str(existing[key]).startswith("chatgpt-web/"):
            existing.pop(key, None)
    _write_json_file(config_path, existing)
    return {"ok": True, "action": "eject", "config_path": str(config_path), "backup_path": str(backup_path)}


def _opencode_config_with_chatgpt(existing: dict[str, Any], *, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    config = dict(existing)
    template = _read_json_file(_project_root() / "integrations" / "opencode" / "opencode.example.json")
    template_provider = (template.get("provider") or {}).get("chatgpt-web") if isinstance(template.get("provider"), dict) else {}
    provider = dict(config.get("provider") if isinstance(config.get("provider"), dict) else {})
    chatgpt_provider = dict(template_provider if isinstance(template_provider, dict) else {})
    options = dict(chatgpt_provider.get("options") if isinstance(chatgpt_provider.get("options"), dict) else {})
    options["baseURL"] = base_url
    options["apiKey"] = api_key
    chatgpt_provider["options"] = options
    provider["chatgpt-web"] = chatgpt_provider
    config["provider"] = provider
    config["model"] = model
    config["small_model"] = model
    command = dict(config.get("command") if isinstance(config.get("command"), dict) else {})
    command.update(
        {
            "chatgpt:usage": {"template": "/chatgpt:usage", "description": "Show ChatGPT Web account usage"},
            "chatgpt:remain": {"template": "/chatgpt:remain", "description": "Show remaining ChatGPT Web quota"},
            "chatgpt:remaining": {"template": "/chatgpt:remain", "description": "Alias for remaining ChatGPT Web quota"},
            "chatgpt:research": {
                "template": "Run ChatGPT Deep Research for: $ARGUMENTS",
                "description": "Ask the local ChatGPT bridge to run Deep Research",
            },
        }
    )
    config["command"] = command
    return config


def _opencode_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "opencode" / "opencode.json"


def _opencode_state_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "chatgpt-api" / "opencode-setup.json"


def _opencode_backup_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}.chatgpt-api.bak")


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _admin_db_path(config: OpenAICompatConfig) -> Path:
    return (config.admin_db_path or Path("outputs/chatgpt-admin.sqlite")).expanduser().resolve()


def _admin_store(config: OpenAICompatConfig) -> BridgeAdminStore:
    return BridgeAdminStore(_admin_db_path(config))


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _public_download_url(relative_url: str, public_base_url: str | None) -> str:
    if not public_base_url:
        return relative_url
    base = public_base_url.rstrip("/")
    if base.endswith("/v1") and relative_url.startswith("/v1/"):
        return base + relative_url.removeprefix("/v1")
    return base + relative_url


def _guess_download_content_type(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type:
        if content_type.startswith("text/"):
            return f"{content_type}; charset=utf-8"
        return content_type
    return "application/octet-stream"


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "download"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _should_use_agent_bridge(body: dict[str, Any], tools: list[Any], model_agent_mode: str | None) -> bool:
    if tools:
        return True
    if model_agent_mode:
        return True
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    explicit = (
        body.get("agent_mode")
        or body.get("chatgpt_agent_mode")
        or body.get("tool_bridge")
        or body.get("chatgpt_tool_bridge")
        or metadata.get("agent_mode")
        or metadata.get("chatgpt_agent_mode")
        or metadata.get("tool_bridge")
        or metadata.get("chatgpt_tool_bridge")
    )
    if explicit is None:
        return False
    return str(explicit).strip().lower() not in {"", "0", "false", "none", "off", "plain"}


async def _collect_messages_text_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    messages: list[Any],
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    operation_id: str | None = None,
) -> tuple[str, ChatGPTProvider, str]:
    attempts: list[dict[str, Any]] = []
    last_error: OpenAICompatProviderError | None = None
    provider_messages = _openai_messages_to_provider_messages(messages)
    input_image_count = _provider_message_image_count(provider_messages)
    for account, preflight_metadata in await _account_order_with_usage_preflight(
        config,
        router,
        model_slug,
        thinking_effort,
        _upload_usage_requirements(input_image_count),
    ):
        provider = _provider_for_account(config, account)
        init_metadata = preflight_metadata
        if input_image_count:
            if init_metadata is None:
                init_metadata = await _conversation_init_metadata(provider, model_slug)
            preflight_error = _input_upload_preflight_error(init_metadata, input_image_count)
            if preflight_error is not None:
                compat_error = OpenAICompatProviderError(
                    preflight_error,
                    requested_model,
                    model_slug,
                    init_metadata,
                    account=account,
                    account_attempts=attempts.copy(),
                )
                attempts.append(_account_attempt_summary(account, compat_error))
                last_error = compat_error
                if _should_try_next_account(router, compat_error):
                    continue
                raise compat_error from preflight_error
        try:
            _update_chatgpt_operation(operation_id, account=account, provider=provider)
            features = ("upload", "chat") if input_image_count else ("chat",)
            text = await _with_provider_feature_limits(
                config,
                provider,
                features,
                lambda: _collect_messages_text(
                    provider,
                    provider_messages,
                    model_slug,
                    thinking_effort,
                    temporary_chat,
                    operation_id=operation_id,
                ),
            )
        except ProviderError as exc:
            init_metadata = init_metadata or await _conversation_init_metadata(provider, model_slug)
            compat_error = OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from exc
        if text:
            return account, provider, text
        init_metadata = await _conversation_init_metadata(provider, model_slug)
        compat_error = OpenAICompatProviderError(
            _empty_response_error(),
            requested_model,
            model_slug,
            init_metadata,
            account=account,
            account_attempts=attempts.copy(),
        )
        attempts.append(_account_attempt_summary(account, compat_error))
        last_error = compat_error
        if _should_try_next_account(router, compat_error):
            continue
        raise compat_error

    if last_error is not None:
        last_error.account_attempts = attempts
        raise last_error
    raise OpenAICompatProviderError(
        ProviderError("No ChatGPT accounts are configured"),
        requested_model,
        model_slug,
        account_attempts=attempts,
    )


def _openai_messages_to_provider_messages(messages: list[Any]) -> list[Message]:
    provider_messages: list[Message] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = (_str_or_none(message.get("role")) or "user").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        parts = _openai_content_to_provider_parts(message.get("content"))
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            parts.append(
                ContentPart.text_part(
                    "assistant_tool_calls=" + json.dumps(message["tool_calls"], ensure_ascii=False)
                )
            )
        if role == "tool" and not parts:
            parts.append(ContentPart.text_part(""))
        if parts:
            provider_messages.append(Message(role=role, content=parts))  # type: ignore[arg-type]
    if not provider_messages:
        raise ValueError("messages must contain at least one text or image message")
    return provider_messages


def _openai_content_to_provider_parts(content: Any) -> list[ContentPart]:
    if isinstance(content, str):
        return [ContentPart.text_part(content)]
    if isinstance(content, list):
        parts: list[ContentPart] = []
        for item in content:
            if isinstance(item, str):
                parts.append(ContentPart.text_part(item))
                continue
            if isinstance(item, ContentPart):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"} and isinstance(item.get("text"), str):
                parts.append(ContentPart.text_part(item["text"]))
            elif item_type in {"image_url", "input_image"}:
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    url = _str_or_none(image_url.get("url"))
                else:
                    url = _str_or_none(image_url) or _str_or_none(item.get("url")) or _str_or_none(item.get("image"))
                if url:
                    parts.append(_content_part_from_image_reference(url, item))
        return parts
    if content is None:
        return []
    return [ContentPart.text_part(str(content))]


def _content_part_from_image_reference(reference: Any, metadata: dict[str, Any] | None = None) -> ContentPart:
    image = _image_input_from_reference(reference, metadata)
    return ContentPart.image_bytes(image.data, image.mime_type, image.name)


async def _collect_prompt_text_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    prompt: str,
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    operation_id: str | None = None,
) -> tuple[str, ChatGPTProvider, str, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    last_error: OpenAICompatProviderError | None = None
    for account in _account_order_for_model(config, router, model_slug, thinking_effort):
        provider = _provider_for_account(config, account)
        try:
            _update_chatgpt_operation(operation_id, account=account, provider=provider)
            text = await _with_provider_feature_limit(
                config,
                provider,
                "chat",
                lambda: _collect_prompt_text(
                    provider,
                    prompt,
                    model_slug,
                    thinking_effort,
                    temporary_chat,
                    operation_id=operation_id,
                ),
            )
        except ProviderError as exc:
            init_metadata = await _conversation_init_metadata(provider, model_slug)
            compat_error = OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from exc
        if text:
            return account, provider, text
        init_metadata = await _conversation_init_metadata(provider, model_slug)
        compat_error = OpenAICompatProviderError(
            _empty_response_error(),
            requested_model,
            model_slug,
            init_metadata,
            account=account,
            account_attempts=attempts.copy(),
        )
        attempts.append(_account_attempt_summary(account, compat_error))
        last_error = compat_error
        if _should_try_next_account(router, compat_error):
            continue
        raise compat_error

    if last_error is not None:
        last_error.account_attempts = attempts
        raise last_error
    raise OpenAICompatProviderError(
        ProviderError("No ChatGPT accounts are configured"),
        requested_model,
        model_slug,
        account_attempts=attempts,
    )


async def _collect_deep_research_with_accounts(
    config: OpenAICompatConfig,
    router: AccountRouter,
    prompt: str,
    requested_model: str,
    model_slug: str,
    operation_id: str | None = None,
) -> tuple[str, ChatGPTProvider, str, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    last_error: OpenAICompatProviderError | None = None
    for account, init_metadata in await _account_order_with_usage_preflight(
        config,
        router,
        model_slug,
        None,
        _research_usage_requirements(),
    ):
        provider = _provider_for_account(config, account)
        if init_metadata is None:
            init_metadata = await _conversation_init_metadata(provider, model_slug)
        preflight_error = _deep_research_preflight_error(init_metadata)
        if preflight_error is not None:
            compat_error = OpenAICompatProviderError(
                preflight_error,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from preflight_error
        if _feature_account_concurrency_limit(config, "research", account) <= 0:
            compat_error = OpenAICompatProviderError(
                ProviderError(f"ChatGPT research is disabled for account '{account}' by bridge concurrency settings"),
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error
        try:
            _update_chatgpt_operation(operation_id, account=account, provider=provider)
            result = await _with_provider_feature_limit(
                config,
                provider,
                "research",
                lambda: _collect_deep_research_text(provider, prompt, model_slug, operation_id=operation_id),
            )
        except ProviderError as exc:
            compat_error = OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                init_metadata,
                account=account,
                account_attempts=attempts.copy(),
            )
            attempts.append(_account_attempt_summary(account, compat_error))
            last_error = compat_error
            if _should_try_next_account(router, compat_error):
                continue
            raise compat_error from exc
        if isinstance(result, tuple):
            text, metadata = result
        else:
            text, metadata = result, {}
        if text:
            return account, provider, text, metadata
        compat_error = OpenAICompatProviderError(
            _empty_response_error(),
            requested_model,
            model_slug,
            init_metadata,
            account=account,
            account_attempts=attempts.copy(),
        )
        attempts.append(_account_attempt_summary(account, compat_error))
        last_error = compat_error
        if _should_try_next_account(router, compat_error):
            continue
        raise compat_error

    if last_error is not None:
        last_error.account_attempts = attempts
        raise last_error
    raise OpenAICompatProviderError(
        ProviderError("No ChatGPT accounts are configured"),
        requested_model,
        model_slug,
        account_attempts=attempts,
    )


def _deep_research_preflight_error(init_metadata: dict[str, Any] | None) -> ProviderError | None:
    if not isinstance(init_metadata, dict):
        return None
    blocked = _matching_feature(init_metadata.get("blocked_features"), "deep_research", "openai_deep_research")
    if blocked is not None:
        description = blocked.get("description")
        resets_after = blocked.get("resets_after") or blocked.get("reset_after")
        parts = ["ChatGPT Deep Research is blocked for this account."]
        if resets_after:
            parts.append(f"Reset after {resets_after}.")
        if description:
            parts.append(str(description))
        return ProviderError(" ".join(parts))

    progress = _matching_feature(init_metadata.get("limits_progress"), "deep_research", "openai_deep_research")
    if progress is None:
        return None
    remaining = progress.get("remaining")
    if not isinstance(remaining, (int, float)) or remaining > 0:
        return None
    reset_after = progress.get("reset_after") or progress.get("resets_after")
    reset_text = f" Reset after {reset_after}." if reset_after else ""
    return ProviderError(f"ChatGPT Deep Research limit exhausted for this account. Remaining={remaining}.{reset_text}")


def _image_request_preflight_error(init_metadata: dict[str, Any] | None, input_image_count: int) -> ProviderError | None:
    if not isinstance(init_metadata, dict):
        return None
    image_error = _feature_limit_error(
        init_metadata,
        USAGE_FEATURE_ALIASES["image_gen"],
        "ChatGPT image generation",
        minimum_remaining=1,
    )
    if image_error is not None:
        return image_error
    if input_image_count > 0:
        upload_error = _feature_limit_error(
            init_metadata,
            USAGE_FEATURE_ALIASES["file_upload"],
            "ChatGPT file upload",
            minimum_remaining=input_image_count,
        )
        if upload_error is not None:
            return upload_error
    return None


def _input_upload_preflight_error(init_metadata: dict[str, Any] | None, input_image_count: int) -> ProviderError | None:
    if not isinstance(init_metadata, dict) or input_image_count <= 0:
        return None
    return _feature_limit_error(
        init_metadata,
        USAGE_FEATURE_ALIASES["file_upload"],
        "ChatGPT file upload",
        minimum_remaining=input_image_count,
    )


def _feature_limit_error(
    init_metadata: dict[str, Any],
    names: tuple[str, ...],
    label: str,
    minimum_remaining: int,
) -> ProviderError | None:
    blocked = _matching_feature(init_metadata.get("blocked_features"), *names)
    if blocked is not None:
        description = blocked.get("description")
        resets_after = blocked.get("resets_after") or blocked.get("reset_after")
        parts = [f"{label} is blocked for this account."]
        if resets_after:
            parts.append(f"Reset after {resets_after}.")
        if description:
            parts.append(str(description))
        return ProviderError(" ".join(parts))

    progress = _matching_feature(init_metadata.get("limits_progress"), *names)
    if progress is None:
        return None
    remaining = progress.get("remaining")
    if not isinstance(remaining, (int, float)) or remaining >= minimum_remaining:
        return None
    reset_after = progress.get("reset_after") or progress.get("resets_after")
    reset_text = f" Reset after {reset_after}." if reset_after else ""
    return ProviderError(
        f"{label} remaining quota is too low for this request. "
        f"Remaining={remaining}, required={minimum_remaining}.{reset_text}"
    )


def _provider_message_image_count(messages: list[Message]) -> int:
    count = 0
    for message in messages:
        for part in message.content:
            if part.kind in {"image_bytes", "image_url"}:
                count += 1
    return count


def _matching_feature(features: Any, *names: str) -> dict[str, Any] | None:
    if not isinstance(features, list):
        return None
    wanted = {name for name in names if name}
    for item in features:
        if not isinstance(item, dict):
            continue
        name = item.get("feature_name") or item.get("name")
        if isinstance(name, str) and name in wanted:
            return item
    return None


async def _collect_text(provider: ChatGPTProvider, request: ChatRequest, operation_id: str | None = None) -> str:
    async def operation() -> str:
        chunks: list[str] = []
        async for delta in provider.stream_chat(request):
            if delta.conversation_id:
                _update_chatgpt_operation(operation_id, conversation_id=delta.conversation_id)
                if _chatgpt_operation_cancel_requested(operation_id):
                    _stop_chatgpt_operation_by_id(operation_id)
                    raise ProviderError("ChatGPT operation cancelled")
            if delta.text:
                chunks.append(delta.text)
        return "".join(chunks).strip()

    return await _with_provider_account_limit(provider, operation)


async def _collect_deep_research_text(
    provider: ChatGPTProvider,
    prompt: str,
    model_slug: str,
    operation_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    async def operation() -> Any:
        metadata: dict[str, Any] = {
            "history_and_training_disabled": False,
            "system_hints": [DEEP_RESEARCH_SYSTEM_HINT],
            "selected_sources": [],
            "selected_github_repos": [],
            "selected_all_github_repos": False,
            "serialization_metadata": {"custom_symbol_offsets": []},
            "deep_research_version": "standard",
            "venus_model_variant": "standard",
        }
        if operation_id:
            def on_deep_research_session(conversation_id: str | None, message_id: str | None, session_id: str | None) -> None:
                _update_chatgpt_operation(
                    operation_id,
                    conversation_id=conversation_id,
                    deep_research_message_id=message_id,
                    deep_research_session_id=session_id,
                )

            metadata["on_deep_research_session"] = on_deep_research_session
            metadata["cancel_requested"] = lambda: _chatgpt_operation_cancel_requested(operation_id)
        return await provider.transport.deep_research(
            ChatRequest(
                messages=[Message.text("user", prompt)],
                model=model_slug,
                stream=True,
                metadata=metadata,
            ),
        )

    result = await _with_provider_account_limit(provider, operation)
    return result.text, result.metadata


async def _collect_messages_text(
    provider: ChatGPTProvider,
    messages: list[Message],
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    operation_id: str | None = None,
) -> str:
    return await _collect_text(
        provider,
        ChatRequest(
            messages=messages,
            model=model_slug,
            thinking_effort=thinking_effort,
            stream=True,
            metadata={"history_and_training_disabled": temporary_chat},
        ),
        operation_id=operation_id,
    )


async def _collect_prompt_text(
    provider: ChatGPTProvider,
    prompt: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
    operation_id: str | None = None,
) -> str:
    return await _collect_text(
        provider,
        ChatRequest(
            messages=[Message.text("user", prompt)],
            model=model_slug,
            thinking_effort=thinking_effort,
            stream=True,
            metadata={"history_and_training_disabled": temporary_chat},
        ),
        operation_id=operation_id,
    )


async def _retry_low_quality_tool_calls(
    provider: ChatGPTProvider,
    original_prompt: str,
    messages: list[Any],
    tools: list[Any],
    text: str,
    tool_calls: list[dict[str, Any]],
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
) -> tuple[str, list[dict[str, Any]]]:
    for _ in range(1):
        quality_issue = _frontend_tool_call_quality_issue(messages, tool_calls)
        if not quality_issue:
            return text, tool_calls
        try:
            text = await _collect_prompt_text(
                provider,
                _build_low_quality_tool_retry_prompt(original_prompt, tool_calls, quality_issue),
                model_slug,
                thinking_effort,
                temporary_chat,
            )
        except ProviderError as exc:
            raise OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                await _conversation_init_metadata(provider, model_slug),
            ) from exc
        if not text:
            raise OpenAICompatProviderError(
                _empty_response_error(),
                requested_model,
                model_slug,
                await _conversation_init_metadata(provider, model_slug),
            )
        tool_calls = _filter_repeated_successful_tool_calls(_parse_tool_calls(text, tools), messages)

    quality_issue = _frontend_tool_call_quality_issue(messages, tool_calls)
    if quality_issue:
        raise OpenAICompatProviderError(
            ProviderError(f"Tool call rejected for quality after retries: {quality_issue}"),
            requested_model,
            model_slug,
            await _conversation_init_metadata(provider, model_slug),
        )
    return text, tool_calls


async def _retry_tool_policy_issues(
    provider: ChatGPTProvider,
    original_prompt: str,
    messages: list[Any],
    tools: list[Any],
    text: str,
    tool_calls: list[dict[str, Any]],
    requested_model: str,
    model_slug: str,
    thinking_effort: str | None,
    temporary_chat: bool,
) -> tuple[str, list[dict[str, Any]]]:
    for _ in range(1):
        policy_issue = _tool_call_policy_issue(messages, tool_calls)
        if not policy_issue:
            return text, tool_calls
        try:
            text = await _collect_prompt_text(
                provider,
                _build_tool_policy_retry_prompt(original_prompt, tool_calls, policy_issue),
                model_slug,
                thinking_effort,
                temporary_chat,
            )
        except ProviderError as exc:
            raise OpenAICompatProviderError(
                exc,
                requested_model,
                model_slug,
                await _conversation_init_metadata(provider, model_slug),
            ) from exc
        if not text:
            raise OpenAICompatProviderError(
                _empty_response_error(),
                requested_model,
                model_slug,
                await _conversation_init_metadata(provider, model_slug),
            )
        tool_calls = _filter_repeated_successful_tool_calls(_parse_tool_calls(text, tools), messages)

    policy_issue = _tool_call_policy_issue(messages, tool_calls)
    if policy_issue:
        raise OpenAICompatProviderError(
            ProviderError(f"Tool call rejected by agent policy after retries: {policy_issue}"),
            requested_model,
            model_slug,
            await _conversation_init_metadata(provider, model_slug),
        )
    return text, tool_calls


async def _conversation_init_metadata(provider: ChatGPTProvider, provider_model: str) -> dict[str, Any] | None:
    try:
        async def operation() -> dict[str, Any] | None:
            return await asyncio.to_thread(
                provider.transport.conversation_init,
                provider_model if provider_model != "auto" else None,
                None,
                None,
            )

        return await _with_provider_account_limit(provider, operation)
    except ProviderError:
        return None
    except Exception:
        return None


def _should_try_next_account(router: AccountRouter, exc: OpenAICompatProviderError) -> bool:
    if len(router.accounts) <= 1 or router.strategy == "sticky":
        return False
    raw_message = str(exc.original)
    if "by bridge concurrency settings" in raw_message:
        return True
    provider_status = _provider_status_code(raw_message)
    code, _, _, _ = _classify_provider_error(raw_message, provider_status)
    return code in {
        "chatgpt_auth_or_browser_challenge",
        "chatgpt_empty_response",
        "chatgpt_model_limit",
        "chatgpt_rate_limited",
        "chatgpt_unsupported_model",
    }


async def _dedupe_image_request(cache_key: str, producer: Any) -> dict[str, Any]:
    now = time.time()
    owner = False
    with _IMAGE_REQUEST_CACHE_LOCK:
        _prune_image_request_cache(now)
        entry = _IMAGE_REQUEST_CACHE.get(cache_key)
        if entry is None:
            entry = _ImageRequestCacheEntry(event=threading.Event(), created_at=now)
            _IMAGE_REQUEST_CACHE[cache_key] = entry
            owner = True
        elif entry.event.is_set():
            if entry.response is not None:
                return _clone_json_dict(entry.response)
            _IMAGE_REQUEST_CACHE.pop(cache_key, None)
            entry = _ImageRequestCacheEntry(event=threading.Event(), created_at=now)
            _IMAGE_REQUEST_CACHE[cache_key] = entry
            owner = True

    if not owner:
        await asyncio.to_thread(entry.event.wait)
        if entry.response is not None:
            return _clone_json_dict(entry.response)
        if entry.error is not None:
            raise entry.error
        raise ProviderError("ChatGPT image request dedupe entry completed without a response")

    try:
        response = await producer()
    except BaseException as exc:
        with _IMAGE_REQUEST_CACHE_LOCK:
            entry.error = exc
            entry.event.set()
            _IMAGE_REQUEST_CACHE.pop(cache_key, None)
        raise

    with _IMAGE_REQUEST_CACHE_LOCK:
        entry.response = _clone_json_dict(response)
        entry.event.set()
    return response


def _prune_image_request_cache(now: float) -> None:
    expired = [
        key
        for key, entry in _IMAGE_REQUEST_CACHE.items()
        if entry.event.is_set() and now - entry.created_at > _IMAGE_REQUEST_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _IMAGE_REQUEST_CACHE.pop(key, None)


def _image_request_cache_key(
    kind: str,
    config: OpenAICompatConfig,
    requested_model: str,
    model_slug: str,
    prompt: str,
    response_format: str | None = None,
    output_path: str | None = None,
    output_dir: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    payload = {
        "accounts": list(_accounts_for_config(config)),
        "default_output_dir": str(config.image_output_dir),
        "kind": kind,
        "metadata": metadata or {},
        "model_slug": model_slug,
        "output_dir": output_dir or "",
        "output_path": output_path or "",
        "prompt": prompt.strip(),
        "requested_model": requested_model,
        "response_format": response_format or "",
        "strategy": _normalize_account_strategy(config.account_strategy),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clone_json_dict(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _model_fallback_for_config(config: OpenAICompatConfig, current_model: str) -> str | None:
    fallback = (config.model_fallback or "").strip()
    if not fallback or fallback.lower() in {"none", "off", "false", "disabled"}:
        return None
    fallback_model, _ = _resolve_model_alias(fallback, None)
    return fallback if fallback_model != current_model else None


def _should_try_fallback_model(exc: OpenAICompatProviderError) -> bool:
    raw_message = str(exc.original)
    provider_status = _provider_status_code(raw_message)
    code, _, _, _ = _classify_provider_error(raw_message, provider_status)
    return code in {
        "chatgpt_empty_response",
        "chatgpt_model_limit",
        "chatgpt_rate_limited",
        "chatgpt_unsupported_model",
    }


def _account_attempt_summary(account: str, exc: OpenAICompatProviderError) -> dict[str, Any]:
    raw_message = str(exc.original)
    provider_status = _provider_status_code(raw_message)
    code, error_type, http_status, hint = _classify_provider_error(raw_message, provider_status)
    return {
        "account": account,
        "code": code,
        "type": error_type,
        "http_status": http_status,
        "provider_status": provider_status,
        "hint": hint,
        "chatgpt_model_limit": _matching_model_limit(
            exc.init_metadata or {},
            _str_or_none(exc.provider_model),
            _str_or_none(exc.requested_model),
        )
        if isinstance(exc.init_metadata, dict)
        else None,
    }


def _provider_error_status_and_payload(exc: ProviderError) -> tuple[int, dict[str, Any]]:
    requested_model = getattr(exc, "requested_model", None)
    provider_model = getattr(exc, "provider_model", None)
    init_metadata = getattr(exc, "init_metadata", None)
    raw_message = str(getattr(exc, "original", exc))
    provider_status = _provider_status_code(raw_message)
    code, error_type, http_status, hint = _classify_provider_error(raw_message, provider_status)
    init_summary = _chatgpt_init_summary(init_metadata, _str_or_none(provider_model), _str_or_none(requested_model))

    message = _provider_error_message(
        raw_message=raw_message,
        requested_model=_str_or_none(requested_model),
        provider_model=_str_or_none(provider_model),
        provider_status=provider_status,
        hint=hint,
        init_summary=init_summary,
    )
    error: dict[str, Any] = {
        "message": message,
        "type": error_type,
        "code": code,
        "provider": "chatgpt-web",
        "provider_status": provider_status,
        "raw_provider_error": raw_message,
    }
    if isinstance(init_metadata, dict):
        error["chatgpt_default_model_slug"] = init_metadata.get("default_model_slug")
        error["chatgpt_model_limit"] = _matching_model_limit(
            init_metadata,
            _str_or_none(provider_model),
            _str_or_none(requested_model),
        )
        error["chatgpt_deep_research_limit"] = _matching_feature(
            init_metadata.get("limits_progress"),
            "deep_research",
            "openai_deep_research",
        )
        error["chatgpt_blocked_features"] = init_metadata.get("blocked_features")
        error["chatgpt_limits_progress"] = init_metadata.get("limits_progress")
    account = getattr(exc, "account", None)
    if account:
        error["chatgpt_account"] = account
    account_attempts = getattr(exc, "account_attempts", None)
    if account_attempts:
        error["chatgpt_account_attempts"] = account_attempts
    return http_status, {"error": error}


def _empty_response_error() -> ProviderError:
    return ProviderError(
        "ChatGPT conversation returned empty assistant text. "
        "The selected model may be limited, unavailable for this account, or only recoverable through web UI auto retry."
    )


def _provider_status_code(message: str) -> int | None:
    match = re.search(r"failed:\s*(\d{3})", message, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _classify_provider_error(message: str, provider_status: int | None) -> tuple[str, str, int, str]:
    normalized = message.lower()
    if "cancelled" in normalized or "canceled" in normalized:
        return (
            "chatgpt_operation_cancelled",
            "request_cancelled",
            499,
            "The bridge operation was cancelled by request.",
        )
    if "account capture" in normalized and "not configured" in normalized:
        return (
            "chatgpt_missing_account_capture",
            "invalid_request_error",
            400,
            "Add or update a ChatGPT account capture from the Bridge Console Accounts page or CLI before making provider calls.",
        )
    if "cloudflare browser challenge" in normalized:
        return (
            "chatgpt_browser_challenge",
            "provider_auth_error",
            401,
            "The captured browser session was rejected by Cloudflare before ChatGPT handled it. Safari captures may replay, but this Chrome capture needs a fresh compatible capture or a browser-backed Chrome connector.",
        )
    if provider_status in {401, 403}:
        return (
            "chatgpt_auth_or_browser_challenge",
            "provider_auth_error",
            401,
            "Refresh the account capture/cookies, then retry.",
        )
    if provider_status == 429 or any(keyword in normalized for keyword in ("rate limit", "too many", "quota")):
        return (
            "chatgpt_rate_limited",
            "provider_rate_limit",
            429,
            "Wait and retry, or switch to chatgpt-web/auto.",
        )
    if any(keyword in normalized for keyword in ("not valid", "invalid model", "unsupported model")):
        return (
            "chatgpt_unsupported_model",
            "provider_model_error",
            400,
            "Pick a model from /v1/models or switch to chatgpt-web/auto.",
        )
    if "empty assistant text" in normalized:
        return (
            "chatgpt_empty_response",
            "provider_model_error",
            400,
            "No assistant text came back. This often means the selected model is limited or unavailable for the account. Switch to chatgpt-web/auto or retry later.",
        )
    if "image generation returned no image asset" in normalized:
        return (
            "chatgpt_image_no_asset",
            "provider_model_error",
            400,
            "ChatGPT completed the image request without returning an image asset. This can happen when image generation is limited, blocked, or only recoverable through the web UI retry path. Try another account/model or retry later.",
        )
    if "tool call rejected for quality" in normalized:
        return (
            "chatgpt_tool_call_quality_rejected",
            "provider_tool_error",
            400,
            "The model produced a tool call that did not satisfy the bridge quality gate. Retry with a more explicit task or use a stronger account/model.",
        )
    if "tool call rejected by agent policy" in normalized:
        return (
            "chatgpt_tool_call_policy_rejected",
            "provider_tool_error",
            400,
            "The model produced a tool call that violated the agent bridge policy. Retry with a more explicit task or switch agent prompt mode.",
        )
    if provider_status in {409, 422} or any(keyword in normalized for keyword in ("model limit", "limit", "capacity", "unavailable")):
        return (
            "chatgpt_model_limit",
            "provider_model_error",
            400,
            "The selected ChatGPT Web model is probably limited or unavailable. Switch to chatgpt-web/auto or retry later.",
        )
    return (
        "chatgpt_provider_error",
        "provider_error",
        502,
        "Retry later or refresh the account capture if the error persists.",
    )


def _chatgpt_init_summary(
    init_metadata: Any,
    provider_model: str | None,
    requested_model: str | None,
) -> str:
    if not isinstance(init_metadata, dict):
        return ""
    parts: list[str] = []
    default_model = init_metadata.get("default_model_slug")
    if default_model:
        parts.append(f"ChatGPT default model is `{default_model}`.")

    model_limit = _matching_model_limit(init_metadata, provider_model, requested_model)
    if model_limit:
        resets_after = model_limit.get("resets_after")
        if resets_after:
            parts.append(f"Selected model limit resets after {resets_after}.")
        description = model_limit.get("description")
        if description:
            parts.append(str(description))

    blocked = init_metadata.get("blocked_features")
    if isinstance(blocked, list):
        blocked_descriptions = [
            str(item.get("description"))
            for item in blocked
            if isinstance(item, dict) and item.get("description")
        ]
        if blocked_descriptions:
            parts.append("Blocked feature note: " + " ".join(blocked_descriptions[:2]))
    deep_research_limit = _matching_feature(init_metadata.get("limits_progress"), "deep_research", "openai_deep_research")
    if deep_research_limit:
        remaining = deep_research_limit.get("remaining")
        reset_after = deep_research_limit.get("reset_after") or deep_research_limit.get("resets_after")
        parts.append(
            "Deep Research remaining="
            f"{remaining if remaining is not None else '-'}"
            + (f", resets after {reset_after}." if reset_after else ".")
        )
    return " ".join(parts)


def _matching_model_limit(
    init_metadata: dict[str, Any],
    provider_model: str | None,
    requested_model: str | None,
) -> dict[str, Any] | None:
    model_limits = init_metadata.get("model_limits")
    if not isinstance(model_limits, list):
        return None
    candidates = {model for model in (provider_model, requested_model) if model}
    if not candidates:
        return model_limits[0] if model_limits and isinstance(model_limits[0], dict) else None
    for item in model_limits:
        if not isinstance(item, dict):
            continue
        if item.get("model_slug") in candidates or item.get("using_default_model_slug") in candidates:
            return item
    for item in model_limits:
        if isinstance(item, dict):
            return item
    return None


def _provider_error_message(
    raw_message: str,
    requested_model: str | None,
    provider_model: str | None,
    provider_status: int | None,
    hint: str,
    init_summary: str = "",
) -> str:
    model_text = ""
    if requested_model:
        model_text = f" for requested model `{requested_model}`"
        if provider_model and provider_model != requested_model:
            model_text += f" mapped to ChatGPT Web model `{provider_model}`"
    status_text = f" Provider status: {provider_status}." if provider_status is not None else ""
    init_text = f" {init_summary}" if init_summary else ""
    return f"ChatGPT Web request failed{model_text}.{status_text} {hint}{init_text} Raw error: {raw_message}"


def _provider_for_account(
    config: OpenAICompatConfig,
    account: str | None = None,
    *,
    refresh_web_tokens: bool = True,
) -> ChatGPTProvider:
    resolved_account = account or config.account
    capture_path = resolve_account_capture_path(resolved_account, config.accounts_dir)
    if not capture_path.exists():
        raise ProviderError(
            f"ChatGPT account capture for '{resolved_account}' is not configured. "
            "Add one from the Bridge Console Accounts page or run "
            "`python3 -m chatgpt_api admin account add --paste --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key`."
        )
    capture = CapturedRequest.from_file(capture_path)
    auth = ChatGPTAuthConfig.from_captured_request(capture)
    provider = ChatGPTProvider(
        ChatGPTWebTransport(
            auth,
            impersonate=_impersonate_for_capture(config.impersonate, capture),
            timeout=config.web_timeout,
            refresh_web_tokens=refresh_web_tokens,
        )
    )
    setattr(provider, "_chatgpt_api_account", resolved_account)
    return provider


def _impersonate_for_capture(configured: str | None, capture: CapturedRequest) -> str:
    selected = (configured or "").strip()
    if selected and selected.lower() not in {"auto", "default", "safari18_4", "safari18_0"}:
        return selected
    user_agent = (capture.headers.get("user-agent") or "").lower()
    if "chrome/" in user_agent or "chromium/" in user_agent:
        return "chrome"
    if "firefox/" in user_agent:
        return "firefox135"
    return selected or "safari18_4"


async def _maybe_handle_local_chatgpt_command(
    config: OpenAICompatConfig,
    messages: list[Any],
    requested_model: str,
    router: AccountRouter,
) -> dict[str, Any] | None:
    if _latest_message_role(messages) != "user":
        return None
    latest = _latest_user_message_text(messages).strip().lower()
    if latest not in {"/chatgpt:usage", "chatgpt:usage", "/chatgpt:remain", "chatgpt:remain"}:
        return None
    usage = await _account_usage_response(config, router)
    return _completion_response(
        requested_model,
        _account_usage_markdown_table(usage),
        [],
        account="local",
    )


async def _account_usage_response(config: OpenAICompatConfig, router: AccountRouter) -> dict[str, Any]:
    accounts = await asyncio.gather(*(_account_usage_entry(config, account) for account in router.accounts))
    return {
        "object": "chatgpt.usage",
        "provider": "chatgpt-web",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "account_strategy": router.strategy,
        "mode": "live",
        "accounts": accounts,
    }


async def _account_usage_entry(config: OpenAICompatConfig, account: str, *, probe_chat: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "account": account,
        "ok": False,
        **_account_capture_usage_summary(config, account),
    }
    try:
        provider = _provider_for_account(config, account)
    except Exception as exc:  # noqa: BLE001 - status endpoint should report per-account failures.
        entry["error"] = _public_status_error(exc)
        return entry

    try:
        init_metadata = await _conversation_init_metadata(provider, "auto")
    except Exception as exc:  # noqa: BLE001 - keep usage endpoint best-effort.
        entry["error"] = _public_status_error(exc)
        return entry

    if not isinstance(init_metadata, dict):
        entry.update(
            {
                "ok": None,
                "status": "metadata_unavailable",
                "warning": "ChatGPT conversation/init returned no usage metadata. The account capture may still be valid; run an account check to probe chat auth.",
            }
        )
        if probe_chat:
            try:
                probe_text = await _account_chat_probe(provider)
            except Exception as exc:  # noqa: BLE001 - report probe failure without hiding capture details.
                first_error = _public_status_error(exc)
                try:
                    no_refresh_provider = _provider_for_account(config, account, refresh_web_tokens=False)
                    probe_text = await _account_chat_probe(no_refresh_provider)
                except Exception as fallback_exc:  # noqa: BLE001 - keep both failure reasons visible.
                    entry.update(
                        {
                            "ok": False,
                            "status": "chat_probe_failed",
                            "error": (
                                f"{_public_status_error(fallback_exc)} "
                                f"Refresh-token probe also failed: {first_error}"
                            ),
                        }
                    )
                    return entry
                entry["chat_probe_fallback"] = "no_refresh_web_tokens"
            entry.update(
                {
                    "ok": True,
                    "status": "chat_ok_metadata_unavailable",
                    "chat_probe": {
                        "ok": True,
                        "preview": probe_text[:120],
                    },
                }
            )
        return entry

    entry.update(
        {
            "ok": True,
            "default_model_slug": init_metadata.get("default_model_slug"),
            "atlas_mode_enabled": init_metadata.get("atlas_mode_enabled"),
            "features": _usage_features(init_metadata),
            "model_limits": _compact_feature_list(init_metadata.get("model_limits")),
            "limits_progress": _compact_feature_list(init_metadata.get("limits_progress")),
            "blocked_features": _compact_feature_list(init_metadata.get("blocked_features")),
        }
    )
    return entry


async def _account_chat_probe(provider: ChatGPTProvider) -> str:
    return await _collect_text(
        provider,
        ChatRequest(
            messages=[Message.text("user", "Reply with exactly: ok")],
            model="auto",
            stream=True,
            metadata={
                "history_and_training_disabled": True,
                "source": "bridge_console_account_probe",
            },
        ),
    )


def _account_capture_usage_summary(config: OpenAICompatConfig, account: str) -> dict[str, Any]:
    try:
        capture = CapturedRequest.from_file(resolve_account_capture_path(account, config.accounts_dir))
        settings_path = resolve_account_settings_path(account, config.accounts_dir)
        settings = load_settings_file(str(settings_path)) if settings_path.exists() else {}
        info = detect_account_info(capture, settings)
        capabilities = infer_account_capabilities(info)
    except Exception as exc:  # noqa: BLE001 - usage can still include live error details.
        return {"profile_error": _public_status_error(exc)}
    return {
        "plan_type": info.plan_type,
        "plan_bucket": info.plan_bucket,
        "profile": info.to_redacted_dict(),
        "capabilities": capabilities,
    }


def _account_usage_markdown_table(usage: dict[str, Any]) -> str:
    rows = [
        "| Account | Plan | OK | Default | Deep Research | Image Gen | File Upload | Paste File | Model Limit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for account in usage.get("accounts", []):
        if not isinstance(account, dict):
            continue
        features = account.get("features") if isinstance(account.get("features"), dict) else {}
        rows.append(
            "| "
            + " | ".join(
                [
                    _md_cell(account.get("account")),
                    _md_cell(account.get("plan_type") or account.get("plan_bucket") or "-"),
                    _md_cell(_usage_ok_cell(account)),
                    _md_cell(account.get("default_model_slug") or "-"),
                    _md_cell(_usage_feature_cell(features.get("deep_research"))),
                    _md_cell(_usage_feature_cell(features.get("image_gen"))),
                    _md_cell(_usage_feature_cell(features.get("file_upload"))),
                    _md_cell(_usage_feature_cell(features.get("paste_text_to_file"))),
                    _md_cell(_usage_model_limit_cell(account.get("model_limits"))),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _usage_ok_cell(account: dict[str, Any]) -> str:
    if account.get("ok") is True:
        return "yes"
    if account.get("ok") is False:
        return str(account.get("error") or account.get("profile_error") or "no")
    return str(account.get("warning") or account.get("status") or "not reported")


def _usage_feature_cell(feature: Any) -> str:
    if not isinstance(feature, dict):
        return "not reported"
    if feature.get("blocked"):
        reset = feature.get("reset_after") or feature.get("resets_after")
        reason = feature.get("description") or feature.get("block_reason") or "blocked"
        return f"blocked; reset {reset}" if reset else str(reason)
    remaining = feature.get("remaining")
    reset = feature.get("reset_after") or feature.get("resets_after")
    if remaining is not None:
        return f"{remaining}; reset {reset}" if reset else str(remaining)
    status = feature.get("status")
    return str(status) if status and status != "not_reported" else "not reported"


def _usage_model_limit_cell(model_limits: Any) -> str:
    if not isinstance(model_limits, list) or not model_limits:
        return "-"
    first = next((item for item in model_limits if isinstance(item, dict)), None)
    if not first:
        return "-"
    model = first.get("model_slug") or first.get("using_default_model_slug") or "model"
    reset = first.get("resets_after") or first.get("reset_after")
    return f"{model}; reset {reset}" if reset else str(model)


def _md_cell(value: Any) -> str:
    text = "-" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _usage_features(init_metadata: dict[str, Any]) -> dict[str, Any]:
    progress = init_metadata.get("limits_progress")
    blocked_features = init_metadata.get("blocked_features")
    result: dict[str, Any] = {}
    for key, names in USAGE_FEATURE_ALIASES.items():
        limit = _matching_feature(progress, *names)
        blocked = _matching_feature(blocked_features, *names)
        result[key] = _compact_usage_feature(limit, blocked)
    return result


def _compact_usage_feature(limit: dict[str, Any] | None, blocked: dict[str, Any] | None) -> dict[str, Any]:
    feature: dict[str, Any] = {
        "reported": bool(limit or blocked),
        "remaining": None,
        "reset_after": None,
        "resets_after": None,
        "limit": None,
        "blocked": bool(blocked),
        "description": None,
    }
    if limit:
        feature.update(_compact_feature(limit))
    if blocked:
        compact_blocked = _compact_feature(blocked)
        feature["blocked"] = True
        feature["block_reason"] = compact_blocked.get("block_reason")
        feature["description"] = compact_blocked.get("description") or feature.get("description")
        feature["resets_after"] = compact_blocked.get("resets_after") or feature.get("resets_after")
        feature["reset_after"] = compact_blocked.get("reset_after") or feature.get("reset_after")
        feature["limit"] = compact_blocked.get("limit") if compact_blocked.get("limit") is not None else feature.get("limit")
    remaining = feature.get("remaining")
    if isinstance(remaining, (int, float)):
        feature["status"] = "exhausted" if remaining <= 0 else "available"
    elif feature["blocked"]:
        feature["status"] = "blocked"
    else:
        feature["status"] = "not_reported"
    return feature


def _compact_feature_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_compact_feature(item) for item in value if isinstance(item, dict)]


def _compact_feature(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "block_reason",
        "description",
        "feature_name",
        "limit",
        "model_slug",
        "name",
        "remaining",
        "reset_after",
        "resets_after",
        "resets_after_text",
        "title",
        "using_default_model_slug",
    }
    return {key: item.get(key) for key in allowed if key in item}


def _public_status_error(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _models_response(config: OpenAICompatConfig) -> dict[str, Any]:
    models = _models_for_config(config)
    return {
        "object": "list",
        "data": [
            {"id": model["id"], "object": "model", "created": 0, "owned_by": "chatgpt-web"}
            for model in models
        ],
    }


def _models_for_config(config: OpenAICompatConfig) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for account in _accounts_for_config(config):
        for model in _models_for_account(config, account):
            merged.setdefault(model["id"], model)
    merged.setdefault("gpt-image-1", {"id": "gpt-image-1", "name": "ChatGPT Image"})
    merged.setdefault("chatgpt-deep-research", {"id": "chatgpt-deep-research", "name": "ChatGPT Deep Research"})
    return list(merged.values())


def _models_for_account(config: OpenAICompatConfig, account: str | None = None) -> list[dict[str, str]]:
    selected_account = account or config.account
    capture_path = resolve_account_capture_path(selected_account, config.accounts_dir)
    models: list[dict[str, str]] = [{"id": "auto", "name": "ChatGPT Auto"}]
    if not capture_path.exists():
        return _models_with_agent_modes(models)
    capture = CapturedRequest.from_file(capture_path)
    settings_path = resolve_account_settings_path(selected_account, config.accounts_dir)
    settings = load_settings_file(str(settings_path)) if settings_path.exists() else {}
    capabilities = infer_account_capabilities(detect_account_info(capture, settings))

    if "gpt-5-5" in capabilities["supported_models"]:
        models.append({"id": "gpt-5-5", "name": "GPT-5.5"})
    if capabilities.get("thinking_model"):
        for effort, label in {
            "standard": "Medium",
            "extended": "High",
            "max": "Extra High",
        }.items():
            if effort in capabilities["thinking_efforts"]:
                models.append({"id": f"gpt-5-5-thinking-{effort}", "name": f"GPT-5.5 {label}"})
    if capabilities.get("pro_model"):
        for effort, label in {
            "standard": "Pro Standard",
            "extended": "Pro Extended",
        }.items():
            if effort in capabilities["pro_efforts"]:
                models.append({"id": f"gpt-5-5-pro-{effort}", "name": f"GPT-5.5 {label}"})
    return _models_with_agent_modes(models)


def _models_with_agent_modes(models: list[dict[str, str]]) -> list[dict[str, str]]:
    expanded = list(models)
    for model in models:
        model_id = model["id"]
        model_name = model["name"]
        expanded.append({"id": f"{model_id}@optimized", "name": f"{model_name} (optimized agent bridge)"})
        expanded.append({"id": f"{model_id}@opencode", "name": f"{model_name} (opencode prompt bridge)"})
    return expanded


def _resolve_model_alias(model: str, explicit_effort: str | None) -> tuple[str, str | None]:
    if model in DEEP_RESEARCH_MODEL_ALIASES:
        return "auto", None
    aliases = {
        "gpt-5-5-thinking-standard": ("gpt-5-5-thinking", "standard"),
        "gpt-5-5-thinking-extended": ("gpt-5-5-thinking", "extended"),
        "gpt-5-5-thinking-max": ("gpt-5-5-thinking", "max"),
        "gpt-5-5-pro-standard": ("gpt-5-5-pro", "standard"),
        "gpt-5-5-pro-extended": ("gpt-5-5-pro", "extended"),
    }
    if model in aliases:
        return aliases[model]
    if model == "auto":
        return "auto", None
    return model, explicit_effort


def _resolve_image_model_alias(model: str) -> str:
    aliases = {
        "gpt-image-1": "auto",
        "dall-e-3": "auto",
        "dall-e-2": "auto",
        "chatgpt-image": "auto",
    }
    return aliases.get(model, model)


def _split_model_agent_mode(model: str) -> tuple[str, str | None]:
    for separator in ("@", "#"):
        if separator not in model:
            continue
        base, mode = model.rsplit(separator, 1)
        if base and mode:
            return base, mode
    return model, None


def _resolve_agent_prompt_mode(config: OpenAICompatConfig, body: dict[str, Any], model_agent_mode: str | None) -> str:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    body_mode = (
        model_agent_mode
        or _str_or_none(body.get("chatgpt_agent_mode"))
        or _str_or_none(body.get("agent_mode"))
        or _str_or_none(metadata.get("chatgpt_agent_mode"))
        or _str_or_none(metadata.get("agent_mode"))
        or config.agent_prompt_mode
    )
    return _normalize_agent_prompt_mode(body_mode)


def _resolve_temporary_chat_mode(config: OpenAICompatConfig, body: dict[str, Any]) -> bool:
    if _request_is_deep_research(body, _str_or_none(body.get("model")) or ""):
        return False
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for value in (
        body.get("history_and_training_disabled"),
        body.get("temporary_chat"),
        body.get("chatgpt_temporary_chat"),
        metadata.get("history_and_training_disabled"),
        metadata.get("temporary_chat"),
        metadata.get("chatgpt_temporary_chat"),
    ):
        parsed = _bool_or_none(value)
        if parsed is not None:
            return parsed
    return config.temporary_chat


def _request_is_deep_research(body: dict[str, Any], requested_model: str) -> bool:
    model, _ = _split_model_agent_mode(requested_model)
    if model in DEEP_RESEARCH_MODEL_ALIASES:
        return True
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    if _bool_or_none(body.get("deep_research")) is True or _bool_or_none(metadata.get("deep_research")) is True:
        return True
    if _str_or_none(body.get("deep_research_version")) or _str_or_none(metadata.get("deep_research_version")):
        return True
    hints = _request_system_hints(body)
    return DEEP_RESEARCH_SYSTEM_HINT in hints


def _request_system_hints(body: dict[str, Any]) -> list[str]:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    hints: list[str] = []
    for value in (body.get("system_hints"), metadata.get("system_hints")):
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item and item not in hints:
                hints.append(item)
    return hints


def _normalize_agent_prompt_mode(value: str | None) -> str:
    normalized = (value or "optimized").strip().lower().replace("_", "-")
    normalized = AGENT_PROMPT_MODE_ALIASES.get(normalized, normalized)
    if normalized not in AGENT_PROMPT_MODES:
        raise ValueError(f"unsupported agent prompt mode: {value}")
    return normalized


def _build_chat_prompt(
    messages: list[Any],
    tools: list[Any],
    tool_choice: Any,
    agent_prompt_mode: str = "optimized",
) -> str:
    transcript = _format_messages(messages)
    if not tools:
        return transcript
    mode = _normalize_agent_prompt_mode(agent_prompt_mode)
    if mode == "opencode":
        return "\n\n".join(
            [
                TOOL_BRIDGE_PROMPT,
                f"AGENT_PROMPT_MODE:\n{mode}",
                f"TOOL_CHOICE:\n{json.dumps(tool_choice, ensure_ascii=False)}",
                f"AVAILABLE_TOOLS:\n{json.dumps(_safe_tools(tools), ensure_ascii=False)}",
                f"LATEST_USER_MESSAGE:\n{_latest_user_message_text(messages)}",
                f"CONVERSATION_TRANSCRIPT:\n{transcript}",
            ]
        )
    return "\n\n".join(
        [
            OPTIMIZED_TOOL_BRIDGE_PROMPT,
            f"AGENT_PROMPT_MODE:\n{mode}",
            f"TOOL_CHOICE:\n{json.dumps(tool_choice, ensure_ascii=False)}",
            f"AVAILABLE_TOOLS_COMPACT:\n{json.dumps(_compact_tools(tools), ensure_ascii=False)}",
            f"LATEST_USER_MESSAGE:\n{_latest_user_message_text(messages)}",
            f"RECENT_TRANSCRIPT:\n{_format_compact_messages(messages)}",
        ]
    )


def _build_missing_tool_retry_prompt(original_prompt: str, invalid_response: str) -> str:
    return "\n\n".join(
        [
            original_prompt,
            "PREVIOUS_RESPONSE_WITHOUT_TOOL_CALL:",
            invalid_response,
            (
                "The previous response is invalid because the user requested a workspace action "
                "but no tool call was returned. Return exactly one JSON object that calls one "
                "available tool to make progress on LATEST_USER_MESSAGE. If the task creates "
                "or edits a file and apply_patch is available, call apply_patch rather than "
                "asking the user to repeat the request. Do not answer normally."
            ),
        ]
    )


def _build_low_quality_tool_retry_prompt(
    original_prompt: str,
    tool_calls: list[dict[str, Any]],
    quality_issue: str,
) -> str:
    return "\n\n".join(
        [
            original_prompt,
            "PREVIOUS_TOOL_CALL_REJECTED_FOR_QUALITY:",
            json.dumps(_tool_calls_for_prompt(tool_calls), ensure_ascii=False),
            (
                "The previous tool call is too low quality for LATEST_USER_MESSAGE: "
                f"{quality_issue}. Return exactly one JSON object that calls an available "
                "file-editing tool with a much stronger implementation. If write is available, "
                "use write for a new full-file artifact; otherwise use apply_patch. For a polished landing "
                "page, create a complete single-file HTML artifact with a distinctive concept, "
                "at least 260 non-empty lines, at least five real sections, responsive CSS, strong typography, visual assets "
                "implemented with CSS/SVG or loaded media, detailed copy, hover/focus states, "
                "and mobile layout. Do not answer normally."
            ),
        ]
    )


def _build_tool_policy_retry_prompt(
    original_prompt: str,
    tool_calls: list[dict[str, Any]],
    policy_issue: str,
) -> str:
    return "\n\n".join(
        [
            original_prompt,
            "PREVIOUS_TOOL_CALL_REJECTED_BY_AGENT_POLICY:",
            json.dumps(_tool_calls_for_prompt(tool_calls), ensure_ascii=False),
            (
                "The previous tool call is invalid for LATEST_USER_MESSAGE: "
                f"{policy_issue}. Return exactly one JSON object that calls an allowed "
                "tool with corrected arguments. Do not explain, ask the user to repeat, "
                "or answer normally."
            ),
        ]
    )


def _tool_calls_for_prompt(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        compact.append(
            {
                "name": function.get("name"),
                "arguments": _truncate(str(function.get("arguments", "")), 1200),
            }
        )
    return compact


def _should_retry_for_missing_tool_call(
    messages: list[Any],
    tools: list[Any],
    tool_choice: Any,
    response_text: str,
) -> bool:
    if not tools:
        return False
    latest = _latest_user_message_text(messages).lower()
    latest_requires_tool = _latest_user_requires_tool(latest)
    if _has_tool_result_after_latest_user(messages):
        return latest_requires_tool and _response_abandons_workspace_action(response_text)
    if _tool_choice_requires_tool(tool_choice):
        return True
    if latest_requires_tool:
        return True
    return _response_claims_workspace_action(response_text)


def _tool_choice_requires_tool(tool_choice: Any) -> bool:
    if isinstance(tool_choice, str):
        return tool_choice not in {"", "auto", "none"}
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") == "function"
    return False


def _has_tool_result_after_latest_user(messages: list[Any]) -> bool:
    latest_user_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            latest_user_index = index
    if latest_user_index == -1:
        return False
    for message in messages[latest_user_index + 1 :]:
        if isinstance(message, dict) and message.get("role") == "tool":
            return True
    return False


def _has_successful_tool_result_after_latest_user(messages: list[Any]) -> bool:
    latest_user_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            latest_user_index = index
    if latest_user_index == -1:
        return False

    success_keywords = (
        "completed",
        "created",
        "success",
        "updated",
        "wrote",
        "wrote file successfully",
        "เขียนไฟล์สำเร็จ",
        "สำเร็จ",
    )
    for message in messages[latest_user_index + 1 :]:
        if not (isinstance(message, dict) and message.get("role") == "tool"):
            continue
        content = _message_content_to_text(message.get("content")).lower()
        if _tool_result_looks_failed(content):
            continue
        if any(keyword in content for keyword in success_keywords):
            return True
    return False


def _tool_result_looks_failed(content: str) -> bool:
    failure_keywords = (
        "error",
        "failed",
        "not found",
        "traceback",
        "ไม่พบ",
        "ล้มเหลว",
    )
    return any(keyword in content for keyword in failure_keywords)


def _latest_user_requires_tool(text: str) -> bool:
    direct_keywords = (
        "apply_patch",
        "bash",
        "build",
        "compile",
        "create",
        "delete",
        "edit",
        "execute",
        "find",
        "generate",
        "grep",
        "install",
        "list",
        "ls",
        "mkdir",
        "patch",
        "read",
        "remove",
        "replace",
        "run",
        "save",
        "search",
        "serve",
        "start",
        "test",
        "touch",
        "write",
        "แก้",
        "เขียน",
        "ค้นหา",
        "ทดสอบ",
        "เปิด server",
        "ไฟล์",
        "บันทึก",
        "แทนที่",
        "ทำไฟล์",
        "ไว้ที่",
        "รัน",
        "ลบ",
        "สร้าง",
    )
    if any(keyword in text for keyword in direct_keywords):
        return True
    if re.search(r"(?:^|[\s~/./\\-])[\w.-]+\.(?:css|html|js|json|jsx|md|mjs|py|ts|tsx|txt|yaml|yml)\b", text):
        return True
    return "ลอง" in text and any(keyword in text for keyword in ("ls", "รัน", "เทส", "สร้าง", "แก้"))


def _latest_user_requests_image_generation(text: str) -> bool:
    lowered = text.lower()
    thai_direct = (
        "สร้างรูป",
        "วาดรูป",
        "ทำรูป",
        "เจนรูป",
        "สร้างภาพ",
        "วาดภาพ",
        "ทำภาพ",
        "generate รูป",
        "generate ภาพ",
    )
    if any(phrase in lowered for phrase in thai_direct):
        return True
    if ("รูป" in lowered or "ภาพ" in lowered) and any(verb in lowered for verb in ("สร้าง", "วาด", "ทำ", "เจน")):
        return True
    if any(noun in lowered for noun in ("image", "picture", "photo", "illustration")) and any(
        verb in lowered for verb in ("generate", "create", "make", "draw", "render")
    ):
        return True
    if re.search(r"\bdraw\s+(?:me\s+)?(?:a|an|the)?\s*\w+", lowered):
        return True
    return False


def _image_output_path_from_text(text: str) -> str | None:
    quoted = re.findall(r"`([^`]+\.(?:png|jpg|jpeg|webp|gif))`", text, flags=re.IGNORECASE)
    if quoted:
        return quoted[-1].strip()
    patterns = (
        r"(?:save|saved|write|output)\s+(?:it\s+)?(?:to|as|at)\s+([^\n\r]+)",
        r"(?:เซฟ|บันทึก|ไว้|เอาไปไว้|เก็บ)\s*(?:ไว้)?\s*(?:ที่|ใน|เป็น)?\s*([~./\\A-Za-z0-9ก-๙ _.-]+\.(?:png|jpg|jpeg|webp|gif))",
        r"(?:เซฟ|บันทึก|ไว้|เอาไปไว้|เก็บ)\s*(?:ไว้)?\s*(?:ที่|ใน)\s*([~./\\A-Za-z0-9ก-๙ _.-]+)",
        r"(?:to|at|in)\s+([~./\\A-Za-z0-9_-][~./\\A-Za-z0-9 _.-]*\.(?:png|jpg|jpeg|webp|gif))",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_extracted_path(match.group(1))
    return None


def _clean_extracted_path(path: str) -> str:
    cleaned = path.strip().strip("'\"“”‘’")
    cleaned = re.split(r"\s+(?:แล้ว|ด้วย|หน่อย|please|and|แล้วบอก|บอก)", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    return cleaned.rstrip(".,;:。")


def _response_claims_workspace_action(text: str) -> bool:
    normalized = text.lower()
    claim_keywords = (
        "created",
        "edited",
        "updated",
        "deleted",
        "wrote",
        "ran",
        "listed",
        "สร้างไฟล์",
        "แก้ไฟล์",
        "ลบไฟล์",
        "รันแล้ว",
    )
    return any(keyword in normalized for keyword in claim_keywords)


def _response_is_tool_call_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{") or "tool_calls" not in stripped.lower():
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and any(key in payload for key in ("tool_calls", "Tool_calls", "toolCalls"))


def _tool_call_policy_issue(messages: list[Any], tool_calls: list[dict[str, Any]]) -> str | None:
    latest = _latest_user_message_text(messages).lower()
    if not _latest_user_requests_file_mutation(latest):
        return None
    for call in tool_calls:
        name, arguments = _tool_call_name_and_arguments(call)
        if name != "bash":
            continue
        command = _str_or_none(arguments.get("command")) or ""
        if _bash_command_looks_like_file_mutation(command):
            return "file creation or editing must use a dedicated file tool such as write, edit, or apply_patch instead of bash redirection"
    return None


def _latest_user_requests_file_mutation(text: str) -> bool:
    file_terms = (".css", ".html", ".js", ".json", ".md", ".py", ".ts", ".tsx", ".txt", "file", "ไฟล์")
    mutation_terms = (
        "create",
        "edit",
        "replace",
        "save",
        "write",
        "แก้",
        "เขียน",
        "บันทึก",
        "แทนที่",
        "ทำไฟล์",
        "สร้าง",
    )
    return any(term in text for term in file_terms) and any(term in text for term in mutation_terms)


def _bash_command_looks_like_file_mutation(command: str) -> bool:
    normalized = command.strip().lower()
    if re.search(r"(^|\s)(echo|printf)\b[\s\S]*\s>\s*\S+", normalized):
        return True
    if re.search(r"\b(cat|tee)\b[\s\S]*(>|<<)", normalized):
        return True
    if re.search(r"(^|\s)(touch|rm|mv|cp)\s+\S+", normalized):
        return True
    return False


def _response_abandons_workspace_action(text: str) -> bool:
    normalized = text.lower()
    abandon_keywords = (
        "ask me again",
        "call the tool",
        "can't",
        "cannot",
        "couldn't",
        "tell me again",
        "try again",
        "unable",
        "write tool",
        "ทำไม่ได้",
        "บอกผมอีกครั้ง",
        "บอกใหม่",
        "ยังสร้าง",
        "ไม่ได้ถูกเรียก",
        "ไม่ได้สร้าง",
        "ไม่สามารถ",
    )
    return any(keyword in normalized for keyword in abandon_keywords)


def _has_completed_file_action_after_latest_user(messages: list[Any]) -> bool:
    latest_user_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            latest_user_index = index
    if latest_user_index == -1:
        return False

    tool_calls_by_id: dict[str, tuple[str | None, dict[str, Any]]] = {}
    for message in messages[latest_user_index + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                call_id = _str_or_none(call.get("id"))
                if not call_id:
                    continue
                tool_calls_by_id[call_id] = _tool_call_name_and_arguments(call)
            continue
        if message.get("role") != "tool":
            continue
        content = _message_content_to_text(message.get("content")).lower()
        if _tool_result_looks_failed(content):
            continue
        call_id = _str_or_none(message.get("tool_call_id"))
        name, arguments = tool_calls_by_id.get(call_id or "", (None, {}))
        if name in {"apply_patch", "edit", "write", "write_file"}:
            return True
        if name == "bash" and _bash_command_looks_like_file_mutation(_str_or_none(arguments.get("command")) or ""):
            return True
    return False


def _frontend_tool_call_quality_issue(messages: list[Any], tool_calls: list[dict[str, Any]]) -> str | None:
    latest = _latest_user_message_text(messages).lower()
    if not _latest_user_requests_polished_frontend(latest):
        return None
    if not tool_calls:
        return None
    for call in tool_calls:
        name, arguments = _tool_call_name_and_arguments(call)
        if name not in {"apply_patch", "edit", "write", "write_file"}:
            continue
        artifact = _frontend_artifact_text(arguments)
        if not artifact:
            continue
        issue = _frontend_artifact_quality_issue(artifact)
        if issue:
            return issue
    return None


def _latest_user_requests_polished_frontend(text: str) -> bool:
    frontend_keywords = (
        "html",
        "landing",
        "landing page",
        "page",
        "site",
        "website",
        "frontend",
        "หน้าเว็บ",
        "เว็บ",
        "แลนดิ้ง",
        "landing",
    )
    quality_keywords = (
        "beautiful",
        "polished",
        "premium",
        "ดีๆ",
        "สวย",
        "เอาดี",
        "เจ๋ง",
        "หรู",
    )
    return any(keyword in text for keyword in frontend_keywords) and any(keyword in text for keyword in quality_keywords)


def _tool_call_name_and_arguments(call: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = _str_or_none(function.get("name"))
    raw_arguments = function.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed = {}
    elif isinstance(raw_arguments, dict):
        parsed = raw_arguments
    else:
        parsed = {}
    return name, parsed


def _frontend_artifact_text(arguments: dict[str, Any]) -> str:
    for key in ("patchText", "content", "newString", "text"):
        value = arguments.get(key)
        if isinstance(value, str) and ("<html" in value.lower() or "<!doctype html" in value.lower()):
            return value
    return ""


def _frontend_artifact_quality_issue(artifact: str) -> str | None:
    lowered = artifact.lower()
    line_count = len([line for line in artifact.splitlines() if line.strip()])
    section_count = lowered.count("<section") + lowered.count("<article")
    generic_terms = (
        "build something amazing",
        "build stunning digital products faster",
        "fast, responsive, and elegant",
        "lorem ipsum",
        "mylanding",
        "powerful features",
        "product showcase",
        "production-grade creative platform",
    )

    issues: list[str] = []
    if line_count < 260:
        issues.append(f"only {line_count} non-empty lines")
    if section_count < 5:
        issues.append(f"only {section_count} semantic content sections")
    if "@media" not in lowered:
        issues.append("no responsive media query")
    if not any(token in lowered for token in ("<svg", "background-image", "radial-gradient", "linear-gradient")):
        issues.append("no meaningful visual asset or art direction")
    if any(term in lowered for term in generic_terms):
        issues.append("generic template copy")
    if "orb" in lowered:
        issues.append("decorative orb filler")
    return "; ".join(issues) if issues else None


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


def _format_messages(messages: list[Any]) -> str:
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _str_or_none(message.get("role")) or "user"
        content = _message_content_to_text(message.get("content"))
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            content = "\n".join([content, f"assistant_tool_calls={json.dumps(message['tool_calls'], ensure_ascii=False)}"]).strip()
        if role == "tool":
            name = _str_or_none(message.get("name")) or _str_or_none(message.get("tool_call_id")) or "tool"
            lines.append(f"tool_result[{name}]: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def _format_compact_messages(messages: list[Any], max_messages: int = 18) -> str:
    relevant = messages[-max_messages:]
    lines: list[str] = []
    for message in relevant:
        if not isinstance(message, dict):
            continue
        role = _str_or_none(message.get("role")) or "user"
        content_limit = 1600 if role == "system" else 4000
        content = _truncate(_message_content_to_text(message.get("content")), content_limit)
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            content = "\n".join(
                [
                    content,
                    "assistant_tool_calls="
                    + _truncate(json.dumps(message["tool_calls"], ensure_ascii=False), 2500),
                ]
            ).strip()
        if role == "tool":
            name = _str_or_none(message.get("name")) or _str_or_none(message.get("tool_call_id")) or "tool"
            lines.append(f"tool_result[{name}]: {_truncate(content, 5000)}")
        elif role == "system":
            lines.append(f"system_summary: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                parts.append(f"[image_url:{item.get('image_url')}]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _latest_user_message_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _message_content_to_text(message.get("content"))
    return ""


def _latest_message_role(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = _str_or_none(message.get("role"))
        if role:
            return role
    return None


def _safe_tools(tools: list[Any]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            continue
        function = tool["function"]
        safe.append(
            {
                "type": "function",
                "function": {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                },
            }
        )
    return safe


def _compact_tools(tools: list[Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for tool in _safe_tools(tools):
        function = tool["function"]
        parameters = function.get("parameters", {})
        compact.append(
            {
                "name": function.get("name"),
                "description": _truncate(str(function.get("description", "")), 700),
                "parameters": _compact_schema(parameters if isinstance(parameters, dict) else {}),
            }
        )
    return compact


def _compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if schema.get("type"):
        compact["type"] = schema.get("type")
    if isinstance(schema.get("required"), list):
        compact["required"] = schema["required"]
    properties = schema.get("properties")
    if isinstance(properties, dict):
        compact_properties: dict[str, Any] = {}
        for name, raw_property in properties.items():
            if not isinstance(raw_property, dict):
                continue
            compact_property: dict[str, Any] = {}
            for key in ("type", "format"):
                if raw_property.get(key):
                    compact_property[key] = raw_property[key]
            if raw_property.get("enum"):
                compact_property["enum"] = raw_property["enum"][:20] if isinstance(raw_property["enum"], list) else raw_property["enum"]
            if raw_property.get("description"):
                compact_property["description"] = _truncate(str(raw_property["description"]), 260)
            compact_properties[name] = compact_property
        compact["properties"] = compact_properties
    return compact


def _parse_tool_calls(text: str, tools: list[Any]) -> list[dict[str, Any]]:
    if not tools:
        return []
    allowed = {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    parsed = _parse_json_object(text)
    if not parsed:
        calls = _parse_tool_call_fragment(text, allowed)
    else:
        calls = _tool_call_entries(parsed) or _parse_tool_call_fragment(text, allowed)
    if not isinstance(calls, list):
        return []
    result: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = _str_or_none(call.get("name")) or _str_or_none(function.get("name"))
        if not name or name not in allowed:
            continue
        arguments = call.get("arguments", function.get("arguments", {}))
        if isinstance(arguments, str):
            argument_text = arguments
        else:
            argument_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        result.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": argument_text},
            }
        )
    return result


def _parse_tool_call_fragment(text: str, allowed: set[Any]) -> list[dict[str, Any]] | None:
    best: list[dict[str, Any]] = []
    for raw_name in allowed:
        name = _str_or_none(raw_name)
        if not name:
            continue
        for marker in (f'"name":"{name}"', f'"name": "{name}"', f':"{name}"', f': "{name}"'):
            search_from = 0
            while True:
                marker_index = text.find(marker, search_from)
                if marker_index == -1:
                    break
                arguments_index = text.find('"arguments"', marker_index)
                if arguments_index == -1:
                    search_from = marker_index + len(marker)
                    continue
                colon_index = text.find(":", arguments_index)
                if colon_index == -1:
                    search_from = marker_index + len(marker)
                    continue
                parsed_arguments = _parse_json_value_at(text, colon_index + 1)
                if isinstance(parsed_arguments, dict):
                    best.append({"name": name, "arguments": parsed_arguments})
                search_from = marker_index + len(marker)
    return best or None


def _parse_json_value_at(text: str, start: int) -> Any:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None
    if text[index] == "{":
        end = _find_balanced_json_end(text, index, "{", "}")
    elif text[index] == "[":
        end = _find_balanced_json_end(text, index, "[", "]")
    elif text[index] == '"':
        end = _find_json_string_end(text, index)
    else:
        return None
    if end == -1:
        return None
    try:
        return json.loads(text[index : end + 1])
    except json.JSONDecodeError:
        return None


def _find_balanced_json_end(text: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _find_json_string_end(text: str, start: int) -> int:
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
    return -1


def _tool_call_entries(parsed: dict[str, Any]) -> list[Any] | None:
    for key, value in parsed.items():
        normalized = key.replace("-", "_").lower()
        if normalized in {"tool_calls", "toolcalls"} and isinstance(value, list):
            return value
        if normalized in {"tool_call", "toolcall"} and isinstance(value, dict):
            return [value]
    if _str_or_none(parsed.get("name")) or (
        isinstance(parsed.get("function"), dict) and _str_or_none(parsed["function"].get("name"))
    ):
        return [parsed]
    return None


def _filter_repeated_successful_tool_calls(
    tool_calls: list[dict[str, Any]],
    messages: list[Any],
) -> list[dict[str, Any]]:
    if not tool_calls:
        return tool_calls
    successful_signatures = _successful_tool_signatures(messages)
    if not successful_signatures:
        return tool_calls
    return [call for call in tool_calls if _tool_call_signature(call) not in successful_signatures]


def _successful_tool_signatures(messages: list[Any]) -> set[tuple[str, str]]:
    tool_call_signatures_by_id: dict[str, tuple[str, str]] = {}
    successful_signatures: set[tuple[str, str]] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                call_id = _str_or_none(call.get("id"))
                signature = _tool_call_signature(call)
                if call_id and signature:
                    tool_call_signatures_by_id[call_id] = signature
        if message.get("role") == "tool" and _tool_result_succeeded(message.get("content")):
            call_id = _str_or_none(message.get("tool_call_id"))
            if call_id and call_id in tool_call_signatures_by_id:
                successful_signatures.add(tool_call_signatures_by_id[call_id])
    return successful_signatures


def _tool_call_signature(call: dict[str, Any]) -> tuple[str, str] | None:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = _str_or_none(function.get("name")) or _str_or_none(call.get("name"))
    if not name:
        return None
    arguments = function.get("arguments", call.get("arguments", {}))
    return name, _normalize_tool_arguments(arguments)


def _normalize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments.strip()
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_result_succeeded(content: Any) -> bool:
    text = _message_content_to_text(content).lower()
    success_markers = ("success", "succeeded", "completed", "updated the following files", "done")
    failure_markers = ("error", "failed", "failure", "traceback", "exception")
    return any(marker in text for marker in success_markers) and not any(marker in text for marker in failure_markers)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _completion_response(
    model: str,
    text: str,
    tool_calls: list[dict[str, Any]],
    account: str | None = None,
    fallback_model: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    finish_reason = "stop"
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    else:
        message["content"] = text
    response = {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    if account:
        response["chatgpt_account"] = account
    if fallback_model:
        response["chatgpt_fallback_model"] = fallback_model
    if extra:
        response.update(extra)
    return response


def _send_sse_completion(handler: BaseHTTPRequestHandler, response: dict[str, Any]) -> None:
    _send_sse_headers(handler)
    choice = response["choices"][0]
    message = choice["message"]
    chunk_base = {
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
    }
    _write_sse(handler, {**chunk_base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    if message.get("tool_calls"):
        _write_sse(
            handler,
            {
                **chunk_base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {**tool_call, "index": index}
                                for index, tool_call in enumerate(message["tool_calls"])
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        )
    elif message.get("content"):
        _write_sse_content(handler, chunk_base, message["content"])
    _write_sse_finish(handler, chunk_base, choice["finish_reason"])


def _send_sse_headers(handler: BaseHTTPRequestHandler, extra_headers: dict[str, str] | None = None) -> None:
    handler.send_response(200)
    _send_cors_headers(handler)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Connection", "close")
    for name, value in (extra_headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()


def _is_client_disconnect_error(exc: BaseException) -> bool:
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


def _write_sse_content(handler: BaseHTTPRequestHandler, chunk_base: dict[str, Any], text: str) -> None:
    if not text:
        return
    _write_sse(
        handler,
        {
            **chunk_base,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
    )


def _write_sse_tool_calls(
    handler: BaseHTTPRequestHandler,
    chunk_base: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> None:
    _write_sse(
        handler,
        {
            **chunk_base,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {**tool_call, "index": index} for index, tool_call in enumerate(tool_calls)
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
    )


def _write_sse_finish(handler: BaseHTTPRequestHandler, chunk_base: dict[str, Any], finish_reason: str) -> None:
    _write_sse(handler, {**chunk_base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
    try:
        handler.wfile.write(b"data: [DONE]\n\n")
    except Exception as exc:
        if _is_client_disconnect_error(exc):
            raise _ClientDisconnected() from exc
        raise
    handler.close_connection = True
    try:
        handler.wfile.flush()
    except Exception as exc:
        if _is_client_disconnect_error(exc):
            raise _ClientDisconnected() from exc
        pass


def _write_sse(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    try:
        handler.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
    except Exception as exc:
        if _is_client_disconnect_error(exc):
            raise _ClientDisconnected() from exc
        raise
    try:
        handler.wfile.flush()
    except Exception as exc:
        if _is_client_disconnect_error(exc):
            raise _ClientDisconnected() from exc
        pass


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"1", "true", "yes", "on", "temporary", "incognito", "private"}:
            return True
        if normalized in {"0", "false", "no", "off", "normal", "regular", "default"}:
            return False
    return None
