# SPDX-License-Identifier: MIT
"""Exa web search through the *keyless* MCP endpoint (``mcp.exa.ai``).

This is the same keyless endpoint OpenCode's WebSearch service uses: no API
key, no signup.  It is a courtesy quota on shared infrastructure, so it can be
rate-limited; the keyed :py:mod:`searx.engines.exaapi` engine remains the
primary Exa backend.

The endpoint speaks MCP over Streamable HTTP and answers with SSE frames
(``event: message`` / ``data: {...}``).  The tool result is a single text blob
whose entries are separated by ``\\n\\n---\\n\\n`` and look like::

    Title: ...
    URL: ...
    Published: ...
    Author: ...
    Highlights:
    ...
"""

import json
import typing as t

from dateutil import parser
from searx.extended_types import SXNG_Response
from searx.result_types import EngineResults

if t.TYPE_CHECKING:
    from searx.search.processors import OnlineParams

about = {
    "website": "https://exa.ai",
    "wikidata_id": None,
    "official_api_documentation": "https://docs.exa.ai",
    "use_official_api": True,
    "require_api_key": False,
    "results": "JSON",
}

categories = ["general"]
paging = False
results_per_page = 10

base_url = "https://mcp.exa.ai/mcp"

_SEPARATOR = "\n\n---\n\n"


def request(query: str, params: "OnlineParams") -> None:
    params["url"] = base_url
    params["method"] = "POST"
    params["headers"]["Content-Type"] = "application/json"
    params["headers"]["Accept"] = "application/json, text/event-stream"
    params["json"] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {"query": query, "numResults": results_per_page},
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


def _parse_block(block: str) -> dict[str, t.Any] | None:
    title = ""
    url = ""
    published = ""
    content = ""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Title: "):
            title = line[len("Title: ") :].strip()
        elif line.startswith("URL: "):
            url = line[len("URL: ") :].strip()
        elif line.startswith("Published: "):
            published = line[len("Published: ") :].strip()
        elif line.startswith("Highlights:"):
            content = "\n".join(lines[index + 1 :]).strip()
            break
    if not url:
        return None
    return {
        "url": url,
        "title": title or url,
        "content": content,
        "publishedDate": _published(published),
    }


def response(resp: SXNG_Response) -> EngineResults:
    res = EngineResults()
    content = (_payload(resp).get("result") or {}).get("content") or []
    if not content:
        return res

    body = content[0].get("text") or ""
    for block in body.split(_SEPARATOR):
        item = _parse_block(block)
        if item:
            res.add(res.types.MainResult(**item))
    return res
