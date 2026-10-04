# exa-loader

A tiny **external web loader** for Open WebUI that fetches pages through
[Exa](https://exa.ai)'s `/contents` API and returns clean, article-style text.

## Why

Open WebUI's built-in web loader (`safe_web`) extracts text with
`BeautifulSoup(html, "lxml").get_text()` over the whole DOM — so navigation,
sidebars, footers and ads are included alongside the article. Exa already
returns cleaned main-content text for the same URLs, and this stack already
has an Exa API key for web search.

Instead of adding a new vendor, this adapter lets Open WebUI reuse Exa for
*fetching* too.

## How it works

Open WebUI's `external` web loader:

- POSTs `{"urls": ["https://…"]}`
- sends `Authorization: Bearer <web.loader.external_web_loader_api_key>`
- expects back a JSON **list** of `{"page_content": str, "metadata": {...}}`

This service validates the bearer token, forwards the URLs to
`POST https://api.exa.ai/contents` (`text: true`) using the Exa key, and
reshapes the results.

## Configuration

Environment:

| Var | Meaning | Default |
|---|---|---|
| `EXA_API_KEY_FILE` | File containing the Exa API key | — |
| `EXA_API_KEY` | Inline Exa key (fallback) | — |
| `LOADER_TOKEN_FILE` | File containing the shared bearer token | — |
| `LOADER_TOKEN` | Inline bearer token (fallback) | — |
| `EXA_API_URL` | Exa contents endpoint | `https://api.exa.ai/contents` |
| `EXA_TEXT_MAX_CHARS` | Optional per-page cap; empty = full text | *(empty)* |
| `EXA_TIMEOUT` | Upstream request timeout (seconds) | `90` |
| `MAX_URLS` | Max URLs accepted per request | `100` |
| `MAX_BODY_BYTES` | Max accepted request body size | `2097152` |
| `PORT` | Listen port | `8080` |

Both the Exa key **and** the bearer token are required: the service refuses to
start without them, so it cannot come up with auth silently disabled.

Open WebUI settings (Admin → Web Search, or directly in the config DB):

- `web.loader.engine` = `external`
- `web.loader.external_web_loader_url` = `http://exa-loader:8080/contents`
- `web.loader.external_web_loader_api_key` = `<the loader token>`

The service is internal-only (no published ports).
