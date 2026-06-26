# Architecture

This project should stay provider-first, not opencode-first.

The core product is a local provider framework with an OpenAI-shaped API facade.
Integrations such as opencode should live at the edge so the same server can
also be used by OpenAI SDKs, curl, editor plugins, tests, or future clients.

## Repository Shape

Keep these pieces in this repo:

- `chatgpt_api/core`: provider-neutral types and registry.
- `chatgpt_api/providers/*`: provider implementations.
- `chatgpt_api/api/*`: HTTP API facades, including OpenAI-shaped routes.
- `integrations/*`: examples and thin config templates for external tools.
- `docs/*`: public behavior, security model, and compatibility notes.

Do not make opencode a hard dependency of the core package. Keep the opencode
configuration in `integrations/opencode` while it is only a config/template
layer. Split it into a separate repo only if it becomes a real npm package,
opencode plugin, release artifact, or has its own test/build pipeline.

## Public Use Modes

There are three useful ways to use the project:

1. Routed local API:

   ```sh
   python3 -m chatgpt_api api chat \
     --message "hello" \
     --accounts free,go,plus-main,pro-main \
     --account-strategy random \
     --base-url http://127.0.0.1:8000/v1 \
     --api-key local-dev-key
   ```

2. OpenAI-shaped local API for any client:

   ```sh
   chatgpt-api serve --host 127.0.0.1 --port 8000
   ```

   This exposes `/v1/models` and `/v1/chat/completions` so OpenAI-shaped
   clients can switch their `baseURL` to this server. This is the main public
   API mode: people can test OpenAI API-shaped requests against a ChatGPT-Web
   backed local server before deciding whether to use the paid OpenAI API.

3. Tool-agent integrations:

   Tools such as opencode can use the same OpenAI-shaped API. They are
   integrations of the public API, not the reason the API exists.

## Provider Boundaries

The framework should not pretend every provider is the same internally. Each
provider owns its own auth, token refresh, payload shape, and quirks. The core
only sees normalized request/response types.

ChatGPT Web is one provider:

- account capture lives under `secrets/accounts/<account>/chatgpt-request.txt`
- account metadata is detected from capture/settings
- model choices are conservative and account-specific
- Free/Go should default to the core model only
- Pro can expose Pro models only when the account supports them

## OpenAI-Shaped Facade

The local API should prioritize compatibility with common clients:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- non-streaming response shape compatible with Chat Completions
- streaming SSE response shape compatible with Chat Completions chunks
- OpenAI-style `tools` and `tool_calls`

The facade is an adapter. It should not leak ChatGPT Web tokens, cookies,
sentinel tokens, or raw copied request data.

The goal is API-shape compatibility, not pretending to be the official OpenAI
service. A user should be able to point an OpenAI SDK-style client at:

```text
http://127.0.0.1:8000/v1
```

for local testing, then later point the same client at:

```text
https://api.openai.com/v1
```

with a real OpenAI API key. The application code should need minimal changes:
typically `baseURL`, API key, and model name.

## Tool Calling

The server should not edit files itself.

Tool execution belongs to the client or agent runtime, for example opencode.
The server only translates between:

- OpenAI-shaped client request with `tools`
- ChatGPT Web text interaction
- OpenAI-shaped assistant response with `tool_calls`

Current bridge contract:

1. Client sends `tools` to `/v1/chat/completions`.
2. Server injects a strict tool-planning prompt.
3. ChatGPT returns JSON:

   ```json
   {"tool_calls":[{"name":"write_file","arguments":{"path":"a.txt","content":"ok"}}]}
   ```

4. Server validates the tool name against the supplied tool list.
5. Server returns OpenAI-style `tool_calls`.
6. Client executes the tool and sends a later `role: "tool"` message.

This keeps file writes, shell commands, approvals, workspace roots, and sandbox
rules outside this server. It is safer for open source users and easier to
debug.

## Recommended Defaults

For public releases:

- default account: `free`
- default server bind: `127.0.0.1`
- default local API auth: off for local-only experiments, documented opt-in via
  `CHATGPT_API_KEY`
- default model for opencode Free/Go: `auto`, so ChatGPT Web can choose a
  fallback model like the web UI when GPT-5.5 is temporarily limited
- explicit model for Free/Go tests: `gpt-5-5`
- default model for Pro API tests: `gpt-5-5`
- optional Pro/Thinking aliases exposed through `/v1/models`

Do not enable experimental Thinking for Free/Go by default. If we later support
it, make it an explicit opt-in flag and document that it can fail or hit limits.

## Open Source Rules

- Never commit files under `secrets/`.
- Never log raw headers or cookies.
- Never claim this is the official OpenAI API.
- Keep ChatGPT-Web-specific behavior in provider code, not API/client code.
- Keep opencode-specific behavior in `integrations/opencode`, not core.
- Keep the OpenAI-shaped API small and predictable so users can test with
  this local ChatGPT-Web-backed server before switching their client to the
  official paid OpenAI API.

## API Module Map

The API layer used to live mostly in one large module. It is now split around
ownership boundaries that matter for open-source maintenance:

- `chatgpt_api/api/config.py`: immutable server configuration object.
- `chatgpt_api/api/http_utils.py`: small HTTP response, CORS, auth, and body
  parsing helpers.
- `chatgpt_api/api/prompts.py`: agent/tool bridge and Deep Research prompt
  policy strings.
- `chatgpt_api/api/image_inputs.py`: local path, URL, data URL, base64, and
  multimodal content-part parsing for OCR, image edit, and composite requests.
- `chatgpt_api/api/admin_store.py`: SQLite metadata for artifacts and persisted
  operator settings.
- `chatgpt_api/api/openai_compat.py`: route orchestrator, account routing,
  operation cancellation, streaming, admin endpoints, and response shaping.

`openai_compat.py` is still intentionally the main facade while routes are
stabilizing. Future splits should move one domain at a time:

- account routing and concurrency into `chatgpt_api/api/routing.py`
- artifact/download registration into `chatgpt_api/api/artifacts.py`
- admin route payloads into `chatgpt_api/api/admin_routes.py`
- tool-call parsing and retry policy into `chatgpt_api/api/tool_bridge.py`

The rule for future extraction is simple: move code only when it creates a
clear ownership boundary and keep tests passing after each step.
