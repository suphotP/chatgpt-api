"""Small HTTP helpers shared by API facades."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from typing import Any


def authorize(handler: BaseHTTPRequestHandler, api_key: str | None) -> bool:
    if not api_key:
        return True
    expected = f"Bearer {api_key}"
    if handler.headers.get("authorization") == expected:
        return True
    send_json(handler, 401, {"error": {"message": "unauthorized", "type": "authentication_error"}})
    return False


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length", "0") or "0")
    raw = handler.rfile.read(length)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Expose-Headers", "X-ChatGPT-Operation-Id")


def send_cors_preflight(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(204)
    send_cors_headers(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    send_cors_headers(handler)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_text(handler: BaseHTTPRequestHandler, status: int, text: str) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    send_cors_headers(handler)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[-1]).strip().lower()


def cancel_operation_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"/v1/chatgpt/operations/([^/]+)/cancel", path)
    return match.group(1) if match else None


def operation_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"/v1/chatgpt/operations/([^/]+)", path)
    return match.group(1) if match else None
