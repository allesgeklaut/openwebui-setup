"""Backend adapter bounds, projections, and error-redaction tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from audiobookshelf_mcp.client import (
    MAX_RESPONSE_BYTES,
    MAX_SEARCH_LIMIT,
    ABSAuthError,
    ABSClient,
    ABSClientError,
    ABSInvalidRequest,
    ABSNotAllowed,
    ABSNotFound,
    ABSResponseError,
    ABSUnavailable,
)
from audiobookshelf_mcp.config import Config

BACKEND_API_KEY = "backend-secret-api-key"

RequestRecorder = list[httpx.Request]


@pytest.fixture
async def open_client(
    config_factory: Callable[..., Config],
) -> Any:
    clients: list[ABSClient] = []

    def factory(
        transport: httpx.AsyncBaseTransport,
        **overrides: object,
    ) -> ABSClient:
        client = ABSClient(config_factory(**overrides), transport=transport)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


def _transport(
    payload: object,
    *,
    status_code: int = 200,
    requests: RequestRecorder | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.method == "GET"
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _book_item(
    item_id: str = "item1",
    *,
    library_id: str = "lib1",
    title: str = "A Book",
    published_year: object = 2001,
    duration: object = 3600.9,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "libraryId": library_id,
        "mediaType": "book",
        "media": {
            "duration": duration,
            "metadata": {
                "title": title,
                "authors": [{"id": "a1", "name": "Ann Author"}],
                "narrators": ["Nora Narrator"],
                "series": [{"id": "s1", "name": "A Series"}],
                "publishedYear": published_year,
                "description": "private description",
                "asdf": "/private/path",
            },
            "audioFiles": [{"metadata": {"path": "/private/audio.mp3"}}],
        },
    }


async def test_list_libraries_projects_and_filters(open_client: Any) -> None:
    payload = {
        "libraries": [
            {
                "id": "lib1",
                "name": "Books",
                "mediaType": "book",
                "folders": [{"path": "/private/books"}],
                "settings": {"coverAspectRatio": 1},
            },
            {
                "id": "lib2",
                "name": "Podcasts\n",
                "mediaType": "podcast",
                "folders": [{"path": "/private/podcasts"}],
            },
            {"id": "lib3", "name": "Disallowed", "mediaType": "book"},
            {"id": "../escape", "name": "Bad ID", "mediaType": "book"},
            "not-an-object",
        ]
    }

    client = open_client(_transport(payload))
    libraries = await client.list_libraries()

    assert libraries == [
        {"id": "lib1", "name": "Books", "media_type": "book"},
        {"id": "lib2", "name": "Podcasts", "media_type": "podcast"},
    ]
    rendered = json.dumps(libraries)
    assert "/private" not in rendered
    assert "folders" not in rendered


async def test_list_libraries_rejects_unexpected_shape(open_client: Any) -> None:
    client = open_client(_transport({"libraries": "nope"}))

    with pytest.raises(ABSResponseError, match="unexpected response"):
        await client.list_libraries()


async def test_search_projects_mixed_libraries(open_client: Any) -> None:
    payload = {
        "book": [{"libraryItem": _book_item()}],
        "podcast": [
            {
                "libraryItem": {
                    "id": "pod1",
                    "libraryId": "lib2",
                    "mediaType": "podcast",
                    "media": {
                        "metadata": {
                            "title": "A Podcast",
                            "author": "The Host",
                            "feedUrl": "https://private.example/feed?token=x",
                            "imageUrl": "https://private.example/image",
                            "description": "private",
                        }
                    },
                }
            },
            {
                "libraryItem": {
                    "id": "pod-disallowed",
                    "libraryId": "lib3",
                    "mediaType": "podcast",
                    "media": {"metadata": {"title": "Do not disclose"}},
                }
            },
        ],
        "episodes": [{"libraryItem": _book_item("episode1")}],
    }

    client = open_client(_transport(payload))
    items = await client.search_library("lib1", "a", 5)

    assert [item["id"] for item in items] == ["item1"]
    assert items[0] == {
        "id": "item1",
        "library_id": "lib1",
        "media_type": "book",
        "title": "A Book",
        "authors": ["Ann Author"],
        "narrators": ["Nora Narrator"],
        "series": ["A Series"],
        "published_year": 2001,
        "duration_seconds": 3600,
    }

    rendered = json.dumps(items)
    for forbidden in (
        "private",
        "feedUrl",
        "imageUrl",
        "description",
        "audioFiles",
        "/private",
        "lib2",
        "lib3",
        "pod1",
        "A Podcast",
        "Do not disclose",
    ):
        assert forbidden not in rendered


async def test_search_excludes_other_allowed_library_results(
    open_client: Any,
) -> None:
    payload = {
        "book": [
            {"libraryItem": _book_item("item1", library_id="lib1")},
            {
                "libraryItem": _book_item(
                    "item2", library_id="lib2", title="Other library title"
                )
            },
        ]
    }

    client = open_client(_transport(payload))
    items = await client.search_library("lib1", "a", 5)

    assert [item["id"] for item in items] == ["item1"]
    rendered = json.dumps(items)
    assert "item2" not in rendered
    assert "lib2" not in rendered
    assert "Other library title" not in rendered


async def test_search_reads_installed_string_published_year(
    open_client: Any,
) -> None:
    payload = {"book": [{"libraryItem": _book_item(published_year="1965")}]}
    client = open_client(_transport(payload))

    items = await client.search_library("lib1", "a", 5)

    assert items[0]["published_year"] == 1965


async def test_search_caps_results_and_accepts_unwrapped_items(
    open_client: Any,
) -> None:
    payload = {
        "book": [
            {
                "libraryItem": _book_item(f"book{index}", title=f"Book {index}")
            }
            for index in range(3)
        ],
        "podcast": [
            {
                "id": "podcast1",
                "libraryId": "lib1",
                "mediaType": "podcast",
                "media": {"metadata": {"title": "Podcast"}},
            }
        ],
    }

    client = open_client(_transport(payload))
    items = await client.search_library("lib1", "book", 2)

    assert [item["id"] for item in items] == ["book0", "book1"]
    assert all(item["library_id"] == "lib1" for item in items)


async def test_search_filters_items_with_explicit_disallowed_library(
    open_client: Any,
) -> None:
    payload = {
        "book": [
            {"libraryItem": _book_item("ok", library_id="lib1")},
            {"libraryItem": _book_item("bad", library_id="lib3")},
        ]
    }

    client = open_client(_transport(payload))
    items = await client.search_library("lib1", "book", 10)

    assert [item["id"] for item in items] == ["ok"]


async def test_search_drops_missing_or_wrong_shaped_library_ids(
    open_client: Any,
) -> None:
    missing = _book_item("missing")
    del missing["libraryId"]
    wrong_shape = _book_item("wrong-shape", library_id="../lib1")
    allowed = _book_item("allowed", library_id="lib1")
    payload = {
        "book": [
            {"libraryItem": missing},
            {"libraryItem": wrong_shape},
            {"libraryItem": allowed},
        ]
    }

    client = open_client(_transport(payload))
    items = await client.search_library("lib1", "book", 10)

    assert [item["id"] for item in items] == ["allowed"]


async def test_search_requires_a_known_result_section(open_client: Any) -> None:
    client = open_client(_transport({"authors": []}))

    with pytest.raises(ABSResponseError, match="unexpected response"):
        await client.search_library("lib1", "x", 5)


@pytest.mark.parametrize(
    ("library_id", "query", "limit", "error"),
    [
        ("lib3", "x", 5, ABSNotAllowed),
        ("../lib1", "x", 5, ABSInvalidRequest),
        ("lib.1", "x", 5, ABSInvalidRequest),
        ("lib/1", "x", 5, ABSInvalidRequest),
        ("lib1", "", 5, ABSInvalidRequest),
        ("lib1", "   ", 5, ABSInvalidRequest),
        ("lib1", "x" * 201, 5, ABSInvalidRequest),
        ("lib1", "bad\x00query", 5, ABSInvalidRequest),
        ("lib1", "bad\tquery", 5, ABSInvalidRequest),
        ("lib1", "bad\nquery", 5, ABSInvalidRequest),
        ("lib1", "bad\x7fquery", 5, ABSInvalidRequest),
        ("lib1", "   ", 5, ABSInvalidRequest),
        ("lib1", "x", 0, ABSInvalidRequest),
        ("lib1", "x", MAX_SEARCH_LIMIT + 1, ABSInvalidRequest),
    ],
)
async def test_search_rejects_bad_arguments_before_backend(
    open_client: Any,
    library_id: str,
    query: str,
    limit: int,
    error: type[ABSClientError],
) -> None:
    requests: RequestRecorder = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = open_client(httpx.MockTransport(handler))
    with pytest.raises(error):
        await client.search_library(library_id, query, limit)
    assert requests == []


@pytest.mark.parametrize("limit", [True, False, 5.0, "5"])
async def test_search_rejects_non_integer_limit(
    open_client: Any, limit: object
) -> None:
    requests: RequestRecorder = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = open_client(httpx.MockTransport(handler))
    with pytest.raises(ABSInvalidRequest, match="integer"):
        await client.search_library("lib1", "x", limit)  # type: ignore[arg-type]
    assert requests == []


async def test_get_item_returns_projected_item(open_client: Any) -> None:
    requests: RequestRecorder = []
    client = open_client(_transport(_book_item(), requests=requests))

    item = await client.get_item("item1")

    assert item["id"] == "item1"
    assert item["title"] == "A Book"
    assert item["library_id"] == "lib1"
    assert item["published_year"] == 2001
    assert len(requests) == 1
    assert requests[0].url.path == "/api/items/item1"
    assert requests[0].url.params["expanded"] == "1"
    assert requests[0].headers["Authorization"] == f"Bearer {BACKEND_API_KEY}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1965, 1965),
        ("1965", 1965),
        (1000, 1000),
        ("1000", 1000),
        (9999, 9999),
        ("9999", 9999),
    ],
)
async def test_get_item_reads_valid_published_year_representations(
    open_client: Any, value: object, expected: int
) -> None:
    client = open_client(_transport(_book_item(published_year=value)))

    item = await client.get_item("item1")

    assert item["published_year"] == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        999,
        10000,
        1965.0,
        "1965.0",
        " 1965",
        "1965 ",
        "19a5",
        "١٩٦٥",
        "１９６５",
        "0000",
        "0999",
        "10000",
        "",
        None,
    ],
)
async def test_get_item_rejects_invalid_published_year_values(
    open_client: Any, value: object
) -> None:
    client = open_client(_transport(_book_item(published_year=value)))

    item = await client.get_item("item1")

    assert item["published_year"] is None


async def test_get_item_cross_library_is_rejected(open_client: Any) -> None:
    payload = _book_item("item1", library_id="lib3", title="Secret title")
    client = open_client(_transport(payload))

    with pytest.raises(ABSNotAllowed, match="not permitted") as raised:
        await client.get_item("item1")

    assert "Secret title" not in str(raised.value)
    assert "lib3" not in str(raised.value)


async def test_get_item_missing_library_id_is_rejected(open_client: Any) -> None:
    payload = _book_item()
    del payload["libraryId"]
    client = open_client(_transport(payload))

    with pytest.raises(ABSNotAllowed):
        await client.get_item("item1")


@pytest.mark.parametrize(
    "item_id", ["../item", "a/b", "a.b", "item?x=1", "", "x" * 129]
)
async def test_get_item_rejects_malformed_ids_before_backend(
    open_client: Any, item_id: str
) -> None:
    requests: RequestRecorder = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = open_client(httpx.MockTransport(handler))
    with pytest.raises(ABSInvalidRequest):
        await client.get_item(item_id)
    assert requests == []


async def test_redirect_is_not_followed(open_client: Any) -> None:
    requests: RequestRecorder = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "http://attacker.example/steal"},
        )

    client = open_client(httpx.MockTransport(handler))
    with pytest.raises(ABSResponseError) as raised:
        await client.list_libraries()

    assert "attacker" not in str(raised.value)
    assert "http" not in str(raised.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status_code", "error", "message"),
    [
        (401, ABSAuthError, "rejected the configured API key"),
        (403, ABSNotFound, "not found"),
        (404, ABSNotFound, "not found"),
        (500, ABSUnavailable, "unavailable"),
        (418, ABSResponseError, "unexpected response"),
    ],
)
async def test_error_responses_are_redacted(
    open_client: Any,
    status_code: int,
    error: type[ABSClientError],
    message: str,
) -> None:
    leak = f"{BACKEND_API_KEY} http://abs.test/private/body"
    client = open_client(
        _transport({"error": leak}, status_code=status_code)
    )

    with pytest.raises(error, match=message) as raised:
        await client.list_libraries()

    assert BACKEND_API_KEY not in str(raised.value)
    assert "http" not in str(raised.value)
    assert "private" not in str(raised.value)


async def test_get_item_404_maps_to_generic_not_found(open_client: Any) -> None:
    client = open_client(_transport({"error": "missing"}, status_code=404))

    with pytest.raises(ABSNotAllowed, match="not permitted"):
        await client.get_item("item1")


@pytest.mark.parametrize("status_code", [403, 404])
async def test_item_forbidden_and_missing_share_safe_output(
    open_client: Any, status_code: int
) -> None:
    leak = "secret backend body"
    client = open_client(
        httpx.MockTransport(
            lambda request: httpx.Response(status_code, text=leak)
        )
    )

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_item("item1")

    assert str(raised.value) == "Item not found or not permitted."
    assert leak not in str(raised.value)


async def test_get_item_small_disallowed_library_is_generic(
    open_client: Any,
) -> None:
    client = open_client(
        _transport(_book_item(library_id="lib3", title="Secret library title"))
    )

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_item("item1")

    assert str(raised.value) == "Item not found or not permitted."
    assert "Secret library title" not in str(raised.value)


@pytest.mark.parametrize("library_id", ["lib1", "lib3"])
async def test_get_item_oversized_payload_is_generic_not_permitted(
    open_client: Any, library_id: str
) -> None:
    payload = _book_item(
        library_id=library_id, title="Oversized private title"
    )
    payload["media"]["metadata"]["description"] = "x" * MAX_RESPONSE_BYTES
    client = open_client(_transport(payload))

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_item("item1")

    rendered = str(raised.value)
    assert rendered == "Item not found or not permitted."
    assert "Oversized private title" not in rendered
    assert "xxxx" not in rendered


async def test_get_item_malformed_payload_is_generic_not_permitted(
    open_client: Any,
) -> None:
    leak = "malformed-private-body"
    client = open_client(
        httpx.MockTransport(lambda request: httpx.Response(200, text=leak))
    )

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_item("item1")

    assert str(raised.value) == "Item not found or not permitted."
    assert leak not in str(raised.value)


async def test_get_item_other_error_status_is_generic_not_permitted(
    open_client: Any,
) -> None:
    client = open_client(_transport({"error": "leak"}, status_code=418))

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_item("item1")

    assert str(raised.value) == "Item not found or not permitted."
    assert "leak" not in str(raised.value)


@pytest.mark.parametrize(
    ("status_code", "error"),
    [(401, ABSAuthError), (500, ABSUnavailable)],
)
async def test_get_item_keeps_auth_and_backend_errors_separate(
    open_client: Any, status_code: int, error: type[ABSClientError]
) -> None:
    client = open_client(_transport({"error": "backend"}, status_code=status_code))

    with pytest.raises(error) as raised:
        await client.get_item("item1")

    assert str(raised.value) != "Item not found or not permitted."


async def test_get_item_huge_duration_integer_returns_none(
    open_client: Any,
) -> None:
    client = open_client(_transport(_book_item(duration=10**400)))

    item = await client.get_item("item1")

    assert item["duration_seconds"] is None


async def test_search_oversized_response_remains_explicit(
    open_client: Any,
) -> None:
    body = b'{"book": "' + b"z" * (MAX_RESPONSE_BYTES + 1) + b'"}'
    client = open_client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        )
    )

    with pytest.raises(ABSResponseError, match="size limit"):
        await client.search_library("lib1", "a", 5)


async def test_oversized_response_is_rejected(open_client: Any) -> None:
    body = b'{"libraries": "' + b"x" * (MAX_RESPONSE_BYTES + 1) + b'"}'
    client = open_client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        )
    )

    with pytest.raises(ABSResponseError, match="size limit") as raised:
        await client.list_libraries()

    assert "xxxx" not in str(raised.value)


class _UnboundedStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> Any:
        yield b'{"libraries": "'
        yield b"y" * MAX_RESPONSE_BYTES


async def test_oversized_response_without_content_length_is_rejected(
    open_client: Any,
) -> None:
    client = open_client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=_UnboundedStream())
        )
    )

    with pytest.raises(ABSResponseError, match="size limit") as raised:
        await client.list_libraries()

    assert "yyyy" not in str(raised.value)


async def test_total_request_timeout_bounds_streaming(
    open_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "audiobookshelf_mcp.client.REQUEST_TIMEOUT_SECONDS", 0.05
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"libraries": []})

    client = open_client(httpx.MockTransport(handler))

    with pytest.raises(ABSUnavailable, match="timed out"):
        await client.list_libraries()


async def test_timeout_is_mapped_without_backend_details(
    open_client: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out at {request.url}")

    client = open_client(httpx.MockTransport(handler))

    with pytest.raises(ABSUnavailable, match="timed out") as raised:
        await client.list_libraries()

    assert "abs.test" not in str(raised.value)
    assert "http" not in str(raised.value)


async def test_connection_error_is_mapped_without_backend_details(
    open_client: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    client = open_client(httpx.MockTransport(handler))

    with pytest.raises(ABSUnavailable, match="unavailable") as raised:
        await client.list_libraries()

    assert "abs.test" not in str(raised.value)


async def test_malformed_json_is_rejected_without_echo(open_client: Any) -> None:
    leak = "not-json-" + BACKEND_API_KEY
    client = open_client(
        httpx.MockTransport(lambda request: httpx.Response(200, text=leak))
    )

    with pytest.raises(ABSResponseError, match="unexpected response") as raised:
        await client.list_libraries()

    assert "not-json" not in str(raised.value)
    assert BACKEND_API_KEY not in str(raised.value)


async def test_http_client_hardening(open_client: Any) -> None:
    client = open_client(httpx.MockTransport(lambda request: httpx.Response(200)))
    http_client = client._client

    assert http_client.follow_redirects is False
    assert http_client.trust_env is False
    assert http_client.timeout.connect == 10.0
    assert http_client.timeout.read == 10.0
    assert http_client.timeout.write == 10.0
    assert http_client.timeout.pool == 10.0
    assert http_client.headers["Accept-Encoding"] == "identity"


async def test_text_projection_is_bounded(open_client: Any) -> None:
    payload = _book_item(
        title="  Title\nwith\u200bcontrols  " + "x" * 400,
    )
    client = open_client(_transport(payload))

    item = await client.get_item("item1")

    assert len(item["title"]) <= 300
    assert "\n" not in item["title"]
    assert "\u200b" not in item["title"]
    assert item["title"].startswith("Title with controls")
