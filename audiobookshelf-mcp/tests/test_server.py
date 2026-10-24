"""Transport authentication, DNS-rebinding, body-limit, and MCP lifecycle tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from audiobookshelf_mcp.client import MAX_RESPONSE_BYTES
from audiobookshelf_mcp.config import Config
from audiobookshelf_mcp.server import (
    MAX_REQUEST_BODY_BYTES,
    SharedBearerVerifier,
    create_server,
)

# Duplicated here rather than imported from conftest so this module also works
# with pytest import modes that do not expose conftest as a top-level module.
TEST_MCP_TOKEN = "test-client-token-" + "a" * 24
BACKEND_API_KEY = "backend-secret-api-key"

BASE_URL = "http://audiobookshelf-mcp:8000"
INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.0"},
    },
}


def _headers(token: str | None = TEST_MCP_TOKEN) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc_payload(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if "result" in payload or "error" in payload:
                    return payload
        raise AssertionError(f"no JSON-RPC payload in SSE: {response.text!r}")
    return response.json()


@asynccontextmanager
async def _serve(
    config: Config,
    backend: httpx.AsyncBaseTransport,
) -> AsyncIterator[httpx.AsyncClient]:
    server = create_server(config, transport=backend)
    app = server.streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
        ) as http:
            yield http


def _backend_transport(
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if requests is not None:
            requests.append(request)

        if request.url.path == "/api/libraries":
            return httpx.Response(
                200,
                json={
                    "libraries": [
                        {
                            "id": "lib1",
                            "name": "Books",
                            "mediaType": "book",
                            "folders": [{"path": "/private/books"}],
                        },
                        {"id": "lib3", "name": "Hidden", "mediaType": "book"},
                    ]
                },
            )
        if request.url.path == "/api/libraries/lib1/search":
            assert request.url.params["q"] == "dune"
            assert request.url.params["limit"] == "5"
            return httpx.Response(
                200,
                json={
                    "book": [
                        {
                            "libraryItem": {
                                "id": "item1",
                                "libraryId": "lib1",
                                "mediaType": "book",
                                "media": {
                                    "duration": 7200,
                                    "metadata": {
                                        "title": "Dune",
                                        "authors": [{"name": "Frank Herbert"}],
                                        "publishedYear": 1965,
                                    },
                                },
                            }
                        }
                    ]
                },
            )
        if request.url.path == "/api/items/item1":
            return httpx.Response(
                200,
                json={
                    "id": "item1",
                    "libraryId": "lib1",
                    "mediaType": "book",
                    "media": {
                        "metadata": {
                            "title": "Dune",
                            "authors": [{"name": "Frank Herbert"}],
                        }
                    },
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_every_mcp_verb_requires_a_valid_token(
    config_factory: Callable[..., Config],
    method: str,
    token: str | None,
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        if method == "POST":
            response = await http.request(
                method, "/mcp", json=INITIALIZE_REQUEST, headers=headers
            )
        else:
            response = await http.request(method, "/mcp", headers=headers)

    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    assert "resource_metadata" not in response.headers["www-authenticate"]


async def test_correct_token_initializes(
    config_factory: Callable[..., Config],
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        response = await http.post(
            "/mcp", json=INITIALIZE_REQUEST, headers=_headers()
        )

    assert response.status_code == 200
    payload = _rpc_payload(response)
    assert payload["result"]["serverInfo"]["name"] == "audiobookshelf"
    assert payload["result"]["protocolVersion"] == "2025-06-18"
    assert "mcp-session-id" not in response.headers


async def test_host_validation_accepts_default_and_rejects_unknown(
    config_factory: Callable[..., Config],
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        allowed = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers=_headers(),
        )
        rejected = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={**_headers(), "Host": "attacker.example"},
        )
        wrong_port = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={**_headers(), "Host": "localhost:9999"},
        )

    assert allowed.status_code == 200
    assert rejected.status_code == 421
    assert wrong_port.status_code == 421


async def test_origin_is_rejected_when_not_configured(
    config_factory: Callable[..., Config],
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        response = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={**_headers(), "Origin": "https://attacker.example"},
        )

    assert response.status_code == 403


async def test_explicit_origin_allowlist(
    config_factory: Callable[..., Config],
) -> None:
    config = config_factory(allowed_origins=("https://ui.example",))
    async with _serve(config, _backend_transport()) as http:
        allowed = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={**_headers(), "Origin": "https://ui.example"},
        )
        rejected = await http.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={**_headers(), "Origin": "https://other.example"},
        )

    assert allowed.status_code == 200
    assert rejected.status_code == 403


async def test_oversized_request_body_is_rejected(
    config_factory: Callable[..., Config],
) -> None:
    body = b"{" + b" " * MAX_REQUEST_BODY_BYTES + b"}"
    async with _serve(config_factory(), _backend_transport()) as http:
        response = await http.post(
            "/mcp",
            content=body,
            headers=_headers(),
        )

    assert response.status_code == 413
    assert "too large" in response.text


async def test_no_cors_headers(
    config_factory: Callable[..., Config],
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        response = await http.options(
            "/mcp",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


async def test_no_oauth_protected_resource_metadata(
    config_factory: Callable[..., Config],
) -> None:
    async with _serve(config_factory(), _backend_transport()) as http:
        response = await http.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 404


async def test_shared_bearer_verifier() -> None:
    verifier = SharedBearerVerifier(TEST_MCP_TOKEN)

    access = await verifier.verify_token(TEST_MCP_TOKEN)
    assert access is not None
    assert access.scopes == []

    assert await verifier.verify_token(TEST_MCP_TOKEN[:-1]) is None
    assert await verifier.verify_token("") is None
    assert await verifier.verify_token("x" * 32) is None
    assert await verifier.verify_token("\u00e9" * 32) is None
    assert await verifier.verify_token("x" * 40) is None


def _tool_payload(result: Any) -> dict[str, Any]:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def test_streamable_http_tool_lifecycle(
    config_factory: Callable[..., Config],
) -> None:
    requests: list[httpx.Request] = []
    transport = _backend_transport(requests)
    server = create_server(config_factory(), transport=transport)
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "audiobookshelf"

                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "list_libraries",
                        "search_library",
                        "get_item",
                    }

                    libraries_result = await session.call_tool(
                        "list_libraries", {}
                    )
                    assert libraries_result.isError is not True
                    libraries = _tool_payload(libraries_result)
                    assert libraries["count"] == 1
                    assert libraries["libraries"] == [
                        {"id": "lib1", "name": "Books", "media_type": "book"}
                    ]

                    search_result = await session.call_tool(
                        "search_library",
                        {"library_id": "lib1", "query": "dune", "limit": 5},
                    )
                    assert search_result.isError is not True
                    search = _tool_payload(search_result)
                    assert search["items"][0]["title"] == "Dune"
                    assert search["items"][0]["library_id"] == "lib1"

                    item_result = await session.call_tool(
                        "get_item", {"item_id": "item1"}
                    )
                    assert item_result.isError is not True
                    assert _tool_payload(item_result)["authors"] == [
                        "Frank Herbert"
                    ]

    assert requests
    assert all(request.method == "GET" for request in requests)


async def test_disallowed_library_never_reaches_backend(
    config_factory: Callable[..., Config],
) -> None:
    requests: list[httpx.Request] = []
    server = create_server(
        config_factory(), transport=_backend_transport(requests)
    )
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "search_library",
                        {"library_id": "lib3", "query": "dune"},
                    )

    assert result.isError is True
    text = json.dumps([content.model_dump() for content in result.content])
    assert "not permitted" in text
    assert "lib3" not in text
    assert requests == []


async def test_cross_library_item_error_is_redacted(
    config_factory: Callable[..., Config],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/items/item1"
        return httpx.Response(
            200,
            json={
                "id": "item1",
                "libraryId": "lib3",
                "mediaType": "book",
                "media": {"metadata": {"title": "Secret title"}},
            },
        )

    server = create_server(config_factory(), transport=httpx.MockTransport(handler))
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "get_item", {"item_id": "item1"}
                    )

    assert result.isError is True
    text = json.dumps([content.model_dump() for content in result.content])
    assert "not permitted" in text
    assert "Secret title" not in text
    assert "lib3" not in text


async def test_tool_limit_rejects_coercible_values_before_backend(
    config_factory: Callable[..., Config],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"book": []})

    server = create_server(
        config_factory(), transport=httpx.MockTransport(handler)
    )
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    search_tool = next(
                        tool for tool in tools.tools if tool.name == "search_library"
                    )
                    limit_schema = search_tool.inputSchema["properties"]["limit"]
                    assert limit_schema["minimum"] == 1
                    assert limit_schema["maximum"] == 25

                    for bad_limit in (True, 1.0, "2", 1.5, 0, 26):
                        result = await session.call_tool(
                            "search_library",
                            {
                                "library_id": "lib1",
                                "query": "x",
                                "limit": bad_limit,
                            },
                        )
                        assert result.isError is True, bad_limit
                        assert requests == [], bad_limit

                    result = await session.call_tool(
                        "search_library",
                        {"library_id": "lib1", "query": "x", "limit": 5},
                    )
                    assert result.isError is not True
                    assert len(requests) == 1
                    assert requests[0].url.params["limit"] == "5"


async def test_backend_error_body_does_not_reach_tool_output(
    config_factory: Callable[..., Config],
) -> None:
    leak = "private-backend-body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"{leak} {BACKEND_API_KEY} http://abs.test/private",
        )

    server = create_server(config_factory(), transport=httpx.MockTransport(handler))
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "get_item", {"item_id": "item1"}
                    )

    assert result.isError is True
    text = json.dumps([content.model_dump() for content in result.content])
    assert "unavailable" in text
    for secret in (leak, BACKEND_API_KEY, "abs.test", "private"):
        assert secret not in text


async def test_oversized_backend_response_does_not_reach_tool_output(
    config_factory: Callable[..., Config],
) -> None:
    body = b'{"libraries": "' + b"z" * (MAX_RESPONSE_BYTES + 1) + b'"}'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "application/json"},
        )
    )
    server = create_server(config_factory(), transport=transport)
    app = server.streamable_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}"},
        ) as http:
            async with streamable_http_client(
                f"{BASE_URL}/mcp", http_client=http
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("list_libraries", {})

    assert result.isError is True
    text = json.dumps([content.model_dump() for content in result.content])
    assert "size limit" in text
    assert "zzzz" not in text
