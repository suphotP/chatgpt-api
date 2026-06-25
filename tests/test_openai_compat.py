import base64
import json

import pytest

import chatgpt_api.api.openai_compat as compat
from chatgpt_api.api.openai_compat import (
    AccountRouter,
    OpenAICompatConfig,
    OpenAICompatProviderError,
    _account_order_for_model,
    _build_chat_prompt,
    _chat_image_intercept_enabled,
    _classify_provider_error,
    _completion_response,
    _filter_repeated_successful_tool_calls,
    _image_aspect_ratio_from_body,
    _image_generation,
    _image_generation_response,
    _image_inputs_from_body,
    _image_edit,
    _models_for_config,
    _frontend_tool_call_quality_issue,
    _has_completed_file_action_after_latest_user,
    _has_successful_tool_result_after_latest_user,
    _models_with_agent_modes,
    _normalize_agent_prompt_mode,
    _parse_tool_calls,
    _provider_for_account,
    _provider_error_status_and_payload,
    _response_is_tool_call_json,
    _resolve_agent_prompt_mode,
    _resolve_image_model_alias,
    _resolve_model_alias,
    _should_retry_for_missing_tool_call,
    _split_model_agent_mode,
    _tool_call_policy_issue,
)
from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.types import ChatDelta, ImageAsset, ImageResponse
from chatgpt_api.providers.chatgpt.crypto import decrypt_text, is_encrypted, load_secrets_key


def test_resolve_model_alias_maps_intelligence_presets():
    assert _resolve_model_alias("gpt-5-5-thinking-standard", None) == ("gpt-5-5-thinking", "standard")
    assert _resolve_model_alias("gpt-5-5-thinking-extended", None) == ("gpt-5-5-thinking", "extended")
    assert _resolve_model_alias("gpt-5-5-thinking-max", None) == ("gpt-5-5-thinking", "max")
    assert _resolve_model_alias("gpt-5-5-pro-standard", None) == ("gpt-5-5-pro", "standard")
    assert _resolve_model_alias("gpt-5-5-pro-extended", None) == ("gpt-5-5-pro", "extended")
    assert _resolve_model_alias("chatgpt-deep-research", None) == ("auto", None)
    assert _resolve_model_alias("chatgpt-web/chatgpt-deep-research", None) == ("auto", None)
    assert _resolve_model_alias("auto", None) == ("auto", None)


def test_resolve_image_model_alias_maps_openai_names_to_auto():
    assert _resolve_image_model_alias("gpt-image-1") == "auto"
    assert _resolve_image_model_alias("chatgpt-image") == "auto"
    assert _resolve_image_model_alias("gpt-5-5") == "gpt-5-5"


def test_split_model_agent_mode_accepts_suffix():
    assert _split_model_agent_mode("auto@optimized") == ("auto", "optimized")
    assert _split_model_agent_mode("gpt-5-5-thinking-standard@opencode") == (
        "gpt-5-5-thinking-standard",
        "opencode",
    )
    assert _split_model_agent_mode("auto") == ("auto", None)


def test_normalize_agent_prompt_mode_aliases():
    assert _normalize_agent_prompt_mode("fast") == "optimized"
    assert _normalize_agent_prompt_mode("full") == "opencode"


def test_resolve_agent_prompt_mode_prefers_model_suffix():
    config = OpenAICompatConfig(account="free", agent_prompt_mode="opencode")
    body = {"metadata": {"agent_mode": "optimized"}}

    assert _resolve_agent_prompt_mode(config, body, "optimized") == "optimized"


def test_account_router_round_robin_rotates_start_account():
    router = AccountRouter(("free", "pro", "plus"), "round-robin")

    assert router.order() == ("free", "pro", "plus")
    assert router.order() == ("pro", "plus", "free")
    assert router.order() == ("plus", "free", "pro")


def test_account_router_weighted_prefers_high_weight_accounts():
    router = AccountRouter(("free", "pro"), "weighted")

    first_accounts = [router.order()[0] for _ in range(6)]

    assert "pro" in first_accounts


def test_provider_error_payload_includes_account_attempts():
    error = OpenAICompatProviderError(
        ProviderError("ChatGPT conversation returned empty assistant text."),
        requested_model="gpt-5-5",
        provider_model="gpt-5-5",
        account="free",
        account_attempts=[{"account": "free", "code": "chatgpt_empty_response"}],
    )

    _, payload = _provider_error_status_and_payload(error)

    assert payload["error"]["chatgpt_account"] == "free"
    assert payload["error"]["chatgpt_account_attempts"][0]["account"] == "free"


def test_classify_cloudflare_browser_challenge():
    code, error_type, status, hint = _classify_provider_error(
        "ChatGPT prepare failed: 403 Cloudflare browser challenge HTML.",
        403,
    )

    assert code == "chatgpt_browser_challenge"
    assert error_type == "provider_auth_error"
    assert status == 401
    assert "Cloudflare" in hint


def test_provider_error_payload_for_missing_account_capture():
    error = OpenAICompatProviderError(
        ProviderError("ChatGPT account capture for 'free' is not configured."),
        requested_model=None,
        provider_model=None,
        account="free",
    )

    status, payload = _provider_error_status_and_payload(error)

    assert status == 400
    assert payload["error"]["code"] == "chatgpt_missing_account_capture"
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["chatgpt_account"] == "free"


def test_admin_save_capture_requires_recommended_fields(tmp_path):
    capture_text = """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer fake-token
Cookie: oai-did=device-1; __Secure-next-auth.session-token.0=session-1
Request Data: {"action":"next","model":"auto"}
"""
    config = OpenAICompatConfig(
        account="pro",
        accounts_dir=tmp_path / "accounts",
        admin_db_path=tmp_path / "admin.sqlite",
    )

    status, payload = compat._save_account_capture_payload(
        config,
        {"account": "pro", "capture_text": capture_text},
    )

    assert status == 400
    assert payload["error"]["type"] == "invalid_request_error"
    assert "openai-sentinel-proof-token" in payload["error"]["failed"]
    assert not (tmp_path / "accounts" / "pro" / "chatgpt-request.txt").exists()


def test_admin_save_capture_writes_only_after_full_validation(tmp_path):
    capture_text = """
URL: https://chatgpt.com/backend-api/f/conversation
Authorization: Bearer fake-token
Cookie: oai-did=device-1; __Secure-next-auth.session-token.0=session-1
OpenAI-Sentinel-Chat-Requirements-Token: req-token
OpenAI-Sentinel-Proof-Token: proof-token
OpenAI-Sentinel-Turnstile-Token: turnstile-token
x-conduit-token: conduit-token
OAI-Device-Id: device-1
OAI-Session-Id: session-1
Request Data: {"action":"next","model":"auto"}
"""
    config = OpenAICompatConfig(
        account="pro",
        accounts_dir=tmp_path / "accounts",
        admin_db_path=tmp_path / "admin.sqlite",
    )

    status, payload = compat._save_account_capture_payload(
        config,
        {"account": "pro", "capture_text": capture_text},
    )

    assert status == 200
    assert payload["saved"] is True
    assert payload["inspection"]["ok"] is True
    assert payload["inspection"]["warnings"] == []
    on_disk = (tmp_path / "accounts" / "pro" / "chatgpt-request.txt").read_text(encoding="utf-8")
    assert is_encrypted(on_disk)
    assert on_disk != capture_text
    assert decrypt_text(on_disk, load_secrets_key(tmp_path / "accounts")) == capture_text


def test_admin_save_capture_allows_prepare_capture_without_request_json(tmp_path):
    capture_text = """
Request URL
https://chatgpt.com/backend-api/f/conversation/prepare
authorization
Bearer fake-token
cookie
oai-did=device-1; g_state={"i_l":0}; __Secure-next-auth.session-token.0=session-1
openai-sentinel-chat-requirements-token
req-token
openai-sentinel-proof-token
proof-token
openai-sentinel-turnstile-token
turnstile-token
x-conduit-token
no-token
oai-device-id
device-1
oai-session-id
session-1
"""
    config = OpenAICompatConfig(
        account="pro",
        accounts_dir=tmp_path / "accounts",
        admin_db_path=tmp_path / "admin.sqlite",
    )

    status, payload = compat._save_account_capture_payload(
        config,
        {"account": "pro", "capture_text": capture_text},
    )

    assert status == 200
    assert payload["saved"] is True
    assert payload["inspection"]["ok"] is True
    assert payload["inspection"]["warnings"] == []


def test_admin_save_capture_rejects_invalid_account_name(tmp_path):
    config = OpenAICompatConfig(
        account="pro",
        accounts_dir=tmp_path / "accounts",
        admin_db_path=tmp_path / "admin.sqlite",
    )

    with pytest.raises(ValueError, match="account must use only English"):
        compat._save_account_capture_payload(
            config,
            {"account": "โปร", "capture_text": "URL: https://chatgpt.com"},
        )


def test_admin_delete_account_removes_empty_account_from_list(tmp_path):
    accounts_dir = tmp_path / "accounts"
    capture_dir = accounts_dir / "free-2"
    capture_dir.mkdir(parents=True)
    (capture_dir / "chatgpt-request.txt").write_text("URL: https://chatgpt.com/backend-api/f/conversation\n")
    config = OpenAICompatConfig(
        account="pro",
        accounts=("pro",),
        accounts_dir=accounts_dir,
        admin_db_path=tmp_path / "admin.sqlite",
    )
    router = AccountRouter(("pro",), "failover")

    delete_payload = compat._admin_account_delete_payload(config, {"account": "free-2"})
    accounts_payload = compat._admin_accounts_response(config, router)

    assert delete_payload["deleted"]["capture"] is True
    assert delete_payload["deleted"]["empty_directory"] is True
    assert "free-2" not in delete_payload["remaining_accounts"]
    assert [account["account"] for account in accounts_payload["accounts"]] == ["pro"]


def test_admin_accounts_response_ignores_empty_account_dirs(tmp_path):
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "free-2").mkdir(parents=True)
    config = OpenAICompatConfig(
        account="pro",
        accounts=("pro",),
        accounts_dir=accounts_dir,
        admin_db_path=tmp_path / "admin.sqlite",
    )
    router = AccountRouter(("pro",), "failover")

    payload = compat._admin_accounts_response(config, router)

    assert [account["account"] for account in payload["accounts"]] == ["pro"]


def test_provider_for_account_uses_chrome_impersonation_for_chrome_capture(tmp_path):
    capture_dir = tmp_path / "accounts" / "free-2"
    capture_dir.mkdir(parents=True)
    (capture_dir / "chatgpt-request.txt").write_text(
        """
Request URL
https://chatgpt.com/backend-api/f/conversation
authorization
Bearer fake-token
cookie
oai-did=device-1
user-agent
Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36
{"action":"next","messages":[],"model":"auto","parent_message_id":"client-created-root"}
""",
        encoding="utf-8",
    )

    provider = compat._provider_for_account(
        OpenAICompatConfig(account="free-2", accounts_dir=tmp_path / "accounts"),
        "free-2",
    )

    assert provider.transport.impersonate == "chrome"


def test_multi_account_chat_falls_back_after_provider_error(monkeypatch):
    calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

    def fake_provider_for_account(config, account=None):
        return FakeProvider(account or config.account)

    async def fake_collect_messages_text(provider, messages, model_slug, thinking_effort, temporary_chat):
        assert temporary_chat is True
        calls.append(provider.account)
        if provider.account == "free":
            raise ProviderError("ChatGPT conversation failed: 422 model limit")
        return "ok from pro"

    async def fake_conversation_init_metadata(provider, provider_model):
        return {"default_model_slug": "auto", "model_limits": [{"model_slug": provider_model}]}

    monkeypatch.setattr(compat, "_provider_for_account", fake_provider_for_account)
    monkeypatch.setattr(compat, "_collect_messages_text", fake_collect_messages_text)
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="free", accounts=("free", "pro"), account_strategy="failover"),
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
    )

    assert calls == ["free", "pro"]
    assert response["choices"][0]["message"]["content"] == "ok from pro"


def test_stream_disconnect_stops_chatgpt_conversation(monkeypatch):
    stop_calls = []

    class FakeTransport:
        def stop_conversation(self, conversation_id, *, exclude_async_types=None):
            stop_calls.append((conversation_id, exclude_async_types))
            return {"status": "ok"}

    class FakeProvider:
        transport = FakeTransport()

        async def stream_chat(self, request):
            yield ChatDelta(conversation_id="conversation-1")
            yield ChatDelta(text="hello", conversation_id="conversation-1")

    def fake_provider_for_account(config, account=None):
        return FakeProvider()

    monkeypatch.setattr(compat, "_provider_for_account", fake_provider_for_account)

    def disconnected(_text):
        raise compat._ClientDisconnected()

    with pytest.raises(compat._ClientDisconnected):
        compat.asyncio.run(
            compat._stream_messages_text_with_accounts(
                OpenAICompatConfig(account="free"),
                AccountRouter(("free",), "sticky"),
                [{"role": "user", "content": "hello"}],
                "auto",
                "auto",
                None,
                True,
                disconnected,
            )
        )

    assert stop_calls == [("conversation-1", ["pro_mode"])]


def test_cancel_chatgpt_operation_calls_stop_conversation():
    stop_calls = []

    class FakeTransport:
        def stop_conversation(self, conversation_id, *, exclude_async_types=None):
            stop_calls.append((conversation_id, exclude_async_types))
            return {"status": "ok"}

    class FakeProvider:
        transport = FakeTransport()

    operation = compat._create_chatgpt_operation("chat")
    compat._update_chatgpt_operation(
        operation.operation_id,
        provider=FakeProvider(),
        account="free",
        conversation_id="conversation-1",
    )

    status, payload = compat._cancel_chatgpt_operation(operation.operation_id)

    assert status == 200
    assert payload["operation"]["cancel_requested"] is True
    assert payload["operation"]["conversation_id"] == "conversation-1"
    assert stop_calls == [("conversation-1", ["pro_mode"])]


def test_chat_falls_back_to_auto_model_after_empty_explicit_model(monkeypatch):
    calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_collect_prompt_text(provider, prompt, model_slug, thinking_effort, temporary_chat):
        assert temporary_chat is True
        calls.append((provider.account, model_slug, thinking_effort))
        if model_slug == "gpt-5-5-pro":
            raise ProviderError("ChatGPT conversation returned empty assistant text.")
        return "ok from auto"

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_collect_prompt_text", fake_collect_prompt_text)
    monkeypatch.setattr(compat, "_conversation_init_metadata", lambda provider, provider_model: compat.asyncio.sleep(0, result={}))

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="pro", accounts=("pro",), model_fallback="auto"),
            {"model": "gpt-5-5-pro-extended@optimized", "messages": [{"role": "user", "content": "hello"}]},
        )
    )

    assert calls == [("pro", "gpt-5-5-pro", "extended"), ("pro", "auto", None)]
    assert response["choices"][0]["message"]["content"] == "ok from auto"
    assert response["chatgpt_fallback_model"] == "auto"


def test_deep_research_uses_normal_chat_and_bypasses_tool_prompt(monkeypatch, tmp_path):
    seen = {}

    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_collect_deep_research_text(provider, prompt, model_slug):
        seen["account"] = provider.account
        seen["prompt"] = prompt
        seen["model_slug"] = model_slug
        return "research result citeturn1view0"

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_collect_deep_research_text", fake_collect_deep_research_text)
    monkeypatch.setattr(compat, "_conversation_init_metadata", lambda provider, provider_model: compat.asyncio.sleep(0, result={}))

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(
                account="pro",
                accounts=("pro",),
                account_strategy="failover",
                research_output_dir=tmp_path,
            ),
            {
                "model": "chatgpt-deep-research",
                "messages": [{"role": "user", "content": "ค้นคว้าแบบละเอียด"}],
                "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}],
                "metadata": {"temporary_chat": True},
            },
        )
    )

    assert seen == {"account": "pro", "prompt": "ค้นคว้าแบบละเอียด", "model_slug": "auto"}
    assert response["choices"][0]["message"]["content"].startswith("Deep Research complete.\nSaved report: ")
    assert response["chatgpt_account"] == "pro"
    assert response["chatgpt_research_report_path"].endswith(".md")
    assert compat.Path(response["chatgpt_research_report_path"]).read_text(encoding="utf-8") == "research result"


def test_deep_research_accepts_opencode_provider_prefixed_model(monkeypatch, tmp_path):
    seen = {}

    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_collect_deep_research_text(provider, prompt, model_slug):
        seen["account"] = provider.account
        seen["prompt"] = prompt
        seen["model_slug"] = model_slug
        return "research result", {"status": "completed"}

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_collect_deep_research_text", fake_collect_deep_research_text)
    monkeypatch.setattr(compat, "_conversation_init_metadata", lambda provider, provider_model: compat.asyncio.sleep(0, result={}))

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="pro", accounts=("pro",), research_output_dir=tmp_path),
            {
                "model": "chatgpt-web/chatgpt-deep-research@optimized",
                "messages": [{"role": "user", "content": "research this"}],
            },
        )
    )

    assert seen == {"account": "pro", "prompt": "research this", "model_slug": "auto"}
    assert response["chatgpt_account"] == "pro"
    assert response["chatgpt_research"]["status"] == "completed"
    assert response["chatgpt_research_report_path"].endswith(".md")
    assert response["chatgpt_research_report_download_url"].startswith("/v1/chatgpt/files/")
    assert response["choices"][0]["message"]["content"] == (
        f"Deep Research complete.\n"
        f"Saved report: {response['chatgpt_research_report_path']}\n"
        f"Download link: {response['chatgpt_research_report_download_url']}"
    )


def test_deep_research_skips_account_with_exhausted_preflight_limit(monkeypatch, tmp_path):
    calls = []
    init_calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_conversation_init_metadata(provider, provider_model):
        init_calls.append(provider.account)
        if provider.account == "free":
            return {
                "limits_progress": [
                    {
                        "feature_name": "deep_research",
                        "remaining": 0,
                        "reset_after": "2026-06-25T00:00:00+00:00",
                    }
                ]
            }
        return {"limits_progress": [{"feature_name": "deep_research", "remaining": 3}]}

    async def fake_collect_deep_research_text(provider, prompt, model_slug):
        calls.append(provider.account)
        return "research from pro", {"status": "completed"}

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_collect_deep_research_text", fake_collect_deep_research_text)

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(
                account="free",
                accounts=("free", "pro"),
                account_strategy="failover",
                research_output_dir=tmp_path,
            ),
            {
                "model": "chatgpt-deep-research",
                "messages": [{"role": "user", "content": "research this"}],
            },
        )
    )

    assert init_calls == ["free", "pro"]
    assert calls == ["pro"]
    assert response["chatgpt_account"] == "pro"
    assert response["chatgpt_research_report_download_url"].startswith("/v1/chatgpt/files/")
    assert response["choices"][0]["message"]["content"] == (
        f"Deep Research complete.\n"
        f"Saved report: {response['chatgpt_research_report_path']}\n"
        f"Download link: {response['chatgpt_research_report_download_url']}"
    )
    assert compat.Path(response["chatgpt_research_report_path"]).read_text(encoding="utf-8") == "research from pro"


def test_deep_research_preflight_limit_error_payload(monkeypatch):
    class FakeProvider:
        account = "free"

    async def fake_conversation_init_metadata(provider, provider_model):
        return {
            "limits_progress": [
                {
                    "feature_name": "deep_research",
                    "remaining": 0,
                    "reset_after": "2026-06-25T00:00:00+00:00",
                }
            ]
        }

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider())
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)

    with pytest.raises(OpenAICompatProviderError) as raised:
        compat.asyncio.run(
            compat._chat_completion(
                OpenAICompatConfig(account="free", accounts=("free",), account_strategy="failover"),
                {
                    "model": "chatgpt-deep-research",
                    "messages": [{"role": "user", "content": "research this"}],
                },
            )
        )

    status, payload = _provider_error_status_and_payload(raised.value)

    assert status == 400
    assert payload["error"]["code"] == "chatgpt_model_limit"
    assert payload["error"]["chatgpt_deep_research_limit"]["remaining"] == 0
    assert "2026-06-25T00:00:00+00:00" in payload["error"]["message"]


def test_account_usage_response_summarizes_remaining(monkeypatch):
    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_conversation_init_metadata(provider, provider_model):
        assert provider_model == "auto"
        return {
            "default_model_slug": "auto",
            "limits_progress": [
                {"feature_name": "deep_research", "remaining": 2, "reset_after": "2026-06-25T00:00:00+00:00"},
                {"feature_name": "image_gen", "remaining": 5, "reset_after": "2026-06-24T00:00:00+00:00"},
            ],
            "model_limits": [{"model_slug": "gpt-5-5", "resets_after": "2026-06-24T01:00:00+00:00"}],
            "blocked_features": [
                {
                    "name": "file_upload",
                    "description": "Attachments are currently blocked.",
                    "resets_after": "2026-06-24T02:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_account_capture_usage_summary", lambda config, account: {"plan_type": account})

    response = compat.asyncio.run(
        compat._account_usage_response(
            OpenAICompatConfig(account="free", accounts=("free", "pro")),
            AccountRouter(("free", "pro"), "failover"),
        )
    )

    assert response["object"] == "chatgpt.usage"
    assert [account["account"] for account in response["accounts"]] == ["free", "pro"]
    assert response["accounts"][0]["ok"] is True
    assert response["accounts"][0]["plan_type"] == "free"
    assert response["accounts"][0]["features"]["deep_research"]["remaining"] == 2
    assert response["accounts"][0]["features"]["image_gen"]["status"] == "available"
    assert response["accounts"][0]["features"]["file_upload"]["blocked"] is True
    assert response["accounts"][0]["model_limits"][0]["model_slug"] == "gpt-5-5"


def test_account_usage_metadata_unavailable_is_not_live_error(monkeypatch):
    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_conversation_init_metadata(provider, provider_model):
        assert provider_model == "auto"
        return None

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_account_capture_usage_summary", lambda config, account: {"plan_type": "free"})

    response = compat.asyncio.run(
        compat._account_usage_response(
            OpenAICompatConfig(account="free-2", accounts=("free-2",)),
            AccountRouter(("free-2",), "failover"),
        )
    )

    account = response["accounts"][0]
    assert account["ok"] is None
    assert account["status"] == "metadata_unavailable"
    assert "error" not in account
    assert "conversation/init returned no usage metadata" in account["warning"]


def test_admin_account_check_probes_chat_when_metadata_unavailable(monkeypatch):
    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_conversation_init_metadata(provider, provider_model):
        assert provider_model == "auto"
        return None

    async def fake_collect_text(provider, request):
        assert provider.account == "free-2"
        assert request.model == "auto"
        assert request.metadata["history_and_training_disabled"] is True
        return "ok"

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_collect_text", fake_collect_text)
    monkeypatch.setattr(compat, "_account_capture_usage_summary", lambda config, account: {"plan_type": "free"})

    response = compat.asyncio.run(
        compat._admin_accounts_check_payload(
            OpenAICompatConfig(account="free-2", accounts=("free-2",)),
            AccountRouter(("free-2",), "failover"),
            {"account": "free-2"},
        )
    )

    account = response["accounts"][0]
    assert account["ok"] is True
    assert account["status"] == "chat_ok_metadata_unavailable"
    assert account["chat_probe"] == {"ok": True, "preview": "ok"}


def test_admin_account_check_retries_probe_without_refresh(monkeypatch):
    class FakeProvider:
        def __init__(self, account, refresh_web_tokens):
            self.account = account
            self.refresh_web_tokens = refresh_web_tokens

    async def fake_conversation_init_metadata(provider, provider_model):
        assert provider_model == "auto"
        return None

    async def fake_collect_text(provider, request):
        assert request.model == "auto"
        if provider.refresh_web_tokens:
            raise ProviderError("ChatGPT prepare failed: 403")
        return "ok"

    def fake_provider_for_account(config, account=None, *, refresh_web_tokens=True):
        return FakeProvider(account or config.account, refresh_web_tokens)

    monkeypatch.setattr(compat, "_provider_for_account", fake_provider_for_account)
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_collect_text", fake_collect_text)
    monkeypatch.setattr(compat, "_account_capture_usage_summary", lambda config, account: {"plan_type": "free"})

    response = compat.asyncio.run(
        compat._admin_accounts_check_payload(
            OpenAICompatConfig(account="free-2", accounts=("free-2",)),
            AccountRouter(("free-2",), "failover"),
            {"account": "free-2"},
        )
    )

    account = response["accounts"][0]
    assert account["ok"] is True
    assert account["chat_probe_fallback"] == "no_refresh_web_tokens"
    assert account["chat_probe"]["preview"] == "ok"


def test_local_usage_command_returns_markdown_table_without_provider_chat(monkeypatch):
    class FakeProvider:
        def __init__(self, account):
            self.account = account

    async def fake_conversation_init_metadata(provider, provider_model):
        return {
            "default_model_slug": "auto",
            "limits_progress": [{"feature_name": "deep_research", "remaining": 4}],
        }

    async def fail_collect_prompt_text(*args, **kwargs):
        raise AssertionError("local usage command should not call ChatGPT chat completion")

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)
    monkeypatch.setattr(compat, "_collect_prompt_text", fail_collect_prompt_text)
    monkeypatch.setattr(
        compat,
        "_account_capture_usage_summary",
        lambda config, account: {"plan_type": account},
    )

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="free", accounts=("free", "pro"), account_strategy="failover"),
            {"model": "auto@optimized", "messages": [{"role": "user", "content": "/chatgpt:usage"}]},
        )
    )

    content = response["choices"][0]["message"]["content"]
    assert response["chatgpt_account"] == "local"
    assert "| Account | Plan | OK | Default | Deep Research |" in content
    assert "| free | free | yes | auto | 4 |" in content


def test_account_order_prefers_accounts_that_support_requested_model(monkeypatch):
    def fake_account_supports_model(config, account, model_slug, thinking_effort):
        assert model_slug == "gpt-5-5-pro"
        assert thinking_effort == "extended"
        return {"free": False, "pro": True, "unknown": None}[account]

    monkeypatch.setattr(compat, "_account_supports_model", fake_account_supports_model)
    router = AccountRouter(("free", "unknown", "pro"), "failover")

    assert _account_order_for_model(
        OpenAICompatConfig(account="free", accounts=("free", "unknown", "pro")),
        router,
        "gpt-5-5-pro",
        "extended",
    ) == ("pro", "unknown", "free")


def test_multi_account_image_generation_falls_back_after_provider_error(monkeypatch, tmp_path):
    calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            calls.append(self.account)
            if self.account == "free":
                raise ProviderError("ChatGPT conversation failed: 422 model limit")
            return ImageResponse(images=[ImageAsset(data=b"png", mime_type="image/png")], prompt=request.prompt)

    def fake_provider_for_account(config, account=None):
        return FakeProvider(account or config.account)

    async def fake_conversation_init_metadata(provider, provider_model):
        return {"default_model_slug": "auto", "model_limits": [{"model_slug": provider_model}]}

    monkeypatch.setattr(compat, "_provider_for_account", fake_provider_for_account)
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_conversation_init_metadata)

    response = compat.asyncio.run(
        _image_generation(
            OpenAICompatConfig(
                account="free",
                accounts=("free", "pro"),
                account_strategy="failover",
                image_output_dir=tmp_path,
            ),
            {"model": "auto", "prompt": "draw", "response_format": "b64_json"},
        )
    )

    assert calls == ["free", "pro"]
    assert response["chatgpt_account"] == "pro"
    assert response["data"][0]["b64_json"] == base64.b64encode(b"png").decode("ascii")
    assert response["data"][0]["path"].endswith(".png")
    assert str(tmp_path) in response["data"][0]["path"]
    assert compat.Path(response["data"][0]["path"]).read_bytes() == b"png"


def test_multi_account_image_generation_does_not_fall_back_after_missing_asset(monkeypatch):
    calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            calls.append(self.account)
            if self.account == "free":
                raise ProviderError("ChatGPT image generation returned no image asset")
            return ImageResponse(images=[ImageAsset(url="https://example.test/image.png")], prompt=request.prompt)

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", lambda provider, provider_model: compat.asyncio.sleep(0, result={}))

    with pytest.raises(OpenAICompatProviderError):
        compat.asyncio.run(
            _image_generation(
                OpenAICompatConfig(account="free", accounts=("free", "pro"), account_strategy="failover"),
                {"model": "auto", "prompt": "draw"},
            )
        )

    assert calls == ["free"]


def test_image_generation_response_returns_url_by_default():
    response = _image_generation_response(
        "auto",
        ImageResponse(images=[ImageAsset(url="https://example.test/image.png")], prompt="draw"),
        "url",
        account="pro",
    )

    assert response["model"] == "auto"
    assert response["chatgpt_account"] == "pro"
    assert response["data"] == [{"url": "https://example.test/image.png"}]


def test_image_generation_response_saves_downloaded_image_bytes(tmp_path):
    response = _image_generation_response(
        "auto",
        ImageResponse(images=[ImageAsset(data=b"png", mime_type="image/png")], prompt="draw cat"),
        "url",
        account="pro",
        default_output_dir=tmp_path,
    )

    path = compat.Path(response["data"][0]["path"])
    assert path.parent == tmp_path
    assert path.read_bytes() == b"png"
    assert response["data"][0]["url"].startswith("/v1/chatgpt/files/")
    assert response["data"][0]["download_url"] == response["data"][0]["url"]
    assert response["data"][0]["file_id"]


def test_chat_image_request_saves_to_requested_path(monkeypatch, tmp_path):
    requested_path = tmp_path / "cat.png"

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            assert "สร้างรูปแมว" in request.prompt
            return ImageResponse(images=[ImageAsset(data=b"cat", mime_type="image/png")], prompt=request.prompt)

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="pro", accounts=("pro",), image_output_dir=tmp_path),
            {
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": f"สร้างรูปแมว แล้วเซฟไว้ที่ `{requested_path}`",
                    }
                ],
                "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}],
            },
        )
    )

    content = response["choices"][0]["message"]["content"]
    assert str(requested_path) in content
    assert requested_path.read_bytes() == b"cat"


def test_chat_image_request_does_not_repeat_after_assistant_response(monkeypatch, tmp_path):
    calls = {"image": 0, "text": 0}

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            calls["image"] += 1
            raise AssertionError("image generation should not repeat after assistant response")

    async def fake_collect_messages_text(provider, messages, model_slug, thinking_effort, temporary_chat):
        calls["text"] += 1
        return "Already saved."

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_collect_messages_text", fake_collect_messages_text)

    response = compat.asyncio.run(
        compat._chat_completion(
            OpenAICompatConfig(account="pro", accounts=("pro",), image_output_dir=tmp_path),
            {
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "สร้างรูปแมวให้หน่อย"},
                    {"role": "assistant", "content": "Image generated and saved to:\n/tmp/cat.png"},
                ],
            },
        )
    )

    assert calls == {"image": 0, "text": 1}
    assert response["choices"][0]["message"]["content"] == "Already saved."


def test_chat_image_intercept_ignores_structured_game_prompt():
    prompt = json.dumps(
        {
            "task": "Continue one turn of an interactive character game.",
            "output_contract": {
                "reply": "assistant dialogue",
                "state_patch": {},
                "scene": {
                    "image_prompt": "production image prompt if this scene is worth illustrating",
                    "image_recommended": True,
                },
            },
            "rules": ["Return valid JSON only."],
        }
    )

    assert _chat_image_intercept_enabled({}, prompt) is False
    assert _chat_image_intercept_enabled({"chatgpt_image_intercept": True}, prompt) is True


def test_chat_image_request_dedupes_identical_retry(monkeypatch, tmp_path):
    with compat._IMAGE_REQUEST_CACHE_LOCK:
        compat._IMAGE_REQUEST_CACHE.clear()

    requested_path = tmp_path / "retry-cat.png"
    calls = []

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            calls.append(self.account)
            return ImageResponse(images=[ImageAsset(data=b"cat", mime_type="image/png")], prompt=request.prompt)

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))

    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": f"สร้างรูปแมว แล้วเซฟไว้ที่ `{requested_path}`"}],
    }
    config = OpenAICompatConfig(account="pro", accounts=("pro",), image_output_dir=tmp_path)

    first = compat.asyncio.run(compat._chat_completion(config, body))
    second = compat.asyncio.run(compat._chat_completion(config, body))

    assert calls == ["pro"]
    assert second == first
    assert requested_path.read_bytes() == b"cat"


def test_image_inputs_from_body_accepts_path_and_data_url(tmp_path):
    image_path = tmp_path / "icon.png"
    image_path.write_bytes(b"png")
    data_url = "data:image/png;base64," + base64.b64encode(b"inline").decode("ascii")

    inputs = _image_inputs_from_body({"images": [str(image_path), data_url]}, require=True)

    assert [item.data for item in inputs] == [b"png", b"inline"]
    assert inputs[0].name == "icon.png"
    assert inputs[1].mime_type == "image/png"


def test_image_inputs_from_body_rejects_more_than_ten():
    with pytest.raises(ValueError, match="at most 10"):
        _image_inputs_from_body({"images": ["data:image/png;base64,aW1n"] * 11}, require=True)


def test_image_aspect_ratio_accepts_supported_values():
    assert _image_aspect_ratio_from_body({"aspect_ratio": "1:1"}) == "1:1"
    assert _image_aspect_ratio_from_body({"size": "1792x1024"}) == "16:9"


def test_image_edit_uses_multiple_input_images_and_one_output(monkeypatch, tmp_path):
    calls = []
    image_path = tmp_path / "ref.png"
    image_path.write_bytes(b"ref")

    class FakeProvider:
        def __init__(self, account):
            self.account = account

        async def generate_image(self, request):
            calls.append(request)
            assert len(request.input_images) == 2
            assert "Return exactly one edited/generated image." in request.prompt
            assert "Make the aspect ratio 1:1." in request.prompt
            return ImageResponse(images=[ImageAsset(data=b"edited", mime_type="image/png")], prompt=request.prompt)

    async def fake_init_metadata(provider, model):
        return {"limits_progress": []}

    monkeypatch.setattr(compat, "_provider_for_account", lambda config, account=None: FakeProvider(account or config.account))
    monkeypatch.setattr(compat, "_conversation_init_metadata", fake_init_metadata)

    response = compat.asyncio.run(
        _image_edit(
            OpenAICompatConfig(account="pro", accounts=("pro",), image_output_dir=tmp_path),
            {
                "model": "auto",
                "prompt": "turn these references into one app icon",
                "images": [
                    str(image_path),
                    "data:image/png;base64," + base64.b64encode(b"inline").decode("ascii"),
                ],
                "aspect_ratio": "1:1",
                "response_format": "url",
            },
        )
    )

    assert len(calls) == 1
    assert response["input_image_count"] == 2
    assert response["aspect_ratio"] == "1:1"
    assert response["data"][0]["download_url"]
    assert (tmp_path / response["data"][0]["filename"]).exists()


def test_models_for_config_includes_openai_image_alias(monkeypatch):
    monkeypatch.setattr(compat, "_models_for_account", lambda config, account=None: [{"id": "auto", "name": "Auto"}])

    models = _models_for_config(OpenAICompatConfig(account="free"))

    assert {"id": "gpt-image-1", "name": "ChatGPT Image"} in models


def test_models_for_config_handles_missing_capture(tmp_path):
    models = _models_for_config(OpenAICompatConfig(account="free", accounts_dir=tmp_path))
    ids = {model["id"] for model in models}

    assert "auto" in ids
    assert "auto@optimized" in ids
    assert "gpt-image-1" in ids
    assert "chatgpt-deep-research" in ids


def test_provider_for_account_reports_missing_capture(tmp_path):
    with pytest.raises(ProviderError, match="account capture"):
        _provider_for_account(OpenAICompatConfig(account="free", accounts_dir=tmp_path))


def test_build_chat_prompt_optimized_uses_compact_tools():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "write",
                "description": "Writes a file" * 200,
                "parameters": {
                    "type": "object",
                    "required": ["filePath", "content"],
                    "properties": {
                        "filePath": {"type": "string", "description": "Absolute file path"},
                        "content": {"type": "string", "description": "File content"},
                    },
                },
            },
        }
    ]
    messages = [
        {"role": "system", "content": "long opencode prompt " * 500},
        {"role": "user", "content": "สร้างไฟล์ a.txt"},
    ]

    prompt = _build_chat_prompt(messages, tools, "auto", "optimized")

    assert "AVAILABLE_TOOLS_COMPACT" in prompt
    assert "AVAILABLE_TOOLS:" not in prompt
    assert "RECENT_TRANSCRIPT" in prompt
    assert "Prefer file tools over bash" in prompt
    assert "you must call a tool" in prompt
    assert 'TOOL_CHOICE "auto" still allows and requires tool calls' in prompt
    assert "Do not answer with shell commands" in prompt


def test_build_chat_prompt_opencode_uses_full_tools():
    tools = [{"type": "function", "function": {"name": "write", "description": "Writes a file", "parameters": {}}}]
    messages = [{"role": "user", "content": "สร้างไฟล์ a.txt"}]

    prompt = _build_chat_prompt(messages, tools, "auto", "opencode")

    assert "AVAILABLE_TOOLS:" in prompt
    assert "CONVERSATION_TRANSCRIPT" in prompt
    assert "AVAILABLE_TOOLS_COMPACT" not in prompt


def test_models_with_agent_modes_adds_suffix_aliases():
    models = _models_with_agent_modes([{"id": "auto", "name": "ChatGPT Auto"}])
    ids = {model["id"] for model in models}

    assert {"auto", "auto@optimized", "auto@opencode"} <= ids


def test_provider_error_payload_explains_model_limit_status():
    error = OpenAICompatProviderError(
        ProviderError("ChatGPT conversation failed: 422 {\"detail\":\"model limit\"}"),
        requested_model="gpt-5-5",
        provider_model="gpt-5-5",
    )

    status, payload = _provider_error_status_and_payload(error)

    assert status == 400
    assert payload["error"]["code"] == "chatgpt_model_limit"
    assert payload["error"]["provider_status"] == 422
    assert "chatgpt-web/auto" in payload["error"]["message"]
    assert "gpt-5-5" in payload["error"]["message"]


def test_provider_error_payload_classifies_auth_challenge():
    status, payload = _provider_error_status_and_payload(ProviderError("ChatGPT prepare failed: 403"))

    assert status == 401
    assert payload["error"]["code"] == "chatgpt_auth_or_browser_challenge"


def test_provider_error_payload_explains_empty_response():
    error = OpenAICompatProviderError(
        ProviderError("ChatGPT conversation returned empty assistant text."),
        requested_model="gpt-5-5-thinking-standard",
        provider_model="gpt-5-5-thinking",
        init_metadata={
            "default_model_slug": "auto",
            "model_limits": [
                {
                    "model_slug": "gpt-5-5-thinking",
                    "using_default_model_slug": "gpt-5-5",
                    "resets_after": "2026-06-23T23:29:21.567258+00:00",
                    "description": None,
                }
            ],
            "blocked_features": [
                {
                    "name": "file_upload",
                    "description": "Attachments are not available until the model limit resets.",
                }
            ],
        },
    )

    status, payload = _provider_error_status_and_payload(error)

    assert status == 400
    assert payload["error"]["code"] == "chatgpt_empty_response"
    assert "chatgpt-web/auto" in payload["error"]["message"]
    assert "2026-06-23T23:29:21.567258+00:00" in payload["error"]["message"]
    assert payload["error"]["chatgpt_default_model_slug"] == "auto"
    assert payload["error"]["chatgpt_model_limit"]["model_slug"] == "gpt-5-5-thinking"


def test_parse_tool_calls_from_json_bridge_response():
    tools = [
        {
            "type": "function",
            "function": {"name": "write_file", "parameters": {"type": "object"}},
        }
    ]
    text = json.dumps({"tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "ok"}}]})

    calls = _parse_tool_calls(text, tools)

    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "write_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt", "content": "ok"}


def test_parse_tool_calls_accepts_case_variant_from_model():
    tools = [
        {
            "type": "function",
            "function": {"name": "bash", "parameters": {"type": "object"}},
        }
    ]
    text = json.dumps(
        {
            "Tool_calls": [
                {
                    "name": "bash",
                    "arguments": {
                        "command": "ls",
                        "description": "Lists files in current directory",
                    },
                }
            ]
        }
    )

    calls = _parse_tool_calls(text, tools)

    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "command": "ls",
        "description": "Lists files in current directory",
    }


def test_parse_tool_calls_accepts_openai_function_shape():
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    text = json.dumps(
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"ls","description":"Lists files"}',
                    }
                }
            ]
        }
    )

    calls = _parse_tool_calls(text, tools)

    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls", "description": "Lists files"}


def test_parse_tool_calls_accepts_malformed_name_fragment():
    tools = [{"type": "function", "function": {"name": "apply_patch", "parameters": {"type": "object"}}}]
    text = (
        '":"apply_patch","arguments":{"patchText":"*** Begin Patch\\n'
        "*** Add File: /Users/work/Desktop/restaurant-landing.html\\n"
        "+<!DOCTYPE html>\\n"
        "*** End Patch\"}\n"
        "สร้างไฟล์ landing page ไว้ที่ Desktop ให้แล้ว"
    )

    calls = _parse_tool_calls(text, tools)

    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "apply_patch"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "patchText": "*** Begin Patch\n*** Add File: /Users/work/Desktop/restaurant-landing.html\n+<!DOCTYPE html>\n*** End Patch"
    }


def test_parse_tool_calls_ignores_fragment_for_unknown_tool():
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    text = '":"apply_patch","arguments":{"patchText":"*** Begin Patch\\n*** End Patch"}'

    assert _parse_tool_calls(text, tools) == []


def test_parse_tool_calls_ignores_unknown_tool():
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    assert _parse_tool_calls('{"tool_calls":[{"name":"delete_everything","arguments":{}}]}', tools) == []


def test_completion_response_uses_tool_finish_reason():
    response = _completion_response(
        "gpt-5-5",
        '{"tool_calls":[]}',
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "read_file"


def test_should_retry_when_file_task_claims_success_without_tool_call():
    tools = [{"type": "function", "function": {"name": "apply_patch", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "สร้างไฟล์ restaurant-landing.html ให้หน่อย"}]

    assert _should_retry_for_missing_tool_call(messages, tools, "auto", "Created `restaurant-landing.html`.") is True


def test_should_retry_when_file_path_task_says_done_without_tool_call():
    tools = [{"type": "function", "function": {"name": "apply_patch", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "แทนที่ landing.html ด้วย landing page สวยๆ เอาดีๆ"}]

    assert _should_retry_for_missing_tool_call(messages, tools, "auto", "Done.") is True


def test_should_retry_when_agent_abandons_after_tool_result():
    tools = [{"type": "function", "function": {"name": "apply_patch", "parameters": {"type": "object"}}}]
    messages = [
        {"role": "user", "content": "แทนที่ landing.html ด้วย landing page สวยๆ เอาดีๆ"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": '{"pattern":"**/landing.html"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "No files found"},
    ]
    response = "Write tool ไม่ได้ถูกเรียกถูก schema ใน runtime นี้ เลยยังสร้างไฟล์ให้จริงไม่ได้"

    assert _should_retry_for_missing_tool_call(messages, tools, "auto", response) is True


def test_should_not_retry_final_answer_after_tool_result():
    tools = [{"type": "function", "function": {"name": "apply_patch", "parameters": {"type": "object"}}}]
    messages = [
        {"role": "user", "content": "สร้างไฟล์ restaurant-landing.html ให้หน่อย"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "apply_patch", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Success. Updated files."},
    ]

    assert _should_retry_for_missing_tool_call(messages, tools, "auto", "สร้างให้แล้ว") is False


def test_detects_successful_tool_result_after_latest_user():
    messages = [
        {"role": "user", "content": "สร้าง landing.html"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Wrote file successfully."},
    ]

    assert _has_successful_tool_result_after_latest_user(messages) is True


def test_detects_tool_call_json_text():
    assert _response_is_tool_call_json('{"tool_calls":[{"name":"write","arguments":{}}]}') is True
    assert _response_is_tool_call_json("Done.") is False


def test_tool_policy_rejects_bash_file_redirection_for_file_write_task():
    messages = [{"role": "user", "content": "สร้างไฟล์ refresh.txt ในโฟลเดอร์นี้"}]
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps(
                    {"command": "echo refreshed > refresh.txt", "description": "Create refresh file"}
                ),
            },
        }
    ]

    issue = _tool_call_policy_issue(messages, tool_calls)

    assert issue is not None
    assert "dedicated file tool" in issue


def test_completed_file_action_detects_successful_bash_file_write():
    messages = [
        {"role": "user", "content": "สร้างไฟล์ refresh.txt ในโฟลเดอร์นี้"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps(
                            {"command": "echo refreshed > refresh.txt", "description": "Create refresh file"}
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "(no output)"},
    ]

    assert _has_completed_file_action_after_latest_user(messages) is True


def test_frontend_tool_quality_rejects_tiny_generic_landing_page():
    messages = [{"role": "user", "content": "สร้าง landing page สวยๆ เอาดีๆ ไว้ที่ ~/Desktop/landing.html"}]
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "apply_patch",
                "arguments": json.dumps(
                    {
                        "patchText": (
                            "*** Begin Patch\n"
                            "*** Add File: landing.html\n"
                            "+<!DOCTYPE html>\n"
                            "+<html><body><section><h1>Build Something Amazing</h1></section></body></html>\n"
                            "*** End Patch\n"
                        )
                    }
                ),
            },
        }
    ]

    issue = _frontend_tool_call_quality_issue(messages, tool_calls)

    assert issue is not None
    assert "non-empty lines" in issue
    assert "semantic content sections" in issue


def test_frontend_tool_quality_rejects_short_write_content():
    messages = [{"role": "user", "content": "แทนที่ landing.html ด้วย landing page สวยจริง เอาดีๆ"}]
    sections = "\n".join(f"<section><h2>Section {index}</h2></section>" for index in range(6))
    content = f"<!doctype html>\n<html><head><style>@media(max-width:900px){{body{{padding:0}}}}</style></head><body>{sections}<svg></svg></body></html>"
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": json.dumps({"filePath": "/Users/work/Desktop/landing.html", "content": content}),
            },
        }
    ]

    issue = _frontend_tool_call_quality_issue(messages, tool_calls)

    assert issue is not None
    assert "non-empty lines" in issue


def test_frontend_tool_quality_rejects_generic_orb_filler():
    messages = [{"role": "user", "content": "เขียน landing.html ใหม่เป็น landing page สวยจริงระดับ production"}]
    filler = "\n".join(f"<div>Line {index}</div>" for index in range(270))
    content = (
        "<!doctype html>\n"
        "<html><head><style>@media(max-width:900px){body{padding:0}}</style></head><body>"
        "<section><h1>Build Stunning Digital Products Faster</h1></section>"
        "<section></section><section></section><section></section><section></section>"
        "<div class='orb orb1'></div>"
        "<svg></svg>"
        f"{filler}"
        "</body></html>"
    )
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": json.dumps({"filePath": "/Users/work/Desktop/landing.html", "content": content}),
            },
        }
    ]

    issue = _frontend_tool_call_quality_issue(messages, tool_calls)

    assert issue is not None
    assert "generic template copy" in issue
    assert "decorative orb filler" in issue


def test_frontend_tool_quality_checks_edit_new_string():
    messages = [{"role": "user", "content": "แทนที่ landing.html ด้วย landing page สวยจริง เอาดีๆ"}]
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps(
                    {
                        "filePath": "/Users/work/Desktop/landing.html",
                        "oldString": "<!doctype html>",
                        "newString": "<!doctype html>\n<html><body><section><h1>Short</h1></section></body></html>",
                    }
                ),
            },
        }
    ]

    issue = _frontend_tool_call_quality_issue(messages, tool_calls)

    assert issue is not None
    assert "semantic content sections" in issue


def test_frontend_tool_quality_accepts_substantial_landing_page():
    messages = [{"role": "user", "content": "สร้าง landing page สวยๆ เอาดีๆ ไว้ที่ ~/Desktop/landing.html"}]
    sections = "\n".join(f"<section><h2>Section {index}</h2><p>Specific copy.</p></section>" for index in range(6))
    css_lines = "\n".join(f".u{index} {{ color: #{index:03d}; }}" for index in range(270))
    content = f"<!DOCTYPE html><html><head><style>{css_lines}\n@media (max-width: 720px) {{ body {{ padding: 0; }} }}</style></head><body>{sections}<svg></svg></body></html>"
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "apply_patch",
                "arguments": json.dumps({"patchText": f"*** Begin Patch\n*** Add File: landing.html\n+{content}\n*** End Patch\n"}),
            },
        }
    ]

    assert _frontend_tool_call_quality_issue(messages, tool_calls) is None


def test_filter_repeated_successful_tool_calls_drops_exact_retry():
    previous_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "apply_patch", "arguments": '{"patchText":"*** Begin Patch\\n*** Add File: hello.txt\\n+ok\\n*** End Patch"}'},
    }
    messages = [
        {"role": "assistant", "tool_calls": [previous_call]},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Success. Updated the following files:\nA hello.txt",
        },
    ]
    retry = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "apply_patch", "arguments": {"patchText": "*** Begin Patch\n*** Add File: hello.txt\n+ok\n*** End Patch"}},
    }

    assert _filter_repeated_successful_tool_calls([retry], messages) == []


def test_filter_repeated_successful_tool_calls_allows_retry_after_failure():
    previous_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "apply_patch", "arguments": '{"patchText":"bad"}'},
    }
    messages = [
        {"role": "assistant", "tool_calls": [previous_call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Error: patch failed"},
    ]
    retry = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "apply_patch", "arguments": {"patchText": "bad"}},
    }

    assert _filter_repeated_successful_tool_calls([retry], messages) == [retry]
