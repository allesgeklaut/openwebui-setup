"""Mistral-agent-backed web search + fetch for Open WebUI.

Serves both of Open WebUI's `external` web contracts from one process:

  * Search engine:  POST /search  {"query", "count"}  -> [{"link", "title", "snippet"}]
  * Web loader:      POST /fetch   {"urls": [...]}     -> [{"page_content", "metadata"}]

Both are powered by the "Web Scout" Mistral agent (mistral-small-latest +
web_search): a fast agent whose instructions make it run the search / open the
URL and then reply only "DONE".  This service harvests the RAW tool results
(the search hits, the opened page text) and reshapes them into the contracts
above — the agent never synthesizes anything, that stays with whatever chat
model Open WebUI is using.

This replaces the previous Exa-based search engine + exa-loader so that all
web access goes through Mistral (EU) instead of a US search provider.

Snippet enrichment: search results are SERP-style (description + snippets).
To keep roughly the content richness Exa gave (up to 8000 chars of page text
per result), the top ENRICH_TOP results are additionally fetched directly
over HTTPS by this service (no third party involved — the target site just
sees a normal page request) and their text is appended to the snippet.
Set ENRICH_TOP=0 to disable.

Known limit: the Mistral tool channel caps a fetched page at 32768 chars
(the tool result carries "truncated_response" with the inner JSON possibly
cut mid-string — parsed leniently below).

Stdlib only -- no third-party dependencies.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(stream=sys.stdout, level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("research-search")


def _env_int(name: str, default: int | None, *, minimum: int = 0) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer (got {raw!r})")
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum} (got {value})")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number (got {raw!r})")
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum} (got {value})")
    return value


MISTRAL_API_URL = os.environ.get("MISTRAL_API_URL", "https://api.mistral.ai/v1/conversations")
MISTRAL_TIMEOUT = _env_float("MISTRAL_TIMEOUT", 120.0, minimum=0.1)
SCOUT_AGENT_ID = (os.environ.get("SCOUT_AGENT_ID") or "").strip()
PORT = _env_int("PORT", 8080) or 8080
MAX_BODY_BYTES = _env_int("MAX_BODY_BYTES", 2 * 1024 * 1024) or 2 * 1024 * 1024

# search contract
DEFAULT_COUNT = _env_int("DEFAULT_COUNT", 8, minimum=1) or 8
MAX_COUNT = _env_int("MAX_COUNT", 20, minimum=1) or 20

# snippet enrichment (direct HTTP, no Mistral cost)
ENRICH_TOP = _env_int("ENRICH_TOP", 3) or 0
ENRICH_TIMEOUT = _env_float("ENRICH_TIMEOUT", 10.0, minimum=0.1)
ENRICH_MAX_BYTES = _env_int("ENRICH_MAX_BYTES", 2 * 1024 * 1024) or 2 * 1024 * 1024
SNIPPET_PAGE_MAX = _env_int("SNIPPET_PAGE_MAX", 6000) or 6000
SNIPPET_MAX = _env_int("SNIPPET_MAX", 8000) or 8000

# fetch contract
MAX_URLS = _env_int("MAX_URLS", 20, minimum=1) or 20
FETCH_WORKERS = _env_int("FETCH_WORKERS", 4, minimum=1) or 4

# A browser-like UA: many sites reject unknown agents outright, and this
# fetch is indistinguishable from a normal reader hitting the page.
ENRICH_USER_AGENT = os.environ.get(
    "ENRICH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)


def _read_secret(value: str | None, file_env: str | None) -> str:
    """Read a secret from env, or from a *_FILE (raw token file)."""
    if file_env:
        path = os.environ.get(file_env)
        if path:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            log.error("%s points at %s, which does not exist", file_env, path)
    return (value or "").strip()


def _read_mistral_key() -> str:
    """Read the Mistral API key.

    Supports MISTRAL_API_KEY directly, or MISTRAL_API_KEY_FILE pointing at
    an env-style file (KEY=VALUE lines) such as /opt/secrets/mistral.env —
    the same single source of truth the litellm stack uses.
    """
    direct = (os.environ.get("MISTRAL_API_KEY") or "").strip()
    if direct:
        return direct
    path = os.environ.get("MISTRAL_API_KEY_FILE")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() == "MISTRAL_API_KEY":
                        return val.strip().strip("'\"")
        log.error("%s has no MISTRAL_API_KEY= line", path)
    return ""


MISTRAL_API_KEY = _read_mistral_key()
RESEARCH_TOKEN = _read_secret(os.environ.get("RESEARCH_TOKEN"), "RESEARCH_TOKEN_FILE")
# Bytes form: HTTP headers are decoded latin-1, so they may contain non-ASCII
# characters, and hmac.compare_digest rejects str operands with non-ASCII chars.
_EXPECTED_AUTHORIZATION = f"Bearer {RESEARCH_TOKEN}".encode("utf-8")


# --------------------------------------------------------------------------
# Mistral conversations API
# --------------------------------------------------------------------------

def call_scout(content: str) -> dict:
    """One-shot conversation with the Web Scout agent (store=false).

    Returns the full conversation response; tool executions live in
    outputs[*].type == "tool.execution" with info.result payloads.
    """
    payload = {
        "agent_id": SCOUT_AGENT_ID,
        "store": False,
        "inputs": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        MISTRAL_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=MISTRAL_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tool_executions(conv: dict) -> list[dict]:
    return [
        e
        for e in conv.get("outputs", [])
        if isinstance(e, dict) and e.get("type") == "tool.execution"
    ]


# --------------------------------------------------------------------------
# Search mode: harvest SERP entries from tool results
# --------------------------------------------------------------------------

def parse_search_entries(raw) -> list[dict]:
    """Normalize a web_search tool result into a flat entry list.

    Observed shape: a JSON string containing a dict keyed by opaque ids whose
    values are {url, title, description, snippets, rank, date, source, ...}.
    Be defensive: accept lists and {"results": [...]} wrappers too.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        if isinstance(raw.get("results"), list):
            raw = raw["results"]
        else:
            raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("url")]


def harvest_search_entries(conv: dict) -> list[dict]:
    """All search entries across every web_search execution, best-first."""
    by_url: dict[str, dict] = {}
    for e in tool_executions(conv):
        for entry in parse_search_entries((e.get("info") or {}).get("result")):
            url = entry.get("url")
            existing = by_url.get(url)
            if existing is None or _rank(entry) < _rank(existing):
                by_url[url] = entry
    entries = list(by_url.values())
    entries.sort(key=lambda e: _rank(e))
    return entries


def _rank(entry: dict) -> int:
    rank = entry.get("rank")
    if isinstance(rank, (int, float)):
        return int(rank)
    return 1 << 30


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _serp_snippet(entry: dict) -> str:
    parts: list[str] = []
    desc = _strip_tags(entry.get("description") or "")
    if desc:
        parts.append(desc)
    snippets = entry.get("snippets") or []
    if isinstance(snippets, str):
        snippets = [snippets]
    for s in snippets:
        if isinstance(s, str) and s.strip():
            parts.append(s.strip())
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Fetch mode: extract page text from a FETCH PAGE tool result
# --------------------------------------------------------------------------

def _loads_maybe(text: str):
    """json.loads that also salvages JSON cut mid-string (tool truncation)."""
    try:
        return json.loads(text)
    except Exception:
        pass
    # Salvage {"url": "...", "content": "<cut mid-string>} progressively.
    url = None
    m = re.search(r'"url"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        try:
            url = json.loads('"' + m.group(1) + '"')
        except Exception:
            url = None
    m = re.search(r'"content"\s*:\s*"(.*)', text, re.S)
    if not m:
        return {"url": url} if url else None
    body = m.group(1)
    for cut in range(len(body), max(len(body) - 16, 0), -1):
        try:
            return {"url": url, "content": json.loads('"' + body[:cut] + '"')}
        except Exception:
            continue
    return {"url": url} if url else None


def _page_fields(obj) -> dict | None:
    if isinstance(obj, dict) and obj.get("content"):
        return {
            "url": obj.get("url") or "",
            "content": obj["content"],
            "title": obj.get("title") or "",
        }
    return None


def extract_page(data) -> dict | None:
    """{"url","content","title"} from a tool-result payload, leniently.

    Accepts (in any nesting): a JSON string, a content block
    {"type": "text", "text": "..."}, a list of those, the page dict itself,
    or the observed wrapper {"truncated_response"|"response": "<json string
    of {url, content}>", "info": "..."} (with the inner JSON possibly cut
    mid-string by the tool's 32768-char cap — salvaged by _loads_maybe).
    """
    if data is None:
        return None
    obj = _loads_maybe(data) if isinstance(data, str) else data
    if obj is None:
        return None
    # content block -> recurse into its text payload
    if isinstance(obj, dict) and isinstance(obj.get("text"), str) and not obj.get("content"):
        return extract_page(obj["text"])
    if isinstance(obj, list):
        for entry in obj:
            page = extract_page(entry)
            if page:
                return page
        return None
    page = _page_fields(obj)
    if page:
        return page
    if isinstance(obj, dict):
        inner = obj.get("response") or obj.get("truncated_response")
        if inner:
            return extract_page(inner)
    return None


def fetch_page_via_agent(url: str) -> dict | None:
    """Agent-assisted fetch fallback: search for the URL, return the page's
    own SERP entry (description + snippets straight from the page) as text.

    (The small model always passes the URL as a search query rather than the
    tool's url parameter — three instruction variants could not change that.
    Searching for the exact URL reliably puts the page itself at rank 0, so
    we use that.)
    """
    conv = call_scout(f"FETCH PAGE: {url}")
    # A real page-open (model passes {"url": ...})? Future-proof: harvest the
    # full page text if the tool ever returns one.
    for e in tool_executions(conv):
        page = extract_page((e.get("info") or {}).get("result"))
        if page and page.get("content"):
            if not page.get("url"):
                page["url"] = url
            page.setdefault("title", "")
            return page
    # SERP fallback: the page's own entry (description + snippets).
    best = None
    for e in tool_executions(conv):
        for entry in parse_search_entries((e.get("info") or {}).get("result")):
            if entry.get("url") == url:
                best = entry
                break
            if best is None and entry.get("rank") == 0:
                best = entry
        if best:
            break
    if not best:
        return None
    text = _serp_snippet(best)
    if not text:
        return None
    return {"url": url, "content": text, "title": (best.get("title") or "").strip()}


def fetch_url(url: str) -> dict | None:
    """Fetch one URL: direct HTTPS first (free, full page), agent fallback
    (JS-only pages the direct fetch cannot parse; returns page snippets)."""
    try:
        text, title = fetch_page_text(url)
        return {
            "page_content": text,
            "metadata": {"source": url, "url": url, "title": title},
        }
    except Exception as exc:
        log.info("direct fetch failed for %s (%s), trying agent", url, exc)
    page = fetch_page_via_agent(url)
    if not page:
        return None
    return {
        "page_content": page["content"],
        "metadata": {"source": page.get("url") or url, "url": page.get("url") or url, "title": page.get("title") or ""},
    }


# --------------------------------------------------------------------------
# Snippet enrichment: direct HTTPS fetch of top results (no third party)
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    # note: <head> is NOT skipped — <title> lives there. script/style are
    # skipped individually wherever they appear.
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {
        "p", "div", "br", "li", "tr", "section", "article", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = 0
        self._chunks: list[str] = []
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title += 1
        elif tag in self.BLOCK and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title" and self._in_title:
            self._in_title -= 1
        elif tag in self.BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
        else:
            self._chunks.append(data)

    def text(self) -> str:
        out = "".join(self._chunks)
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\s*\n\s*", "\n", out)
        return out.strip()


def fetch_page_text(url: str) -> tuple[str, str]:
    """Direct HTTPS fetch + HTML->text. Returns (text, title).

    Raises on any failure (non-HTML, JS-only shell, timeout, HTTP error).
    """
    req = urllib.request.Request(url, headers={"User-Agent": ENRICH_USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "xml" not in ctype:
            raise ValueError(f"not HTML ({ctype})")
        body = resp.read(ENRICH_MAX_BYTES)
    extractor = _TextExtractor()
    extractor.feed(body.decode("utf-8", "replace"))
    text = extractor.text()
    if len(text) < 200:  # likely a JS shell page
        raise ValueError("page text too short (JS-only page?)")
    return text, extractor.title.strip()


def search_results(query: str, count: int) -> list[dict]:
    """Full /search flow: scout conversation -> SERP -> enrichment -> contract."""
    conv = call_scout(query)
    entries = harvest_search_entries(conv)
    if not entries:
        raise RuntimeError("agent returned no search results")
    count = max(1, min(count, MAX_COUNT))
    results = [
        {"link": e["url"], "title": (e.get("title") or e["url"]).strip(), "snippet": _serp_snippet(e)}
        for e in entries[:count]
    ]
    if ENRICH_TOP > 0:
        top = results[:ENRICH_TOP]

        def _enrich(item):
            try:
                page_text, _title = fetch_page_text(item["link"])
            except Exception as exc:
                log.info("enrichment skipped for %s: %s", item["link"], exc)
                return
            item["snippet"] = (item["snippet"] + "\n\n--- page excerpt ---\n" + page_text[:SNIPPET_PAGE_MAX]).strip()

        with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(top) or 1)) as pool:
            list(pool.map(_enrich, top))
    for item in results:
        if len(item["snippet"]) > SNIPPET_MAX:
            item["snippet"] = item["snippet"][:SNIPPET_MAX]
    return results


def fetch_urls(urls: list[str]) -> list[dict]:
    """Full /fetch flow: one agent page-open per URL -> Open WebUI Documents."""
    docs: list[dict] = []
    failures: list[str] = []

    def _one(url: str):
        try:
            return fetch_url(url)
        except Exception as exc:
            log.warning("fetch failed for %s: %s", url, exc)
            return None

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        outcomes = list(pool.map(_one, urls))
    for url, doc in zip(urls, outcomes):
        if doc:
            docs.append(doc)
        else:
            failures.append(url)
    if failures:
        log.warning("failed URLs: %s", failures)
    return docs


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "research-search/1.0"

    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not RESEARCH_TOKEN:
            return True  # no token configured -> open (dev only)
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(
            auth.encode("utf-8", "surrogateescape"), _EXPECTED_AUTHORIZATION
        )

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return None
        try:
            body = self.rfile.read(length) if length else b"{}"
            return json.loads(body or b"{}")
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return None

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/health", "/healthz"):
            self._send(200, {"status": "ok", "agent": SCOUT_AGENT_ID or None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/", "/search", "/fetch", "/contents"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            log.warning("Rejected request with bad/missing token")
            self._send(401, {"error": "unauthorized"})
            return

        payload = self._read_json()
        if payload is None:
            return

        if self.path in ("/", "/search"):
            self._handle_search(payload)
        else:
            self._handle_fetch(payload)

    def _handle_search(self, payload: dict) -> None:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            self._send(400, {"error": "missing 'query' string"})
            return
        count = payload.get("count")
        if not isinstance(count, int) or count < 1:
            count = DEFAULT_COUNT
        try:
            results = search_results(query.strip(), count)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            log.error("Mistral HTTP %s: %s", exc.code, detail)
            self._send(502, {"error": f"Mistral HTTP {exc.code}", "detail": detail})
            return
        except Exception as exc:
            log.exception("Search failed")
            self._send(502, {"error": str(exc)})
            return
        self._send(200, results)

    def _handle_fetch(self, payload: dict) -> None:
        raw_urls = payload.get("urls")
        if isinstance(payload.get("url"), str):
            raw_urls = [payload["url"]]
        if not isinstance(raw_urls, list) or not raw_urls:
            self._send(400, {"error": "missing 'urls' list"})
            return
        seen: set[str] = set()
        urls = []
        for url in raw_urls:
            if isinstance(url, str) and url.strip() and url not in seen:
                seen.add(url)
                urls.append(url.strip())
        urls = urls[:MAX_URLS]
        try:
            docs = fetch_urls(urls)
        except Exception as exc:
            log.exception("Fetch failed")
            self._send(502, {"error": str(exc)})
            return
        if not docs:
            # Every requested URL failed. Surface it rather than returning
            # 200 [], which Open WebUI would silently turn into empty content.
            self._send(502, {"error": "no content extracted", "urls": urls})
            return
        # Open WebUI iterates the top-level JSON value as the document list.
        self._send(200, docs)

    def log_message(self, fmt, *args):  # quieter, structured-ish logging
        log.info("%s - %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    missing = []
    if not MISTRAL_API_KEY:
        missing.append("MISTRAL_API_KEY / MISTRAL_API_KEY_FILE")
    if not RESEARCH_TOKEN:
        missing.append("RESEARCH_TOKEN / RESEARCH_TOKEN_FILE")
    if not SCOUT_AGENT_ID:
        missing.append("SCOUT_AGENT_ID")
    if missing:
        log.error("Refusing to start: %s required", ", ".join(missing))
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info(
        "research-search listening on 0.0.0.0:%s (agent %s, enrich_top=%d)",
        PORT, SCOUT_AGENT_ID, ENRICH_TOP,
    )
    server.serve_forever()
