# research-search

A tiny **Mistral-agent-backed web search + URL fetch** service for Open WebUI.
It serves *both* of Open WebUI's `external` web contracts from one process, so
all web access goes through [Mistral](https://mistral.ai) (EU) instead of a
US search provider.

## Why

The stack previously used Exa for both web search (`web.search.engine=exa`)
and URL fetching (`exa-loader`). For privacy, all of that moved to Mistral:

- **Search**: the "Web Scout" Mistral agent (`mistral-small-latest` +
  `web_search`) runs the query — refining it up to 3 times if needed — and
  this service harvests the *raw* search results. The agent never answers the
  question; synthesis stays with whatever chat model Open WebUI is using, so
  research quality composes with every model. Results are **SERP-style only**
  (description + snippets); when a result matters, the chat model requests the
  full page via the fetch loader.
- **Fetch**: **Mistral-only by design** — this service performs no direct
  HTTP fetching. Every URL goes through the scout agent, which *opens* the
  page (one `{"url": ...}` tool call with the current Studio instructions),
  yielding the actual page text. If the open never happens, the page's own
  SERP description/snippets serve as text (last resort). Consequences:
  - Target sites see Mistral's fetchers, never this server's IP.
  - There is no outbound-fetch attack surface (SSRF) in this service.
  - Mistral's tool channel caps a page at 32768 chars. Truncated pages are
    **flagged, never silently truncated**: `metadata.truncated = true` and a
    note appended to `page_content` so the chat model knows the text is
    incomplete (accepted tradeoff of the Mistral-only choice).
- **PDFs and other binaries never reach this service**: Open WebUI probes
  the URL's Content-Type itself and routes binary types (PDF, DOCX, …)
  through its own Tika extraction (`CONTENT_EXTRACTION_ENGINE=tika`);
  the external loader only sees text/HTML. (Verified anyway: Mistral's
  agent also extracts PDF text — an arxiv PDF returned 32K chars of real
  paper text through /fetch.)

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
snippets) serves as text with `metadata.fetch_mode = "serp"`.

## Known limits

- **Quality**: a small A/B against Exa (3 queries) showed comparable results
  with Exa slightly ahead on precision; the multi-query agent loop beats a
  single-shot search. Search results are snippets-only now — the chat model
  compensates by fetching pages via the loader when needed.
- **Truncation**: Mistral's tool channel caps a fetched page at 32768
  chars (the result JSON can be cut mid-string — salvaged leniently). The
  cap is server-side with no knob, the open takes only `{"url": ...}` (no
  offset/section parameter), and re-opening is byte-deterministic — so
  the first 32K is the best effort, and it is **flagged** (`metadata.truncated`
  + note in `page_content`) so no model is misled into treating it as the
  whole page.
- **Cost**: every search turn costs $0.03–0.09 (1–3 `web_search` calls);
  every fetch now costs one scout conversation (search + open ≈ $0.06) —
  there is no free direct-fetch path anymore, by design.
- **Latency**: each fetch is one agent conversation (~2–5 s).

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
| `FETCH_WORKERS` | Parallel fetch workers per request | `4` |
| `PORT` | Listen port | `8080` |

The Mistral key **and** bearer token are required: the service refuses to
start without them, so it cannot come up with auth silently disabled.

Open WebUI config (Admin → Web Search, or directly in the config DB — DB rows
win over env on existing installs):

| Key | Value |
|---|---|
| `web.search.engine` | `external` |
| `web.search.external_web_search_url` | `http://research-search:8080/search` |
| `web.search.external_web_search_api_key` | token in `/opt/secrets/research-search.token` |
| `web.loader.engine` | `external` |
| `web.loader.external_web_loader_url` | `http://research-search:8080/fetch` |
| `web.loader.external_web_loader_api_key` | token in `/opt/secrets/research-search.token` |

Secrets live outside the repo: `/opt/secrets/mistral.env` (shared with the
litellm stack) and `/opt/secrets/research-search.token`. The service is
internal-only (no published ports).

## Rollback

Pre-change config backup: `data/backups/config-web-backup-2026-10-20.json`
(all `web.search.*` / `web.loader.*` rows, incl. the Exa key and loader URL).
Restore = stop open-webui, write the rows back, `git revert` the exa-loader
removal commit, `docker compose up -d`.

## Tests

`python3 test_app.py` — unit tests for the search-entry, page-text and
truncation-detection parsing (including the truncated-JSON salvage),
stdlib only, runs on host.
