# CLI

`chatgpt-api` is the operator CLI for the local ChatGPT Web Bridge.

Use the module command in docs and automation:

```sh
python3 -m chatgpt_api <command>
```

If your shell can find the installed console script, this shorter form is also
valid:

```sh
chatgpt-api <command>
```

It has four layers:

- local capture tools that inspect account files without starting the API
- `server` commands that launch the main API
- `admin` commands that manage a running API server
- `api` commands that call the running `/v1` API the same way an app would

## First Checks

```sh
python3 -m chatgpt_api doctor
python3 -m chatgpt_api doctor --json
python3 -m chatgpt_api menu
python3 -m chatgpt_api
```

`doctor` checks Python version, local account capture profiles, Docker files,
`/health`, and `/v1/models`.

Running the CLI with no arguments in an interactive terminal opens the
same control menu. Non-interactive shells and Docker/CI should use explicit
subcommands.

Example JSON shape:

```json
{
  "object": "chatgpt.doctor",
  "ok": true,
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "<set>",
  "checks": [
    {"name": "python", "ok": true, "detail": "3.12.0"},
    {"name": "account_profiles", "ok": true, "detail": "2 profile(s) in secrets/accounts"}
  ]
}
```

## Start The API

`--account` and `--accounts` take local account names. Those names are aliases
you choose when saving captures, for example `free`, `go`, `plus-main`,
`pro-main`, or `work-pro`. They are not automatic plan selectors.

If you omit `--accounts`, the server auto-discovers every saved capture under
`secrets/accounts/*`. Add `--accounts` only when you want to pin a clean pool,
control order, or skip an old/broken capture.

Recommended local command:

```sh
python3 -m chatgpt_api server start \
  --account-strategy failover \
  --api-key local-dev-key \
  --host 127.0.0.1 \
  --port 8000 \
  --public-base-url http://127.0.0.1:8000/v1
```

LAN command:

```sh
python3 -m chatgpt_api server start \
  --account-strategy failover \
  --api-key local-dev-key \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base-url http://192.168.1.203:8000/v1
```

Print a preset without starting:

```sh
python3 -m chatgpt_api server command --preset local
python3 -m chatgpt_api server command --preset lan
python3 -m chatgpt_api server command --preset docker
python3 -m chatgpt_api server command \
  --preset local \
  --host 127.0.0.1 \
  --port 8010 \
  --public-base-url http://127.0.0.1:8010/v1 \
  --account-strategy random
```

`chatgpt-api serve` is still supported and accepts the same server flags.

Fully interactive launch:

```sh
python3 -m chatgpt_api server start --interactive
```

The interactive launch asks for account aliases, routing strategy, host, port,
Bearer key, public download URL, prompt mode, fallback policy, privacy mode,
concurrency limits, output directories, and admin DB path before starting the
API.

## Runtime Admin

All admin commands talk to a running API server.

```sh
python3 -m chatgpt_api admin status --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin capacity --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin models --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin usage --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Use `--json` for stable machine output:

```sh
python3 -m chatgpt_api admin capacity --json --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

## API Consumer Commands

Use `api ...` when you want to test the real bridge route that an app, LAN
client, Docker container, opencode adapter, or game frontend will call.

These commands go through the running API server, so they use the server's
account router, model aliases, concurrency limits, artifact store, download
URLs, and operation cancellation. This is different from the older direct
provider commands such as `chatgpt-api chat` and `chatgpt-api image`, which
read a local account capture directly and are best treated as low-level probes.

Every `api` command accepts:

```text
--base-url http://127.0.0.1:8000/v1
--api-key local-dev-key
--timeout 180
```

Commands that send provider work also accept route overrides:

```text
--account plus-main
--accounts free,go,plus-main,pro-main
--account-strategy auto|sticky|failover|random|round-robin|weighted|quota-aware
```

Use `--account` for one specific local alias. Use `--accounts` plus
`--account-strategy` when you want the server to choose among several accounts
for the request. These are local aliases, not plan names.

Read-only checks:

```sh
python3 -m chatgpt_api api health --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api status --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api capacity --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api usage --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api models --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api artifacts --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Chat through the router:

```sh
python3 -m chatgpt_api api chat \
  --message "Reply with exactly: bridge ok" \
  --model auto \
  --accounts free,go,plus-main,pro-main \
  --account-strategy random \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Streaming chat:

```sh
python3 -m chatgpt_api api chat \
  --message "Stream one short sentence." \
  --stream \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Image generation:

```sh
python3 -m chatgpt_api api image \
  --prompt "small blue app icon, no text" \
  --size 1024x1024 \
  --output-dir ./outputs/manual-images \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Image edit or multi-image composite:

```sh
python3 -m chatgpt_api api edit \
  --prompt "Change the icon letters to FW while preserving the style" \
  --input-image ./favicon.png \
  --aspect-ratio 1:1 \
  --output-dir ./outputs/manual-edits \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

OCR or vision:

```sh
python3 -m chatgpt_api api vision \
  --mode ocr \
  --input-image ./panel.png \
  --prompt 'Return strict JSON only. Schema: {"items":[{"text":"string","bbox":{"x":0,"y":0,"w":0,"h":0},"confidence":"low|medium|high"}]}. Use pixel coordinates relative to the input image. Estimate boxes when exact layout is uncertain.' \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

BBox output is prompted model output. It is useful for prototypes and overlay
work, but it is not a native OCR layout engine.

Deep Research with a client-chosen operation id:

```sh
python3 -m chatgpt_api api research \
  --prompt "Research whether LLMs can reach AGI. Save a concise markdown report." \
  --operation-id chatgptop_research_demo \
  --output-dir ./outputs/manual-research \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Cancel from another terminal:

```sh
python3 -m chatgpt_api api operation \
  --operation-id chatgptop_research_demo \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key

python3 -m chatgpt_api api cancel \
  --operation-id chatgptop_research_demo \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

For Deep Research, cancellation is a two-stage flow. The bridge first needs the
ChatGPT connector identifiers from the Deep Research widget stream
(`conversation_id`, assistant `message_id`, and widget `session_id`). The
widget stream itself is read through WSS. Once those identifiers are known, the
bridge sends ChatGPT's Deep Research MCP `stop` call. If cancel is requested
before the widget session is discovered, the operation is marked
`cancel_requested` and the stop call is attempted as soon as the session is
available. For the cleanest manual flow, poll `api operation` until it shows
`deep_research_ready=yes`, or wait until the ChatGPT Deep Research bypass /
confirmation period has passed and the research has actually started, then run
`api cancel`. This is best-effort and may not be instant.

Operation ids are live runtime records. They are useful while the request is
running, but they are not restored after an API process/container restart. Saved
image and research artifact URLs are separate and can be restored from the admin
DB when the output files are still mounted on disk.

## Account Management

List accounts known by the running API:

```sh
python3 -m chatgpt_api admin accounts --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin account list --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Live-check all accounts:

```sh
python3 -m chatgpt_api admin check-accounts --account all --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin account verify --account all --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Add or refresh an account from copied browser request details. The CLI first
inspects the capture, refuses to save if required or recommended checks fail,
then live-checks the account after saving.

```sh
python3 -m chatgpt_api admin account add \
  --account pro-main \
  --capture-file ./chatgpt-request.txt \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

The same flow can be fully interactive:

```sh
python3 -m chatgpt_api menu
```

Or paste a capture without preparing a file:

```sh
python3 -m chatgpt_api admin account add --paste \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

The CLI will ask for the account name and then accept pasted headers plus
payload, or a full `Copy as cURL` command, until a line containing only
`END_CAPTURE`.

For `Copy as cURL`, paste the whole command. It must include the ChatGPT URL,
Authorization, cookies through either `Cookie:` or `-b`, and the JSON body in
`--data-raw` or another curl data flag.

Update an existing account after tokens/cookies expire:

```sh
python3 -m chatgpt_api admin account update \
  --account pro-main \
  --capture-file ./chatgpt-request.txt \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

`save-capture` remains as an alias-style command and uses the same validation
and live-verify flow.

Account names should be ASCII slugs such as `free-main`, `pro-main`,
`free-2`, or `team-main`.

Delete an account capture and settings:

```sh
python3 -m chatgpt_api admin account delete \
  --account old-free-main \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

The older `admin delete-account` command is still available.

## Limits

Recommended local defaults:

```sh
python3 -m chatgpt_api admin set-limits \
  --chat free=1,go=2,plus=3,pro=4 \
  --upload free=1,go=1,plus=1,pro=1 \
  --image free=1,go=1,plus=2,pro=3 \
  --research free=1,go=1,plus=2,pro=2 \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

You can raise limits, but ChatGPT Web may still apply hidden short rate limits,
especially for image and research bursts.

`--upload` controls source-image work shared by OCR, image description,
chat-with-image, image edits, and multi-image composite requests. These calls
also consume ChatGPT `file_upload` usage when ChatGPT reports that feature for
the account. Keep the default `1` unless you have tested the account under
load.

Multi-account API routes preflight reported usage before sending the real
request. For image edit/composite, accounts need both upload capacity and image
generation capacity. For OCR/vision/chat-with-image, accounts need upload
capacity. For Deep Research, accounts need research capacity. `not_reported`
means the bridge does not see a counter, so it is treated as unknown but
allowed rather than exhausted.

## Artifacts

List generated images and Deep Research reports:

```sh
chatgpt-api admin artifacts --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Delete metadata only:

```sh
chatgpt-api admin delete-artifact --file-id <id> --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Delete metadata and file:

```sh
chatgpt-api admin delete-artifact --file-id <id> --delete-file --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

Download files through:

```text
GET/HEAD /v1/chatgpt/files/{file_id}/{filename}
```

The download route restores artifact metadata from the admin DB after an API
restart, as long as the file path still exists. In Docker, keep `/data/outputs`
mounted if you want old library links to keep working.

Use returned `download_url` for browsers or LAN clients. Use returned local
`path` only when the client can access the same filesystem.

## Smoke Tests

Chat:

```sh
chatgpt-api admin test-chat \
  --message "Reply with exactly: bridge ok" \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Image:

```sh
chatgpt-api admin test-image \
  --prompt "simple blue app icon, no text" \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Vision / OCR through the running API:

```sh
python3 -m chatgpt_api api vision \
  --mode ocr \
  --input-image ./favicon.png \
  --prompt "Extract visible letters only" \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Structured OCR with approximate bounding boxes:

```sh
python3 -m chatgpt_api api vision \
  --mode ocr \
  --input-image ./panel.png \
  --prompt 'Return strict JSON only. Schema: {"items":[{"text":"string","bbox":{"x":0,"y":0,"w":0,"h":0},"confidence":"low|medium|high"}]}. Use pixel coordinates relative to the input image. Estimate boxes when exact layout is uncertain.' \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

The CLI prints the model response. If you ask for JSON, parse stdout as model
JSON, but remember that boxes are estimated by ChatGPT rather than returned from
a native OCR layout engine.

Image edit with one source image:

```sh
python3 -m chatgpt_api api edit \
  --prompt "Change the icon letters to FW while preserving the same style" \
  --input-image ./favicon.png \
  --aspect-ratio 1:1 \
  --output-dir ./outputs/manual-edits \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Image composite with multiple source images:

```sh
python3 -m chatgpt_api api edit \
  --prompt "Combine these references into one manga cover image" \
  --input-image ./character.png \
  --input-image ./background.png \
  --input-image ./logo.png \
  --aspect-ratio 3:4 \
  --output-dir ./outputs/manual-edits \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

The CLI accepts at most 10 input images per request. Image edit/composite
returns one final image. Supported ratios are `auto`, `1:1`, `3:4`, `9:16`,
`4:3`, and `16:9`.

Important: source images should already match one of those ratios if you need
layout and object positions to stay stable. `auto` with an unusual ratio can
produce a different canvas size.

Models:

```sh
chatgpt-api admin models --json --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

## Raw Local Capture Tools

These do not need the API server:

```sh
chatgpt-api accounts
chatgpt-api inspect-capture --account pro-main
chatgpt-api account-info --account pro-main
chatgpt-api account-capabilities --account pro-main
chatgpt-api account-limits --account pro-main
chatgpt-api account-models --account pro-main
chatgpt-api account-check --account pro-main
```
