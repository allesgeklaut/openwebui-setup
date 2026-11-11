# SPDX-License-Identifier: MIT
"""TinyFish web/news search through the *keyless* MCP endpoint.

Same keyless endpoint OpenCode's WebSearch service uses
(``X-TinyFish-Access-Mode: keyless``): no API key, no signup.  It is a courtesy
quota on shared infrastructure and can be rate-limited.

The MCP tool result is a text part containing a JSON document::

    {"query": "...", "results": [{"title", "url", "snippet", "date", ...}]}

Set ``tinyfish_categ`` to ``news`` in the engine configuration to use the news
index (``domain_type``); the default ``web`` uses the web index.
"""

import json
import typing as t

from dateutil import parser
from searx.extended_types import SXNG_Response
from searx.result_types import EngineResults

if t.TYPE_CHECKING:
    from searx.search.processors import OnlineParams

about = {
    "website": "https://tinyfish.ai",
    "wikidata_id": None,
    "official_api_documentation": "https://docs.tinyfish.ai/search-api",
    "use_official_api": True,
    "require_api_key": False,
    "results": "JSON",
}

categories = ["general"]
paging = False

base_url = "https://agent.tinyfish.ai/mcp"
tinyfish_categ = "web"

_TINYFISH_CATEGORIES = ("web", "news")


def setup(_: dict[str, t.Any]) -> None:
    if tinyfish_categ not in _TINYFISH_CATEGORIES:
        raise ValueError(f"invalid tinyfish_categ: {tinyfish_categ!r} (not in {_TINYFISH_CATEGORIES})")


def request(query: str, params: "OnlineParams") -> None:
    params["url"] = base_url
    params["method"] = "POST"
    params["headers"]["Content-Type"] = "application/json"
    params["headers"]["Accept"] = "application/json, text/event-stream"
    params["headers"]["X-TinyFish-Access-Mode"] = "keyless"
    params["json"] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {"query": query, "domain_type": tinyfish_categ},
        },
    }


def _payload(resp: SXNG_Response) -> dict[str, t.Any]:
    """Return the JSON-RPC payload, unwrapping an SSE frame when present.

    The endpoint sends notification frames before the result frame, so the
    frame carrying ``result`` or ``error`` is the one to return.
    """
    text = resp.text or ""
    payloads: list[dict[str, t.Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                payloads.append(json.loads(line[len("data: ") :]))
            except json.JSONDecodeError:
                continue
    for payload in payloads:
        if "result" in payload or "error" in payload:
            return payload
    if payloads:
        return payloads[-1]
    return json.loads(text)


def _published(value: str):
    if not value:
        return None
    try:
        return parser.parse(value)
    except (parser.ParserError, TypeError, OverflowError):
        return None


def response(resp: SXNG_Response) -> EngineResults:
    res = EngineResults()
    content = (_payload(resp).get("result") or {}).get("content") or []
    if not content:
        return res

    try:
        data = json.loads(content[0].get("text") or "{}")
    except json.JSONDecodeError:
        return res

    for item in data.get("results") or []:
        url = item.get("url")
        if not url:
            continue
        res.add(
            res.types.MainResult(
                url=url,
                title=item.get("title") or url,
                content=item.get("snippet") or "",
                publishedDate=_published(item.get("date") or ""),
            )
        )
    return res
