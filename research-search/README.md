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
  research quality composes with every model.
- **Fetch**: URLs are fetched directly over HTTPS by this service (full page
  text, no third party involved). If that fails (JS-only pages, non-HTML),
  the scout agent fetches instead: it searches for the URL and then *opens*
  the page (two-step agentic flow — the small model only performs the page
  open as a second call after the search presents `can_open`), which yields
  the actual page text (capped at 32768 chars by Mistral's tool channel).
  Only if the open never happens does the page's SERP description/snippets
  serve as text.
- **PDFs and other binaries never reach this service**: Open WebUI probes
  the URL's Content-Type itself and routes binary types (PDF, DOCX, …)
  through its own Tika extraction (`CONTENT_EXTRACTION_ENGINE=tika`);
  the external loader only sees text/HTML. (Verified anyway: Mistral's
  agent fallback also extracts PDF text — an arxiv PDF returned 32K chars
  of real paper text through /fetch.)

## How it works

**Search contract** (Open WebUI `external` search engine):

- POSTs `{"query": "...", "count": 8}` to `/search`
- sends `Authorization: Bearer <web.search.external_web_search_api_key>`
- expects back a JSON **list** of `{"link", "title", "snippet"}`

The scout conversation's `web_search` tool results (url, title, description,
snippets, rank) are normalized, deduped across the agent's multiple searches,
and rank-ordered. The top `ENRICH_TOP` results additionally get a direct-HTTPS
page excerpt appended to their snippet (free, no third party) so the chat
model sees real page text — roughly the content richness Exa's snippets had.

**Fetch contract** (Open WebUI `external` web loader):

- POSTs `{"urls": ["https://…"]}` to `/fetch`
- sends `Authorization: Bearer <web.loader.external_web_loader_api_key>`
- expects back a JSON **list** of `{"page_content", "metadata"}`

Per URL: direct HTTPS fetch + HTML→text extraction (title included). On
failure (non-HTML, JS-only shell, timeout) the agent fallback searches for
the URL and returns the page's own SERP entry as text.

## Known limits

- **Quality**: a small A/B against Exa (3 queries) showed comparable results
  with Exa slightly ahead on precision; snippets with page excerpts close
  much of the gap. The multi-query agent loop beats a single-shot search.
- **Agent fetch**: Mistral's tool channel caps a fetched page at 32768
  chars (the result JSON can be cut mid-string — salvaged leniently). The
  cap is server-side with no knob, the open takes only `{"url": ...}` (no
  offset/section parameter), and re-opening is byte-deterministic — so
  "fetching the whole page in multiple goes" cannot walk past the first
  32K. Full-page text comes from the direct HTTPS path instead; for
  pages that only the agent can read, first-32K is the best effort.
  The agent's page-open is a *second* tool call after the URL search (the
  small model never opens directly), so the scout instructions must allow
  two calls for FETCH PAGE.
- **Cost**: every search turn costs $0.03–0.09 (1–3 `web_search` calls);
  fetches are free unless the direct fetch fails and the agent fallback
  triggers (then one search + one open ≈ $0.06).

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
| `ENRICH_TOP` | Top results enriched with direct page excerpts | `3` |
| `SNIPPET_PAGE_MAX` | Max page excerpt per snippet (chars) | `6000` |
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

`python3 test_app.py` — unit tests for the search-entry and page-text
parsing (including the truncated-JSON salvage), stdlib only, runs on host.
