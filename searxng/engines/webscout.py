# SPDX-License-Identifier: MIT
"""Web search via the local ``research-search`` service (Mistral "Web Scout").

The stack's own agent-backed search: :ref:`research-search/app.py` serves
``POST /search {"query", "count"}`` → ``[{"link", "title", "snippet"}]``,
running one Web Scout Mistral Studio agent conversation per search (the agent
does the searching, this service harvests the raw tool results). Adding it
here makes SearXNG the single consolidated entry point over every search
backend: the keyed Exa API, the keyless APIs, and the Mistral agent.

Differences from the other engines worth knowing:

- **Cost**: every search is a Mistral agent conversation (billed), unlike the
  free/flat-priced API engines.
- **Latency**: an agent round-trip takes seconds, not milliseconds — this is
  by far the slowest engine on the instance.
- **Privacy**: target sites see Mistral's fetchers, never this host's IP.

The bearer token is injected at container startup by ``entrypoint-wrapper.sh``
(the ``__SCOUT_TOKEN__`` placeholder pattern, same as ``__EXA_API_KEY__``);
the token itself lives only in ``/opt/secrets/research-search.token``.
"""

import typing as t

from searx.extended_types import SXNG_Response
from searx.result_types import EngineResults

if t.TYPE_CHECKING:
    from searx.search.processors import OnlineParams

about = {
    "website": "https://mistral.ai",
    "wikidata_id": None,
    "official_api_documentation": None,
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

categories = ["general"]
paging = False
results_per_page = 10

# SearXNG's default network is https-only (network.initialize pins curl to
# PROTOCOLS_STR=https unless a network/engine opts out), so talking to the
# plain-http internal research-search service requires opting in here.
enable_http = True

# Internal service (same compose network), not reachable from outside.
base_url = "http://research-search:8080/search"

api_key: str | None = None
"""Bearer token for research-search, injected via the __SCOUT_TOKEN__ placeholder."""


def request(query: str, params: "OnlineParams") -> None:
    params["url"] = base_url
    params["method"] = "POST"
    params["headers"]["Content-Type"] = "application/json"
    params["headers"]["Authorization"] = f"Bearer {api_key}"
    params["json"] = {"query": query, "count": results_per_page}


def response(resp: SXNG_Response) -> EngineResults:
    res = EngineResults()
    for item in resp.json() or []:
        if not isinstance(item, dict):
            continue
        url = item.get("link")
        if not url:
            continue
        res.add(
            res.types.MainResult(
                url=url,
                title=(item.get("title") or url).strip(),
                content=item.get("snippet") or "",
            )
        )
    return res
