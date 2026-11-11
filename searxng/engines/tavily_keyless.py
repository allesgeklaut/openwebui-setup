# SPDX-License-Identifier: MIT
"""Tavily web search through the *keyless* access mode.

Same keyless mode OpenCode's WebSearch service uses
(``X-Tavily-Access-Mode: keyless``): no API key, no signup.  It is a courtesy
quota on shared infrastructure and can be rate-limited.

The POST body is passed via ``params['json']``, so SearXNG's network layer
JSON-encodes it — quotes and backslashes in the query cannot break the
request (unlike the generic ``json_engine``, which formats the body as a
string template).
"""

import typing as t

from dateutil import parser
from searx.extended_types import SXNG_Response
from searx.result_types import EngineResults

if t.TYPE_CHECKING:
    from searx.search.processors import OnlineParams

about = {
    "website": "https://tavily.com",
    "wikidata_id": None,
    "official_api_documentation": "https://docs.tavily.com",
    "use_official_api": True,
    "require_api_key": False,
    "results": "JSON",
}

categories = ["general", "news"]
paging = False
results_per_page = 10

base_url = "https://api.tavily.com/search"
search_depth = "basic"


def request(query: str, params: "OnlineParams") -> None:
    params["url"] = base_url
    params["method"] = "POST"
    params["headers"]["Content-Type"] = "application/json"
    params["headers"]["X-Tavily-Access-Mode"] = "keyless"
    params["json"] = {
        "query": query,
        "max_results": results_per_page,
        "search_depth": search_depth,
    }


def _published(value: str | None):
    if not value:
        return None
    try:
        return parser.parse(value)
    except (parser.ParserError, TypeError, OverflowError):
        return None


def response(resp: SXNG_Response) -> EngineResults:
    res = EngineResults()
    for item in (resp.json() or {}).get("results") or []:
        url = item.get("url")
        if not url:
            continue
        res.add(
            res.types.MainResult(
                url=url,
                title=item.get("title") or url,
                content=item.get("content") or "",
                publishedDate=_published(item.get("published_date")),
            )
        )
    return res
