import pytest

from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.types import ChatRequest, ContentPart, ImageInput, ImageRequest, Message
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.transport import (
    ChatGPTEndpoints,
    ChatGPTWebTransport,
    _conversation_headers,
    _deep_research_confirm_payload,
    _deep_research_mcp_get_state_payload,
    _deep_research_mcp_skip_sleep_payload,
    _deep_research_report_text_from_value,
    _deep_research_waits_for_plan_confirmation,
    _deep_research_widget_info_from_value,
    _events_from_encoded_sse,
    _events_from_websocket_item,
    _event_to_delta,
    _image_asset_pointers_from_events,
    _image_dimensions,
    _json_headers_for_token_refresh,
    _latest_message_id_from_value,
    _mark_payload_sent_after_prepare,
    _prepare_payload,
    _stream_handoff_topic,
)


def test_stream_status_url_uses_conversation_id():
    assert (
        ChatGPTEndpoints(base_url="https://example.test").stream_status_url("conversation-1")
        == "https://example.test/backend-api/conversation/conversation-1/stream_status"
    )


def test_build_variant_payload_omits_new_messages():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))
    request = ChatRequest(
        messages=[Message.text("user", "ignored")],
        model="gpt-5-5",
        conversation_id="c1",
        parent_message_id="m1",
        action="variant",
        variant_purpose="comparison_implicit",
    )

    payload = transport.build_chat_payload(request)

    assert payload["action"] == "variant"
    assert payload["conversation_id"] == "c1"
    assert payload["parent_message_id"] == "m1"
    assert payload["variant_purpose"] == "comparison_implicit"
    assert "messages" not in payload


def test_build_next_payload_includes_messages():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))

    payload = transport.build_chat_payload(
        ChatRequest(messages=[Message.text("user", "hello")], model="auto", thinking_effort="extended")
    )

    assert payload["action"] == "next"
    assert payload["parent_message_id"] == "client-created-root"
    assert payload["client_prepare_state"] == "success"
    assert payload["messages"][0]["author"]["role"] == "user"
    assert payload["messages"][0]["content"] == {"content_type": "text", "parts": ["hello"]}
    assert payload["thinking_effort"] == "extended"
    assert payload["force_parallel_switch"] == "auto"
    assert payload["paragen_cot_summary_display_override"] == "allow"


def test_prepare_payload_uses_partial_query_for_next_message():
    payload = {
        "action": "next",
        "conversation_id": "conversation-1",
        "parent_message_id": "parent-1",
        "model": "auto",
        "client_prepare_state": "success",
        "timezone_offset_min": -420,
        "timezone": "Asia/Bangkok",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "messages": [
            {
                "id": "message-1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["hello"]},
                "metadata": {"serialization_metadata": {"custom_symbol_offsets": []}},
            }
        ],
    }

    prepared = _prepare_payload(payload)

    assert prepared["client_prepare_state"] == "none"
    assert prepared["partial_query"]["id"] == "message-1"
    assert prepared["partial_query"]["content"]["parts"] == ["hello"]
    assert "messages" not in prepared


def test_prepare_headers_target_prepare_endpoint():
    headers = _json_headers_for_token_refresh(
        {
            "accept": "text/event-stream",
            "content-type": "application/json",
            "openai-sentinel-chat-requirements-token": "requirements-token",
            "x-conduit-token": "old-token",
            "x-openai-target-path": "/backend-api/f/conversation",
            "x-openai-target-route": "/backend-api/f/conversation",
        }
    )

    assert headers["accept"] == "*/*"
    assert headers["openai-sentinel-chat-requirements-token"] == "requirements-token"
    assert headers["x-conduit-token"] == "no-token"
    assert headers["x-openai-target-path"] == "/backend-api/f/conversation/prepare"
    assert headers["x-openai-target-route"] == "/backend-api/f/conversation/prepare"


def test_next_payload_is_marked_sent_after_prepare():
    payload = {"action": "next", "client_prepare_state": "success"}

    _mark_payload_sent_after_prepare(payload)

    assert payload["client_prepare_state"] == "sent"


def test_build_next_payload_can_mark_temporary_chat():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))

    payload = transport.build_chat_payload(
        ChatRequest(
            messages=[Message.text("user", "hello")],
            model="auto",
            metadata={"history_and_training_disabled": True},
        )
    )

    assert payload["history_and_training_disabled"] is True


def test_build_next_payload_can_mark_deep_research_normal_chat():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))

    payload = transport.build_chat_payload(
        ChatRequest(
            messages=[Message.text("user", "research this")],
            model="auto",
            metadata={
                "history_and_training_disabled": False,
                "system_hints": ["connector:connector_openai_deep_research"],
                "deep_research_version": "standard",
                "venus_model_variant": "standard",
            },
        )
    )

    assert payload["history_and_training_disabled"] is False
    assert payload["system_hints"] == ["connector:connector_openai_deep_research"]
    message_metadata = payload["messages"][0]["metadata"]
    assert message_metadata["deep_research_version"] == "standard"
    assert message_metadata["venus_model_variant"] == "standard"
    assert message_metadata["system_hints"] == ["connector:connector_openai_deep_research"]
    assert message_metadata["selected_sources"] == []
    assert message_metadata["selected_github_repos"] == []
    assert message_metadata["selected_all_github_repos"] is False
    assert message_metadata["serialization_metadata"] == {"custom_symbol_offsets": []}
    assert message_metadata["user_timezone"]


def test_prepare_payload_drops_messages():
    payload = _prepare_payload(
        {
            "action": "variant",
            "conversation_id": "c1",
            "parent_message_id": "m1",
            "messages": [{"id": "ignored"}],
            "thinking_effort": "extended",
            "client_contextual_info": {"page_width": 1},
            "force_parallel_switch": "auto",
        }
    )

    assert payload == {
        "action": "variant",
        "conversation_id": "c1",
        "parent_message_id": "m1",
        "thinking_effort": "extended",
        "client_contextual_info": {"page_width": 1},
        "force_parallel_switch": "auto",
    }


def test_build_next_payload_uses_capture_template_without_reusing_thread_ids():
    auth = ChatGPTAuthConfig(
        access_token="fake",
        captured_request_json={
            "model": "gpt-5-5-thinking",
            "conversation_id": "captured-conversation",
            "parent_message_id": "captured-parent",
            "client_contextual_info": {"app_name": "chatgpt.com", "page_width": 1255},
            "timezone": "Asia/Bangkok",
        },
    )
    transport = ChatGPTWebTransport(auth)

    payload = transport.build_chat_payload(ChatRequest(messages=[Message.text("user", "hello")]))

    assert payload["model"] == "gpt-5-5-thinking"
    assert payload["client_contextual_info"]["page_width"] == 1255
    assert payload["parent_message_id"] == "client-created-root"
    assert "conversation_id" not in payload


def test_build_next_payload_prefers_local_timezone_over_capture_template(monkeypatch):
    import chatgpt_api.providers.chatgpt.transport as transport_module

    monkeypatch.setattr(
        transport_module,
        "local_timezone_payload",
        lambda: {"timezone_offset_min": 300, "timezone": "America/New_York"},
    )
    auth = ChatGPTAuthConfig(
        access_token="fake",
        captured_request_json={"timezone_offset_min": -420, "timezone": "Asia/Bangkok"},
    )
    transport = ChatGPTWebTransport(auth)

    payload = transport.build_chat_payload(ChatRequest(messages=[Message.text("user", "hello")]))

    assert payload["timezone_offset_min"] == 300
    assert payload["timezone"] == "America/New_York"


def test_build_image_payload_uses_picture_hint_and_multimodal_attachment():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))

    payload = transport._build_image_chat_payload(
        ImageRequest(prompt="translate this image", model="auto"),
        [
            {
                "file_id": "file_123",
                "file_name": "panel.png",
                "file_size": 123,
                "mime_type": "image/png",
                "width": 10,
                "height": 20,
            }
        ],
    )

    assert payload["system_hints"] == ["picture_v2"]
    message = payload["messages"][0]
    assert message["content"]["content_type"] == "multimodal_text"
    assert message["content"]["parts"][0]["asset_pointer"] == "file-service://file_123"
    assert message["content"]["parts"][1] == "translate this image"
    assert message["metadata"]["attachments"][0]["id"] == "file_123"
    assert "history_and_training_disabled" not in payload


def test_build_chat_payload_uploads_image_bytes_for_multimodal_message(monkeypatch):
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))

    def fake_upload(data, mime_type, image_name, headers):
        assert data == b"image-bytes"
        assert mime_type == "image/png"
        assert image_name == "panel.png"
        assert headers["authorization"] == "Bearer fake"
        return {
            "file_id": "file_panel",
            "file_name": "panel.png",
            "file_size": len(data),
            "mime_type": mime_type,
            "width": 64,
            "height": 64,
        }

    monkeypatch.setattr(transport, "_upload_file", fake_upload)
    payload = transport._build_chat_payload_with_uploaded_media(
        ChatRequest(
            messages=[
                Message(
                    role="user",
                    content=[
                        ContentPart.image_bytes(b"image-bytes", "image/png", "panel.png"),
                        ContentPart.text_part("OCR this image"),
                    ],
                )
            ],
            model="auto",
        ),
        {"authorization": "Bearer fake"},
    )

    assert payload["system_hints"] == ["picture_v2"]
    message = payload["messages"][0]
    assert message["content"]["content_type"] == "multimodal_text"
    assert message["content"]["parts"][0]["asset_pointer"] == "file-service://file_panel"
    assert message["content"]["parts"][1] == "OCR this image"
    assert message["metadata"]["attachments"][0]["id"] == "file_panel"


def test_generate_image_accepts_multiple_input_images(monkeypatch):
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"))
    uploaded = []

    def fake_upload(data, mime_type, image_name, headers):
        uploaded.append((data, mime_type, image_name))
        return {
            "file_id": f"file_{len(uploaded)}",
            "file_name": image_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": 8,
            "height": 8,
        }

    monkeypatch.setattr(transport, "_upload_file", fake_upload)
    monkeypatch.setattr(transport, "_refresh_web_tokens", lambda headers, payload: headers)
    monkeypatch.setattr(transport, "_post_conversation", lambda url, headers, payload: [{"conversation_id": "c1"}])
    monkeypatch.setattr(
        transport,
        "_poll_generated_image_assets",
        lambda conversation_id, headers, input_assets, timeout, poll_interval, cancel_requested: ["sediment://generated"],
    )
    monkeypatch.setattr(
        transport,
        "_download_generated_image",
        lambda asset, headers, conversation_id: type("Image", (), {"data": b"out", "url": None, "mime_type": "image/png", "raw": None})(),
    )

    response = transport._generate_image_sync(
        ImageRequest(
            prompt="combine",
            input_images=[
                ImageInput(b"one", "image/png", "one.png"),
                ImageInput(b"two", "image/jpeg", "two.jpg"),
            ],
            model="auto",
        )
    )

    assert uploaded == [(b"one", "image/png", "one.png"), (b"two", "image/jpeg", "two.jpg")]
    assert response.images[0].data == b"out"


def test_conversation_headers_drop_transport_headers():
    headers = {"authorization": "Bearer fake", "content-length": "5", "accept-encoding": "br"}

    replay = _conversation_headers(headers)

    assert replay["authorization"] == "Bearer fake"
    assert replay["accept"] == "text/event-stream"
    assert replay["content-type"] == "application/json"
    assert "content-length" not in replay
    assert "accept-encoding" not in replay


def test_event_to_delta_extracts_text():
    delta = _event_to_delta({"v": "hello", "conversation_id": "c1"})

    assert delta is not None
    assert delta.text == "hello"
    assert delta.conversation_id == "c1"


def test_event_to_delta_ignores_non_content_patch_values():
    delta = _event_to_delta(
        {
            "o": "patch",
            "v": [
                {"p": "/message/content/parts/0", "o": "append", "v": "hello"},
                {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
            ],
        }
    )

    assert delta is not None
    assert delta.text == "hello"


def test_stream_handoff_topic_prefers_websocket_topic():
    topic = _stream_handoff_topic(
        {
            "type": "stream_handoff",
            "options": [
                {"type": "resume_sse_endpoint", "topic_id": "sse-topic"},
                {"type": "subscribe_ws_topic", "topic_id": "ws-topic"},
            ],
        }
    )

    assert topic == "ws-topic"


def test_events_from_encoded_sse_extracts_json_payloads():
    events = _events_from_encoded_sse('event: delta\ndata: {"p":"/message/content/parts/0","v":"hi"}\n\n')

    assert events == [{"p": "/message/content/parts/0", "v": "hi"}]


def test_events_from_websocket_item_decodes_stream_item():
    events = _events_from_websocket_item(
        {
            "type": "message",
            "topic_id": "topic-1",
            "payload": {
                "type": "conversation-turn-stream",
                "payload": {
                    "type": "stream-item",
                    "encoded_item": 'event: delta\ndata: {"v":"hi"}\n\n',
                },
            },
        },
        "topic-1",
    )

    assert events == [{"v": "hi"}]


def test_deep_research_widget_info_finds_websocket_topic_inputs():
    info = _deep_research_widget_info_from_value(
        [
            {
                "v": {
                    "message": {
                        "metadata": {
                            "chatgpt_sdk": {
                                "tool_response_metadata": {
                                    "websocket_url": "wss://ws.chatgpt.test/session",
                                    "openai/widgetSessionId": "widget-123",
                                }
                            }
                        }
                    }
                }
            }
        ]
    )

    assert info is not None
    assert info.websocket_url == "wss://ws.chatgpt.test/session"
    assert info.widget_session_id == "widget-123"


def test_deep_research_cancel_uses_widget_session_ids(monkeypatch):
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="fake"), refresh_web_tokens=False)
    sessions = []
    stops = []

    events = [
        {"v": {"conversation_id": "conversation-1", "message": {"id": "message-1"}}},
        {
            "v": {
                "message": {
                    "metadata": {
                        "chatgpt_sdk": {
                            "tool_response_metadata": {
                                "websocket_url": "wss://ws.chatgpt.test/session",
                                "openai/widgetSessionId": "widget-123",
                            }
                        }
                    }
                }
            }
        },
    ]

    monkeypatch.setattr(transport, "_post_conversation", lambda url, headers, payload: events)
    monkeypatch.setattr(transport, "_maybe_skip_deep_research_sleep", lambda *args, **kwargs: {})
    monkeypatch.setattr(transport, "_maybe_confirm_deep_research_plan", lambda *args, **kwargs: ({}, None))

    def fake_stop(conversation_id, message_id, session_id, *, headers=None):
        stops.append((conversation_id, message_id, session_id))
        return {"status": "ok"}

    monkeypatch.setattr(transport, "stop_deep_research", fake_stop)

    request = ChatRequest(
        messages=[Message.text("user", "research")],
        model="auto",
        metadata={
            "on_deep_research_session": lambda conversation_id, message_id, session_id: sessions.append(
                (conversation_id, message_id, session_id)
            ),
            "cancel_requested": lambda: True,
        },
    )

    with pytest.raises(ProviderError, match="cancelled"):
        transport._deep_research_sync(request)

    assert sessions == [("conversation-1", "message-1", "widget-123")]
    assert stops == [("conversation-1", "message-1", "widget-123")]


def test_deep_research_report_text_from_widget_update():
    text = _deep_research_report_text_from_value(
        {
            "type": "conversation-update",
            "payload": {
                "update_type": "update-widget-state",
                "update_content": {
                    "updates": [
                        {
                            "widget_state": {
                                "status": "completed",
                                "report_message": {
                                    "content": {
                                        "content_type": "text",
                                        "parts": ["# Report\n\nDone."],
                                    }
                                },
                            }
                        }
                    ]
                },
            },
        }
    )

    assert text == "# Report\n\nDone."


def test_deep_research_report_text_from_serialized_widget_state():
    text = _deep_research_report_text_from_value(
        {
            "metadata": {
                "widget_state": (
                    '{"status":"completed","report_message":{"content":'
                    '{"content_type":"text","parts":["Saved report"]}}}'
                )
            }
        }
    )

    assert text == "Saved report"


def test_deep_research_waits_for_plan_confirmation():
    assert _deep_research_waits_for_plan_confirmation({"status": "waiting_for_user_response_on_plan"})
    assert _deep_research_waits_for_plan_confirmation({"waiting_for_user_response_on_plan_until": "2026-06-24T00:00:00Z"})
    assert not _deep_research_waits_for_plan_confirmation({"status": "completed"})


def test_deep_research_confirm_payload_matches_prepare_shape():
    payload = _deep_research_confirm_payload(
        {
            "model": "auto",
            "timezone_offset_min": -420,
            "timezone": "Asia/Bangkok",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["connector:connector_openai_deep_research"],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        },
        "conversation-1",
        "parent-1",
    )

    assert payload == {
        "action": "next",
        "conversation_id": "conversation-1",
        "parent_message_id": "parent-1",
        "model": "auto",
        "client_prepare_state": "none",
        "timezone_offset_min": -420,
        "timezone": "Asia/Bangkok",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": ["connector:connector_openai_deep_research"],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
    }
    assert "messages" not in payload


def test_deep_research_mcp_get_state_payload_matches_web_shape():
    assert _deep_research_mcp_get_state_payload("conversation-1", "message-1", "session-1") == {
        "app_uri": "connectors://connector_openai_deep_research",
        "tool_name": "get_state",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "tool_input": {"session_id": "session-1"},
    }


def test_deep_research_mcp_skip_sleep_payload_matches_web_shape():
    assert _deep_research_mcp_skip_sleep_payload("conversation-1", "message-1", "session-1") == {
        "app_uri": "connectors://connector_openai_deep_research",
        "tool_name": "skip_sleep",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "tool_input": {"session_id": "session-1"},
    }


def test_latest_message_id_from_value_prefers_last_message_node():
    assert (
        _latest_message_id_from_value(
            [
                {"v": {"message": {"id": "first"}}},
                {"v": {"message": {"id": "second"}}},
            ]
        )
        == "second"
    )


def test_image_asset_pointers_from_events_finds_patch_and_message_parts():
    events = [
        {
            "v": [
                {"p": "/message/content/parts/0/asset_pointer", "v": "file-service://generated_1"},
                {"p": "/message/content/parts/1", "v": "ignored"},
            ]
        },
        {
            "v": {
                "message": {
                    "content": {
                        "parts": [
                            {"content_type": "image_asset_pointer", "asset_pointer": "file-service://generated_2"}
                        ]
                    }
                }
            }
        },
    ]

    assets = _image_asset_pointers_from_events(events, exclude={"file-service://input_1"})

    assert assets == ["file-service://generated_1", "file-service://generated_2"]


def test_image_dimensions_reads_png_header():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (32).to_bytes(4, "big") + (16).to_bytes(4, "big")

    assert _image_dimensions(png) == (32, 16)
