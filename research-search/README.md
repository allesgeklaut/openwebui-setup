# research-search

Mistral Web Scout search and page-fetch adapter. In this stack, SearXNG's
opt-in `!scout` engine calls `/search`. `/fetch` is available as an external
Open WebUI loader, but **the current defaults are SearXNG search and direct
HTTP page loading**, not this service.

Only requests sent to this service use Mistral's search/fetch infrastructure.
The service does not directly download target pages. Each search or fetched
URL starts a billed agent conversation; cancellation/timeouts at the caller
do not guarantee that the upstream conversation stops.

## How it works

**Search contract** (Open WebUI `external` search engine):

- POSTs `{"query": "...", "count": 8}` to `/search`
- sends `Authorization: Bearer <web.search.external_web_search_api_key>`
- expects back a JSON **list** of `{"link", "title", "snippet"}`

The scout conversation's `web_search` tool results (url, title, description,
snippets, rank) are normalized, deduped across the agent's multiple searches,
and rank-ordered. No page content is fetched for search results — snippets
only. The chat model can request a full page through the fetch loader whenever
a specific result matters.

**Fetch contract** (Open WebUI `external` web loader):

- POSTs `{"urls": ["https://…"]}` to `/fetch`
- sends `Authorization: Bearer <web.loader.external_web_loader_api_key>`
- expects back a JSON **list** of `{"page_content", "metadata"}`

Per URL: the scout agent opens the page and this service harvests the
opened page text. Pages cut by Mistral's 32768-char tool cap come back with
`metadata.truncated = true` and a truncation note inside `page_content`.
If the open never happens, the page's own SERP entry (description +
snippets) serves as text with `metadata.fetch_mode = "serp"`. A refused
open (`can_open=false` plus the fetcher's canned refusal sentence) counts
as a failed open: the refusal is never returned as page content, and the
URL 502s when no SERP entry exists either.

## Limits

- Search returns snippets, not full pages or a synthesized answer.
- Mistral's tool channel limits page text to about 32K characters. Truncated
  output is flagged; the service cannot paginate the upstream page text.
- A refused/blocked open falls back to SERP snippets, then HTTP 502 if no
  usable snippets exist. Refusal messages are not returned as page content.
- Calls can take seconds or longer and incur Mistral usage charges.
- Open WebUI may probe URLs or route binary documents to Tika outside this
  service; selecting it does not make the entire stack Mistral-only.

## Configuration

Environment:

| Var | Meaning | Default |
|---|---|---|
| `MISTRAL_API_KEY_FILE` | Env-style file with `MISTRAL_API_KEY=` (shared with litellm) | — |
| `MISTRAL_API_KEY` | Inline Mistral key (fallback) | — |
| `RESEARCH_TOKEN_FILE` | File containing the shared bearer token | — |
| `RESEARCH_TOKEN` | Inline bearer token (fallback) | — |
| `SCOUT_AGENT_ID` | Mistral agent id for search/fetch | — |
| `MISTRAL_API_URL` | Mistral conversations endpoint | `https://api.mistral.ai/v1/conversations` |
| `MISTRAL_TIMEOUT` | Mistral request timeout (seconds) | `120` |
| `DEFAULT_COUNT` / `MAX_COUNT` | Result count fallback / hard cap | `8` / `20` |
| `SNIPPET_MAX` | Max total snippet length (chars) | `8000` |
| `MAX_URLS` | Max URLs accepted per fetch request | `20` |
| `FETCH_WORKERS` | Parallel fetch workers per request | `4` (compose sets `8`) |
| `PORT` | Listen port | `8080` |

The Mistral key **and** bearer token are required: the service refuses to
start without them, so it cannot come up with auth silently disabled.

To opt into this service directly, use Open WebUI's administration settings
(not direct database writes). These settings are **alternatives**, not the
current default configuration:

| Key | Value |
|---|---|
| `web.search.engine` | `external` |
| `web.search.external_web_search_url` | `http://research-search:8080/search` |
| `web.search.external_web_search_api_key` | token in `/opt/secrets/research-search.token` |
| `web.loader.engine` | `external` |
| `web.loader.external_web_loader_url` | `http://research-search:8080/fetch` |
| `web.loader.external_web_loader_api_key` | token in `/opt/secrets/research-search.token` |

The search and loader options can be selected independently. In native
tool-calling mode the chat model chooses pages to fetch; the forced pipeline
fetches result pages automatically unless bypassed. This service's
`FETCH_WORKERS`, not the external loader's concurrency setting, controls
parallel fetches within a request.

Secrets live outside the repo: `/opt/secrets/mistral.env` (shared with the
litellm stack) and `/opt/secrets/research-search.token`. The service is
internal-only (no published ports).

## Tests

`python3 test_app.py` — unit tests for the search-entry, page-text and
truncation-detection parsing (including the truncated-JSON salvage),
stdlib only, runs on host.
