# Project Analysis And Open Source Maintainer Notes

This document is the maintainer-level map of the project. It explains what the
repo is, what each surface owns, what changed during the open-source cleanup,
how account captures work, how artifacts are served, and what should be split
next.

## Product Intent

`chatgpt-api` is a local bridge framework. It lets a user run a local API that
looks close enough to common OpenAI client conventions for development, while
using ChatGPT Web browser sessions as the first provider target.

The project is not the official OpenAI API and should not be described as a
drop-in clone. The accurate public wording is:

- OpenAI-shaped
- Chat Completions-style
- `/v1` local bridge
- ChatGPT Web backed provider

The bridge exists so people can build real local apps, test client flows, and
show practical use cases such as:

- local chat and streaming
- image generation
- image edit/composite from uploaded images
- OCR or image description prompts
- Deep Research report downloads
- opencode-style tool-agent integration
- a full-stack character roleplay game consuming the bridge as a backend

The opencode integration is a showcase and power-user adapter. The character
game is the main product-style use case because it proves a normal app server
can call the bridge and own its own game state, UI, cancellation, image queues,
and artifact presentation.

## Repository Shape

Top-level source ownership:

```text
chatgpt_api/
  core/                 Provider-neutral request and response contracts.
  providers/chatgpt/    ChatGPT Web capture, auth, proof, transport, models.
  api/                  Local HTTP API facade and admin/runtime surface.

apps/
  bridge-console/       Operator console UI for accounts, docs, tests, storage.
  character-game/       SvelteKit roleplay game use case.

integrations/
  opencode/             opencode consumer config and launcher scripts.

docs/                   Public documentation, operator guide, architecture.
references/legacy/      Legacy provider experiment, not part of runtime.
tests/                  Python unit tests for capture, provider, CLI, API.
```

Generated runtime data is intentionally outside the source tree:

```text
secrets/                Local account captures. Never commit.
outputs/                Generated images, research reports, sqlite metadata.
apps/*/build            Generated frontend/server build output. Never commit.
apps/*/dist             Generated static build output. Never commit.
```

## Runtime Surfaces

### Python CLI

The `chatgpt-api` command is the operator entrypoint. It supports direct
provider actions and remote admin calls against a running API.

Primary commands:

```sh
python3 -m chatgpt_api doctor
python3 -m chatgpt_api menu
python3 -m chatgpt_api server command --preset local
python3 -m chatgpt_api server command --preset local --port 8010 --account-strategy random
python3 -m chatgpt_api server start --api-key local-dev-key
python3 -m chatgpt_api admin status --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin account add --account pro-main --capture-file ./chatgpt-request.txt
chatgpt-api admin account update --account pro-main --capture-file ./chatgpt-request.txt
chatgpt-api admin account verify --account all
chatgpt-api admin account delete --account old-free-main
python3 -m chatgpt_api api chat --message "hello" --accounts free,go,plus-main,pro-main --account-strategy random --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api image --prompt "small icon" --output-dir ./outputs/manual-images --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api vision --mode ocr --input-image ./panel.png --prompt "Extract text" --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

`--account` values are local account names chosen by the operator when saving a
capture. Use names like `free`, `go`, `plus-main`, `pro-main`, `work-pro`, or
`free-2`. If `--accounts`/`CHATGPT_ACCOUNTS` is omitted, saved captures under
`secrets/accounts/*` are auto-discovered.
Use `api ...` commands to exercise the real server router, strategy,
cancellation, and artifact store. Use direct `chat`, `image`, and `vision`
commands only as local account-capture diagnostics.
They are not automatic plan selectors.

`chatgpt_api/cli.py` is still large. It should be split later into:

- `chatgpt_api/cli/main.py`
- `chatgpt_api/cli/server.py`
- `chatgpt_api/cli/admin.py`
- `chatgpt_api/cli/account.py`
- `chatgpt_api/cli/media.py`

That split is mechanical but should be done after the public command contract is
stable, because import movement can easily break console and Docker docs.

### Local Bridge API

The local API listens on `http://127.0.0.1:8000` by default and exposes a `/v1`
base path for client compatibility.

Core paths:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
POST /v1/images/generations
POST /v1/images/edits
POST /v1/chatgpt/vision
GET  /v1/chatgpt/usage
GET/HEAD /v1/chatgpt/files/{file_id}/{filename}
GET  /v1/chatgpt/operations/{operation_id}
POST /v1/chatgpt/operations/{operation_id}/cancel
```

Admin paths live under `/v1/chatgpt/admin/*`. They are intentionally local
operator endpoints, not stable public client endpoints.

### Bridge Console

`apps/bridge-console` is the operator control plane. It should answer these
questions quickly:

- Is the bridge online?
- Which accounts are configured?
- Which accounts are healthy?
- What model/image/research capacity is available?
- How do I add, update, verify, or delete account captures?
- How do I test chat, image, vision, research, and opencode routes?
- Where are generated artifacts stored?
- What exact curl or SDK-shaped request should an app send?

The console should not become an app backend. It should call the Bridge API and
render state. Account capture parsing and persistence belong in the Bridge API.

### Character Game

`apps/character-game` is a real full-stack use case. It is intentionally a
separate SvelteKit app that calls the local bridge from its server routes.

The game owns:

- session creation
- story state
- route themes and starting scenarios
- user-visible chat state
- streaming turn UX
- scene-art jobs and cancellation
- local SQLite state
- image cache/download handling

The bridge owns:

- ChatGPT Web routing
- account credentials
- chat/image/research requests
- artifact storage and download URLs

This separation matters. A future app should be able to replace the game with a
different service while keeping the bridge API unchanged.

### opencode Integration

`integrations/opencode` is a consumer adapter. It injects or ejects an opencode
provider config pointing at the local bridge. It should not configure ChatGPT
accounts, server ports, routing, or quotas. Those belong to the Bridge API and
console.

opencode use is mostly proof that:

- the bridge can handle tool-call style clients
- model suffixes such as `@optimized` and `@opencode` can select prompt modes
- ChatGPT Web can be used for coding-agent experiments

It is not the architectural center of the repo.

## API Module Split

The API layer now has smaller modules:

```text
chatgpt_api/api/config.py
chatgpt_api/api/http_utils.py
chatgpt_api/api/prompts.py
chatgpt_api/api/image_inputs.py
chatgpt_api/api/admin_store.py
chatgpt_api/api/openai_compat.py
```

### `config.py`

Defines `OpenAICompatConfig`, the server configuration object. Keeping it in its
own module prevents route code, CLI code, and tests from depending on a giant
HTTP facade file just to construct config.

### `http_utils.py`

Owns small HTTP helper functions:

- bearer auth
- JSON request body parsing
- CORS headers
- JSON/text responses
- query parsing helpers
- operation cancel path extraction

These helpers are deliberately boring. They should stay generic and free of
ChatGPT provider logic.

### `prompts.py`

Owns prompt policy constants:

- strict tool bridge prompt
- optimized tool bridge prompt
- prompt mode aliases
- Deep Research system hint
- Deep Research model aliases

This makes prompt audits easier. Prompt changes can be reviewed without
scrolling through HTTP route logic.

### `image_inputs.py`

Owns image input parsing and image edit prompt helpers.

Supported input forms:

- local paths
- HTTP/HTTPS URLs
- `data:image/...;base64,...`
- raw base64 image strings
- JSON dicts with `url`, `path`, `image`, `input_image`, `base64`, `b64_json`,
  `data`, `data_url`, or `dataUrl`
- OpenAI-style multimodal content parts with `image_url`

Limits:

- up to 10 input images per request
- image edit returns one completed output image
- OCR/describe returns text, not a stored artifact

Aspect ratios currently documented for edit/composite:

```text
auto
1:1
3:4
9:16
4:3
16:9
```

Important product warning: source images should already match one of the
supported ratios when image positioning matters. If `auto` is used with an odd
ratio, ChatGPT can change canvas dimensions or move objects.

### `admin_store.py`

Owns SQLite metadata for generated files and persisted operator settings. This
is intentionally lightweight so the bridge can be shipped without a full
database dependency.

### `openai_compat.py`

Still owns route orchestration. It remains large because it combines:

- account routing
- concurrency limits
- operation cancellation
- chat completion response shaping
- streaming SSE
- image and research artifact registration
- admin route payloads
- model alias resolution
- tool-call parsing and retry policy

Future extraction should be incremental and test-gated. The best next splits:

```text
chatgpt_api/api/routing.py        AccountRouter, leases, plan limits.
chatgpt_api/api/operations.py     operation ids, cancellation, stop calls.
chatgpt_api/api/artifacts.py      file id registration, download URLs.
chatgpt_api/api/admin_routes.py   admin payload builders.
chatgpt_api/api/tool_bridge.py    tool prompt, parsing, retry quality checks.
chatgpt_api/api/model_catalog.py  model aliases and model list construction.
```

Do not split just to reduce line count. Split only when the new file has a real
owner and tests prove the public behavior did not move.

## Account Capture Model

Every ChatGPT Web account needs a copied browser request. The bridge replays
that browser-shaped request locally. The capture contains cookies, bearer
tokens, sentinel proof tokens, and ChatGPT Web headers. Treat it like a password.

Supported browsers today:

- Safari
- Chrome

Recommended flow:

1. Open `https://chatgpt.com`.
2. Use private/incognito mode or a dedicated browser profile.
3. Sign in to the target account.
4. Do not sign out after collecting the capture.
5. Open a new ChatGPT conversation.
6. Open DevTools or Web Inspector.
7. Go to Network.
8. Send any small message.
9. Search for `conversation`.
10. Select `https://chatgpt.com/backend-api/f/conversation`.
11. Copy headers and payload/request data, or use `Copy as cURL`.
12. Save through the console or CLI.

Safari-specific:

- copy all Headers from the request details
- expand `Request Data:`
- copy the full JSON body

Chrome-specific:

- preferred: right-click the request and use `Copy as cURL`
- the pasted cURL must include URL, Authorization, cookies through `Cookie` or
  `-b`, and `--data-raw`
- alternative: copy the Headers tab and Payload tab together

Captures often last around 10 days, but this is not guaranteed. Refresh
important accounts once per week so demos and long background jobs do not stop
because a browser session expired.

If the user logs out of ChatGPT, the capture can stop working. The bridge cannot
repair that locally; the fix is to collect a fresh capture.

Account names are constrained to:

```text
[A-Za-z0-9][A-Za-z0-9_-]{0,63}
```

This avoids path bugs, Unicode normalization issues, shell escaping surprises,
and UI sorting problems.

## Account Routing And Capacity

The bridge can route across multiple accounts. Local concurrency limits are
separate from ChatGPT Web quota. Both matter.

Recommended local plan defaults:

```text
chat:     free=1, go=2, plus=3, pro=4
upload:   free=1, go=1, plus=1, pro=1
image:    free=1, go=1, plus=2, pro=3
research: free=1, go=1, plus=2, pro=2
```

If there are two Pro accounts and both are routed, the recommended image
parallelism is 6 total because each Pro account contributes 3 image slots.

This is only the local limiter. ChatGPT Web may still impose hidden short-term
rate limits. For example, a Pro account can show a high daily image quota but
still hit a temporary 5-10 minute cooldown if too many generations are fired too
quickly.

Before sending real provider work, the API routes now preflight reported usage
for request-specific features:

- OCR, describe, and chat-with-image: `file_upload`
- image edit/composite: `file_upload` plus `image_gen`
- image generation: `image_gen`
- Deep Research: `deep_research`

The preflight changes account order; it does not reject accounts just because a
counter is absent. A `not_reported` feature is treated as unknown capacity, not
as exhausted, because some account captures do not expose every limit counter.

Free and Go accounts should default to `auto` model selection. They should not
be forced into high-effort or Pro-only model lanes.

## Models

The bridge exposes model aliases through `GET /v1/models`. The live list depends
on routed account captures and settings, but the public model surface can
include:

```text
auto
gpt-5-5
gpt-5-5-thinking-standard
gpt-5-5-thinking-extended
gpt-5-5-thinking-max
gpt-5-5-pro-standard
gpt-5-5-pro-extended
auto@optimized
auto@opencode
gpt-5-5@optimized
gpt-5-5@opencode
gpt-image-1
chatgpt-deep-research
```

Notes:

- `auto` maps to ChatGPT Web auto routing.
- `@optimized` and `@opencode` are local prompt bridge modes for tool-agent
  clients.
- `gpt-image-1` is the image-generation route alias.
- `chatgpt-deep-research` is a chat route alias that enables the Deep Research
  connector and saves a markdown report.
- Free/Go should be locked to `auto` in product UX unless a user deliberately
  enables advanced behavior.

## Image, Vision, And Edit Flows

The bridge supports three related image use cases:

### Generate

`POST /v1/images/generations`

Input:

- prompt
- optional model alias
- optional operation id
- optional output path metadata

Output:

- OpenAI-style image response
- completed local file path for same-machine scripts
- `download_url` for browser/LAN clients

### Edit Or Composite

`POST /v1/images/edits`

Input:

- prompt
- one or more input images, up to 10
- optional `aspect_ratio`

Output:

- one completed image artifact
- local `path`
- public `download_url`

Single-image edit is the normal workflow. Multi-image edit is treated as a
composite instruction, for example "combine these references into one new
scene." The bridge should upload the input images and ask ChatGPT to produce one
new output.

### Vision, OCR, Describe

`POST /v1/chatgpt/vision`

Input:

- `mode`: `ocr`, `describe`, or similar local mode
- one or more input images, up to 10
- prompt

Output:

- text response by default
- prompt-shaped structured output, such as strict JSON or estimated bbox items,
  when the caller provides a schema in the prompt
- no file artifact unless the caller asks a separate image route to generate
  one

Bounding boxes are model-estimated rather than native OCR engine coordinates.
Use them for prototypes and overlays, or pair the route with a dedicated OCR
engine when exact layout precision matters.

Vision and image edit share the same upload concurrency limiter and use the
ChatGPT `file_upload` counter when that usage is reported.

## Deep Research

Deep Research is triggered through chat completions:

```json
{
  "model": "chatgpt-deep-research",
  "messages": [
    {
      "role": "user",
      "content": "Research whether LLMs can reach AGI. Keep it concise."
    }
  ]
}
```

The ChatGPT Web connector uses a normal, non-temporary chat mode. Temporary chat
does not support all Deep Research behavior. The bridge handles the known
`skip_sleep` path to avoid waiting for the full web UI confirmation delay when
that state appears.

Deep Research output is saved as markdown under the configured research output
directory. The API response should tell the caller only that the report is done
and where it can be downloaded:

```text
Done. Deep Research report saved.
path=/local/path/report.md
download_url=http://127.0.0.1:8000/v1/chatgpt/files/<file_id>/report.md
```

The bridge should not summarize the report again in the client response. The
report file is the artifact.

## Artifacts And Downloads

Images and research reports are registered in the admin store and served by the
Bridge API:

```text
GET/HEAD /v1/chatgpt/files/{file_id}/{filename}
```

The download handler first checks the live in-memory registry, then falls back
to the admin SQLite artifact record. This keeps download URLs valid after an
API restart when the output file and admin DB are on persistent storage.

Responses usually include:

```json
{
  "path": "/Users/work/Desktop/chatgpt-api/outputs/chatgpt-images/example.png",
  "download_url": "http://127.0.0.1:8000/v1/chatgpt/files/chgptfile_x/example.png"
}
```

Use `path` only when the caller runs on the same machine or inside the same
mounted container volume. Use `download_url` for browsers, LAN devices, and
frontend applications.

For LAN use, set:

```sh
CHATGPT_PUBLIC_BASE_URL=http://<machine-lan-ip>:8000/v1
```

If this is left as `127.0.0.1`, another device on the LAN will try to download
from itself instead of the bridge host.

Artifacts should only be listed after a real completed file exists. Queued or
failed image jobs should not appear as successful library entries.

## Cancellation

The API supports operation cancellation:

```text
GET  /v1/chatgpt/operations/{operation_id}
POST /v1/chatgpt/operations/{operation_id}/cancel
```

Chat/image/research routes should attach operation ids to long-running provider
requests. If a browser client refreshes the page, navigates away, or cancels a
request, app servers should call the cancel endpoint so ChatGPT Web does not
keep spending account quota in the background.

Operation ids are not durable records. They exist for the live API process so a
currently running request can be inspected or cancelled. After restart, old
operation ids may return 404 even though completed artifact downloads still work
through the persisted admin DB and mounted output files.

Deep Research reads the widget session through WSS, then uses the connector MCP
`stop` call once `conversation_id`, assistant `message_id`, and widget
`session_id` are known. Normal chat and image routes use the ChatGPT Web stop
conversation path where available. Clients should poll the operation status
route and treat `deep_research_ready=true` as the point where a manual Deep
Research cancel can send the MCP stop call immediately.

## Docker

Compose runs three production-style services:

```text
chatgpt-api      http://127.0.0.1:8000
bridge-console   http://127.0.0.1:8080
character-game   http://127.0.0.1:3000
```

The API Dockerfile builds a Python wheel in a build stage and installs the wheel
in the runtime image. The console Dockerfile builds static Vite assets and
serves them through nginx. The game Dockerfile builds the SvelteKit node output
and runs the production server.

Host volumes:

```text
./secrets/accounts  -> /data/secrets/accounts
./outputs           -> /data/outputs
./outputs/character-game -> /data
```

This keeps credentials and generated artifacts outside image layers.

## Security Model

Never commit or print raw:

- request captures
- `Authorization` headers
- cookies
- sentinel tokens
- conduit tokens
- `.env`
- `secrets/`

The console and CLI should show redacted summaries. If a command needs to show
whether a capture is valid, it should display detected plan/account metadata and
missing-field diagnostics without exposing the secret values.

## Current Refactor Status

Completed in this cleanup pass:

- moved legacy `OpenaiChat.py` into `references/legacy/`
- moved root screenshots/log evidence into `docs/assets/screenshots/`
- removed generated Python caches and frontend build output from the source tree
- extracted API config into `chatgpt_api/api/config.py`
- extracted HTTP helpers into `chatgpt_api/api/http_utils.py`
- extracted prompt policy into `chatgpt_api/api/prompts.py`
- extracted image input parsing into `chatgpt_api/api/image_inputs.py`
- updated README and architecture language toward "OpenAI-shaped"
- added a dedicated account capture guide for Chrome and Safari

Python behavior was verified after extraction with:

```sh
python3 -m compileall chatgpt_api
python3 -m pytest
```

At the time of this document, the full Python suite passes.

The frontend and Docker surfaces were also verified with:

```sh
bun run --cwd apps/bridge-console check
bun run --cwd apps/bridge-console build
bun run --cwd apps/character-game check
bun run --cwd apps/character-game test
bun run --cwd apps/character-game build
docker compose up -d --build
curl -H 'Authorization: Bearer local-dev-key' http://127.0.0.1:8000/health
curl http://127.0.0.1:3000/api/status
```

Screenshot evidence from the Docker stack lives in
`docs/assets/screenshots/README.md` and covers:

- bridge console overview
- account capture management
- embedded API docs
- artifact/library surface
- opencode integration surface
- character-game use case with API online

## Remaining Technical Debt

The project is now better structured, but several areas should still be split
before a polished public release:

1. `chatgpt_api/api/openai_compat.py` is still too large.

   It should be split by domain after route behavior stabilizes. Best order:
   routing, operations, artifacts, admin payloads, tool bridge, model catalog.

2. `chatgpt_api/cli.py` is still too large.

   It should become a CLI package with subcommand modules. Keep public command
   names stable while moving implementation.

3. `integrations/opencode/chatgpt-opencode.mjs` is still large.

   The current script is acceptable as a showcase, but a real public package
   should split prompt presets, config writing, API probes, and terminal UI.

4. More tests should cover image edit and Deep Research behavior.

   Existing tests cover route shape and transport well, but long-running
   provider flows should have more mocked-state tests.

5. Console E2E screenshots should become repeatable.

   The repo should eventually have a Playwright script that starts Docker or
   local dev servers, captures console/game screenshots, and stores redacted
   artifacts under `docs/assets/screenshots/`.

6. Public compatibility language must stay careful.

   The repo should avoid promising plug-and-play official OpenAI parity. It is
   intentionally OpenAI-shaped plus ChatGPT-Web-specific extensions.

## Open Source Release Checklist

Before publishing:

- `secrets/` ignored and absent from commits
- `outputs/` ignored and absent from commits except intentional sample docs
- no copied browser headers in docs, tests, screenshots, or logs
- no bearer tokens in screenshots
- `python3 -m pytest` passes
- Docker Compose builds cleanly
- console opens on `:8080`
- game opens on `:3000`
- `/health`, `/v1/models`, and `/v1/chatgpt/admin/status` respond
- README includes Chrome/Safari capture instructions
- docs explain LAN artifact download URLs
- opencode integration is clearly marked as an optional showcase
- character game is documented as the primary app use case

## Maintainer Principle

Keep the project boring at the boundaries:

- provider quirks stay in providers
- API response shapes stay predictable
- account secrets stay out of logs
- generated files stay out of source
- integrations stay optional
- app use cases own their own app state

That is what makes the project reusable instead of only working on one machine.
