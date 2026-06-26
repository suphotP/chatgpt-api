"""ChatGPT Web transport boundary.

This file is deliberately provider-specific. Core code should not import it.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from chatgpt_api.core.errors import ProviderError, ProviderNotConfigured, ProviderNotReady
from chatgpt_api.core.types import ChatDelta, ChatRequest, ImageAsset, ImageInput, ImageRequest, ImageResponse
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.proof import decode_proof_config, generate_proof_token
from chatgpt_api.providers.chatgpt.timezone import local_timezone_payload

MAX_CHATGPT_INPUT_IMAGES = 10


@dataclass(frozen=True, slots=True)
class ChatGPTEndpoints:
    base_url: str = "https://chatgpt.com"
    conversation_init_url: str = "https://chatgpt.com/backend-api/conversation/init"
    conversation_url: str = "https://chatgpt.com/backend-api/f/conversation"
    prepare_url: str = "https://chatgpt.com/backend-api/f/conversation/prepare"
    requirements_url: str = "https://chatgpt.com/backend-api/sentinel/chat-requirements"
    stop_conversation_url: str = "https://chatgpt.com/backend-api/stop_conversation"
    call_mcp_url: str = "https://chatgpt.com/backend-api/ecosystem/call_mcp"
    websocket_url: str = "https://chatgpt.com/backend-api/celsius/ws/user"

    def stream_status_url(self, conversation_id: str) -> str:
        return f"{self.base_url}/backend-api/conversation/{conversation_id}/stream_status"


@dataclass(frozen=True, slots=True)
class _DeepResearchWidgetInfo:
    websocket_url: str
    widget_session_id: str


@dataclass(frozen=True, slots=True)
class DeepResearchResult:
    text: str
    metadata: dict[str, Any]


class ChatGPTWebTransport:
    """Owns ChatGPT Web HTTP details.

    The first implementation step keeps the seam explicit: auth parsing and
    request shaping are in place, but the fragile proof/conversation protocol
    will be filled in with real tokens and observed payloads.
    """

    def __init__(
        self,
        auth: ChatGPTAuthConfig,
        endpoints: ChatGPTEndpoints | None = None,
        timeout: float = 180.0,
        refresh_web_tokens: bool = True,
        impersonate: str = "safari18_4",
    ) -> None:
        self.auth = auth
        self.endpoints = endpoints or ChatGPTEndpoints()
        self.timeout = timeout
        self.refresh_web_tokens = refresh_web_tokens
        self.impersonate = impersonate

    def ensure_configured(self) -> None:
        if not self.auth.access_token:
            raise ProviderNotConfigured(
                "ChatGPT access token is missing. Set CHATGPT_ACCESS_TOKEN or CHATGPT_HAR_PATH."
            )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[ChatDelta]:
        self.ensure_configured()
        headers = self.auth.request_headers()
        payload = self._build_chat_payload_with_uploaded_media(request, headers)
        conversation_url = self.auth.captured_url or self.endpoints.conversation_url
        if self.refresh_web_tokens:
            headers = await asyncio.to_thread(self._refresh_web_tokens, headers, payload)
            _mark_payload_sent_after_prepare(payload)

        async for event in self._stream_conversation_events(conversation_url, headers, payload):
            delta = _event_to_delta(event)
            if delta is not None:
                yield delta

    async def deep_research(self, request: ChatRequest) -> DeepResearchResult:
        self.ensure_configured()
        return await asyncio.to_thread(self._deep_research_sync, request)

    async def generate_image(self, request: ImageRequest) -> ImageResponse:
        self.ensure_configured()
        return await asyncio.to_thread(self._generate_image_sync, request)

    def build_chat_payload(self, request: ChatRequest) -> dict[str, Any]:
        captured_payload = request.metadata.get("captured_request_json")
        if isinstance(captured_payload, dict):
            return dict(captured_payload)

        template = self.auth.captured_request_json if isinstance(self.auth.captured_request_json, dict) else {}
        timezone_payload = local_timezone_payload()
        system_hints = _merged_system_hints(template.get("system_hints"), request.metadata.get("system_hints"))
        payload: dict[str, Any] = {
            "action": request.action,
            "model": request.model or str(template.get("model") or "auto"),
            "timezone_offset_min": timezone_payload["timezone_offset_min"],
            "timezone": timezone_payload["timezone"],
            "conversation_mode": template.get("conversation_mode", {"kind": "primary_assistant"}),
            "system_hints": system_hints,
            "supports_buffering": template.get("supports_buffering", True),
            "supported_encodings": template.get("supported_encodings", ["v1"]),
            "client_contextual_info": template.get("client_contextual_info", _default_client_contextual_info()),
            "paragen_cot_summary_display_override": template.get("paragen_cot_summary_display_override", "allow"),
            "force_parallel_switch": template.get("force_parallel_switch", "auto"),
        }
        if "enable_message_followups" in template:
            payload["enable_message_followups"] = template["enable_message_followups"]
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        if request.parent_message_id:
            payload["parent_message_id"] = request.parent_message_id
        if request.variant_purpose:
            payload["variant_purpose"] = request.variant_purpose
        if request.thinking_effort:
            payload["thinking_effort"] = request.thinking_effort
        history_disabled = request.metadata.get("history_and_training_disabled")
        if isinstance(history_disabled, bool):
            payload["history_and_training_disabled"] = history_disabled
        if request.action == "next":
            payload.setdefault("parent_message_id", "client-created-root")
            payload.setdefault("client_prepare_state", "success")
            messages = [_message_to_chatgpt(message) for message in request.messages]
            _apply_latest_user_message_metadata(messages, _extra_user_message_metadata(request.metadata, timezone_payload))
            payload["messages"] = messages
        elif request.action == "variant":
            payload.setdefault("client_prepare_state", "none")
            payload.setdefault("force_parallel_switch", "auto")
        elif request.action == "continue":
            payload.setdefault("client_prepare_state", "none")
        return payload

    def _build_chat_payload_with_uploaded_media(
        self,
        request: ChatRequest,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        payload = self.build_chat_payload(request)
        if request.action != "next" or "messages" not in payload:
            return payload
        captured_payload = request.metadata.get("captured_request_json")
        if isinstance(captured_payload, dict):
            return payload

        uploaded_any = False
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            uploaded_files = self._upload_message_media(message, headers)
            uploaded_any = uploaded_any or bool(uploaded_files)
            messages.append(_message_to_chatgpt(message, uploaded_files))
        if not uploaded_any:
            return payload

        timezone_payload = local_timezone_payload()
        _apply_latest_user_message_metadata(messages, _extra_user_message_metadata(request.metadata, timezone_payload))
        system_hints = list(payload.get("system_hints") or [])
        if "picture_v2" not in system_hints:
            system_hints.append("picture_v2")
        payload["system_hints"] = system_hints
        payload["messages"] = messages
        return payload

    def _upload_message_media(self, message: Any, headers: dict[str, str]) -> list[dict[str, Any]]:
        uploaded_files: list[dict[str, Any]] = []
        for part in message.content:
            if part.kind != "image_bytes" or not part.data:
                continue
            uploaded_files.append(
                self._upload_file(
                    part.data,
                    part.mime_type or "image/png",
                    part.name,
                    headers,
                )
            )
        return uploaded_files

    def conversation_init(
        self,
        requested_default_model: str | None = None,
        conversation_id: str | None = None,
        conversation_origin: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web transport") from exc

        timezone_payload = local_timezone_payload()
        payload = {
            "requested_default_model": requested_default_model,
            "conversation_id": conversation_id,
            "timezone_offset_min": timezone_payload["timezone_offset_min"],
            "conversation_origin": conversation_origin,
        }
        response = requests.post(
            self.endpoints.conversation_init_url,
            headers=_json_headers_for_token_refresh(self.auth.request_headers()),
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT conversation init failed: {response.status_code} {_body_preview(response)}")
        return _json_response(response)

    def stop_conversation(
        self,
        conversation_id: str,
        *,
        exclude_async_types: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web stop requests") from exc

        payload: dict[str, Any] = {"conversation_id": conversation_id}
        if exclude_async_types is not None:
            payload["exclude_async_types"] = exclude_async_types
        response = requests.post(
            self.endpoints.stop_conversation_url,
            headers=_json_headers_for_token_refresh(headers or self.auth.request_headers()),
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=min(float(self.timeout), 30.0),
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT stop conversation failed: {response.status_code} {_body_preview(response)}")
        return _json_response(response)

    def stop_deep_research(
        self,
        conversation_id: str,
        message_id: str,
        session_id: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Deep Research stop requests") from exc

        response = requests.post(
            self.endpoints.call_mcp_url,
            headers=_json_headers_for_token_refresh(headers or self.auth.request_headers()),
            data=json.dumps(
                _deep_research_mcp_payload("stop", conversation_id, message_id, session_id),
                separators=(",", ":"),
            ).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=min(float(self.timeout), 30.0),
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT Deep Research MCP stop failed: {response.status_code} {_body_preview(response)}")
        return _json_response(response)

    def build_image_payload(self, request: ImageRequest) -> dict[str, Any]:
        return self._build_image_chat_payload(request, [])

    def _generate_image_sync(self, request: ImageRequest) -> ImageResponse:
        headers = self.auth.request_headers()
        input_images = list(request.input_images)
        if request.image is not None:
            input_images.insert(
                0,
                ImageInput(
                    data=request.image,
                    mime_type=request.image_mime_type,
                    name=request.metadata.get("image_name") if isinstance(request.metadata.get("image_name"), str) else None,
                ),
            )
        if len(input_images) > MAX_CHATGPT_INPUT_IMAGES:
            raise ProviderError(f"ChatGPT image requests support at most {MAX_CHATGPT_INPUT_IMAGES} input images")

        uploaded_files: list[dict[str, Any]] = []
        for image in input_images:
            uploaded_files.append(
                self._upload_file(
                    image.data,
                    image.mime_type,
                    image.name,
                    headers,
                )
            )
        input_assets = {
            f"file-service://{file_data['file_id']}"
            for file_data in uploaded_files
            if isinstance(file_data.get("file_id"), str)
        }
        payload = self._build_image_chat_payload(request, uploaded_files)
        if self.refresh_web_tokens:
            headers = self._refresh_web_tokens(headers, payload)

        conversation_url = self.auth.captured_url or self.endpoints.conversation_url
        events = self._post_conversation(conversation_url, headers, payload)
        assets = _image_asset_pointers_from_events(events, exclude=input_assets)
        conversation_id = _conversation_id_from_events(events)
        on_conversation_id = request.metadata.get("on_conversation_id")
        cancel_requested = request.metadata.get("cancel_requested")
        if conversation_id and callable(on_conversation_id):
            on_conversation_id(conversation_id)
        if conversation_id and callable(cancel_requested) and cancel_requested():
            self.stop_conversation(conversation_id, exclude_async_types=["pro_mode"])
            raise ProviderError("ChatGPT image generation cancelled")
        if not assets and conversation_id:
            assets = self._poll_generated_image_assets(
                conversation_id,
                headers,
                input_assets,
                timeout=float(request.metadata.get("wait_timeout", min(self.timeout, 180.0))),
                poll_interval=float(request.metadata.get("poll_interval", 3.0)),
                cancel_requested=cancel_requested if callable(cancel_requested) else None,
            )
        if not assets:
            raise ProviderError("ChatGPT image generation returned no image asset")
        images = [self._download_generated_image(asset, headers, conversation_id) for asset in assets]
        return ImageResponse(images=images, prompt=request.prompt, raw={"events": events, "assets": assets})

    def _build_image_chat_payload(self, request: ImageRequest, uploaded_files: list[dict[str, Any]]) -> dict[str, Any]:
        template = self.auth.captured_request_json if isinstance(self.auth.captured_request_json, dict) else {}
        timezone_payload = local_timezone_payload()
        system_hints = list(template.get("system_hints") or [])
        if "picture_v2" not in system_hints:
            system_hints.append("picture_v2")
        payload: dict[str, Any] = {
            "action": "next",
            "parent_message_id": "client-created-root",
            "model": request.model or str(template.get("model") or "auto"),
            "client_prepare_state": "success",
            "timezone_offset_min": timezone_payload["timezone_offset_min"],
            "timezone": timezone_payload["timezone"],
            "conversation_mode": template.get("conversation_mode", {"kind": "primary_assistant"}),
            "enable_message_followups": template.get("enable_message_followups", True),
            "system_hints": system_hints,
            "supports_buffering": template.get("supports_buffering", True),
            "supported_encodings": template.get("supported_encodings", ["v1"]),
            "client_contextual_info": template.get("client_contextual_info", _default_client_contextual_info()),
            "paragen_cot_summary_display_override": template.get("paragen_cot_summary_display_override", "allow"),
            "force_parallel_switch": template.get("force_parallel_switch", "auto"),
            "messages": [_image_message_to_chatgpt(request.prompt, uploaded_files)],
        }
        return payload

    def _upload_file(
        self,
        data: bytes,
        mime_type: str,
        image_name: str | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web image upload") from exc

        extension = _extension_for_mime_type(mime_type)
        width, height = _image_dimensions(data)
        file_name = image_name or f"file-{len(data)}{extension}"
        create_response = requests.post(
            f"{self.endpoints.base_url}/backend-api/files",
            headers=_json_headers_for_token_refresh(headers),
            data=json.dumps(
                {
                    "file_name": file_name,
                    "file_size": len(data),
                    "use_case": "multimodal" if mime_type.startswith("image/") else "my_files",
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if create_response.status_code >= 400:
            raise ProviderError(f"ChatGPT file create failed: {create_response.status_code} {_body_preview(create_response)}")
        file_data = _json_response(create_response)
        upload_url = file_data.get("upload_url")
        file_id = file_data.get("file_id")
        if not isinstance(upload_url, str) or not isinstance(file_id, str):
            raise ProviderError("ChatGPT file create response did not include upload_url and file_id")

        upload_response = requests.put(
            upload_url,
            headers={
                "content-type": mime_type,
                "origin": self.endpoints.base_url,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
            },
            data=data,
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if upload_response.status_code >= 400:
            raise ProviderError(f"ChatGPT file upload failed: {upload_response.status_code} {_body_preview(upload_response)}")

        uploaded_response = requests.post(
            f"{self.endpoints.base_url}/backend-api/files/{file_id}/uploaded",
            headers=_json_headers_for_token_refresh(headers),
            data=b"{}",
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if uploaded_response.status_code >= 400:
            raise ProviderError(f"ChatGPT file uploaded marker failed: {uploaded_response.status_code} {_body_preview(uploaded_response)}")
        uploaded_data = _json_response(uploaded_response)
        return {
            **file_data,
            **uploaded_data,
            "file_id": file_id,
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
            "use_case": "multimodal" if mime_type.startswith("image/") else "my_files",
        }

    def _poll_generated_image_assets(
        self,
        conversation_id: str,
        headers: dict[str, str],
        input_assets: set[str],
        timeout: float,
        poll_interval: float,
        cancel_requested: Any | None = None,
    ) -> list[str]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web image polling") from exc

        deadline = time.monotonic() + max(timeout, 1.0)
        saw_image_task = False
        while time.monotonic() < deadline:
            if callable(cancel_requested) and cancel_requested():
                self.stop_conversation(conversation_id, exclude_async_types=["pro_mode"])
                raise ProviderError("ChatGPT image generation cancelled")
            response = requests.get(
                f"{self.endpoints.base_url}/backend-api/conversation/{conversation_id}",
                headers=_json_headers_for_token_refresh(headers),
                impersonate=self.impersonate,
                timeout=min(self.timeout, 30.0),
            )
            if response.status_code >= 400:
                raise ProviderError(f"ChatGPT conversation poll failed: {response.status_code} {_body_preview(response)}")
            data = _json_response(response)
            assets = _image_asset_pointers_from_value(data, exclude=input_assets)
            if assets:
                return assets
            task_status = _conversation_image_task_status(data)
            if task_status:
                saw_image_task = True
            if not saw_image_task and time.monotonic() + poll_interval >= deadline:
                return []
            time.sleep(max(poll_interval, 0.5))
        return []

    def _download_generated_image(
        self,
        asset_pointer: str,
        headers: dict[str, str],
        conversation_id: str | None,
    ) -> ImageAsset:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web image download") from exc

        asset_id, is_sediment = _asset_id(asset_pointer)
        if is_sediment:
            if not conversation_id:
                raise ProviderError("ChatGPT sediment image asset requires conversation_id")
            metadata_url = (
                f"{self.endpoints.base_url}/backend-api/files/download/{asset_id}"
                f"?conversation_id={conversation_id}&inline=false"
            )
        else:
            metadata_url = f"{self.endpoints.base_url}/backend-api/files/{asset_id}/download"
        metadata_response = requests.get(
            metadata_url,
            headers=_json_headers_for_token_refresh(headers),
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if metadata_response.status_code >= 400:
            raise ProviderError(f"ChatGPT image metadata download failed: {metadata_response.status_code} {_body_preview(metadata_response)}")
        download_url = _json_response(metadata_response).get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise ProviderError("ChatGPT image metadata response did not include download_url")

        image_response = requests.get(
            download_url,
            headers=_asset_download_headers(headers),
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if image_response.status_code >= 400:
            raise ProviderError(f"ChatGPT generated image download failed: {image_response.status_code} {_body_preview(image_response)}")
        return ImageAsset(
            data=image_response.content,
            url=download_url,
            mime_type=image_response.headers.get("content-type"),
            raw={"asset_pointer": asset_pointer, "metadata_url": metadata_url},
        )

    def _refresh_web_tokens(self, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, str]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web transport") from exc

        refreshed = dict(headers)
        refresh_timeout = min(float(self.timeout), 30.0)
        prepare_response = requests.post(
            self.endpoints.prepare_url,
            headers=_json_headers_for_token_refresh(headers),
            data=json.dumps(_prepare_payload(payload), separators=(",", ":")).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=refresh_timeout,
        )
        if prepare_response.status_code >= 400:
            raise ProviderError(f"ChatGPT prepare failed: {prepare_response.status_code} {_body_preview(prepare_response)}")
        prepare_json = _json_response(prepare_response)
        conduit_token = prepare_json.get("conduit_token") or prepare_response.headers.get("x-conduit-token")
        if conduit_token:
            refreshed["x-conduit-token"] = conduit_token

        requirements_response = requests.post(
            self.endpoints.requirements_url,
            headers=_json_headers_for_token_refresh(headers),
            data=b'{"p":null}',
            impersonate=self.impersonate,
            timeout=refresh_timeout,
        )
        if requirements_response.status_code >= 400:
            raise ProviderError(
                f"ChatGPT requirements failed: {requirements_response.status_code} {_body_preview(requirements_response)}"
            )
        requirements_json = _json_response(requirements_response)
        requirements_token = requirements_json.get("token")
        if requirements_token:
            refreshed["openai-sentinel-chat-requirements-token"] = requirements_token

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
        return refreshed

    def _post_conversation(
        self,
        conversation_url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events = list(self._iter_post_conversation(conversation_url, headers, payload))
        events.extend(self._follow_stream_handoffs(events, headers))
        return events

    def _iter_post_conversation(
        self,
        conversation_url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Web transport") from exc

        response = requests.post(
            conversation_url,
            headers=_conversation_headers(headers),
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=self.timeout,
            stream=True,
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT conversation failed: {response.status_code} {_body_preview(response)}")

        events: list[dict[str, Any]] = []
        for raw_line in response.iter_lines():
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
            if not line.startswith("data: "):
                continue
            payload_text = line[6:].strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                event = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event

    async def _stream_conversation_events(
        self,
        conversation_url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()

        def emit(item: dict[str, Any] | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            events: list[dict[str, Any]] = []
            try:
                for event in self._iter_post_conversation(conversation_url, headers, payload):
                    events.append(event)
                    emit(event)
                for event in self._follow_stream_handoffs(events, headers):
                    emit(event)
            except BaseException as exc:  # noqa: BLE001 - forward provider errors to async caller.
                emit(exc)
            finally:
                emit(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item

    def _deep_research_sync(self, request: ChatRequest) -> DeepResearchResult:
        started_monotonic = time.monotonic()
        headers = self.auth.request_headers()
        payload = self.build_chat_payload(request)
        if self.refresh_web_tokens:
            headers = self._refresh_web_tokens(headers, payload)

        conversation_url = self.auth.captured_url or self.endpoints.conversation_url
        events = self._post_conversation(conversation_url, headers, payload)
        result = _deep_research_result_from_value(events)
        if result is not None:
            return _deep_research_result_with_wall_time(result, started_monotonic)

        widget = _deep_research_widget_info_from_value(events)
        if widget is None:
            text = "".join(delta.text for event in events if (delta := _event_to_delta(event)) and delta.text).strip()
            return _deep_research_result_with_wall_time(DeepResearchResult(text=text, metadata={}), started_monotonic)
        conversation_id = _conversation_id_from_events(events)
        message_id = _latest_message_id_from_value(events)
        on_session = request.metadata.get("on_deep_research_session")
        if callable(on_session):
            on_session(conversation_id, message_id, widget.widget_session_id)
        cancel_requested = request.metadata.get("cancel_requested")
        if callable(cancel_requested) and cancel_requested():
            if conversation_id and message_id:
                self.stop_deep_research(conversation_id, message_id, widget.widget_session_id)
            raise ProviderError("ChatGPT Deep Research cancelled")
        skip_sleep_metadata = self._maybe_skip_deep_research_sleep(
            conversation_id,
            message_id,
            widget.widget_session_id,
            headers,
        )
        confirm_metadata, confirm_result = self._maybe_confirm_deep_research_plan(
            conversation_url,
            headers,
            payload,
            events,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if confirm_result is not None:
            return _deep_research_result_with_wall_time(
                _deep_research_result_with_metadata(confirm_result, {**skip_sleep_metadata, **confirm_metadata}),
                started_monotonic,
            )
        result = self._read_deep_research_widget_report(
            widget,
            headers,
            conversation_id,
            message_id,
            cancel_requested=cancel_requested if callable(cancel_requested) else None,
        )
        return _deep_research_result_with_wall_time(
            _deep_research_result_with_metadata(result, {**skip_sleep_metadata, **confirm_metadata}),
            started_monotonic,
        )

    def _follow_stream_handoffs(self, events: list[dict[str, Any]], headers: dict[str, str]) -> list[dict[str, Any]]:
        followed: list[dict[str, Any]] = []
        for event in events:
            topic_id = _stream_handoff_topic(event)
            if topic_id:
                followed.extend(self._read_websocket_topic(topic_id, headers))
        return followed

    def _read_websocket_topic(self, topic_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
        try:
            from curl_cffi import requests
            import websocket
        except ImportError as exc:
            raise ProviderNotConfigured("websocket-client and curl_cffi are required for ChatGPT stream handoff") from exc

        url_response = requests.get(
            self.endpoints.websocket_url,
            headers=_websocket_url_headers(headers),
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        if url_response.status_code >= 400:
            raise ProviderError(f"ChatGPT websocket URL failed: {url_response.status_code} {_body_preview(url_response)}")
        websocket_url = _json_response(url_response).get("websocket_url")
        if not isinstance(websocket_url, str) or not websocket_url:
            raise ProviderError("ChatGPT websocket URL response did not include websocket_url")

        ws_headers = []
        if headers.get("user-agent"):
            ws_headers.append(f"User-Agent: {headers['user-agent']}")

        events: list[dict[str, Any]] = []
        ws = websocket.create_connection(
            websocket_url,
            timeout=min(float(self.timeout), 30.0),
            header=ws_headers,
            origin=self.endpoints.base_url,
        )
        try:
            ws.send(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "command": {
                                "type": "connect",
                                "presence": {"type": "presence", "state": "foreground"},
                            },
                        },
                        {
                            "id": 2,
                            "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"},
                        },
                    ],
                    separators=(",", ":"),
                )
            )
            deadline = time.monotonic() + float(self.timeout)
            done = False
            while not done and time.monotonic() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    break
                if not isinstance(raw, str):
                    continue
                for item in _websocket_items(raw):
                    for event in _events_from_websocket_item(item, topic_id):
                        if event.get("type") == "done":
                            done = True
                        events.append(event)
        finally:
            ws.close()
        return events

    def _read_deep_research_widget_report(
        self,
        widget: _DeepResearchWidgetInfo,
        headers: dict[str, str],
        conversation_id: str | None = None,
        message_id: str | None = None,
        cancel_requested: Any | None = None,
    ) -> DeepResearchResult:
        try:
            import websocket
        except ImportError as exc:
            raise ProviderNotConfigured("websocket-client is required for ChatGPT Deep Research") from exc

        ws_headers = []
        if headers.get("user-agent"):
            ws_headers.append(f"User-Agent: {headers['user-agent']}")

        topic_id = f"api-tool:{widget.widget_session_id}"
        ws = websocket.create_connection(
            widget.websocket_url,
            timeout=min(float(self.timeout), 30.0),
            header=ws_headers,
            origin=self.endpoints.base_url,
        )
        try:
            ws.send(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "command": {
                                "type": "connect",
                                "presence": {"type": "presence", "state": "foreground"},
                            },
                        },
                        {
                            "id": 2,
                            "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"},
                        },
                    ],
                    separators=(",", ":"),
                )
            )
            deadline = time.monotonic() + float(self.timeout)
            last_status: str | None = None
            last_metadata: dict[str, Any] = {}
            last_mcp_poll = 0.0
            while time.monotonic() < deadline:
                if callable(cancel_requested) and cancel_requested():
                    if conversation_id and message_id:
                        self.stop_deep_research(conversation_id, message_id, widget.widget_session_id)
                    raise ProviderError("ChatGPT Deep Research cancelled")
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if callable(cancel_requested) and cancel_requested():
                        if conversation_id and message_id:
                            self.stop_deep_research(conversation_id, message_id, widget.widget_session_id)
                        raise ProviderError("ChatGPT Deep Research cancelled")
                    mcp_result = self._deep_research_get_state_result(
                        conversation_id,
                        message_id,
                        widget.widget_session_id,
                        headers,
                        last_metadata,
                        last_mcp_poll,
                    )
                    last_mcp_poll = time.monotonic()
                    if mcp_result is not None:
                        return mcp_result
                    continue
                if not isinstance(raw, str):
                    continue
                for item in _websocket_items(raw):
                    metadata = _deep_research_metadata_from_value(item)
                    if metadata:
                        last_metadata.update(metadata)
                    result = _deep_research_result_from_value(item)
                    if result is not None:
                        merged_metadata = dict(last_metadata)
                        merged_metadata.update(result.metadata)
                        return DeepResearchResult(text=result.text, metadata=merged_metadata)
                    last_status = _deep_research_widget_status_from_value(item) or last_status
                    if last_status in {"failed", "error", "cancelled", "canceled"}:
                        raise ProviderError(f"ChatGPT Deep Research failed with widget status: {last_status}")
                mcp_result = self._deep_research_get_state_result(
                    conversation_id,
                    message_id,
                    widget.widget_session_id,
                    headers,
                    last_metadata,
                    last_mcp_poll,
                )
                if mcp_result is not None:
                    return mcp_result
                if time.monotonic() - last_mcp_poll >= 15.0:
                    last_mcp_poll = time.monotonic()
        finally:
            ws.close()

        if conversation_id:
            stream_status = self._conversation_stream_status(conversation_id, headers)
            if stream_status:
                last_metadata["stream_status_at_timeout"] = stream_status
        status_suffix = f" Last widget status: {last_status}." if last_status else ""
        stream_suffix = (
            f" Stream status: {last_metadata['stream_status_at_timeout']}."
            if last_metadata.get("stream_status_at_timeout")
            else ""
        )
        mcp_suffix = f" MCP state error: {last_metadata['mcp_get_state_error']}." if last_metadata.get("mcp_get_state_error") else ""
        raise ProviderError(
            f"ChatGPT Deep Research did not return a report before timeout.{status_suffix}{stream_suffix}{mcp_suffix}"
        )

    def _deep_research_get_state_result(
        self,
        conversation_id: str | None,
        message_id: str | None,
        session_id: str,
        headers: dict[str, str],
        last_metadata: dict[str, Any],
        last_poll: float,
    ) -> DeepResearchResult | None:
        if not conversation_id or not message_id:
            return None
        if time.monotonic() - last_poll < 15.0:
            return None
        try:
            state = self._deep_research_get_state(conversation_id, message_id, session_id, headers)
        except ProviderError as exc:
            last_metadata["mcp_get_state_error"] = str(exc)
            return None
        metadata = _deep_research_metadata_from_value(state)
        if metadata:
            last_metadata.update(metadata)
        status = _deep_research_widget_status_from_value(state)
        if status:
            last_metadata["mcp_get_state_status"] = status
        result = _deep_research_result_from_value(state)
        if result is None:
            return None
        merged_metadata = dict(last_metadata)
        merged_metadata.update(result.metadata)
        merged_metadata["mcp_get_state_used"] = True
        return DeepResearchResult(text=result.text, metadata=merged_metadata)

    def _deep_research_get_state(
        self,
        conversation_id: str,
        message_id: str,
        session_id: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Deep Research MCP calls") from exc

        response = requests.post(
            self.endpoints.call_mcp_url,
            headers=_json_headers_for_token_refresh(headers),
            data=json.dumps(
                _deep_research_mcp_payload("get_state", conversation_id, message_id, session_id),
                separators=(",", ":"),
            ).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=min(float(self.timeout), 30.0),
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT Deep Research MCP get_state failed: {response.status_code} {_body_preview(response)}")
        return _json_response(response)

    def _maybe_skip_deep_research_sleep(
        self,
        conversation_id: str | None,
        message_id: str | None,
        session_id: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if not conversation_id or not message_id:
            return {"skip_sleep_skip_reason": "missing conversation_id or message_id"}
        try:
            self._deep_research_skip_sleep(conversation_id, message_id, session_id, headers)
        except ProviderError as exc:
            return {"skip_sleep_error": str(exc)}
        return {"skip_sleep_attempted": True}

    def _deep_research_skip_sleep(
        self,
        conversation_id: str,
        message_id: str,
        session_id: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT Deep Research MCP calls") from exc

        response = requests.post(
            self.endpoints.call_mcp_url,
            headers=_json_headers_for_token_refresh(headers),
            data=json.dumps(
                _deep_research_mcp_payload("skip_sleep", conversation_id, message_id, session_id),
                separators=(",", ":"),
            ).encode("utf-8"),
            impersonate=self.impersonate,
            timeout=min(float(self.timeout), 30.0),
        )
        if response.status_code >= 400:
            raise ProviderError(f"ChatGPT Deep Research MCP skip_sleep failed: {response.status_code} {_body_preview(response)}")
        return _json_response(response)

    def _conversation_stream_status(self, conversation_id: str, headers: dict[str, str]) -> str | None:
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ProviderNotConfigured("curl_cffi is required for ChatGPT stream status") from exc

        response = requests.get(
            self.endpoints.stream_status_url(conversation_id),
            headers=_json_headers_for_token_refresh(headers),
            impersonate=self.impersonate,
            timeout=min(float(self.timeout), 30.0),
        )
        if response.status_code >= 400:
            return None
        status = _json_response(response).get("status")
        return status if isinstance(status, str) else None

    def _maybe_confirm_deep_research_plan(
        self,
        conversation_url: str,
        headers: dict[str, str],
        initial_payload: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> tuple[dict[str, Any], DeepResearchResult | None]:
        metadata = _deep_research_metadata_from_value(events)
        if not _deep_research_waits_for_plan_confirmation(metadata):
            return {}, None
        conversation_id = conversation_id or _conversation_id_from_events(events)
        parent_message_id = message_id or _latest_message_id_from_value(events)
        confirm_metadata: dict[str, Any] = {"plan_confirm_required": True}
        if not conversation_id or not parent_message_id:
            confirm_metadata["plan_confirm_skip_reason"] = "missing conversation_id or parent_message_id"
            return confirm_metadata, None
        before_status = self._conversation_stream_status(conversation_id, headers)
        if before_status:
            confirm_metadata["stream_status_before_confirm"] = before_status
        confirm_payload = _deep_research_confirm_payload(initial_payload, conversation_id, parent_message_id)
        try:
            confirm_headers = self._refresh_web_tokens(headers, confirm_payload)
            confirm_events = self._post_conversation(conversation_url, confirm_headers, confirm_payload)
        except ProviderError as exc:
            confirm_metadata["plan_confirm_error"] = str(exc)
            return confirm_metadata, None
        confirm_metadata["plan_confirm_attempted"] = True
        confirm_metadata["plan_confirm_event_count"] = len(confirm_events)
        after_status = self._conversation_stream_status(conversation_id, confirm_headers)
        if after_status:
            confirm_metadata["stream_status_after_confirm"] = after_status
        result = _deep_research_result_from_value(confirm_events)
        return confirm_metadata, result


def _image_message_to_chatgpt(prompt: str, uploaded_files: list[dict[str, Any]]) -> dict[str, Any]:
    parts: list[Any] = []
    attachments: list[dict[str, Any]] = []
    for file_data in uploaded_files:
        file_part, attachment = _uploaded_file_message_parts(file_data)
        if file_part is None:
            continue
        parts.append(file_part)
        if attachment is not None:
            attachments.append(attachment)
    parts.append(prompt)
    metadata: dict[str, Any] = {"serialization_metadata": {"custom_symbol_offsets": []}}
    if attachments:
        metadata["attachments"] = attachments
    return {
        "id": str(uuid.uuid4()),
        "author": {"role": "user"},
        "create_time": time.time(),
        "content": {
            "content_type": "multimodal_text" if uploaded_files else "text",
            "parts": parts,
        },
        "metadata": metadata,
    }


def _message_to_chatgpt(message: Any, uploaded_files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    parts: list[Any] = []
    attachments: list[dict[str, Any]] = []
    has_media = False
    uploaded_iter = iter(uploaded_files or [])
    for part in message.content:
        if part.kind == "text":
            parts.append(part.text or "")
        elif part.kind == "image_url":
            has_media = True
            parts.append({"type": "image_url", "image_url": part.url, "mime_type": part.mime_type})
        elif part.kind == "image_bytes":
            has_media = True
            file_part, attachment = _uploaded_file_message_parts(next(uploaded_iter, {}))
            if file_part is not None:
                parts.append(file_part)
                if attachment is not None:
                    attachments.append(attachment)
            else:
                parts.append(
                    {
                        "type": "image_bytes",
                        "bytes": part.data,
                        "mime_type": part.mime_type,
                        "name": part.name,
                    }
                )
    metadata: dict[str, Any] = {"__internal": {"search_settings": {}}}
    if attachments:
        metadata["serialization_metadata"] = {"custom_symbol_offsets": []}
        metadata["attachments"] = attachments
    return {
        "id": str(uuid.uuid4()),
        "author": {"role": message.role},
        "create_time": time.time(),
        "content": {
            "content_type": "multimodal_text" if has_media else "text",
            "parts": parts,
        },
        "metadata": metadata,
    }


def _uploaded_file_message_parts(file_data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    file_id = file_data.get("file_id")
    if not isinstance(file_id, str):
        return None, None
    width = file_data.get("width")
    height = file_data.get("height")
    part: dict[str, Any] = {
        "asset_pointer": f"file-service://{file_id}",
        "size_bytes": file_data.get("file_size"),
    }
    if isinstance(width, int):
        part["width"] = width
    if isinstance(height, int):
        part["height"] = height

    attachment: dict[str, Any] = {
        "id": file_id,
        "mimeType": file_data.get("mime_type"),
        "name": file_data.get("file_name"),
        "size": file_data.get("file_size"),
    }
    if isinstance(width, int):
        attachment["width"] = width
    if isinstance(height, int):
        attachment["height"] = height
    return part, attachment


def _merged_system_hints(template_hints: Any, request_hints: Any) -> list[str]:
    merged: list[str] = []
    for value in (template_hints, request_hints):
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item and item not in merged:
                merged.append(item)
    return merged


def _extra_user_message_metadata(metadata: dict[str, Any], timezone_payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "selected_sources",
        "selected_github_repos",
        "selected_all_github_repos",
        "system_hints",
        "serialization_metadata",
        "deep_research_version",
        "venus_model_variant",
        "user_timezone",
    ):
        if key in metadata:
            result[key] = metadata[key]
    if "connector:connector_openai_deep_research" in _merged_system_hints([], metadata.get("system_hints")):
        result.setdefault("system_hints", ["connector:connector_openai_deep_research"])
        result.setdefault("selected_sources", [])
        result.setdefault("selected_github_repos", [])
        result.setdefault("selected_all_github_repos", False)
        result.setdefault("serialization_metadata", {"custom_symbol_offsets": []})
        result.setdefault("deep_research_version", "standard")
        result.setdefault("venus_model_variant", "standard")
        result.setdefault("user_timezone", timezone_payload.get("timezone"))
    return result


def _apply_latest_user_message_metadata(messages: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    if not metadata:
        return
    for message in reversed(messages):
        if message.get("author", {}).get("role") != "user":
            continue
        existing = message.get("metadata")
        if not isinstance(existing, dict):
            existing = {}
            message["metadata"] = existing
        existing.update(metadata)
        return


def _default_client_contextual_info() -> dict[str, Any]:
    return {
        "is_dark_mode": False,
        "time_since_loaded": 20,
        "page_height": 900,
        "page_width": 1440,
        "pixel_ratio": 1,
        "screen_height": 1080,
        "screen_width": 1920,
        "app_name": "chatgpt.com",
    }


def _json_headers_for_token_refresh(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "accept",
        "content-length",
        "x-oai-turn-trace-id",
        "x-openai-target-path",
        "x-openai-target-route",
    }
    refreshed = {
        name: value
        for name, value in headers.items()
        if name not in blocked and _is_replayable_request_header(name)
    }
    refreshed["accept"] = "*/*"
    refreshed["content-type"] = "application/json"
    refreshed["x-conduit-token"] = "no-token"
    refreshed["x-openai-target-path"] = "/backend-api/f/conversation/prepare"
    refreshed["x-openai-target-route"] = "/backend-api/f/conversation/prepare"
    return refreshed


def _conversation_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {"content-length", "host", "accept-encoding"}
    replay = {
        name: value
        for name, value in headers.items()
        if name not in blocked and _is_replayable_request_header(name)
    }
    replay["accept"] = "text/event-stream"
    replay["content-type"] = "application/json"
    return replay


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


def _websocket_url_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {
        "authorization",
        "user-agent",
        "oai-device-id",
        "oai-session-id",
        "oai-client-version",
        "oai-client-build-number",
        "oai-language",
        "origin",
        "referer",
    }
    replay = {name: value for name, value in headers.items() if name in allowed}
    replay["accept"] = "application/json"
    return replay


def _prepare_payload(payload: dict[str, Any]) -> dict[str, Any]:
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
    prepared = {key: value for key, value in payload.items() if key in allowed}
    if payload.get("action") == "next":
        prepared["client_prepare_state"] = "none"
        partial_query = _latest_prepare_partial_query(payload)
        if partial_query:
            prepared["partial_query"] = partial_query
    return prepared


def _mark_payload_sent_after_prepare(payload: dict[str, Any]) -> None:
    if payload.get("action") == "next":
        payload["client_prepare_state"] = "sent"


def _latest_prepare_partial_query(payload: dict[str, Any]) -> dict[str, Any] | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("author", {}).get("role") == "user":
            return dict(message)
    return None


def _json_response(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_preview(response: Any) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
    server = str(getattr(response, "headers", {}).get("server", "")).lower()
    if getattr(response, "status_code", None) == 403 and "html" in content_type and "cloudflare" in server:
        return (
            "Cloudflare browser challenge HTML. The captured browser session could not be replayed by "
            "the HTTP transport; refresh the capture in a replayable browser session or use Safari/browser-backed Chrome."
        )
    return text[:400]


def _event_to_delta(event: dict[str, Any]) -> ChatDelta | None:
    conversation_id = event.get("conversation_id") if isinstance(event.get("conversation_id"), str) else None
    value = event.get("v")
    text = _extract_text(value, event.get("p"))
    if text:
        return ChatDelta(text=text, conversation_id=conversation_id, raw=event)
    event_type = event.get("type") or event.get("event")
    if event_type == "resume_conversation_token":
        return ChatDelta(conversation_id=conversation_id, raw=event)
    return ChatDelta(raw=event) if event else None


def _extract_text(value: Any, path: Any = None) -> str:
    if isinstance(value, str):
        if isinstance(path, str) and path and not path.startswith("/message/content/parts"):
            return ""
        return value
    if isinstance(value, list):
        return "".join(
            item.get("v", "")
            for item in value
            if (
                isinstance(item, dict)
                and isinstance(item.get("v"), str)
                and isinstance(item.get("p"), str)
                and item["p"].startswith("/message/content/parts")
            )
        )
    return ""


def _stream_handoff_topic(event: dict[str, Any]) -> str | None:
    if event.get("type") != "stream_handoff":
        return None
    options = event.get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if (
            isinstance(option, dict)
            and option.get("type") == "subscribe_ws_topic"
            and isinstance(option.get("topic_id"), str)
        ):
            return option["topic_id"]
    return None


def _websocket_items(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [item for item in items if isinstance(item, dict)]


def _events_from_websocket_item(item: dict[str, Any], topic_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    reply = item.get("reply")
    if isinstance(reply, dict):
        catchups = reply.get("catchups")
        if isinstance(catchups, list):
            for catchup in catchups:
                if isinstance(catchup, dict):
                    events.extend(_events_from_websocket_item(catchup, topic_id))

    if item.get("type") != "message" or item.get("topic_id") != topic_id:
        return events
    payload = item.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "conversation-turn-stream":
        return events
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return events
    if inner.get("type") == "done":
        return [{"type": "done", "conversation_id": inner.get("conversation_id")}]
    encoded = inner.get("encoded_item")
    if isinstance(encoded, str):
        events.extend(_events_from_encoded_sse(encoded))
    return events


def _events_from_encoded_sse(encoded: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in encoded.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip() and data_lines:
            events.extend(_json_events_from_sse_data("\n".join(data_lines)))
            data_lines = []
    if data_lines:
        events.extend(_json_events_from_sse_data("\n".join(data_lines)))
    return events


def _json_events_from_sse_data(data: str) -> list[dict[str, Any]]:
    if not data or data == "[DONE]":
        return [{"type": "done"}] if data == "[DONE]" else []
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return []
    return [parsed] if isinstance(parsed, dict) else []


def _deep_research_widget_info_from_value(value: Any) -> _DeepResearchWidgetInfo | None:
    if isinstance(value, dict):
        chatgpt_sdk = value.get("chatgpt_sdk")
        if isinstance(chatgpt_sdk, dict):
            metadata = chatgpt_sdk.get("tool_response_metadata")
            if isinstance(metadata, dict):
                websocket_url = metadata.get("websocket_url")
                widget_session_id = metadata.get("openai/widgetSessionId") or metadata.get("widget_session_id")
                if isinstance(websocket_url, str) and isinstance(widget_session_id, str):
                    return _DeepResearchWidgetInfo(
                        websocket_url=websocket_url,
                        widget_session_id=widget_session_id,
                    )

        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            found = _deep_research_widget_info_from_value(metadata)
            if found is not None:
                return found

        for nested in value.values():
            found = _deep_research_widget_info_from_value(nested)
            if found is not None:
                return found
        return None

    if isinstance(value, list):
        for nested in value:
            found = _deep_research_widget_info_from_value(nested)
            if found is not None:
                return found
    return None


def _deep_research_report_text_from_value(value: Any) -> str:
    result = _deep_research_result_from_value(value)
    return result.text if result is not None else ""


def _deep_research_result_from_value(value: Any) -> DeepResearchResult | None:
    if isinstance(value, dict):
        report_message = value.get("report_message")
        if isinstance(report_message, dict):
            text = _message_content_text(report_message)
            if text:
                return DeepResearchResult(text=text, metadata=_deep_research_metadata_from_widget_state(value))

        widget_state = value.get("widget_state")
        if isinstance(widget_state, str):
            try:
                parsed = json.loads(widget_state)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                result = _deep_research_result_from_value(parsed)
                if result is not None:
                    return result
        elif isinstance(widget_state, dict):
            result = _deep_research_result_from_value(widget_state)
            if result is not None:
                return result

        for nested in value.values():
            result = _deep_research_result_from_value(nested)
            if result is not None:
                return result
        return None

    if isinstance(value, list):
        for nested in value:
            result = _deep_research_result_from_value(nested)
            if result is not None:
                return result
    return None


def _deep_research_metadata_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        widget_state = value.get("widget_state")
        if isinstance(widget_state, str):
            try:
                parsed = json.loads(widget_state)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                metadata = _deep_research_metadata_from_widget_state(parsed)
                if metadata:
                    return metadata
        elif isinstance(widget_state, dict):
            metadata = _deep_research_metadata_from_widget_state(widget_state)
            if metadata:
                return metadata

        metadata = _deep_research_metadata_from_widget_state(value)
        if metadata:
            return metadata
        for nested in value.values():
            metadata = _deep_research_metadata_from_value(nested)
            if metadata:
                return metadata
        return {}

    if isinstance(value, list):
        for nested in value:
            metadata = _deep_research_metadata_from_value(nested)
            if metadata:
                return metadata
    return {}


def _deep_research_metadata_from_widget_state(state: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "status",
        "last_updated_at",
        "research_started_at",
        "research_stopped_at",
        "waiting_for_user_response_on_plan_until",
    ):
        value = state.get(key)
        if isinstance(value, str):
            metadata[key] = value
    plan = state.get("plan")
    if isinstance(plan, dict):
        title = plan.get("title")
        plan_id = plan.get("plan_id")
        version = plan.get("version")
        steps = plan.get("steps")
        if isinstance(title, str):
            metadata["plan_title"] = title
        if isinstance(plan_id, str):
            metadata["plan_id"] = plan_id
        if isinstance(version, int):
            metadata["plan_version"] = version
        if isinstance(steps, list):
            metadata["plan_steps"] = [
                {
                    key: item[key]
                    for key in ("id", "text", "status", "reason")
                    if isinstance(item, dict) and key in item
                }
                for item in steps
                if isinstance(item, dict)
            ]
    started_at = metadata.get("research_started_at")
    stopped_at = metadata.get("research_stopped_at")
    duration = _seconds_between_iso_timestamps(started_at, stopped_at)
    if duration is not None:
        metadata["research_duration_seconds"] = duration
    return metadata


def _deep_research_waits_for_plan_confirmation(metadata: dict[str, Any]) -> bool:
    status = metadata.get("status")
    if status == "waiting_for_user_response_on_plan":
        return True
    return isinstance(metadata.get("waiting_for_user_response_on_plan_until"), str)


def _deep_research_confirm_payload(
    initial_payload: dict[str, Any],
    conversation_id: str,
    parent_message_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "next",
        "conversation_id": conversation_id,
        "parent_message_id": parent_message_id,
        "model": initial_payload.get("model") or "auto",
        "client_prepare_state": "none",
        "timezone_offset_min": initial_payload.get("timezone_offset_min"),
        "timezone": initial_payload.get("timezone"),
        "conversation_mode": initial_payload.get("conversation_mode", {"kind": "primary_assistant"}),
        "system_hints": initial_payload.get("system_hints", []),
        "supports_buffering": initial_payload.get("supports_buffering", True),
        "supported_encodings": initial_payload.get("supported_encodings", ["v1"]),
        "client_contextual_info": initial_payload.get("client_contextual_info", {"app_name": "chatgpt.com"}),
        "paragen_cot_summary_display_override": initial_payload.get("paragen_cot_summary_display_override", "allow"),
        "force_parallel_switch": initial_payload.get("force_parallel_switch", "auto"),
    }
    if "enable_message_followups" in initial_payload:
        payload["enable_message_followups"] = initial_payload["enable_message_followups"]
    return {key: value for key, value in payload.items() if value is not None}


def _deep_research_mcp_payload(tool_name: str, conversation_id: str, message_id: str, session_id: str) -> dict[str, Any]:
    return {
        "app_uri": "connectors://connector_openai_deep_research",
        "tool_name": tool_name,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "tool_input": {"session_id": session_id},
    }


def _deep_research_mcp_get_state_payload(conversation_id: str, message_id: str, session_id: str) -> dict[str, Any]:
    return _deep_research_mcp_payload("get_state", conversation_id, message_id, session_id)


def _deep_research_mcp_skip_sleep_payload(conversation_id: str, message_id: str, session_id: str) -> dict[str, Any]:
    return _deep_research_mcp_payload("skip_sleep", conversation_id, message_id, session_id)


def _latest_message_id_from_value(value: Any) -> str | None:
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            message = item.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                found.append(message["id"])
            for nested in item.values():
                walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return found[-1] if found else None


def _deep_research_result_with_metadata(result: DeepResearchResult, metadata: dict[str, Any]) -> DeepResearchResult:
    if not metadata:
        return result
    merged = dict(metadata)
    merged.update(result.metadata)
    return DeepResearchResult(text=result.text, metadata=merged)


def _deep_research_result_with_wall_time(result: DeepResearchResult, started_monotonic: float) -> DeepResearchResult:
    metadata = dict(result.metadata)
    metadata["request_wall_time_seconds"] = round(max(time.monotonic() - started_monotonic, 0.0), 3)
    return DeepResearchResult(text=result.text, metadata=metadata)


def _seconds_between_iso_timestamps(started_at: Any, stopped_at: Any) -> float | None:
    if not isinstance(started_at, str) or not isinstance(stopped_at, str):
        return None
    start = _parse_iso_timestamp(started_at)
    stop = _parse_iso_timestamp(stopped_at)
    if start is None or stop is None:
        return None
    return max((stop - start).total_seconds(), 0.0)


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _deep_research_widget_status_from_value(value: Any) -> str | None:
    if isinstance(value, dict):
        widget_state = value.get("widget_state")
        if isinstance(widget_state, str):
            try:
                parsed = json.loads(widget_state)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                status = parsed.get("status")
                if isinstance(status, str):
                    return status
                nested = _deep_research_widget_status_from_value(parsed)
                if nested:
                    return nested
        elif isinstance(widget_state, dict):
            status = widget_state.get("status")
            if isinstance(status, str):
                return status
            nested = _deep_research_widget_status_from_value(widget_state)
            if nested:
                return nested

        status = value.get("status")
        if isinstance(status, str) and "report_message" in value:
            return status
        for nested in value.values():
            found = _deep_research_widget_status_from_value(nested)
            if found:
                return found
        return None

    if isinstance(value, list):
        for nested in value:
            found = _deep_research_widget_status_from_value(nested)
            if found:
                return found
    return None


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "\n".join(part for part in parts if isinstance(part, str)).strip()


def _asset_download_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"authorization", "user-agent", "cookie", "origin", "referer"}
    replay = {name: value for name, value in headers.items() if name in allowed}
    replay["accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    return replay


def _extension_for_mime_type(mime_type: str) -> str:
    extensions = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return extensions.get(mime_type.lower(), ".bin")


def _image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if index + 7 <= len(data):
                    height = int.from_bytes(data[index + 3 : index + 5], "big")
                    width = int.from_bytes(data[index + 5 : index + 7], "big")
                    return width, height
                break
            index += segment_length
    return None, None


def _image_asset_pointers_from_events(events: list[dict[str, Any]], exclude: set[str] | None = None) -> list[str]:
    return _image_asset_pointers_from_value(events, exclude=exclude)


def _image_asset_pointers_from_value(value: Any, exclude: set[str] | None = None) -> list[str]:
    excluded = exclude or set()
    found: list[str] = []

    def add(asset: str) -> None:
        if asset not in excluded and asset not in found:
            found.append(asset)

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            content_type = item.get("content_type")
            asset_pointer = item.get("asset_pointer")
            if content_type == "image_asset_pointer" and isinstance(asset_pointer, str):
                add(asset_pointer)
            path = item.get("p")
            path_value = item.get("v")
            if (
                isinstance(path, str)
                and path.endswith("/asset_pointer")
                and isinstance(path_value, str)
                and _is_image_asset_pointer(path_value)
            ):
                add(path_value)
            for nested in item.values():
                walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                walk(nested)
            return
        if isinstance(item, str):
            for match in re.finditer(r"(?:file-service|sediment)://[\w.-]+", item):
                add(match.group(0))

    walk(value)
    return found


def _is_image_asset_pointer(value: str) -> bool:
    return value.startswith("file-service://") or value.startswith("sediment://")


def _asset_id(asset_pointer: str) -> tuple[str, bool]:
    if asset_pointer.startswith("file-service://"):
        return asset_pointer.split("file-service://", 1)[1], False
    if asset_pointer.startswith("sediment://"):
        return asset_pointer.split("sediment://", 1)[1], True
    raise ProviderError(f"Invalid ChatGPT image asset pointer: {asset_pointer}")


def _conversation_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        conversation_id = event.get("conversation_id")
        if isinstance(conversation_id, str):
            return conversation_id
        value = event.get("v")
        if isinstance(value, dict) and isinstance(value.get("conversation_id"), str):
            return value["conversation_id"]
    return None


def _conversation_image_task_status(data: dict[str, Any]) -> str | None:
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return None
    latest_status: str | None = None
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if metadata.get("image_gen_task_id"):
            status = message.get("status")
            latest_status = status if isinstance(status, str) else latest_status
    return latest_status
