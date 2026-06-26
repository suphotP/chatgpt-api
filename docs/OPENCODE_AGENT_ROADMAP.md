# opencode Agent Roadmap

This document keeps the opencode integration honest for open source users.
The goal is an agent that can use opencode tools through an OpenAI-shaped
ChatGPT Web bridge, while making limits and unsupported features visible.

## Current Contract

- The server exposes `/v1/chat/completions` and `/v1/models`.
- opencode executes tools. This server only asks ChatGPT to return tool calls.
- The server can use one ChatGPT account or a comma-separated account pool.
- Multi-account routing supports `auto`, `sticky`, `failover`, `random`,
  `round-robin`, `weighted`, and `quota-aware` strategy names. Image-input,
  image-generation, and Deep Research routes preflight reported feature usage
  before sending the real request. `not_reported` is treated as unknown
  capacity, not as blocked.
- The recommended opencode model is `chatgpt-web/auto@optimized`.
- Explicit model IDs such as `chatgpt-web/gpt-5-5@optimized` should surface
  model limit errors instead of hiding them.

## Setup UX

`bun integrations/opencode/chatgpt-opencode.mjs` opens an interactive setup
wizard. It is only for setup/dev convenience. Daily use should be normal
opencode:

```sh
opencode .
```

The wizard writes normal opencode config plus a local state file:

```text
~/.config/opencode/opencode.json
~/.config/chatgpt-api/opencode-setup.json
```

The state file is intentionally separate from raw ChatGPT captures. It can store
safe preferences such as selected accounts, agent prompt mode, limit strategy,
and image output directory. It must not store raw cookies or copied request
headers outside the existing ignored `secrets/` tree.

## Agent Prompt Modes

`optimized` is the default. It compresses tool schemas and transcript context so
small jobs have a better chance of producing valid tool calls with ChatGPT Web.
Use it for proof-of-concept work, refresh tasks, simple file creation, and
short edits.

`opencode` keeps more of opencode's original long context, tool documentation,
and skill instructions. Use it when you want behavior closer to a full coding
agent and can tolerate heavier prompts, slower turns, and more limit pressure.

## Limit Handling

The server should detect limits before and after a turn:

- Probe `/backend-api/conversation/init` when available.
- Preserve `model_limits`, `blocked_features`, `limits_progress`, and
  `default_model_slug` in normalized error metadata.
- Convert empty assistant responses into readable OpenAI-style errors when they
  likely mean the selected model is limited.
- Include reset times in both UTC ISO format and a local human-readable string.
- Use the machine timezone by default, not a hard-coded country.

Recommended public defaults:

- Free and Go: default to `auto`.
- Plus and Pro: allow explicit advanced models, but still show clear errors when
  a model is limited.
- Thinking modes: opt in only. Do not silently enable them for Free or Go.

## Multi-Account Router

Router strategies:

- `sticky`: keep using the chosen account until it fails.
- `failover`: use the chosen account first, then another eligible account after
  a model limit, auth error, rate limit, unsupported model, or empty response.
- `random`: shuffle account order for each request while keeping failover
  fallbacks if the first random account fails.
- `round-robin`: rotate accounts evenly.
- `weighted`: rotate by configured plan weight.
- `quota-aware`: keep configured account order as the base order, then let
  request-specific usage preflight prefer accounts that still report capacity.

Suggested starting weights:

```text
free = 1
go   = 2
plus = 4
pro  = 10
```

Those weights must be configurable. The default should be `sticky` or
`failover`, not aggressive random rotation, because open source users may not
want every account to burn quota at the same time.

## Model Selection

Model choice should be explicit and explainable:

- `auto` asks ChatGPT Web to choose the browser-style fallback.
- `gpt-5-5` means the user wants that model and should see the limit if it is
  unavailable.
- Thinking and Pro aliases should map to the payload shape captured from the web
  UI, including effort when supported.
- If a requested model is invalid for the account plan, return a clear error and
  suggest compatible models from the account metadata when known.

## Image Generation

ChatGPT image generation is implemented in the provider transport and exposed
through `/v1/images/generations` plus `python3 -m chatgpt_api api image`.
It follows the current
ChatGPT Web flow: create an image turn, wait for the async image asset, fetch the
download URL, then return URL or base64 data.

Target behavior:

- Keep the dedicated image generation request path separate from text chat.
- Save generated images under the configured image output directory.
- If the prompt gives a path, use that path after workspace safety checks.
- If the prompt does not give a path, ask the user where to save or use the
  configured default directory.
- Write metadata next to outputs, including prompt, account, model, created time,
  and source response identifiers when available.

The integration should not pretend image generation is a normal text-only chat
completion. It needs separate output handling because opencode needs local files.

## Attachments

Unsupported by design for now:

- file upload attachments

Use local paths instead. The user should write prompts like:

```text
Read ./src/app.ts and fix the route handling.
Inspect ~/Desktop/example.png and describe what needs changing.
```

For text files, opencode should use read, grep, glob, or bash tools. For images,
the provider can upload an input image when called explicitly through
`python3 -m chatgpt_api api edit --input-image`, but opencode attachment
forwarding is not part of the current public contract.

## Capture UX

Future setup should make account capture harder to misuse:

- Accept pasted browser request summaries.
- Validate required fields before saving.
- Redact secrets in logs.
- Detect plan from the JWT/settings response when possible.
- Show captured account name, plan, available model aliases, and known reset
  times.
- Keep all raw capture material under ignored `secrets/accounts/<name>/`.

Bad capture format should produce a fixable error, not a broken config file.
