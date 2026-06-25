"""Parse copied ChatGPT browser request captures.

Supported input is intentionally plain text so users can paste a Network panel
request summary into a local ignored file without exporting a full HAR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatgpt_api.providers.chatgpt.crypto import is_encrypted, decrypt_text, load_secrets_key


SECRET_HEADER_NAMES = {
    "authorization",
    "cookie",
    "openai-sentinel-chat-requirements-token",
    "openai-sentinel-proof-token",
    "openai-sentinel-turnstile-token",
    "x-conduit-token",
}


@dataclass(slots=True)
class CapturedRequest:
    url: str | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    request_json: dict[str, Any] | None = None

    @classmethod
    def from_file(cls, path: Path) -> "CapturedRequest":
        text = path.read_text(encoding="utf-8")
        if is_encrypted(text):
            # matches the secrets/accounts/<account>/chatgpt-request.txt layout
            accounts_dir = path.parent.parent
            text = decrypt_text(text, load_secrets_key(accounts_dir))
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "CapturedRequest":
        capture = cls()
        capture.url = _find_summary_value(text, "URL")
        status_text = _find_summary_value(text, "Status")
        if status_text:
            match = re.search(r"\d+", status_text)
            if match:
                capture.status = int(match.group(0))

        capture.headers = _parse_headers(text)
        cookie_header = capture.headers.get("cookie")
        if cookie_header:
            capture.cookies = parse_cookie_header(cookie_header)
        _apply_response_cookie_updates(capture, text)

        request_data = _extract_request_data(text)
        if request_data:
            try:
                parsed = json.loads(request_data)
                if isinstance(parsed, dict):
                    capture.request_json = parsed
            except json.JSONDecodeError:
                capture.request_json = None
        return capture

    def redacted_headers(self) -> dict[str, str]:
        return {
            name: ("<redacted>" if name in SECRET_HEADER_NAMES else value)
            for name, value in self.headers.items()
        }


def _find_summary_value(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    chrome_key = "Request URL" if key == "URL" else "Status Code" if key == "Status" else key
    pattern = re.compile(rf"^{re.escape(chrome_key)}\s*$\n\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in _request_header_pairs(text):
        normalized = name.strip().lower()
        if not _looks_like_header_name(normalized):
            continue
        if not _is_replayable_request_header(normalized):
            continue
        if normalized in {
            "http",
            "https",
            "post",
            "get",
            "summary",
            "request",
            "response",
            "request data",
            "request url",
            "request method",
            "status code",
            "remote address",
            "referrer policy",
            "mime type",
            "set-cookie",
            "source",
            "address",
            "initiator",
            "status",
            "url",
            "x-oai-is-update",
        }:
            continue
        headers[normalized] = value.strip()
    return headers


def _request_header_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    lines = _request_header_lines(text)
    for raw_line in lines:
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        pairs.append((name, value))
    for name, value in _chrome_header_pairs(lines):
        pairs.append((name, value))
    return pairs


def _request_header_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = 0
    found_request_marker = False
    for idx, line in enumerate(lines):
        if line.strip().lower() == "request":
            start = idx + 1
            found_request_marker = True
            break

    if not found_request_marker:
        for idx, line in enumerate(lines):
            if line.strip().lower() == ":authority":
                start = idx
                break

    end = len(lines)
    for idx in range(start, len(lines)):
        marker = lines[idx].strip().lower()
        if marker in {"response", "request data"}:
            end = idx
            break
    return lines[start:end]


def _chrome_header_pairs(lines: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    skip_next = False
    for idx, raw_line in enumerate(lines[:-1]):
        if skip_next:
            skip_next = False
            continue
        name = raw_line.strip()
        value = lines[idx + 1].strip()
        if not name or not value:
            continue
        normalized = name.lower()
        if ":" in name:
            continue
        if not _looks_like_header_name(normalized):
            continue
        if normalized in {"request url", "request method", "status code", "remote address", "referrer policy"}:
            continue
        if _looks_like_header_value(value):
            pairs.append((name, value))
            skip_next = True
    return pairs


def _looks_like_header_name(value: str) -> bool:
    if not value:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value))


def _is_replayable_request_header(name: str) -> bool:
    allowed = {
        "accept",
        "accept-language",
        "authorization",
        "cache-control",
        "content-type",
        "cookie",
        "oai-client-build-number",
        "oai-client-version",
        "oai-device-id",
        "oai-echo-logs",
        "oai-language",
        "oai-session-id",
        "oai-telemetry",
        "openai-sentinel-arkose-token",
        "openai-sentinel-chat-requirements-token",
        "openai-sentinel-proof-token",
        "openai-sentinel-turnstile-token",
        "origin",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "user-agent",
        "x-conduit-token",
        "x-oai-turn-trace-id",
        "x-openai-target-path",
        "x-openai-target-route",
    }
    return name in allowed


def _looks_like_header_value(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if lowered in {
        "request",
        "response",
        "request data",
        "request payload",
        "headers",
        "payload",
        "request url",
        "request method",
        "status code",
        "remote address",
        "referrer policy",
    }:
        return False
    return True


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        cookies[name] = value
    return cookies


def _apply_response_cookie_updates(capture: CapturedRequest, text: str) -> None:
    for value in _header_values_anywhere(text, "set-cookie"):
        name_value = value.split(";", 1)[0].strip()
        if "=" not in name_value:
            continue
        name, cookie_value = name_value.split("=", 1)
        name = name.strip()
        cookie_value = cookie_value.strip()
        if not name:
            continue
        if len(cookie_value) >= 2 and cookie_value[0] == cookie_value[-1] == '"':
            cookie_value = cookie_value[1:-1]
        capture.cookies[name] = cookie_value
    oai_is_updates = _header_values_anywhere(text, "x-oai-is-update")
    if oai_is_updates:
        capture.cookies["__Secure-oai-is"] = oai_is_updates[-1].strip()


def _header_values_anywhere(text: str, header_name: str) -> list[str]:
    values: list[str] = []
    lines = text.splitlines()
    lowered_name = header_name.lower()
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            name, value = line.split(":", 1)
            if name.strip().lower() == lowered_name:
                values.append(value.strip())
            continue
        if line.lower() == lowered_name:
            for next_line in lines[idx + 1 :]:
                value = next_line.strip()
                if value:
                    values.append(value)
                    break
    return values


def _extract_request_data(text: str) -> str | None:
    tail = None
    for marker in ("Request Data:", "Request Payload", "Payload"):
        idx = text.find(marker)
        if idx == -1:
            continue
        tail = text[idx + len(marker) :].strip()
        break
    if not tail:
        return _extract_embedded_request_json(text)
    start = tail.find("{")
    if start == -1:
        return _extract_embedded_request_json(text)
    tail = tail[start:]
    return _balanced_json_object(tail)


def _extract_embedded_request_json(text: str) -> str | None:
    candidates: list[str] = []
    for match in re.finditer(r"{", text):
        candidate = _balanced_json_object(text[match.start() :])
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and _looks_like_chatgpt_request_json(parsed):
            candidates.append(candidate)
    return candidates[-1] if candidates else None


def _looks_like_chatgpt_request_json(value: dict[str, Any]) -> bool:
    request_keys = {
        "action",
        "messages",
        "model",
        "parent_message_id",
        "conversation_id",
        "client_prepare_state",
        "conversation_mode",
    }
    return bool(request_keys & value.keys())


def _balanced_json_object(tail: str) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for pos, char in enumerate(tail):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return tail[: pos + 1]
    return None
