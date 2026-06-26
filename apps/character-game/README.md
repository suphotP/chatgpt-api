# Arcadia Sessions

Production-style character game reference app for `chatgpt-api`.

This app is intentionally a separate fullstack product layer. It calls the local
OpenAI-shaped `/v1` server instead of adding game routes to the core
provider API.

## What It Proves

- OpenAI-shaped chat can drive a stateful character game.
- Image generation can be requested from game turns and stored as scene assets.
- Browser clients never receive ChatGPT captures, API keys, or account data.
- Sessions, messages, relationship state, choices, hints, and image jobs are
  persisted in SQLite.

## Run

Start the API server from the repository root:

```sh
chatgpt-api serve --host 127.0.0.1 --port 8000
```

Then start the app:

```sh
cd apps/character-game
cp .env.example .env
bun install
bun run dev
```

Open the SvelteKit URL, usually `http://localhost:5173`.

## Local-Only UI Mode

When you want to work on UI without spending provider quota:

```sh
CHATGAME_AI_MODE=mock bun run dev
```

Mock mode is not the product path. The product path is the OpenAI-shaped
`CHATGAME_OPENAI_BASE_URL`.

In Docker, keep `CHATGAME_OPENAI_BASE_URL` as the internal container URL, such
as `http://chatgpt-api:8000/v1`, and set
`CHATGAME_PUBLIC_OPENAI_BASE_URL` to the browser/LAN URL, such as
`http://127.0.0.1:8000/v1` or `http://192.168.1.203:8000/v1`. The server uses
the internal URL for fetches; the UI and saved route settings show the public
URL.

## Environment

| Variable                           | Default                    | Purpose                                          |
| ---------------------------------- | -------------------------- | ------------------------------------------------ |
| `CHATGAME_OPENAI_BASE_URL`         | `http://127.0.0.1:8000/v1` | Server-side API URL. In Docker this can be the internal service URL. |
| `CHATGAME_PUBLIC_OPENAI_BASE_URL`  | same as internal URL       | Browser-facing API URL shown in the UI and used for route settings/download links. |
| `CHATGAME_OPENAI_API_KEY`          | `local-dev-key`            | Optional local API key if the server requires it |
| `CHATGAME_CHAT_MODEL`              | `chatgpt-web/auto`         | Chat model passed to `/v1/chat/completions`      |
| `CHATGAME_IMAGE_MODEL`             | `chatgpt-web/auto`         | Image model passed to `/v1/images/generations`   |
| `CHATGAME_DB_PATH`                 | `.data/arcadia.sqlite`     | SQLite database path                             |
| `CHATGAME_IMAGE_DIR`               | `.data/images`             | Generated image storage directory                |
| `CHATGAME_AI_MODE`                 | `live`                     | Set `mock` for no-provider UI development        |

## API Surface

Browser UI calls this app only:

```text
GET  /api/status
GET  /api/catalog
POST /api/sessions
GET  /api/sessions/:session_id
POST /api/sessions/:session_id/turn
POST /api/sessions/:session_id/images
GET  /api/assets/images/:job_id
```

The app server then calls:

```text
POST /v1/chat/completions
POST /v1/images/generations
```

## Checks

```sh
bun run check
bun test
bun run build
```
