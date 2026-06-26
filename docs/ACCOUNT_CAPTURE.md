# Account Capture Guide

This bridge needs one copied ChatGPT Web browser request for each local account.
That copied request is called an account capture.

The capture contains session cookies, bearer tokens, sentinel proof headers, and
the JSON body shape ChatGPT Web currently sends. Treat it like a password.
Paste one browser request at a time. Do not join Safari and Chrome cURL captures
or multiple accounts into one save operation.

## Supported Capture Formats

The parser accepts these formats:

| Browser | Format | Recommended |
| --- | --- | --- |
| Chrome | `Copy as cURL` / `Copy as cURL (bash)` | Yes |
| Chrome | Headers tab plus Payload tab | Yes |
| Safari | Headers summary plus `Request Data` JSON | Yes |

Other browsers may work if their copied request text contains the same URL,
headers, cookies, and JSON body, but they are not documented as supported.

## Security Rules

- Never commit `secrets/`, `.env`, copied headers, cookies, bearer tokens, or
  raw request captures.
- Do not paste real captures into GitHub issues, screenshots, chat logs, or
  public docs.
- Prefer one ChatGPT account per browser profile or private/incognito window.
- Do not log out of ChatGPT after collecting the capture. Logging out can revoke
  the browser session.
- Refresh important captures weekly. They often last around 10 days, but this is
  not guaranteed.
- Account names are local aliases. Use ASCII slugs only, for example
  `free-main`, `pro-main`, `free-2`, or `plus_work`.

## Quick Path

1. Open `https://chatgpt.com`.
2. Sign in to the account you want to add.
3. Open a fresh ChatGPT conversation.
4. Open DevTools or Web Inspector.
5. Go to Network.
6. Clear the Network log.
7. Enable Preserve log.
8. Send a tiny message such as `hello`.
9. Search Network for `conversation`.
10. Select the `POST` request to:

   ```text
   https://chatgpt.com/backend-api/f/conversation
   ```

11. Copy that request using one of the browser-specific methods below.
12. Paste it into the Bridge Console account modal or CLI paste flow.

Do not use telemetry requests such as `flush`, `intake`, or `m`. Those are not
conversation requests and cannot be used as captures.

`/backend-api/f/conversation/prepare` may appear near the real request. The
normal account capture should be the real `POST /backend-api/f/conversation`
request because it includes the request payload template the bridge can reuse.

## Chrome: Copy as cURL

This is now the simplest Chrome path.

1. Select the `POST /backend-api/f/conversation` request.
2. Right-click the request row.
3. Choose `Copy` -> `Copy as cURL` or `Copy as cURL (bash)`.
4. Paste the full command into the console modal or CLI.
5. Do not trim the command before saving locally.

Paste only one cURL command for the account you are adding or updating. If you
have multiple ChatGPT accounts, save them one by one with different local
aliases.

The pasted cURL must still contain all of these pieces:

| Required piece | Why it matters |
| --- | --- |
| `https://chatgpt.com/backend-api/f/conversation` | Tells the bridge which ChatGPT endpoint to replay. |
| `-H 'Authorization: Bearer ...'` | Carries the account access token. |
| `-H 'Cookie: ...'` or `-b '...'` | Carries session cookies and Cloudflare/browser state. |
| `--data-raw '{...}'` | Carries the ChatGPT request JSON template. |
| Sentinel headers | ChatGPT browser proof values, when present. |
| `x-conduit-token` | ChatGPT routing value, when present. |

Redacted example:

```sh
curl 'https://chatgpt.com/backend-api/f/conversation' \
  -X 'POST' \
  -H 'Authorization: Bearer REDACTED' \
  -H 'Cookie: oai-did=REDACTED; __Secure-next-auth.session-token.0=REDACTED' \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'OpenAI-Sentinel-Proof-Token: REDACTED' \
  -H 'OpenAI-Sentinel-Turnstile-Token: REDACTED' \
  -H 'OpenAI-Sentinel-Chat-Requirements-Token: REDACTED' \
  -H 'x-conduit-token: REDACTED' \
  --data-raw '{"action":"next","messages":[{"author":{"role":"user"},"content":{"content_type":"text","parts":["hello"]}}],"parent_message_id":"client-created-root","model":"auto","client_prepare_state":"success"}'
```

Chrome and Safari may serialize cookies differently. Both of these are valid
for local capture import:

```sh
-H 'Cookie: oai-did=REDACTED; __Secure-next-auth.session-token.0=REDACTED'
-b 'oai-did=REDACTED; __Secure-next-auth.session-token.0=REDACTED'
```

If the copied cURL has Authorization but no `Cookie:` header and no `-b` cookie
argument, it is not enough for a ChatGPT Web capture.

For your real local capture, do not replace values with `REDACTED`. Redact only
when posting examples publicly.

## Chrome: Headers Plus Payload

Use this if you do not want to paste cURL.

1. Select the `POST /backend-api/f/conversation` request.
2. Open the Headers tab.
3. Copy the full request details and request headers.
4. Open the Payload tab.
5. Copy the full JSON request payload.
6. Paste both sections together into the console modal or CLI.

Minimum shape:

```text
Request URL
https://chatgpt.com/backend-api/f/conversation

authorization
Bearer REDACTED
cookie
oai-did=REDACTED; __Secure-next-auth.session-token.0=REDACTED
content-type
application/json

Request Payload
{"action":"next","messages":[...],"model":"auto","client_prepare_state":"success"}
```

## Safari: Headers Plus Request Data

Safari's Web Inspector usually shows request headers and request data separately.

1. Select the `POST /backend-api/f/conversation` request.
2. Open the Headers view.
3. Copy the full headers/details section.
4. Find `Request Data:`.
5. Expand it.
6. Copy the full JSON body shown there.
7. Paste both sections together into the console modal or CLI.

Minimum shape:

```text
URL: https://chatgpt.com/backend-api/f/conversation
Status: 200

Request
Authorization: Bearer REDACTED
Cookie: oai-did=REDACTED; __Secure-next-auth.session-token.0=REDACTED
Content-Type: application/json

Request Data
Request Data: {"action":"next","messages":[...],"model":"auto","client_prepare_state":"success"}
```

## Local Self-Check Before Saving

A capture should pass these checks before it is saved:

| Check | Expected |
| --- | --- |
| URL | `https://chatgpt.com/backend-api/f/conversation` |
| Authorization | present |
| Cookie | present |
| Cookies parsed | at least one cookie, usually many |
| Request JSON | present for the normal conversation request |
| `action` | usually `next` |
| `model` | usually `auto` or the selected ChatGPT Web model |

If the console or CLI says:

```text
missing=url,authorization,cookie
```

the pasted text is incomplete or not in a supported shape. For cURL, paste the
whole command, not only the first line. The command must include cookies through
either `-H 'Cookie: ...'` or `-b '...'`.

## Save Through The Console

1. Start Docker or the local API.
2. Open:

   ```text
   http://127.0.0.1:8080
   ```

3. Go to Accounts.
4. Choose Add account or Update capture.
5. Enter an ASCII account name such as `pro-main`.
6. Paste the copied capture.
7. Click Inspect if you want to see what the parser found.
8. Save only when validation passes.
9. Run Check/Verify for that account.

The console should not save a broken capture silently. It parses the capture,
redacts secret summaries, and shows missing required pieces before writing.

## Save Through The CLI

Save from a prepared file:

```sh
chatgpt-api admin account add \
  --account pro-main \
  --capture-file ./chatgpt-request.txt \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Paste interactively:

```sh
chatgpt-api admin account add --paste \
  --account pro-main \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Paste the full capture, then end with:

```text
END_CAPTURE
```

Update an existing account after tokens/cookies expire:

```sh
chatgpt-api admin account update \
  --account pro-main \
  --capture-file ./chatgpt-request.txt \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Verify all accounts:

```sh
chatgpt-api admin account verify \
  --account all \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Delete an account:

```sh
chatgpt-api admin account delete \
  --account old-free-main \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

## File Layout

Default local account files:

```text
secrets/accounts/<account>/chatgpt-request.txt
secrets/accounts/<account>/settings.json
```

Docker account files:

```text
/data/secrets/accounts/<account>/chatgpt-request.txt
/data/secrets/accounts/<account>/settings.json
```

The default Compose file mounts host `./secrets/accounts` to
`/data/secrets/accounts`.

## Refresh Schedule

- Refresh demo accounts once per week.
- Refresh immediately after `403`, session expired, missing metadata, or no
  model/usage metadata returned.
- Keep at least one known-good Pro/Plus account if you plan to demo image
  generation or Deep Research.
- Keep a Free account in the route to prove the bridge can work without a paid
  ChatGPT plan for normal chat.

## Troubleshooting

`missing=url,authorization,cookie`:
: The parser did not find the three minimum required pieces. For cURL, paste the
  entire command including URL, `-H Authorization`, cookies through
  `-H Cookie` or `-b`, and `--data-raw`. For Headers/Payload, copy both tabs.

`missing=request_json`:
: The capture is missing the JSON body. Copy the Payload tab, Safari Request
  Data, or the cURL `--data-raw` argument.

`403` from ChatGPT:
: The browser session, Cloudflare/session proof, or copied headers no longer
  replay. Collect a fresh capture from the same browser family. If Chrome keeps
  failing, try a fresh Chrome profile or Safari.

`conversation/init returned no metadata`:
: ChatGPT did not return the limit/model metadata needed for usage display.
  Retry later or refresh the capture.

Only one account routes:
: Check `CHATGPT_ACCOUNTS`, account names, and whether each account capture
  exists under `secrets/accounts/<name>/`.

Image or file upload blocked:
: Check `/v1/chatgpt/usage`. Free accounts can have low or blocked image/upload
  quota. OCR, describe, chat-with-image, image edit, and composite routes all
  consume upload capacity before the model sees the source image. Image edit and
  composite routes also need image generation quota for the final output. If a
  feature says `not_reported`, the bridge treats it as unknown rather than
  blocked, so the request can still try that account.

Deep Research blocked:
: Use a normal, non-temporary chat mode and a plan that exposes Deep Research.
  Free accounts may show blocked Deep Research in usage metadata.
