"""Exa-backed external web loader for Open WebUI.

Open WebUI's `external` web loader POSTs {"urls": [...]} with an
`Authorization: Bearer <token>` header and expects a JSON list of
{"page_content", "metadata"} back.  This tiny adapter forwards those URLs to
Exa's /contents endpoint (clean, article-style text extraction) and reshapes
the response.  It lets Open WebUI reuse the same Exa key that already powers
web search, giving URL fetch the same quality instead of the built-in
BeautifulSoup get_text() boilerplate dump.

Stdlib only -- no third-party dependencies.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(stream=sys.stdout, level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("exa-loader")


def _env_int(name: str, default: int | None, *, minimum: int = 1) -> int | None:
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


EXA_API_URL = os.environ.get("EXA_API_URL", "https://api.exa.ai/contents")
EXA_TIMEOUT = _env_float("EXA_TIMEOUT", 90.0, minimum=0.1)
# Optional per-page character cap. Unset = let Exa return the full text.
EXA_TEXT_MAX_CHARS = _env_int("EXA_TEXT_MAX_CHARS", None)
PORT = _env_int("PORT", 8080) or 8080
MAX_URLS = _env_int("MAX_URLS", 100) or 100
MAX_BODY_BYTES = _env_int("MAX_BODY_BYTES", 2 * 1024 * 1024) or 2 * 1024 * 1024


def _read_secret(value: str | None, file_env: str | None) -> str:
    if file_env:
        path = os.environ.get(file_env)
        if path:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            log.error("%s points at %s, which does not exist", file_env, path)
    return (value or "").strip()


EXA_API_KEY = _read_secret(os.environ.get("EXA_API_KEY"), "EXA_API_KEY_FILE")
LOADER_TOKEN = _read_secret(os.environ.get("LOADER_TOKEN"), "LOADER_TOKEN_FILE")
# Bytes form: HTTP headers are decoded latin-1, so they may contain non-ASCII
# characters, and hmac.compare_digest rejects str operands with non-ASCII chars.
_EXPECTED_AUTHORIZATION = f"Bearer {LOADER_TOKEN}".encode("utf-8")


def fetch_urls(urls: list[str]) -> list[dict]:
    """Call Exa /contents and reshape results into Open WebUI Documents."""
    if not EXA_API_KEY:
        raise RuntimeError("EXA_API_KEY is not configured")

    payload: dict = {"urls": urls}
    if EXA_TEXT_MAX_CHARS is not None:
        payload["text"] = {"maxCharacters": EXA_TEXT_MAX_CHARS}
    else:
        payload["text"] = True

    req = urllib.request.Request(
        EXA_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {EXA_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=EXA_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    by_url = {r.get("url"): r for r in (data.get("results") or []) if isinstance(r, dict)}

    docs = []
    for url in urls:
        result = by_url.get(url)
        if not result:
            log.warning("Exa returned no result for %s", url)
            continue
        text = result.get("text") or ""
        if not text.strip():
            log.warning("Exa returned empty text for %s", url)
            continue
        docs.append(
            {
                "page_content": text,
                "metadata": {
                    "source": result.get("url") or url,
                    "url": result.get("url") or url,
                    "title": result.get("title") or url,
                    "author": result.get("author"),
                    "publishedDate": result.get("publishedDate"),
                },
            }
        )
    return docs


class Handler(BaseHTTPRequestHandler):
    server_version = "exa-loader/1.0"

    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/health", "/healthz"):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/", "/contents"):
            self._send(404, {"error": "not found"})
            return

        if LOADER_TOKEN:
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode("utf-8", "surrogateescape"), _EXPECTED_AUTHORIZATION):
                log.warning("Rejected request with bad/missing token")
                self._send(401, {"error": "unauthorized"})
                return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return

        try:
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body or b"{}")
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        raw_urls = payload.get("urls")
        if isinstance(payload.get("url"), str):
            raw_urls = [payload["url"]]
        if not isinstance(raw_urls, list) or not raw_urls:
            self._send(400, {"error": "missing 'urls' list"})
            return

        seen: set[str] = set()
        urls = []
        for url in raw_urls:
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                urls.append(url)
        urls = urls[:MAX_URLS]

        try:
            docs = fetch_urls(urls)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            log.error("Exa HTTP %s: %s", exc.code, detail)
            self._send(502, {"error": f"Exa HTTP {exc.code}", "detail": detail})
            return
        except Exception as exc:
            log.exception("Failed to fetch via Exa")
            self._send(502, {"error": str(exc)})
            return

        if not docs:
            # Every requested URL failed or yielded no text. Surface it rather
            # than returning 200 [], which Open WebUI would silently turn into
            # empty content.
            self._send(502, {"error": "no content extracted", "urls": urls})
            return

        # Open WebUI iterates the top-level JSON value as the document list.
        self._send(200, docs)

    def log_message(self, fmt, *args):  # quieter, structured-ish logging
        log.info("%s - %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    missing = []
    if not EXA_API_KEY:
        missing.append("EXA_API_KEY / EXA_API_KEY_FILE")
    if not LOADER_TOKEN:
        missing.append("LOADER_TOKEN / LOADER_TOKEN_FILE")
    if missing:
        log.error("Refusing to start: %s required", ", ".join(missing))
        sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("exa-loader listening on 0.0.0.0:%s (Exa: %s)", PORT, EXA_API_URL)
    server.serve_forever()
