"""Synthetic tests for bounded podcast reads, writes, and refusals.

Every backend response is local and synthetic. The URL guard is stubbed by
default so tests never resolve or contact a real host; the guard's own behavior
is tested with synthetic monkeypatches or in its dedicated test module.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from audiobookshelf_mcp import podcasts
from audiobookshelf_mcp.client import (
    ABSClientError,
    ABSInvalidRequest,
    ABSNotAllowed,
    ABSNotFound,
    ABSResponseError,
)
from audiobookshelf_mcp.podcasts import PodcastClient

FEED_URL = "https://feeds.example/show.xml"
BACKEND_SECRET = "backend-secret-body"
FEED_ID_RE = re.compile(r"\Afeed_[0-9a-f]{64}\Z")

RouteValue = dict[str, Any] | list[Any] | httpx.Response | Callable[[httpx.Request], httpx.Response]
Routes = dict[tuple[str, str], RouteValue]


@pytest.fixture(autouse=True)
def allow_public_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", allow)


@pytest.fixture
async def podcast_factory(config_factory: Callable[..., Any]) -> Any:
    clients: list[PodcastClient] = []

    def make(
        transport: httpx.AsyncBaseTransport,
        *,
        writes: bool = True,
        **overrides: object,
    ) -> PodcastClient:
        client = PodcastClient(
            config_factory(podcast_writes_enabled=writes, **overrides),
            transport=transport,
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


def _make_transport(routes: Routes) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(route):
            return route(request)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    return httpx.MockTransport(handler), requests


def _episode(
    episode_id: str,
    published: object,
    *,
    guid: object = None,
    url: str | None = None,
    duration: object = 61.5,
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "title": f"Title {episode_id}",
        "publishedAt": published,
        "duration": duration,
        "guid": guid if guid is not None else f"guid-{episode_id}",
        "enclosure": {"url": url or f"https://cdn.example/{episode_id}.mp3"},
        "audioFile": {"metadata": {"path": f"/podcasts/pod1/{episode_id}.mp3"}},
    }


def _queue_entry(guid: str, url: str) -> dict[str, Any]:
    return {
        "id": f"queue-{guid}",
        "guid": guid,
        "url": url,
        "episodeDisplayTitle": "Queued",
        "libraryItemId": "pod1",
    }


def _podcast(
    podcast_id: str = "pod1",
    *,
    library_id: str = "lib1",
    feed_url: object = FEED_URL,
    episodes: list[dict[str, Any]] | None = None,
    **media_overrides: object,
) -> dict[str, Any]:
    media: dict[str, Any] = {
        "metadata": {
            "title": "My Show",
            "feedUrl": feed_url,
            "imageUrl": "https://images.example/private-cover.jpg",
            "description": "private description",
        },
        "autoDownloadEpisodes": False,
        "autoDownloadSchedule": "0 */6 * * *",
        "maxEpisodesToKeep": 0,
        "maxNewEpisodesToDownload": 3,
        "episodes": list(episodes or []),
    }
    media.update(media_overrides)
    return {
        "id": podcast_id,
        "libraryId": library_id,
        "mediaType": "podcast",
        "media": media,
        "folderId": "folder1",
        "path": f"/podcasts/{podcast_id}",
    }


def _podcast_library(
    library_id: str = "lib2",
    *,
    media_type: str = "podcast",
    folders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": library_id,
        "mediaType": media_type,
        "name": "Podcasts",
        "folders": folders
        if folders is not None
        else [{"id": "folder1", "fullPath": "/podcasts", "libraryId": library_id}],
    }


def _feed_episode(
    guid: str,
    url: str,
    published: int,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "title": title or f"Episode {guid}",
        "guid": guid,
        "publishedAt": published,
        "enclosure": {"url": url, "type": "audio/mpeg", "length": "12345"},
        "description": "private feed description",
    }


def _feed(*episodes: dict[str, Any], title: str = "Feed Show") -> dict[str, Any]:
    return {
        "podcast": {
            "metadata": {
                "title": title,
                "author": "Feed Author",
                "feedUrl": "https://ignored.example/backend-feed.xml",
                "imageUrl": "https://images.example/cover.jpg",
                "description": "private feed description",
                "explicit": "false",
            },
            "episodes": list(episodes),
        }
    }


def _raw_json(payload: dict[str, Any]) -> httpx.Response:
    """Serialize a fixture to ASCII JSON so lone surrogates survive creation.

    ``httpx.Response(json=...)`` re-encodes the payload itself and fails on a
    lone surrogate, so the escape sequence must be built and sent as raw bytes.
    """
    return httpx.Response(
        200,
        content=json.dumps(payload).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )


def _posts(requests: list[httpx.Request], path: str) -> list[httpx.Request]:
    return [
        request
        for request in requests
        if request.method == "POST" and request.url.path == path
    ]


def _method_paths(requests: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(request.method, request.url.path) for request in requests]


def _delete_handler(
    item: dict[str, Any],
    *,
    fail: dict[str, httpx.Response] | None = None,
    after: Callable[[str], None] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Apply native row removal so fixtures match backend state."""

    def handler(request: httpx.Request) -> httpx.Response:
        episode_id = request.url.path.rsplit("/", 1)[-1]
        if fail is not None and episode_id in fail:
            return fail[episode_id]
        item["media"]["episodes"] = [
            episode
            for episode in item["media"]["episodes"]
            if episode["id"] != episode_id
        ]
        if after is not None:
            after(episode_id)
        return httpx.Response(204)

    return handler


def _retention_routes(
    item: dict[str, Any],
    *,
    fail: dict[str, httpx.Response] | None = None,
    after_delete: Callable[[str], None] | None = None,
    library_route: Callable[[httpx.Request], httpx.Response] | None = None,
    podcast_id: str = "pod1",
    library_id: str = "lib1",
) -> Routes:
    """Native-like retention fixture: the PATCH applies and DELETEs drop rows."""
    handler = _delete_handler(item, fail=fail, after=after_delete)

    def patch(request: httpx.Request) -> httpx.Response:
        item["media"].update(json.loads(request.content))
        return httpx.Response(200, json={})

    def default_library(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_podcast_library(library_id))

    routes: Routes = {
        ("GET", f"/api/items/{podcast_id}"): lambda request: httpx.Response(
            200, json=item
        ),
        ("GET", f"/api/libraries/{library_id}"): library_route or default_library,
        ("PATCH", f"/api/items/{podcast_id}/media"): patch,
    }
    for episode in list(item["media"]["episodes"]):
        routes[
            ("DELETE", f"/api/podcasts/{podcast_id}/episode/{episode['id']}")
        ] = handler
    return routes


async def test_mutations_reject_when_writes_flag_disabled(podcast_factory: Any) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport, writes=False)

    calls = [
        lambda: client.add_podcast("lib2", FEED_URL),
        lambda: client.download_latest_episodes("pod1", 5),
        lambda: client.set_podcast_schedule(
            "pod1", True, "0 */6 * * *", 5, expected_fingerprint="f" * 64
        ),
        lambda: client.set_podcast_retention(
            "pod1", 5, True, expected_fingerprint="f" * 64
        ),
        lambda: client.delete_podcast_episode(
            "pod1", "ep1", expected_fingerprint="f" * 64
        ),
        lambda: client.delete_podcast("pod1", expected_fingerprint="f" * 64),
    ]
    for call in calls:
        with pytest.raises(ABSNotAllowed, match="disabled"):
            await call()

    assert requests == []


@pytest.mark.parametrize("case", ["book", "unlisted-library"])
async def test_state_rejects_nonpodcast_and_cross_library_items(
    podcast_factory: Any, case: str
) -> None:
    item = _podcast()
    if case == "book":
        item["mediaType"] = "book"
        item["media"]["metadata"]["title"] = "Secret Book Title"
    else:
        item["libraryId"] = "lib3"

    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_podcast_state("pod1")

    assert str(raised.value) == "Podcast not found or not permitted."
    assert "Secret Book Title" not in str(raised.value)
    assert _method_paths(requests) == [("GET", "/api/items/pod1")]


async def test_add_podcast_creates_uuid_folder_and_minimal_payload(
    podcast_factory: Any,
) -> None:
    created = _podcast("newpod", library_id="lib2", feed_url=FEED_URL)
    created["media"]["metadata"]["title"] = "New Show"
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(),
        ("GET", "/api/libraries/lib2/items"): {"results": [], "total": 0},
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("g1", "https://cdn.example/ep1.mp3", 1000),
            title="New Show",
        ),
        ("POST", "/api/podcasts"): created,
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.add_podcast("lib2", FEED_URL)

    assert result == {
        "podcast_id": "newpod",
        "library_id": "lib2",
        "title": "New Show",
        "status": "created",
    }
    feed_request = _posts(requests, "/api/podcasts/feed")[0]
    assert json.loads(feed_request.content) == {"rssFeed": FEED_URL}

    create_request = _posts(requests, "/api/podcasts")[0]
    payload = json.loads(create_request.content)
    assert payload["libraryId"] == "lib2"
    assert payload["folderId"] == "folder1"
    assert payload["tags"] == []
    assert payload["media"]["autoDownloadEpisodes"] is False
    assert payload["media"]["metadata"] == {
        "title": "New Show",
        "feedUrl": FEED_URL,
        "author": "Feed Author",
    }
    leaf = payload["path"].rsplit("/", 1)[1]
    uuid.UUID(leaf)
    assert "New Show" not in payload["path"]
    assert "/podcasts/" in payload["path"] + "/"
    assert "imageUrl" not in create_request.content.decode()
    assert not any("opml" in path.lower() for _, path in _method_paths(requests))


async def test_add_podcast_normalized_duplicate_avoids_feed_fetch(
    podcast_factory: Any,
) -> None:
    duplicate = _podcast(
        "existing1",
        library_id="lib2",
        feed_url="HTTPS://Feeds.Example:443/show.xml",
    )
    duplicate["media"]["metadata"]["title"] = "Already There"
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(),
        ("GET", "/api/libraries/lib2/items"): {
            "results": [duplicate],
            "total": 1,
        },
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.add_podcast("lib2", FEED_URL)

    assert result == {
        "podcast_id": "existing1",
        "library_id": "lib2",
        "title": "Already There",
        "status": "already_exists",
    }
    assert _posts(requests, "/api/podcasts/feed") == []
    assert _posts(requests, "/api/podcasts") == []
    page_request = requests[-1]
    assert page_request.url.params["minified"] == "1"
    assert page_request.url.params["limit"] == "100"
    assert page_request.url.params["page"] == "0"


async def test_add_podcast_duplicate_check_paginates(podcast_factory: Any) -> None:
    def filler(page: int) -> list[dict[str, Any]]:
        return [
            {
                "id": f"book{page}{index}",
                "libraryId": "lib2",
                "mediaType": "book",
                "media": {"metadata": {}},
            }
            for index in range(100)
        ]

    page_one = filler(1)
    page_one[0] = _podcast("dup2", library_id="lib2")
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(),
        ("GET", "/api/libraries/lib2/items"): lambda request: httpx.Response(
            200,
            json={
                "results": filler(0) if request.url.params["page"] == "0" else page_one,
                "total": 250,
            },
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.add_podcast("lib2", FEED_URL)

    assert result["status"] == "already_exists"
    pages = [
        request.url.params["page"]
        for request in requests
        if request.url.path == "/api/libraries/lib2/items"
    ]
    assert pages == ["0", "1"]
    assert _posts(requests, "/api/podcasts/feed") == []


@pytest.mark.parametrize(
    "items_payload",
    [
        {"results": [], "total": 1001},
        {"results": [], "total": "0"},
        {"results": "nope", "total": 0},
        {"results": [], "total": 10},
    ],
)
async def test_add_podcast_aborts_when_duplicate_check_cannot_finish(
    podcast_factory: Any, items_payload: dict[str, Any]
) -> None:
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(),
        ("GET", "/api/libraries/lib2/items"): items_payload,
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSResponseError, match="duplicate"):
        await client.add_podcast("lib2", FEED_URL)

    assert _posts(requests, "/api/podcasts/feed") == []
    assert _posts(requests, "/api/podcasts") == []


async def test_add_podcast_requires_actual_podcast_library(
    podcast_factory: Any,
) -> None:
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(media_type="book"),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed, match="Podcast library not found"):
        await client.add_podcast("lib2", FEED_URL)

    assert _method_paths(requests) == [("GET", "/api/libraries/lib2")]


async def test_add_podcast_requires_feed_title_and_safe_folder(
    podcast_factory: Any,
) -> None:
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library(),
        ("GET", "/api/libraries/lib2/items"): {"results": [], "total": 0},
        ("POST", "/api/podcasts/feed"): _feed(title="   "),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSResponseError, match="usable title"):
        await client.add_podcast("lib2", FEED_URL)

    assert _posts(requests, "/api/podcasts") == []

    bad_library = _podcast_library(
        folders=[{"id": "folder1", "fullPath": "/podcasts/../escape"}]
    )
    routes2: Routes = {
        ("GET", "/api/libraries/lib2"): bad_library,
    }
    transport2, requests2 = _make_transport(routes2)
    client2 = podcast_factory(transport2)

    with pytest.raises(ABSResponseError, match="usable folder"):
        await client2.add_podcast("lib2", FEED_URL)

    assert _posts(requests2, "/api/podcasts/feed") == []
    assert _posts(requests2, "/api/podcasts") == []


async def test_add_podcast_url_guard_rejects_before_backend_fetch(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deny(url: str) -> str:
        raise ABSInvalidRequest("Remote URL host is not publicly routable.")

    monkeypatch.setattr(podcasts, "ensure_public_url", deny)
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.add_podcast("lib2", "https://private.example/feed")

    assert requests == []


async def test_download_selects_newest_and_skips_known_episodes(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "ep1",
                published=9000,
                guid="g1",
                url="https://cdn.example/downloaded.mp3",
            )
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("g2", "https://cdn.example/queued.mp3")
    ]
    item["episodesDownloading"] = [
        _queue_entry("g3", "https://cdn.example/current.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("g1", "https://cdn.example/downloaded.mp3", 9000),
            _feed_episode("g2", "https://cdn.example/queued.mp3", 8000),
            _feed_episode("g3", "https://cdn.example/current.mp3", 7000),
            _feed_episode("g5", "https://cdn.example/new5.mp3", 6000, "Newest"),
            _feed_episode("g5", "https://cdn.example/new5.mp3", 5500, "Dup"),
            _feed_episode("g4", "https://cdn.example/new4.mp3", 5000, "Older"),
            _feed_episode("g0", "https://cdn.example/old.mp3", 1000, "Old"),
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 2)

    assert result["status"] == "queued"
    assert result["queued_count"] == 2
    assert [episode["title"] for episode in result["episodes"]] == [
        "Newest",
        "Older",
    ]
    rendered = json.dumps(result)
    for forbidden in (
        "cdn.example",
        "feeds.example",
        "private",
        "/private",
        "g1",
        "g2",
        "g3",
    ):
        assert forbidden not in rendered

    post = _posts(requests, "/api/podcasts/pod1/download-episodes")[0]
    typed = json.loads(post.content)
    assert [episode["guid"] for episode in typed] == ["g5", "g4"]
    assert set(typed[0]) == {"title", "guid", "publishedAt", "enclosure"}
    assert set(typed[0]["enclosure"]) == {"url", "type", "length"}
    assert typed[0]["publishedAt"] == 6000
    assert not any("checknew" in path or "clear-queue" in path for _, path in _method_paths(requests))


async def test_download_nothing_new_sends_no_mutation(podcast_factory: Any) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "ep1", published=2000, guid="g1", url="https://cdn.example/one.mp3"
            )
        ]
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("g1", "https://cdn.example/one.mp3", 2000)
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 5)

    assert result == {
        "podcast_id": "pod1",
        "status": "nothing_new",
        "queued_count": 0,
        "episodes": [],
    }
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_download_validates_every_enclosure_before_post(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("bad", "https://private.example/bad.mp3", 3000, "Bad"),
            _feed_episode("good", "https://cdn.example/good.mp3", 2000, "Good"),
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    async def guard(url: str) -> str:
        if "private.example" in url:
            raise ABSInvalidRequest("Remote URL host is not publicly routable.")
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.download_latest_episodes("pod1", 2)

    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://cdn.example/a%zz.mp3",
        "https://cdn.example/a%FF.mp3",
    ],
)
async def test_download_rejects_native_invalid_percent_enclosures_before_post(
    podcast_factory: Any, bad_url: str
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode(
                "good", "https://cdn.example/a%20b.mp3", 3000, "Valid Encoded"
            ),
            _feed_episode("bad", bad_url, 2000, "Bad Encoding"),
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSInvalidRequest, match="percent encoding") as raised:
        await client.download_latest_episodes("pod1", 2)

    assert bad_url not in str(raised.value)
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_download_accepts_valid_percent_encoded_enclosure(
    podcast_factory: Any,
) -> None:
    encoded_url = "https://cdn.example/a%20b.mp3"
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("encoded", encoded_url, 2000, "Encoded")
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 1)

    assert result["status"] == "queued"
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    sent = json.loads(posts[0].content)
    assert sent[0]["enclosure"]["url"] == encoded_url


@pytest.mark.parametrize("field", ["guid", "type", "length"])
async def test_download_skips_feed_entry_with_lone_surrogate_metadata(
    podcast_factory: Any, field: str
) -> None:
    episode = _feed_episode(
        "g1", "https://cdn.example/a%20b.mp3", 2000, "Bad Metadata"
    )
    if field == "guid":
        episode["guid"] = "\ud800"
    else:
        episode["enclosure"][field] = "\ud800"
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _raw_json(_feed(episode)),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 1)

    assert result["status"] == "nothing_new"
    assert result["queued_count"] == 0
    assert result["episodes"] == []
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_download_keeps_valid_multibyte_guid_unchanged(
    podcast_factory: Any,
) -> None:
    guid = "guid-\u00e9-\U0001f4a9"
    episode = _feed_episode(
        "multibyte", "https://cdn.example/a%20b.mp3", 2000, "Multibyte"
    )
    episode["guid"] = guid
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _raw_json(_feed(episode)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 1)

    assert result["status"] == "queued"
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert json.loads(posts[0].content)[0]["guid"] == guid


async def test_request_body_surrogate_is_fixed_error_not_encoding_crash(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSClientError) as raised:
        await client._request_json(
            "POST",
            "api/podcasts/pod1/download-episodes",
            json_body=[{"guid": "\ud800"}],
            allow_empty=True,
        )

    assert isinstance(raised.value, ABSResponseError)
    assert not isinstance(raised.value, UnicodeEncodeError)
    assert str(raised.value) == "Audiobookshelf returned an unexpected response."
    assert requests == []


async def test_download_rejects_typed_inputs_before_backend(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    for count in (0, 26, True, "2", 2.0):
        with pytest.raises(ABSInvalidRequest, match="integer"):
            await client.download_latest_episodes("pod1", count)  # type: ignore[arg-type]
    assert requests == []


async def test_download_revalidates_feed_url_before_feed_fetch(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    async def deny(url: str) -> str:
        raise ABSInvalidRequest("Remote URL host is not publicly routable.")

    monkeypatch.setattr(podcasts, "ensure_public_url", deny)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.download_latest_episodes("pod1", 1)

    assert _method_paths(requests) == [("GET", "/api/items/pod1")]


async def test_download_latest_rereads_and_skips_newly_known(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): lambda request: httpx.Response(
            200, json=item
        ),
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(3)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    mutated: set[str] = set()

    async def guard(url: str) -> str:
        # Enclosure preflight happens after the feed fetch and before the
        # shared late re-read; mutate there to simulate an external writer.
        if "3.mp3" in url and "queue" not in mutated:
            mutated.add("queue")
            item["episodeDownloadsQueued"] = [
                _queue_entry("g3", "https://cdn.example/3.mp3")
            ]
        if "2.mp3" in url and "downloaded" not in mutated:
            mutated.add("downloaded")
            item["media"]["episodes"] = [
                _episode(
                    "epLate",
                    2000,
                    guid="g2",
                    url="https://cdn.example/2.mp3",
                )
            ]
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    result = await client.download_latest_episodes("pod1", 3)

    assert result["status"] == "queued"
    assert result["queued_count"] == 1
    assert [episode["title"] for episode in result["episodes"]] == [
        "Episode 1"
    ]
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    sent = json.loads(posts[0].content)
    assert [episode["guid"] for episode in sent] == ["g1"]
    assert [episode["guid"] for episode in sent].count("g2") == 0
    item_reads = [
        request
        for request in requests
        if request.method == "GET" and request.url.path == "/api/items/pod1"
    ]
    assert len(item_reads) == 2


async def test_download_latest_fixed_batch_is_not_backfilled_after_late_skip(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(5)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    async def guard(url: str) -> str:
        if "5.mp3" in url:
            item["episodeDownloadsQueued"] = [
                _queue_entry("g5", "https://cdn.example/5.mp3")
            ]
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    result = await client.download_latest_episodes("pod1", 2)

    assert result["queued_count"] == 1
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert [episode["guid"] for episode in json.loads(posts[0].content)] == [
        "g4"
    ]
    assert result["episodes"] == [
        {"title": "Episode 4", "published_at": "1970-01-01T00:00:04+00:00"}
    ]


async def test_download_latest_all_late_known_returns_nothing_new(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(2)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    async def guard(url: str) -> str:
        item["episodesDownloading"] = [
            _queue_entry("g1", "https://cdn.example/1.mp3"),
            _queue_entry("g2", "https://cdn.example/2.mp3"),
        ]
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    result = await client.download_latest_episodes("pod1", 2)

    assert result == {
        "podcast_id": "pod1",
        "status": "nothing_new",
        "queued_count": 0,
        "episodes": [],
    }
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


@pytest.mark.parametrize(
    ("case", "expected_error", "message"),
    [
        ("feed", ABSResponseError, "changed before download"),
        ("library", ABSResponseError, "changed before download"),
        ("path", ABSResponseError, "changed before download"),
        ("folder", ABSResponseError, "changed before download"),
        ("media_type", ABSNotAllowed, "not found or not permitted"),
        ("media_missing", ABSNotAllowed, "not found or not permitted"),
        ("out_of_scope", ABSNotAllowed, "not found or not permitted"),
    ],
)
async def test_download_latest_fails_closed_when_scope_changes_late(
    podcast_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): lambda request: httpx.Response(
            200, json=item
        ),
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(1)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    async def guard(url: str) -> str:
        if case == "feed":
            item["media"]["metadata"]["feedUrl"] = (
                "https://feeds.example/changed.xml"
            )
        elif case == "library":
            item["libraryId"] = "lib2"
        elif case == "path":
            item["path"] = "/podcasts/other"
        elif case == "folder":
            item["folderId"] = "folder2"
        elif case == "media_type":
            item["mediaType"] = "book"
        elif case == "media_missing":
            item["media"] = None
        else:
            item["libraryId"] = "lib3"
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    with pytest.raises(expected_error, match=message):
        await client.download_latest_episodes("pod1", 1)

    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_disable_patches_only_the_flag_and_preserves_zero_limit(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        autoDownloadEpisodes=True,
        autoDownloadSchedule="0 */6 * * *",
        maxNewEpisodesToDownload=0,
        maxEpisodesToKeep=5,
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_schedule(
        "pod1",
        False,
        "0 */6 * * *",
        0,
        expected_fingerprint=fingerprint,
    )

    patch = [
        request
        for request in requests
        if request.method == "PATCH"
        and request.url.path == "/api/items/pod1/media"
    ]
    assert len(patch) == 1
    assert json.loads(patch[0].content) == {"autoDownloadEpisodes": False}
    assert result["podcast_id"] == "pod1"


async def test_enable_patches_policy_after_feed_guard(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast(
        autoDownloadEpisodes=False,
        autoDownloadSchedule="0 */6 * * *",
        maxEpisodesToKeep=5,
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    checked: list[str] = []

    async def guard(url: str) -> str:
        checked.append(url)
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    await client.set_podcast_schedule(
        "pod1",
        True,
        "0 */6 * * *",
        10,
        expected_fingerprint=fingerprint,
    )

    assert checked == [FEED_URL]
    patch = [
        request
        for request in requests
        if request.method == "PATCH"
        and request.url.path == "/api/items/pod1/media"
    ]
    assert json.loads(patch[0].content) == {
        "autoDownloadEpisodes": True,
        "autoDownloadSchedule": "0 */6 * * *",
        "maxNewEpisodesToDownload": 10,
    }


async def test_enable_rejects_nonpublic_existing_feed_before_patch(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    async def deny(url: str) -> str:
        raise ABSInvalidRequest("Remote URL host is not publicly routable.")

    monkeypatch.setattr(podcasts, "ensure_public_url", deny)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.set_podcast_schedule(
            "pod1",
            True,
            "0 */6 * * *",
            5,
            expected_fingerprint=fingerprint,
        )

    assert not any(request.method == "PATCH" for request in requests)


async def test_schedule_typed_inputs_and_stale_fingerprint_never_patch(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    invalid_calls = [
        lambda: client.set_podcast_schedule(
            "pod1", 1, "0 */6 * * *", 5, expected_fingerprint=fingerprint  # type: ignore[arg-type]
        ),
        lambda: client.set_podcast_schedule(
            "pod1", True, "*/5 * * * *", 5, expected_fingerprint=fingerprint
        ),
        lambda: client.set_podcast_schedule(
            "pod1", True, "0 */6 * * *", 0, expected_fingerprint=fingerprint
        ),
        lambda: client.set_podcast_schedule(
            "pod1", True, "0 */6 * * *", 26, expected_fingerprint=fingerprint
        ),
        lambda: client.set_podcast_schedule(
            "pod1", True, "0 */6 * * *", 5, expected_fingerprint=None  # type: ignore[arg-type]
        ),
        lambda: client.set_podcast_schedule(
            "pod1",
            True,
            "0 */6 * * *",
            5,
            expected_fingerprint="0" * 64,
        ),
    ]
    for call in invalid_calls:
        with pytest.raises(ABSInvalidRequest):
            await call()

    assert not any(request.method == "PATCH" for request in requests)

    with pytest.raises(ABSInvalidRequest, match="retry with current settings"):
        await client.set_podcast_schedule(
            "pod1",
            True,
            "0 */6 * * *",
            5,
            expected_fingerprint="0" * 64,
        )


async def test_retention_policy_only_never_deletes_and_reports_note(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    routes: Routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, False, expected_fingerprint=fingerprint
    )

    patches = [request for request in requests if request.method == "PATCH"]
    assert [json.loads(request.content) for request in patches] == [
        {"maxEpisodesToKeep": 1}
    ]
    assert not any(request.method == "DELETE" for request in requests)
    assert result["status"] == "updated"
    assert result["deleted_count"] == 0
    assert "not removed immediately" in result["note"]
    assert result["state"]["keep_episodes"] == 1
    assert result["state"]["queued_download_count"] == 0
    assert result["state"]["active_download_count"] == 0


async def test_retention_trim_deletes_oldest_exact_episode_ids(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    routes: Routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    deletes = [
        (request.url.path, request.url.params.get("hard"))
        for request in requests
        if request.method == "DELETE"
    ]
    assert deletes == [
        ("/api/podcasts/pod1/episode/ep1", "1"),
        ("/api/podcasts/pod1/episode/ep2", "1"),
    ]
    assert result["status"] == "completed"
    assert result["deleted_count"] == 2
    assert result["deleted_episode_ids"] == ["ep1", "ep2"]
    assert result["state"]["downloaded_count"] == 1
    assert result["state"]["keep_episodes"] == 1
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]


async def test_retention_trim_aborts_before_patch_over_100_deletions(
    podcast_factory: Any,
) -> None:
    episodes = [
        _episode(f"ep{index:03d}", published=1000 + index) for index in range(102)
    ]
    item = _podcast(episodes=episodes)
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSInvalidRequest, match="more than 100"):
        await client.set_podcast_retention(
            "pod1", 1, True, expected_fingerprint=fingerprint
        )

    assert not any(request.method in {"PATCH", "DELETE"} for request in requests)


async def test_retention_partial_failure_reports_honest_count_and_stops(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )

    def fail_ep2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=BACKEND_SECRET)

    routes: Routes = _retention_routes(
        item, fail={"ep2": httpx.Response(500, text=BACKEND_SECRET)}
    )
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    deletes = [
        request.url.path for request in requests if request.method == "DELETE"
    ]
    assert deletes == [
        "/api/podcasts/pod1/episode/ep1",
        "/api/podcasts/pod1/episode/ep2",
    ]
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "unavailable" in result["error"]
    assert result["state"]["downloaded_count"] == 2
    assert BACKEND_SECRET not in json.dumps(result)


async def test_retention_trim_refused_while_downloads_active_but_policy_allowed(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("queued", "https://cdn.example/queued.mp3")
    ]
    routes: Routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="queued or active"):
        await client.set_podcast_retention(
            "pod1", 1, True, expected_fingerprint=fingerprint
        )
    assert not any(request.method == "PATCH" for request in requests)

    result = await client.set_podcast_retention(
        "pod1", 1, False, expected_fingerprint=fingerprint
    )
    assert result["status"] == "updated"
    assert result["state"]["queued_download_count"] == 1
    assert result["state"]["active_download_count"] == 0


async def test_retention_typed_inputs_and_stale_fingerprint_never_patch(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    invalid_calls = [
        lambda: client.set_podcast_retention(
            "pod1", True, False, expected_fingerprint=fingerprint  # type: ignore[arg-type]
        ),
        lambda: client.set_podcast_retention(
            "pod1", -1, False, expected_fingerprint=fingerprint
        ),
        lambda: client.set_podcast_retention(
            "pod1", 1001, False, expected_fingerprint=fingerprint
        ),
        lambda: client.set_podcast_retention(
            "pod1", 5, 1, expected_fingerprint=fingerprint  # type: ignore[arg-type]
        ),
        lambda: client.set_podcast_retention(
            "pod1", 0, True, expected_fingerprint=fingerprint
        ),
    ]
    for call in invalid_calls:
        with pytest.raises(ABSInvalidRequest):
            await call()

    with pytest.raises(ABSInvalidRequest, match="retry with current settings"):
        await client.set_podcast_retention(
            "pod1", 1, True, expected_fingerprint="0" * 64
        )
    assert not any(request.method in {"PATCH", "DELETE"} for request in requests)


def _retention_drift(
    item: dict[str, Any],
    library_holder: dict[str, Any],
    case: str,
) -> None:
    """Apply one external change immediately after an acknowledged DELETE."""
    if case == "library-type":
        library_holder["payload"]["mediaType"] = "book"
    elif case == "library-folder":
        library_holder["payload"]["folders"] = [
            {"id": "folder1", "fullPath": "/different-root", "libraryId": "lib1"}
        ]
    elif case == "item-path":
        item["path"] = "/podcasts/other"
    elif case == "folder-id":
        item["folderId"] = "folder2"
    elif case == "feed":
        item["media"]["metadata"]["feedUrl"] = "https://feeds.example/changed.xml"
    elif case == "policy":
        item["media"]["maxEpisodesToKeep"] = 5
    elif case == "queue":
        item["episodesDownloading"] = [
            _queue_entry("gx", "https://cdn.example/x.mp3")
        ]
    elif case == "media-type":
        item["mediaType"] = "book"
    elif case == "library-id":
        item["libraryId"] = "lib2"
    elif case == "target-removed":
        item["media"]["episodes"] = [
            episode
            for episode in item["media"]["episodes"]
            if episode["id"] != "ep2"
        ]
    elif case == "episode-outside":
        _episode_by_id(item, "ep2")["audioFile"] = {
            "metadata": {"path": "/elsewhere/ep2.mp3"}
        }
    elif case == "episode-renamed":
        _episode_by_id(item, "ep2")["audioFile"] = {
            "metadata": {"path": "/podcasts/pod1/renamed.mp3"}
        }
    elif case == "extra-downloaded":
        item["media"]["episodes"].append(_episode("epX", 500))
    else:  # pragma: no cover - parametrization is explicit
        raise AssertionError(case)


def _break_podcast_state(item: dict[str, Any], field: str) -> None:
    """Replace one native field with a value the client must reject."""
    if field == "episodes":
        item["media"]["episodes"] = "bad"
    else:
        item[field] = "bad"


def _episode_by_id(item: dict[str, Any], episode_id: str) -> dict[str, Any]:
    return next(
        episode
        for episode in item["media"]["episodes"]
        if episode["id"] == episode_id
    )


async def test_retention_trim_reproducer_combined_drift_after_first_ack(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    library_holder: dict[str, Any] = {"payload": _podcast_library("lib1")}

    def after_delete(episode_id: str) -> None:
        if episode_id != "ep1":
            return
        _retention_drift(item, library_holder, "library-type")
        _retention_drift(item, library_holder, "episode-outside")
        _retention_drift(item, library_holder, "queue")

    routes = _retention_routes(
        item,
        after_delete=after_delete,
        library_route=lambda request: httpx.Response(
            200, json=library_holder["payload"]
        ),
    )
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    deletes = [request.url.path for request in requests if request.method == "DELETE"]
    assert deletes == ["/api/podcasts/pod1/episode/ep1"]
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "queued or active" in result["error"]
    assert "no files were changed" not in result["error"]
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]
    # The policy was written exactly once; no rollback or retry.
    patches = [request for request in requests if request.method == "PATCH"]
    assert len(patches) == 1


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("library-type", "Podcast library not found"),
        ("library-folder", "this target was not deleted"),
        ("item-path", "changed during deletion"),
        ("folder-id", "changed during deletion"),
        ("feed", "changed during deletion"),
        ("policy", "changed during deletion"),
        ("queue", "queued or active"),
        ("media-type", "not found or not permitted"),
        ("library-id", "changed during deletion"),
        ("target-removed", "changed during deletion"),
        ("episode-outside", "changed during deletion"),
        ("episode-renamed", "changed during deletion"),
        ("extra-downloaded", "changed during deletion"),
    ],
)
async def test_retention_trim_stops_on_late_drift_with_honest_count(
    podcast_factory: Any, case: str, message: str
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    library_holder: dict[str, Any] = {"payload": _podcast_library("lib1")}

    def after_delete(episode_id: str) -> None:
        if episode_id == "ep1":
            _retention_drift(item, library_holder, case)

    routes = _retention_routes(
        item,
        after_delete=after_delete,
        library_route=lambda request: httpx.Response(
            200, json=library_holder["payload"]
        ),
    )
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    deletes = [request.url.path for request in requests if request.method == "DELETE"]
    assert deletes == ["/api/podcasts/pod1/episode/ep1"]
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert message in result["error"]
    assert "no files were changed" not in result["error"]
    assert result["filesystem_removal"] == "not_verified"
    assert len([r for r in requests if r.method == "PATCH"]) == 1


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("policy", "changed during deletion"),
        ("feed", "changed during deletion"),
        ("queue", "queued or active"),
        ("path", "changed during deletion"),
        ("folder", "changed during deletion"),
        ("target-identity", "changed during deletion"),
        ("library-folder", "this target was not deleted"),
        ("library-type", "Podcast library not found"),
    ],
)
async def test_retention_trim_post_patch_drift_stops_before_first_delete(
    podcast_factory: Any, case: str, message: str
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    library_holder: dict[str, Any] = {"payload": _podcast_library("lib1")}
    routes = _retention_routes(
        item,
        library_route=lambda request: httpx.Response(
            200, json=library_holder["payload"]
        ),
    )

    def patch(request: httpx.Request) -> httpx.Response:
        item["media"].update(json.loads(request.content))
        # An external change lands after our own PATCH answered but before
        # the read-back and the first DELETE.
        if case == "policy":
            item["media"]["maxEpisodesToKeep"] = 5
        elif case == "feed":
            item["media"]["metadata"]["feedUrl"] = (
                "https://feeds.example/changed.xml"
            )
        elif case == "queue":
            item["episodesDownloading"] = [
                _queue_entry("gx", "https://cdn.example/x.mp3")
            ]
        elif case == "path":
            item["path"] = "/podcasts/other"
        elif case == "folder":
            item["folderId"] = "folder2"
        elif case == "target-identity":
            _episode_by_id(item, "ep1")["audioFile"] = {
                "metadata": {"path": "/podcasts/pod1/renamed.mp3"}
            }
        elif case == "library-folder":
            library_holder["payload"]["folders"] = [
                {
                    "id": "folder1",
                    "fullPath": "/different-root",
                    "libraryId": "lib1",
                }
            ]
        else:
            library_holder["payload"]["mediaType"] = "book"
        return httpx.Response(200, json={})

    routes[("PATCH", "/api/items/pod1/media")] = patch
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    assert not any(request.method == "DELETE" for request in requests)
    assert result["status"] == "partial"
    assert result["deleted_count"] == 0
    assert result["deleted_episode_ids"] == []
    assert result["failed_episode_id"] == "ep1"
    assert message in result["error"]
    assert "no files were changed" not in result["error"]
    assert "may already have been applied" in result["note"]
    assert len([r for r in requests if r.method == "PATCH"]) == 1


async def test_retention_trim_post_patch_read_failure_is_not_retried(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", published=1000), _episode("ep2", published=2000)]
    )
    routes = _retention_routes(item)
    reads = {"count": 0}

    def failing_item_route(request: httpx.Request) -> httpx.Response:
        reads["count"] += 1
        # First read is the caller's fingerprint; second is the pre-PATCH
        # baseline. The post-PATCH read-back fails.
        if reads["count"] <= 2:
            return httpx.Response(200, json=item)
        return httpx.Response(500, text=BACKEND_SECRET)

    routes[("GET", "/api/items/pod1")] = failing_item_route
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    assert len([r for r in requests if r.method == "PATCH"]) == 1
    assert not any(request.method == "DELETE" for request in requests)
    assert result["status"] == "partial"
    assert result["deleted_count"] == 0
    assert result["deleted_episode_ids"] == []
    assert result["failed_episode_id"] == "ep1"
    assert "unavailable" in result["error"]
    assert result["state"] is None
    assert BACKEND_SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "field",
    ["episodeDownloadsQueued", "episodesDownloading", "episodes"],
)
async def test_retention_trim_post_patch_malformed_state_returns_partial_before_delete(
    podcast_factory: Any, field: str
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )
    routes = _retention_routes(item)

    def patch(request: httpx.Request) -> httpx.Response:
        item["media"].update(json.loads(request.content))
        _break_podcast_state(item, field)
        return httpx.Response(200, json={})

    routes[("PATCH", "/api/items/pod1/media")] = patch
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    assert not any(request.method == "DELETE" for request in requests)
    assert [
        json.loads(request.content)
        for request in requests
        if request.method == "PATCH"
    ] == [{"maxEpisodesToKeep": 1}]
    assert result["status"] == "partial"
    assert result["deleted_count"] == 0
    assert result["deleted_episode_ids"] == []
    assert result["failed_episode_id"] == "ep1"
    assert "unexpected response" in result["error"]
    assert "no files were changed" not in result["error"]
    assert "unchanged" not in result["error"].lower()
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]
    assert result["state"] is None


async def test_retention_trim_readback_failure_after_all_acks_keeps_deletes(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )

    def after_delete(episode_id: str) -> None:
        if episode_id == "ep2":
            _break_podcast_state(item, "episodeDownloadsQueued")

    routes = _retention_routes(item, after_delete=after_delete)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    assert [
        request.url.path for request in requests if request.method == "DELETE"
    ] == [
        "/api/podcasts/pod1/episode/ep1",
        "/api/podcasts/pod1/episode/ep2",
    ]
    assert len([request for request in requests if request.method == "PATCH"]) == 1
    assert result["status"] == "partial"
    assert result["deleted_count"] == 2
    assert result["deleted_episode_ids"] == ["ep1", "ep2"]
    assert "failed_episode_id" not in result
    assert "unexpected response" in result["error"]
    assert "no files were changed" not in result["error"]
    assert "unchanged" not in result["error"].lower()
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]
    assert result["state"] is None


async def test_retention_trim_readback_failure_after_first_ack_keeps_delete(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )

    def after_delete(episode_id: str) -> None:
        if episode_id == "ep1":
            _break_podcast_state(item, "episodes")

    routes = _retention_routes(item, after_delete=after_delete)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 2, True, expected_fingerprint=fingerprint
    )

    assert [
        request.url.path for request in requests if request.method == "DELETE"
    ] == ["/api/podcasts/pod1/episode/ep1"]
    assert len([request for request in requests if request.method == "PATCH"]) == 1
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert "failed_episode_id" not in result
    assert "unexpected response" in result["error"]
    assert "no files were changed" not in result["error"]
    assert "unchanged" not in result["error"].lower()
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]
    assert result["state"] is None


async def test_retention_trim_malformed_state_after_first_ack_stops_second_delete(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep2", published=2000),
            _episode("ep3", published=3000),
        ]
    )

    def after_delete(episode_id: str) -> None:
        if episode_id == "ep1":
            _break_podcast_state(item, "episodesDownloading")

    routes = _retention_routes(item, after_delete=after_delete)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 1, True, expected_fingerprint=fingerprint
    )

    assert [
        request.url.path for request in requests if request.method == "DELETE"
    ] == ["/api/podcasts/pod1/episode/ep1"]
    assert len([request for request in requests if request.method == "PATCH"]) == 1
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "unexpected response" in result["error"]
    assert "no files were changed" not in result["error"]
    assert "may already have been applied" in result["note"]
    assert "not retried" in result["note"]
    assert result["state"] is None


async def test_retention_policy_only_malformed_readback_returns_updated_without_state(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes = _retention_routes(item)

    def patch(request: httpx.Request) -> httpx.Response:
        item["media"].update(json.loads(request.content))
        _break_podcast_state(item, "episodes")
        return httpx.Response(200, json={})

    routes[("PATCH", "/api/items/pod1/media")] = patch
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 5, False, expected_fingerprint=fingerprint
    )

    assert not any(request.method == "DELETE" for request in requests)
    assert len([request for request in requests if request.method == "PATCH"]) == 1
    assert result["status"] == "updated"
    assert result["deleted_count"] == 0
    assert result["state"] is None
    assert "Existing episodes are not removed immediately" in result["note"]


async def test_retention_trim_rejects_duplicate_rows_before_patch(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000),
            _episode("ep1", published=2000),
            _episode("ep2", published=3000),
        ]
    )
    routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="ambiguous"):
        await client.set_podcast_retention(
            "pod1", 1, True, expected_fingerprint=fingerprint
        )

    assert not any(
        request.method in {"PATCH", "DELETE"} for request in requests
    )


async def test_retention_trim_rejects_duplicate_kept_prefix_before_patch(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("epA", published=4000),
            _episode("epA", published=3500),
            _episode("epB", published=3000),
            _episode("epC", published=2000),
        ]
    )
    routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    # Without the whole-set duplicate check, epC and epB would be deleted
    # while the duplicated epA rows consume the keep budget.
    with pytest.raises(ABSResponseError, match="ambiguous"):
        await client.set_podcast_retention(
            "pod1", 2, True, expected_fingerprint=fingerprint
        )

    assert not any(
        request.method in {"PATCH", "DELETE"} for request in requests
    )


async def test_retention_trim_rejects_duplicate_rows_without_surplus(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("epA", published=4000),
            _episode("epA", published=3500),
        ]
    )
    routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="ambiguous"):
        await client.set_podcast_retention(
            "pod1", 5, True, expected_fingerprint=fingerprint
        )

    assert not any(
        request.method in {"PATCH", "DELETE"} for request in requests
    )


async def test_retention_trim_with_no_surplus_keeps_policy_note(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", published=1000), _episode("ep2", published=2000)]
    )
    routes = _retention_routes(item)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.set_podcast_retention(
        "pod1", 5, True, expected_fingerprint=fingerprint
    )

    assert [json.loads(request.content) for request in requests if request.method == "PATCH"] == [
        {"maxEpisodesToKeep": 5}
    ]
    assert not any(request.method == "DELETE" for request in requests)
    assert result["status"] == "completed"
    assert result["deleted_count"] == 0
    assert result["deleted_episode_ids"] == []
    assert "failed_episode_id" not in result
    assert "may already have been applied" in result["note"]
    assert result["state"]["keep_episodes"] == 5
    assert result["state"]["downloaded_count"] == 2


async def test_state_is_safe_and_fingerprint_tracks_feed_and_queue(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", published=1000, guid="g1"),
            _episode("ep2", published=2000, guid="g2"),
        ]
    )
    route_holder: dict[str, Any] = {"item": item}

    def state_route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=route_holder["item"])

    transport, requests = _make_transport(
        {("GET", "/api/items/pod1"): state_route}
    )
    client = podcast_factory(transport)

    state = await client.get_podcast_state("pod1")

    assert set(state) == {
        "podcast_id",
        "library_id",
        "auto_download",
        "schedule",
        "max_new_episodes",
        "keep_episodes",
        "downloaded_count",
        "queued_download_count",
        "active_download_count",
        "fingerprint",
    }
    assert state["downloaded_count"] == 2
    assert state["queued_download_count"] == 0
    assert state["active_download_count"] == 0
    assert len(state["fingerprint"]) == 64
    assert requests[0].url.params["include"] == "downloads"

    rendered = json.dumps(state)
    for forbidden in (
        FEED_URL,
        "images.example",
        "private description",
        "/private",
        "g1",
    ):
        assert forbidden not in rendered

    route_holder["item"] = _podcast(
        feed_url="https://feeds.example/changed.xml",
        episodes=item["media"]["episodes"],
    )
    changed_feed = await client.get_podcast_state("pod1")
    assert changed_feed["fingerprint"] != state["fingerprint"]

    queued = _podcast(
        episodes=item["media"]["episodes"],
    )
    queued["episodeDownloadsQueued"] = [
        _queue_entry("g9", "https://cdn.example/queued.mp3")
    ]
    queued["episodesDownloading"] = [
        _queue_entry("g10", "https://cdn.example/active1.mp3"),
        _queue_entry("g11", "https://cdn.example/active2.mp3"),
    ]
    route_holder["item"] = queued
    with_queue = await client.get_podcast_state("pod1")
    assert with_queue["fingerprint"] != state["fingerprint"]
    assert with_queue["queued_download_count"] == 1
    assert with_queue["active_download_count"] == 2
    queue_rendered = json.dumps(with_queue)
    for forbidden in (
        "cdn.example",
        "queue-g9",
        "queue-g10",
        "queue-g11",
        "https",
    ):
        assert forbidden not in queue_rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("episodeDownloadsQueued", {}),
        ("episodeDownloadsQueued", [1]),
        ("episodesDownloading", "nope"),
        ("episodesDownloading", [None]),
    ],
)
async def test_state_fails_closed_on_malformed_queue_fields(
    podcast_factory: Any, field: str, value: object
) -> None:
    item = _podcast()
    item[field] = value
    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    with pytest.raises(
        ABSResponseError,
        match="Audiobookshelf returned an unexpected response",
    ):
        await client.get_podcast_state("pod1")

    assert len(requests) == 1


async def test_list_episodes_sorted_newest_first_nulls_last_and_safe(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep0", published=None),
            _episode("ep1", published=1000, duration=10.9),
            _episode("ep2", published=3000, duration=20),
            _episode("ep3", published=2000),
        ]
    )
    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    result = await client.list_podcast_episodes("pod1", 2)

    assert result["count"] == 2
    assert [episode["id"] for episode in result["episodes"]] == ["ep2", "ep3"]
    assert result["episodes"][0]["duration_seconds"] == 20
    rendered = json.dumps(result)
    assert "cdn.example" not in rendered
    assert "/private" not in rendered

    all_episodes = await client.list_podcast_episodes("pod1", 50)
    assert [episode["id"] for episode in all_episodes["episodes"]] == [
        "ep2",
        "ep3",
        "ep1",
        "ep0",
    ]
    assert all_episodes["episodes"][-1]["published_at"] is None

    with pytest.raises(ABSInvalidRequest):
        await client.list_podcast_episodes("pod1", True)  # type: ignore[arg-type]
    with pytest.raises(ABSInvalidRequest):
        await client.list_podcast_episodes("pod1", 51)


@pytest.mark.parametrize("limit", [True, 0, 51, "5", 1.0])
async def test_list_episode_limit_rejects_typed_values_before_backend(
    podcast_factory: Any, limit: object
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSInvalidRequest):
        await client.list_podcast_episodes("pod1", limit)  # type: ignore[arg-type]
    assert requests == []


# --- Podcast library listing ---------------------------------------------


def _library_podcast(
    podcast_id: str,
    *,
    library_id: str = "lib2",
    title: str = "Show",
    num_episodes: object = 3,
) -> dict[str, Any]:
    return {
        "id": podcast_id,
        "libraryId": library_id,
        "mediaType": "podcast",
        "media": {
            "metadata": {
                "title": title,
                "feedUrl": "https://feeds.example/private.xml",
                "imageUrl": "https://images.example/private.jpg",
                "description": "private description",
            },
            "numEpisodes": num_episodes,
            "episodes": [{"id": "leaky-episode-id"}],
            "duration": 1234,
        },
        "path": "/podcasts/private-id",
        "folderId": "folder1",
    }


def _list_routes(results: object, total: object) -> Routes:
    return {
        ("GET", "/api/libraries/lib2"): _podcast_library("lib2"),
        ("GET", "/api/libraries/lib2/items"): {
            "results": results,
            "total": total,
        },
    }


async def test_list_podcasts_returns_bounded_safe_page(
    podcast_factory: Any,
) -> None:
    long_title = "x" * 305
    results = [
        _library_podcast("podA", title="  My\u0000Show  ", num_episodes=4),
        _library_podcast("podB", title=long_title, num_episodes=None),
        _library_podcast("podC", title="No Count"),
        _library_podcast("podD", title="Negative", num_episodes=-1),
        _library_podcast("podE", title="Boolean", num_episodes=True),
        _library_podcast("podF", title="String", num_episodes="7"),
    ]
    results[2]["media"].pop("numEpisodes")
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library("lib2"),
        ("GET", "/api/libraries/lib2/items"): {
            "results": results,
            "total": len(results),
        },
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.list_podcasts("lib2", 10)

    assert set(result) == {
        "library_id",
        "podcasts",
        "count",
        "total",
        "page",
        "limit",
        "has_more",
        "next_page",
    }
    assert result["library_id"] == "lib2"
    assert result["count"] == 6
    assert result["total"] == 6
    assert result["page"] == 0
    assert result["limit"] == 10
    assert result["has_more"] is False
    assert result["next_page"] is None
    assert [podcast["id"] for podcast in result["podcasts"]] == [
        "podA",
        "podB",
        "podC",
        "podD",
        "podE",
        "podF",
    ]
    assert result["podcasts"][0]["title"] == "My Show"
    assert result["podcasts"][1]["title"] == long_title[:297] + "..."
    assert [podcast["downloaded_count"] for podcast in result["podcasts"]] == [
        4,
        None,
        None,
        None,
        None,
        None,
    ]
    for podcast in result["podcasts"]:
        assert set(podcast) == {"id", "title", "downloaded_count"}

    items_request = requests[-1]
    assert items_request.url.params["minified"] == "1"
    assert items_request.url.params["limit"] == "10"
    assert items_request.url.params["page"] == "0"

    rendered = json.dumps(result)
    for forbidden in (
        "feeds.example",
        "images.example",
        "private description",
        "/podcasts",
        "leaky-episode-id",
        "http",
    ):
        assert forbidden not in rendered


async def test_list_podcasts_uses_native_page_cursor(
    podcast_factory: Any,
) -> None:
    def page_results(page: int) -> list[dict[str, Any]]:
        if page == 0:
            return [_library_podcast(f"pod{index}") for index in range(50)]
        assert page == 2
        return [_library_podcast(f"pod{index}") for index in range(100, 120)]

    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library("lib2"),
        ("GET", "/api/libraries/lib2/items"): lambda request: httpx.Response(
            200,
            json={
                "results": page_results(int(request.url.params["page"])),
                "total": 120,
            },
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    first = await client.list_podcasts("lib2", 50, page=0)
    last = await client.list_podcasts("lib2", 50, page=2)

    assert first["count"] == 50
    assert first["has_more"] is True
    assert first["next_page"] == 1
    assert first["page"] == 0
    assert last["count"] == 20
    assert last["has_more"] is False
    assert last["next_page"] is None
    assert [request.url.params["page"] for request in requests if request.url.path.endswith("/items")] == [
        "0",
        "2",
    ]


async def test_list_podcasts_page_bound_reports_bounded_note(
    podcast_factory: Any,
) -> None:
    results = [_library_podcast(f"pod{index}") for index in range(50)]
    transport, _ = _make_transport(_list_routes(results, 60000))
    client = podcast_factory(transport)

    result = await client.list_podcasts("lib2", 50, page=1000)

    assert result["page"] == 1000
    assert result["count"] == 50
    assert result["has_more"] is False
    assert result["next_page"] is None
    assert "maximum supported page" in result["note"]
    assert "not a complete archive" in result["note"]


@pytest.mark.parametrize("limit", [True, 0, 51, "5", 1.0])
@pytest.mark.parametrize("page", [True, -1, 1001, "0", 1.0])
async def test_list_podcasts_rejects_typed_bounds_before_backend(
    podcast_factory: Any, limit: object, page: object
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSInvalidRequest):
        await client.list_podcasts(
            "lib2", limit, page=page  # type: ignore[arg-type]
        )
    assert requests == []


async def test_list_podcasts_rejects_oversize_response(
    podcast_factory: Any,
) -> None:
    oversized = b"{" + b'"x":"' + b"z" * (4 * 1024 * 1024) + b'"}'
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library("lib2"),
        ("GET", "/api/libraries/lib2/items"): httpx.Response(
            200,
            content=oversized,
            headers={"Content-Type": "application/json"},
        ),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSResponseError, match="size limit") as raised:
        await client.list_podcasts("lib2")
    assert "zzzz" not in str(raised.value)


async def test_list_podcasts_requires_allowlisted_actual_podcast_library(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSInvalidRequest):
        await client.list_podcasts("bad/id")
    with pytest.raises(ABSNotAllowed, match="Library not found or not permitted"):
        await client.list_podcasts("lib3")
    assert requests == []

    book_transport, book_requests = _make_transport(
        {("GET", "/api/libraries/lib2"): _podcast_library("lib2", media_type="book")}
    )
    book_client = podcast_factory(book_transport)
    with pytest.raises(ABSNotAllowed, match="Podcast library not found"):
        await book_client.list_podcasts("lib2")
    assert _method_paths(book_requests) == [("GET", "/api/libraries/lib2")]


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [], "total": 1},
        {"results": [_library_podcast("podA")], "total": 0},
        {"results": "nope", "total": 0},
        {"results": [], "total": -1},
        {"results": [], "total": True},
        {"results": [], "total": "0"},
        {"results": []},
    ],
)
async def test_list_podcasts_rejects_inconsistent_paging(
    podcast_factory: Any, payload: dict[str, Any]
) -> None:
    routes: Routes = {
        ("GET", "/api/libraries/lib2"): _podcast_library("lib2"),
        ("GET", "/api/libraries/lib2/items"): payload,
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(
        ABSResponseError,
        match="Audiobookshelf returned an unexpected response",
    ):
        await client.list_podcasts("lib2", 5)


@pytest.mark.parametrize(
    ("page", "result_count", "total"),
    [
        (0, 2, 1),
        (1, 2, 6),
    ],
)
async def test_list_podcasts_rejects_rows_beyond_remaining_total(
    podcast_factory: Any, page: int, result_count: int, total: int
) -> None:
    results = [
        _library_podcast(f"pod{index}") for index in range(result_count)
    ]
    transport, requests = _make_transport(_list_routes(results, total))
    client = podcast_factory(transport)

    with pytest.raises(
        ABSResponseError,
        match="Audiobookshelf returned an unexpected response",
    ):
        await client.list_podcasts("lib2", 5, page=page)

    assert len(
        [
            request
            for request in requests
            if request.url.path == "/api/libraries/lib2/items"
        ]
    ) == 1


async def test_list_podcasts_accepts_empty_page_beyond_total(
    podcast_factory: Any,
) -> None:
    transport, _ = _make_transport(_list_routes([], 3))
    client = podcast_factory(transport)

    result = await client.list_podcasts("lib2", 5, page=10)

    assert result["podcasts"] == []
    assert result["count"] == 0
    assert result["total"] == 3
    assert result["page"] == 10
    assert result["has_more"] is False
    assert result["next_page"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(id="bad/id"),
        lambda item: item.update(libraryId="lib1"),
        lambda item: item.update(mediaType="book"),
        lambda item: item.pop("media"),
        lambda item: item["media"].pop("metadata"),
        lambda item: item.update(id=None),
    ],
)
async def test_list_podcasts_fails_closed_on_malformed_items(
    podcast_factory: Any, mutate: Callable[[dict[str, Any]], object]
) -> None:
    item = _library_podcast("podA")
    mutate(item)
    transport, _ = _make_transport(_list_routes([item], 1))
    client = podcast_factory(transport)

    with pytest.raises(
        ABSResponseError,
        match="Audiobookshelf returned an unexpected response",
    ) as raised:
        await client.list_podcasts("lib2")
    assert "podA" not in str(raised.value)
    assert "private" not in str(raised.value)


async def test_podcast_reads_accept_between_1_and_4_mib(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    item["media"]["metadata"]["description"] = "x" * (3 * 1024 * 1024)
    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    state = await client.get_podcast_state("pod1")

    assert state["podcast_id"] == "pod1"
    assert len(requests) == 1


async def test_podcast_reads_reject_over_4_mib_generically(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    item["media"]["metadata"]["description"] = "y" * (4 * 1024 * 1024 + 1)
    transport, _ = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed) as raised:
        await client.get_podcast_state("pod1")

    assert str(raised.value) == "Podcast not found or not permitted."
    assert "yyyy" not in str(raised.value)


async def test_delete_episode_uses_exact_native_endpoint(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episode(
        "pod1", "ep1", expected_fingerprint=fingerprint
    )

    deletes = [request for request in requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path == "/api/podcasts/pod1/episode/ep1"
    assert deletes[0].url.params.get("hard") == "1"
    assert not any("/api/files" in path for _, path in _method_paths(requests))
    assert result["podcast_id"] == "pod1"
    assert result["episode_id"] == "ep1"
    assert result["status"] == "deleted"
    assert result["deleted_count"] == 1
    assert result["filesystem_removal"] == "not_verified"


async def test_delete_episode_must_belong_to_actual_podcast(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("DELETE", "/api/podcasts/pod1/episode/ep2"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSNotFound, match="episode not found"):
        await client.delete_podcast_episode(
            "pod1", "ep2", expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_podcast_uses_item_endpoint_with_hard_delete(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("DELETE", "/api/items/pod1"): httpx.Response(200, text="OK"),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast(
        "pod1", expected_fingerprint=fingerprint
    )

    deletes = [request for request in requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path == "/api/items/pod1"
    assert deletes[0].url.params.get("hard") == "1"
    assert result["status"] == "deleted"
    assert result["library_id"] == "lib1"
    assert result["filesystem_removal"] == "not_verified"


@pytest.mark.parametrize("case", ["book", "unlisted"])
async def test_delete_podcast_rejects_other_scope_before_delete(
    podcast_factory: Any, case: str
) -> None:
    item = _podcast()
    if case == "book":
        item["mediaType"] = "book"
    elif case == "unlisted":
        item["libraryId"] = "lib3"

    routes: Routes = {("GET", "/api/items/pod1"): item}
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed) as raised:
        await client.delete_podcast(
            "pod1", expected_fingerprint="0" * 64
        )

    assert str(raised.value) == "Podcast not found or not permitted."
    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_podcast_missing_item_is_generic_not_found(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed) as raised:
        await client.delete_podcast("pod1", expected_fingerprint="0" * 64)

    assert str(raised.value) == "Podcast not found or not permitted."
    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_response_over_cap_is_ambiguous_and_not_retried(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    oversized = (
        b'{"item": "' + b"z" * (4 * 1024 * 1024 + 1) + b'"}'
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("DELETE", "/api/items/pod1"): httpx.Response(
            200,
            content=oversized,
            headers={"Content-Type": "application/json"},
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="size limit") as raised:
        await client.delete_podcast(
            "pod1", expected_fingerprint=fingerprint
        )

    deletes = [request for request in requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert "zzzz" not in str(raised.value)


async def test_delete_refused_while_downloads_active(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    item["episodesDownloading"] = [
        _queue_entry("current", "https://cdn.example/current.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("DELETE", "/api/items/pod1"): httpx.Response(200, text="OK"),
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="queued or active"):
        await client.delete_podcast(
            "pod1", expected_fingerprint=fingerprint
        )
    with pytest.raises(ABSResponseError, match="queued or active"):
        await client.delete_podcast_episode(
            "pod1", "ep1", expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_typed_inputs_and_stale_fingerprint_never_delete(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("DELETE", "/api/items/pod1"): httpx.Response(200, text="OK"),
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    for podcast_id in ("../pod1", "a/b", "", "x" * 129):
        with pytest.raises(ABSInvalidRequest):
            await client.delete_podcast(
                podcast_id, expected_fingerprint=fingerprint
            )
        with pytest.raises(ABSInvalidRequest):
            await client.delete_podcast_episode(
                podcast_id, "ep1", expected_fingerprint=fingerprint
            )

    for episode_id in ("../ep1", "a/b", ""):
        with pytest.raises(ABSInvalidRequest):
            await client.delete_podcast_episode(
                "pod1", episode_id, expected_fingerprint=fingerprint
            )

    with pytest.raises(ABSInvalidRequest, match="retry with current settings"):
        await client.delete_podcast(
            "pod1", expected_fingerprint="0" * 64
        )
    with pytest.raises(ABSInvalidRequest, match="retry with current settings"):
        await client.delete_podcast_episode(
            "pod1", "ep1", expected_fingerprint="0" * 64
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_backend_missing_is_generic_not_found(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", published=1000)])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            404, text=BACKEND_SECRET
        ),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSNotFound) as raised:
        await client.delete_podcast_episode(
            "pod1", "ep1", expected_fingerprint=fingerprint
        )

    assert BACKEND_SECRET not in str(raised.value)
    assert str(raised.value) == "Requested Audiobookshelf resource was not found."


@pytest.mark.parametrize(
    ("case", "item_path", "folder_id", "folders"),
    [
        ("library-root", "/podcasts", "folder1", None),
        ("outside", "/other/pod1", "folder1", None),
        ("parent-traversal", "/podcasts/../pod1", "folder1", None),
        ("nested", "/podcasts/nested/pod1", "folder1", None),
        ("relative", "podcasts/pod1", "folder1", None),
        ("missing-folder", "/podcasts/pod1", "missing", None),
        (
            "wrong-library-folder",
            "/podcasts/pod1",
            "folder1",
            [{"id": "folder1", "fullPath": "/podcasts", "libraryId": "lib2"}],
        ),
    ],
)
async def test_delete_podcast_rejects_unsafe_item_paths_with_zero_deletes(
    podcast_factory: Any,
    case: str,
    item_path: str,
    folder_id: str,
    folders: list[dict[str, Any]] | None,
) -> None:
    item = _podcast()
    item["path"] = item_path
    item["folderId"] = folder_id
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library(
            "lib1", folders=folders
        ),
        ("DELETE", "/api/items/pod1"): httpx.Response(200, text="OK"),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="not safe to delete") as raised:
        await client.delete_podcast(
            "pod1", expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)
    assert item_path not in str(raised.value)


@pytest.mark.parametrize(
    "audio_path",
    [
        "/podcasts/other/evil.mp3",
        "/podcasts/pod1",
        "/podcasts/pod1/../evil.mp3",
        "/other/pod1/ep1.mp3",
        None,
    ],
)
async def test_delete_episode_rejects_unsafe_audio_paths_with_zero_deletes(
    podcast_factory: Any, audio_path: str | None
) -> None:
    episode = _episode("ep1", published=1000)
    if audio_path is None:
        episode.pop("audioFile")
    else:
        episode["audioFile"] = {"metadata": {"path": audio_path}}
    item = _podcast(episodes=[episode])
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="episode path is not safe"):
        await client.delete_podcast_episode(
            "pod1", "ep1", expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.parametrize("case", ["episode-path", "item-root-path"])
async def test_retention_trim_rejects_unsafe_paths_before_patch(
    podcast_factory: Any, case: str
) -> None:
    episodes = [
        _episode("ep1", published=1000),
        _episode("ep2", published=2000),
    ]
    item = _podcast(episodes=episodes)
    if case == "episode-path":
        episodes[0]["audioFile"] = {
            "metadata": {"path": "/podcasts/other/evil.mp3"}
        }
    else:
        item["path"] = "/podcasts"

    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="not safe to delete"):
        await client.set_podcast_retention(
            "pod1", 1, True, expected_fingerprint=fingerprint
        )

    assert not any(
        request.method in {"PATCH", "DELETE"} for request in requests
    )


async def test_download_accepts_empty_feed_without_mutation(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): {"podcast": {"metadata": {}, "episodes": []}},
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.download_latest_episodes("pod1", 3)

    assert result["status"] == "nothing_new"
    assert result["queued_count"] == 0
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


# --- Bounded downloaded browsing -----------------------------------------


def _titles(payload: dict[str, Any]) -> list[str]:
    return [episode["title"] for episode in payload["episodes"]]


def _selectors_by_title(payload: dict[str, Any]) -> dict[str, str]:
    return {
        episode["title"]: episode["id"]
        for episode in payload["episodes"]
    }


def _series_feed(count: int) -> list[dict[str, Any]]:
    return [
        _feed_episode(
            f"g{index}",
            f"https://cdn.example/{index}.mp3",
            1000 * index,
            f"Episode {index}",
        )
        for index in range(1, count + 1)
    ]


async def test_list_episodes_query_casefold_orders_paging_and_undated(
    podcast_factory: Any,
) -> None:
    episodes = [
        _episode("ep1", 1000),
        _episode("ep2", 3000),
        _episode("ep3", 2000),
        _episode("ep4", None),
        _episode("ep5", 3000),
    ]
    for episode, title in zip(
        episodes,
        ["Alpha One", "beta ALPHA", "Gamma", "Undated Alpha", "alpha"],
        strict=True,
    ):
        episode["title"] = title
    item = _podcast(episodes=episodes)
    transport, requests = _make_transport({("GET", "/api/items/pod1"): item})
    client = podcast_factory(transport)

    newest = await client.list_podcast_episodes("pod1", 5, query="ALPHA")
    assert newest["podcast_id"] == "pod1"
    assert newest["total"] == 4
    assert newest["count"] == 4
    assert newest["limit"] == 5
    assert newest["offset"] == 0
    assert newest["has_more"] is False
    assert newest["next_offset"] is None
    assert [episode["id"] for episode in newest["episodes"]] == [
        "ep5",
        "ep2",
        "ep1",
        "ep4",
    ]
    assert newest["episodes"][-1]["published_at"] is None

    oldest = await client.list_podcast_episodes(
        "pod1", 5, query="alpha", order="oldest"
    )
    assert [episode["id"] for episode in oldest["episodes"]] == [
        "ep1",
        "ep2",
        "ep5",
        "ep4",
    ]

    page = await client.list_podcast_episodes(
        "pod1", 2, query="alpha", order="newest", offset=1
    )
    assert [episode["id"] for episode in page["episodes"]] == ["ep2", "ep1"]
    assert page["count"] == 2
    assert page["total"] == 4
    assert page["has_more"] is True
    assert page["next_offset"] == 3

    last = await client.list_podcast_episodes(
        "pod1", 2, query="alpha", order="newest", offset=3
    )
    assert [episode["id"] for episode in last["episodes"]] == ["ep4"]
    assert last["count"] == 1
    assert last["has_more"] is False
    assert last["next_offset"] is None

    rendered = json.dumps(newest)
    for forbidden in ("cdn.example", "/podcasts", "guid-ep1", "audio/mpeg"):
        assert forbidden not in rendered
    assert len(requests) == 4


async def test_list_episodes_pages_more_than_fifty_downloaded(
    podcast_factory: Any,
) -> None:
    episodes = [
        _episode(f"ep{index:03d}", 1000 * index) for index in range(120)
    ]
    transport, _ = _make_transport(
        {("GET", "/api/items/pod1"): _podcast(episodes=episodes)}
    )
    client = podcast_factory(transport)

    first = await client.list_podcast_episodes("pod1", 50)
    assert first["total"] == 120
    assert first["count"] == 50
    assert first["has_more"] is True
    assert first["next_offset"] == 50
    assert first["episodes"][0]["id"] == "ep119"
    assert first["episodes"][-1]["id"] == "ep070"

    second = await client.list_podcast_episodes("pod1", 50, offset=50)
    assert second["episodes"][0]["id"] == "ep069"
    assert second["episodes"][-1]["id"] == "ep020"
    assert second["has_more"] is True
    assert second["next_offset"] == 100

    third = await client.list_podcast_episodes("pod1", 50, offset=100)
    assert third["count"] == 20
    assert third["episodes"][0]["id"] == "ep019"
    assert third["episodes"][-1]["id"] == "ep000"
    assert third["has_more"] is False
    assert third["next_offset"] is None

    beyond = await client.list_podcast_episodes("pod1", 50, offset=500)
    assert beyond["episodes"] == []
    assert beyond["count"] == 0
    assert beyond["total"] == 120
    assert beyond["has_more"] is False
    assert beyond["next_offset"] is None

    oldest = await client.list_podcast_episodes(
        "pod1", 50, order="oldest"
    )
    assert oldest["episodes"][0]["id"] == "ep000"
    assert oldest["episodes"][-1]["id"] == "ep049"


async def test_list_episode_search_and_page_bounds_never_reach_backend(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    invalid_queries: list[object] = ["", "   ", "x" * 201, "bad\nquery", 5]
    invalid_orders: list[object] = ["Oldest", "NEWEST", "", None, 1]
    invalid_offsets: list[object] = [-1, 100001, True, "0", 1.0, None]
    for query in invalid_queries:
        with pytest.raises(ABSInvalidRequest):
            await client.list_podcast_episodes("pod1", query=query)  # type: ignore[arg-type]
    for order in invalid_orders:
        with pytest.raises(ABSInvalidRequest):
            await client.list_podcast_episodes("pod1", order=order)  # type: ignore[arg-type]
    for offset in invalid_offsets:
        with pytest.raises(ABSInvalidRequest):
            await client.list_podcast_episodes("pod1", offset=offset)  # type: ignore[arg-type]

    assert requests == []


# --- Selected and chronological downloads --------------------------------


async def test_selected_download_arbitrary_entries_in_caller_order(
    podcast_factory: Any,
) -> None:
    feed_entries = _series_feed(12)
    shuffled = feed_entries[::2] + feed_entries[1::2]
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(*shuffled),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    listing = await client.list_available_podcast_episodes("pod1", 50)
    selectors = _selectors_by_title(listing)

    result = await client.download_podcast_episodes(
        "pod1", [selectors["Episode 2"], selectors["Episode 9"]]
    )

    assert result["podcast_id"] == "pod1"
    assert result["status"] == "queued"
    assert result["selected_count"] == 2
    assert result["queued_count"] == 2
    assert result["skipped_count"] == 0
    assert [entry["title"] for entry in result["episodes"]] == [
        "Episode 2",
        "Episode 9",
    ]
    for entry in result["episodes"]:
        assert set(entry) == {"id", "title", "published_at"}
        assert FEED_ID_RE.fullmatch(entry["id"])
    assert "not completed downloads" in result["note"]

    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    sent = json.loads(posts[0].content)
    assert [episode["title"] for episode in sent] == [
        "Episode 2",
        "Episode 9",
    ]
    assert sent[0]["guid"] == "g2"
    assert sent[0]["publishedAt"] == 2000
    assert set(sent[0]) == {"title", "guid", "publishedAt", "enclosure"}
    assert set(sent[0]["enclosure"]) == {"url", "type", "length"}

    item_reads = [
        request
        for request in requests
        if request.method == "GET"
        and request.url.path == "/api/items/pod1"
    ]
    # Browse read, download read, and the just-before-POST re-read.
    assert len(item_reads) == 3

    rendered = json.dumps(result)
    for forbidden in ("cdn.example", "/podcasts", "guid", "audio/mpeg"):
        assert forbidden not in rendered


async def test_selected_download_skips_known_without_backfill(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "epDownloaded",
                100,
                guid="g2",
                url="https://cdn.example/downloaded.mp3",
            )
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("g3", "https://cdn.example/queued.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(6)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_podcast_episodes(
        "pod1",
        [
            selectors["Episode 1"],
            selectors["Episode 2"],
            selectors["Episode 3"],
            selectors["Episode 4"],
        ],
    )

    assert result["status"] == "queued"
    assert result["selected_count"] == 4
    assert result["queued_count"] == 2
    assert result["skipped_count"] == 2
    assert [entry["title"] for entry in result["episodes"]] == [
        "Episode 1",
        "Episode 4",
    ]
    assert [
        (entry["id"], entry["title"], entry["reason"])
        for entry in result["skipped_episodes"]
    ] == [
        (selectors["Episode 2"], "Episode 2", "already_downloaded"),
        (selectors["Episode 3"], "Episode 3", "already_queued"),
    ]
    assert all(
        set(entry) == {"id", "title", "published_at", "reason"}
        for entry in result["skipped_episodes"]
    )
    skipped_rendered = json.dumps(result["skipped_episodes"])
    for forbidden in ("cdn.example", "guid", "downloaded.mp3", "/podcasts"):
        assert forbidden not in skipped_rendered

    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert [episode["title"] for episode in json.loads(posts[0].content)] == [
        "Episode 1",
        "Episode 4",
    ]


async def test_selected_download_reports_downloaded_reason_when_both_known(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "ep2",
                2000,
                guid="g2",
                url="https://cdn.example/2.mp3",
            )
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("g2", "https://cdn.example/2.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(2)),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_podcast_episodes(
        "pod1", [selectors["Episode 2"]]
    )

    assert result["status"] == "nothing_new"
    assert result["queued_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped_episodes"] == [
        {
            "id": selectors["Episode 2"],
            "title": "Episode 2",
            "published_at": "1970-01-01T00:00:02+00:00",
            "reason": "already_downloaded",
        }
    ]
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_selected_download_all_known_sends_no_mutation(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000, guid="g1")]
    )
    item["episodesDownloading"] = [
        _queue_entry("g2", "https://cdn.example/2.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(3)),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_podcast_episodes(
        "pod1", [selectors["Episode 1"], selectors["Episode 2"]]
    )

    assert set(result) == {
        "podcast_id",
        "status",
        "selected_count",
        "queued_count",
        "skipped_count",
        "episodes",
        "skipped_episodes",
        "note",
    }
    assert result["podcast_id"] == "pod1"
    assert result["status"] == "nothing_new"
    assert result["selected_count"] == 2
    assert result["queued_count"] == 0
    assert result["skipped_count"] == 2
    assert result["episodes"] == []
    assert [entry["reason"] for entry in result["skipped_episodes"]] == [
        "already_downloaded",
        "already_queued",
    ]
    assert [entry["id"] for entry in result["skipped_episodes"]] == [
        selectors["Episode 1"],
        selectors["Episode 2"],
    ]
    assert [entry["title"] for entry in result["skipped_episodes"]] == [
        "Episode 1",
        "Episode 2",
    ]
    for entry in result["skipped_episodes"]:
        assert set(entry) == {"id", "title", "published_at", "reason"}
    assert "not completed downloads" in result["note"]
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_selected_download_rejects_stale_selector_before_post(
    podcast_factory: Any,
) -> None:
    holder: dict[str, Any] = {"feed": _feed(*_series_feed(3))}
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )
    selected = selectors["Episode 2"]

    holder["feed"] = _feed(
        _feed_episode("g1", "https://cdn.example/1.mp3", 1000, "Episode 1"),
        _feed_episode("g3", "https://cdn.example/3.mp3", 3000, "Episode 3"),
    )
    with pytest.raises(ABSInvalidRequest, match="Unknown or stale"):
        await client.download_podcast_episodes("pod1", [selected])

    holder["feed"] = _feed(
        _feed_episode("g1", "https://cdn.example/1.mp3", 1000, "Episode 1"),
        _feed_episode("g2", "https://cdn.example/2.mp3", 2500, "Episode 2"),
        _feed_episode("g3", "https://cdn.example/3.mp3", 3000, "Episode 3"),
    )
    with pytest.raises(ABSInvalidRequest, match="Unknown or stale"):
        await client.download_podcast_episodes("pod1", [selected])

    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_selected_download_input_bounds_never_reach_backend(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)
    valid = "feed_" + "a" * 64

    invalid: list[object] = [
        [],
        [valid, valid],
        [123],
        [None],
        [""],
        ["feed_" + "a" * 63],
        ["feed_" + "A" * 64],
        ["feed_" + "a" * 65],
        ["feed_" + "g" * 64],
        [valid] * 26,
        (valid,),
        "not-a-list",
        None,
    ]
    for episode_ids in invalid:
        with pytest.raises(ABSInvalidRequest):
            await client.download_podcast_episodes(
                "pod1", episode_ids  # type: ignore[arg-type]
            )

    assert requests == []


async def test_selected_download_preflights_every_enclosure_before_post(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode(
                "g1", "https://cdn.example/one.mp3", 3000, "One"
            ),
            _feed_episode(
                "g2", "https://private.example/two.mp3", 2000, "Two"
            ),
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )
    seen: list[str] = []

    async def guard(url: str) -> str:
        if "cdn.example" in url or "private.example" in url:
            seen.append(url)
        if "private.example" in url:
            raise ABSInvalidRequest("Remote URL host is not publicly routable.")
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.download_podcast_episodes(
            "pod1", [selectors["One"], selectors["Two"]]
        )

    assert seen == [
        "https://cdn.example/one.mp3",
        "https://private.example/two.mp3",
    ]
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://cdn.example/a%zz.mp3",
        "https://cdn.example/a%FF.mp3",
    ],
)
async def test_selected_download_rejects_invalid_percent_before_post(
    podcast_factory: Any, bad_url: str
) -> None:
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("bad", bad_url, 2000, "Bad")
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    with pytest.raises(ABSInvalidRequest, match="percent encoding") as raised:
        await client.download_podcast_episodes("pod1", [selectors["Bad"]])

    assert bad_url not in str(raised.value)
    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_selected_download_keeps_multibyte_guid_unchanged(
    podcast_factory: Any,
) -> None:
    guid = "guid-\u00e9-\U0001f4a9"
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode(
                guid, "https://cdn.example/a%20b.mp3", 2000, "Multibyte"
            )
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_podcast_episodes(
        "pod1", [selectors["Multibyte"]]
    )

    assert result["status"] == "queued"
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert json.loads(posts[0].content)[0]["guid"] == guid


async def test_selected_download_rereads_and_skips_newly_queued(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _podcast()
    holder: dict[str, Any] = {"item": item, "feed": _feed(*_series_feed(3))}
    routes: Routes = {
        ("GET", "/api/items/pod1"): lambda request: httpx.Response(
            200, json=holder["item"]
        ),
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    async def guard(url: str) -> str:
        if url == "https://cdn.example/2.mp3":
            holder["item"]["episodeDownloadsQueued"] = [
                _queue_entry("g2", "https://cdn.example/2.mp3")
            ]
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    result = await client.download_podcast_episodes(
        "pod1", [selectors["Episode 1"], selectors["Episode 2"]]
    )

    assert result["status"] == "queued"
    assert result["selected_count"] == 2
    assert result["queued_count"] == 1
    assert result["skipped_count"] == 1
    assert result["skipped_episodes"] == [
        {
            "id": selectors["Episode 2"],
            "title": "Episode 2",
            "published_at": "1970-01-01T00:00:02+00:00",
            "reason": "already_queued",
        }
    ]
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert [episode["title"] for episode in json.loads(posts[0].content)] == [
        "Episode 1"
    ]
    item_reads = [
        request
        for request in requests
        if request.method == "GET"
        and request.url.path == "/api/items/pod1"
    ]
    assert len(item_reads) == 3


@pytest.mark.parametrize(
    ("case", "expected_error", "message"),
    [
        ("feed", ABSResponseError, "changed before download"),
        ("library", ABSResponseError, "changed before download"),
        ("path", ABSResponseError, "changed before download"),
        ("folder", ABSResponseError, "changed before download"),
        ("media_type", ABSNotAllowed, "not found or not permitted"),
        ("out_of_scope", ABSNotAllowed, "not found or not permitted"),
    ],
)
async def test_selected_download_fails_closed_when_scope_changes_during_preflight(
    podcast_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    item = _podcast()
    holder: dict[str, Any] = {
        "item": item,
        "feed": _feed(*_series_feed(1)),
    }
    routes: Routes = {
        ("GET", "/api/items/pod1"): lambda request: httpx.Response(
            200, json=holder["item"]
        ),
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    def mutate() -> None:
        if case == "feed":
            holder["item"]["media"]["metadata"]["feedUrl"] = (
                "https://feeds.example/changed.xml"
            )
        elif case == "library":
            holder["item"]["libraryId"] = "lib2"
        elif case == "path":
            holder["item"]["path"] = "/podcasts/other"
        elif case == "folder":
            holder["item"]["folderId"] = "folder2"
        elif case == "media_type":
            holder["item"]["mediaType"] = "book"
        else:
            holder["item"]["libraryId"] = "lib3"

    async def guard(url: str) -> str:
        if url.startswith("https://cdn.example/"):
            mutate()
        return url

    monkeypatch.setattr(podcasts, "ensure_public_url", guard)

    with pytest.raises(expected_error, match=message):
        await client.download_podcast_episodes(
            "pod1", [selectors["Episode 1"]]
        )

    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_chronological_window_includes_anchor_plus_newer(
    podcast_factory: Any,
) -> None:
    feed_entries = _series_feed(20)
    shuffled = feed_entries[::2] + feed_entries[1::2]
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(*shuffled),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_episodes_from(
        "pod1", selectors["Episode 5"], 10
    )

    assert result["status"] == "queued"
    assert result["selected_count"] == 10
    assert result["queued_count"] == 10
    assert result["skipped_count"] == 0
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert [episode["title"] for episode in json.loads(posts[0].content)] == [
        f"Episode {index}" for index in range(5, 15)
    ]


async def test_chronological_window_near_end_returns_fewer(
    podcast_factory: Any,
) -> None:
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(20)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_episodes_from(
        "pod1", selectors["Episode 17"], 10
    )

    assert result["selected_count"] == 4
    assert result["queued_count"] == 4
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert [episode["title"] for episode in json.loads(posts[0].content)] == [
        "Episode 17",
        "Episode 18",
        "Episode 19",
        "Episode 20",
    ]


async def test_chronological_tied_dates_use_opaque_id_ascending(
    podcast_factory: Any,
) -> None:
    entries = [
        _feed_episode(
            f"g{index}",
            f"https://cdn.example/tie{index}.mp3",
            5000,
            f"Tie {index}",
        )
        for index in range(1, 7)
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(
            entries[3], entries[0], entries[5], entries[2], entries[1], entries[4]
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    listing = await client.list_available_podcast_episodes("pod1", 50)

    ids = [entry["id"] for entry in listing["episodes"]]
    assert ids == sorted(ids)
    expected = [entry["title"] for entry in listing["episodes"][2:5]]

    result = await client.download_episodes_from(
        "pod1", ids[2], 3
    )

    assert result["queued_count"] == 3
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert [episode["title"] for episode in json.loads(posts[0].content)] == expected


async def test_chronological_skips_known_without_backfill(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("epSix", 6000, guid="g6"),
            _episode("epTwelve", 12000, guid="g12"),
        ]
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(15)),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    result = await client.download_episodes_from(
        "pod1", selectors["Episode 5"], 10
    )

    assert result["selected_count"] == 10
    assert result["queued_count"] == 8
    assert result["skipped_count"] == 2
    assert [
        (entry["title"], entry["reason"])
        for entry in result["skipped_episodes"]
    ] == [
        ("Episode 6", "already_downloaded"),
        ("Episode 12", "already_downloaded"),
    ]
    assert [entry["id"] for entry in result["skipped_episodes"]] == [
        selectors["Episode 6"],
        selectors["Episode 12"],
    ]
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert [episode["title"] for episode in json.loads(posts[0].content)] == [
        "Episode 5",
        "Episode 7",
        "Episode 8",
        "Episode 9",
        "Episode 10",
        "Episode 11",
        "Episode 13",
        "Episode 14",
    ]


async def test_chronological_stale_anchor_rejects_before_post(
    podcast_factory: Any,
) -> None:
    holder: dict[str, Any] = {"feed": _feed(*_series_feed(5))}
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    selectors = _selectors_by_title(
        await client.list_available_podcast_episodes("pod1", 50)
    )

    holder["feed"] = _feed(*_series_feed(4))
    with pytest.raises(ABSInvalidRequest, match="Unknown or stale"):
        await client.download_episodes_from(
            "pod1", selectors["Episode 5"], 2
        )

    assert _posts(requests, "/api/podcasts/pod1/download-episodes") == []


async def test_chronological_input_bounds_never_reach_backend(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)
    anchor = "feed_" + "a" * 64

    for count in (0, 26, True, "5", 1.0):
        with pytest.raises(ABSInvalidRequest, match="integer"):
            await client.download_episodes_from("pod1", anchor, count)  # type: ignore[arg-type]
    for bad_anchor in (
        "",
        "feed_" + "a" * 63,
        "feed_" + "A" * 64,
        "episode-1",
        123,
        None,
    ):
        with pytest.raises(ABSInvalidRequest):
            await client.download_episodes_from(
                "pod1", bad_anchor, 5  # type: ignore[arg-type]
            )

    assert requests == []



async def test_available_catalogue_dedupes_sorts_pages_and_flags(
    podcast_factory: Any,
) -> None:
    feed_payload = _feed(
        _feed_episode("g1", "https://cdn.example/u1.mp3", 5000, "Newest"),
        _feed_episode("g1", "https://cdn.example/u3.mp3", 4000, "Dup Guid"),
        _feed_episode("g2", "https://cdn.example/u2.mp3", 1000, "Oldest"),
        _feed_episode("g4", "https://cdn.example/u1.mp3", 3000, "Dup Url"),
        _feed_episode("g5", "https://cdn.example/u5.mp3", 3000, "Middle"),
        _feed_episode("g6", "https://cdn.example/u6.mp3", 2000, "Undated"),
    )
    feed_payload["podcast"]["episodes"].pop()  # no publishedAt -> excluded
    feed_payload["podcast"]["episodes"].append(
        {"title": "No Enclosure", "guid": "g7", "publishedAt": 2600}
    )
    feed_payload["podcast"]["episodes"].append(
        {"title": "", "guid": "g8", "publishedAt": 2700}
    )
    item = _podcast(
        episodes=[
            _episode("epA", 9000, guid="g5"),
            _episode(
                "epB", 8000, guid="", url="https://cdn.example/u2.mp3"
            ),
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("g1", "https://cdn.example/u1.mp3")
    ]
    holder: dict[str, Any] = {"feed": feed_payload}
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.list_available_podcast_episodes("pod1")
    assert result["podcast_id"] == "pod1"
    assert result["total"] == 3
    assert result["count"] == 3
    assert result["offset"] == 0
    assert result["limit"] == 25
    assert result["has_more"] is False
    assert result["next_offset"] is None
    assert "metadata-eligible" in result["note"]
    assert "preflight occurs only on download" in result["note"]
    assert "not a complete archive" in result["note"]
    assert _titles(result) == ["Newest", "Middle", "Oldest"]

    newest, middle, oldest = result["episodes"]
    assert newest["downloaded"] is False
    assert newest["queued"] is True
    assert "downloaded_episode_id" not in newest
    assert middle["downloaded"] is True
    assert middle["downloaded_episode_id"] == "epA"
    assert middle["queued"] is False
    assert oldest["downloaded"] is True
    assert oldest["downloaded_episode_id"] == "epB"
    assert all(FEED_ID_RE.fullmatch(entry["id"]) for entry in result["episodes"])
    for entry in result["episodes"]:
        assert set(entry) <= {
            "id",
            "title",
            "published_at",
            "downloaded",
            "queued",
            "downloaded_episode_id",
        }

    rendered = json.dumps(result)
    for forbidden in (
        "cdn.example",
        "guid",
        "private feed description",
        "Undated",
        "No Enclosure",
    ):
        assert forbidden not in rendered

    filtered = await client.list_available_podcast_episodes(
        "pod1", query="MID"
    )
    assert _titles(filtered) == ["Middle"]

    page = await client.list_available_podcast_episodes(
        "pod1", 1, order="newest", offset=1
    )
    assert _titles(page) == ["Middle"]
    assert page["total"] == 3
    assert page["has_more"] is True
    assert page["next_offset"] == 2

    oldest_order = await client.list_available_podcast_episodes(
        "pod1", 25, order="oldest"
    )
    assert _titles(oldest_order) == ["Oldest", "Middle", "Newest"]

    # Reordering the feed must not change selectors or deduplication.
    holder["feed"] = _feed(*reversed(feed_payload["podcast"]["episodes"]))
    reordered = await client.list_available_podcast_episodes("pod1")
    assert [entry["id"] for entry in reordered["episodes"]] == [
        entry["id"] for entry in result["episodes"]
    ]


async def test_available_selectors_track_identity_not_feed_order_or_title(
    podcast_factory: Any,
) -> None:
    item = _podcast()
    base = [
        _feed_episode("g1", "https://cdn.example/a.mp3", 1000, "A"),
        _feed_episode("g2", "https://cdn.example/b.mp3", 2000, "B"),
        _feed_episode("g3", "https://cdn.example/c.mp3", 3000, "C"),
    ]
    holder: dict[str, Any] = {"feed": _feed(*base)}
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): lambda request: httpx.Response(
            200, json=holder["feed"]
        ),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    def ids(payload: dict[str, Any]) -> dict[str, str]:
        return {
            episode["title"]: episode["id"] for episode in payload["episodes"]
        }

    first = ids(await client.list_available_podcast_episodes("pod1", 50))
    assert list(first) == ["C", "B", "A"]

    holder["feed"] = _feed(
        _feed_episode("g2", "https://cdn.example/b.mp3", 2000, "B renamed"),
        _feed_episode("g1", "https://cdn.example/a.mp3", 1000, "A renamed"),
        _feed_episode("g3", "https://cdn.example/c.mp3", 3000, "C renamed"),
    )
    reordered = await client.list_available_podcast_episodes("pod1", 50)
    assert [entry["id"] for entry in reordered["episodes"]] == [
        first["C"],
        first["B"],
        first["A"],
    ]

    holder["feed"] = _feed(
        _feed_episode("g1", "https://cdn.example/a.mp3", 1000, "A"),
        _feed_episode("g2", "https://cdn.example/b.mp3", 2500, "B"),
        _feed_episode("g3", "https://cdn.example/c.mp3", 3000, "C"),
    )
    date_changed = ids(
        await client.list_available_podcast_episodes("pod1", 50)
    )
    assert date_changed["A"] == first["A"]
    assert date_changed["C"] == first["C"]
    assert date_changed["B"] != first["B"]

    holder["feed"] = _feed(
        _feed_episode("g1", "https://cdn.example/a.mp3", 1000, "A"),
        _feed_episode("g2", "https://cdn.example/b2.mp3", 2000, "B"),
        _feed_episode("g3", "https://cdn.example/c.mp3", 3000, "C"),
    )
    url_changed = ids(
        await client.list_available_podcast_episodes("pod1", 50)
    )
    assert url_changed["B"] != first["B"]

    holder["feed"] = _feed(
        _feed_episode("g1", "https://cdn.example/a.mp3", 1000, "A"),
        _feed_episode("g9", "https://cdn.example/b.mp3", 2000, "B"),
        _feed_episode("g3", "https://cdn.example/c.mp3", 3000, "C"),
    )
    guid_changed = ids(
        await client.list_available_podcast_episodes("pod1", 50)
    )
    assert guid_changed["B"] != first["B"]


async def test_available_selectors_are_podcast_scoped(
    podcast_factory: Any,
) -> None:
    feed_episodes = _series_feed(3)
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("GET", "/api/items/pod2"): _podcast("pod2"),
        ("POST", "/api/podcasts/feed"): _feed(*feed_episodes),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    pod1 = await client.list_available_podcast_episodes("pod1")
    pod2 = await client.list_available_podcast_episodes("pod2")
    pod1_ids = [entry["id"] for entry in pod1["episodes"]]
    pod2_ids = [entry["id"] for entry in pod2["episodes"]]
    assert len(set(pod1_ids) & set(pod2_ids)) == 0

    with pytest.raises(ABSInvalidRequest, match="Unknown or stale"):
        await client.download_podcast_episodes("pod1", [pod2_ids[0]])


@pytest.mark.parametrize("case", ["book", "unlisted"])
async def test_available_rejects_other_scope_before_feed_fetch(
    podcast_factory: Any, case: str
) -> None:
    item = _podcast()
    if case == "book":
        item["mediaType"] = "book"
    else:
        item["libraryId"] = "lib3"
    routes: Routes = {("GET", "/api/items/pod1"): item}
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSNotAllowed) as raised:
        await client.list_available_podcast_episodes("pod1")

    assert str(raised.value) == "Podcast not found or not permitted."
    assert _posts(requests, "/api/podcasts/feed") == []


async def test_available_revalidates_stored_feed_url_before_fetch(
    podcast_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, requests = _make_transport(
        {("GET", "/api/items/pod1"): _podcast()}
    )
    client = podcast_factory(transport)

    async def deny(url: str) -> str:
        raise ABSInvalidRequest("Remote URL host is not publicly routable.")

    monkeypatch.setattr(podcasts, "ensure_public_url", deny)

    with pytest.raises(ABSInvalidRequest, match="publicly routable"):
        await client.list_available_podcast_episodes("pod1")

    assert _posts(requests, "/api/podcasts/feed") == []


@pytest.mark.parametrize(
    "feed_payload",
    [
        {"podcast": {"episodes": "nope"}},
        {"podcast": {"episodes": [42]}},
        {
            "podcast": {
                "episodes": [
                    {
                        "title": "x",
                        "guid": "g",
                        "publishedAt": 1,
                        "enclosure": "not-a-dict",
                    }
                ]
            }
        },
    ],
)
async def test_available_malformed_feed_is_generic(
    podcast_factory: Any, feed_payload: dict[str, Any]
) -> None:
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): feed_payload,
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    with pytest.raises(ABSResponseError) as raised:
        await client.list_available_podcast_episodes("pod1")

    assert str(raised.value) == "Audiobookshelf returned an unexpected response."


async def test_available_ambiguous_downloaded_match_omits_native_id(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "epA", 1000, guid="g", url="https://cdn.example/other.mp3"
            ),
            _episode("epB", 2000, guid="other", url="https://cdn.example/u.mp3"),
        ]
    )
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode("g", "https://cdn.example/u.mp3", 1000, "Both")
        ),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    result = await client.list_available_podcast_episodes("pod1")

    assert result["episodes"][0]["downloaded"] is True
    assert "downloaded_episode_id" not in result["episodes"][0]


async def test_available_matches_quoted_and_unquoted_enclosure_urls(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "epDownloaded",
                5000,
                guid="g-downloaded",
                url="https://cdn.example/a b.mp3",
            )
        ]
    )
    item["episodeDownloadsQueued"] = [
        _queue_entry("g-queued", "https://cdn.example/b%20b.mp3")
    ]
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("POST", "/api/podcasts/feed"): _feed(
            _feed_episode(
                "g-feed-encoded",
                "https://cdn.example/a%20b.mp3",
                3000,
                "Encoded",
            ),
            _feed_episode(
                "g-feed-raw", "https://cdn.example/b b.mp3", 2000, "Raw"
            ),
            _feed_episode(
                "g-feed-fresh", "https://cdn.example/c.mp3", 1000, "Fresh"
            ),
        ),
        ("POST", "/api/podcasts/pod1/download-episodes"): httpx.Response(
            200, text="OK"
        ),
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)

    listing = await client.list_available_podcast_episodes("pod1", 50)
    by_title = {entry["title"]: entry for entry in listing["episodes"]}
    assert by_title["Encoded"]["downloaded"] is True
    assert by_title["Encoded"]["downloaded_episode_id"] == "epDownloaded"
    assert by_title["Encoded"]["queued"] is False
    assert by_title["Raw"]["downloaded"] is False
    assert by_title["Raw"]["queued"] is True
    assert by_title["Fresh"]["downloaded"] is False
    assert by_title["Fresh"]["queued"] is False

    result = await client.download_podcast_episodes(
        "pod1",
        [
            by_title["Encoded"]["id"],
            by_title["Raw"]["id"],
            by_title["Fresh"]["id"],
        ],
    )

    assert result["selected_count"] == 3
    assert result["queued_count"] == 1
    assert result["skipped_count"] == 2
    posts = _posts(requests, "/api/podcasts/pod1/download-episodes")
    assert len(posts) == 1
    assert [entry["title"] for entry in json.loads(posts[0].content)] == [
        "Fresh"
    ]


async def test_available_pages_past_fifty_feed_entries(
    podcast_factory: Any,
) -> None:
    routes: Routes = {
        ("GET", "/api/items/pod1"): _podcast(),
        ("POST", "/api/podcasts/feed"): _feed(*_series_feed(60)),
    }
    transport, _ = _make_transport(routes)
    client = podcast_factory(transport)

    first = await client.list_available_podcast_episodes("pod1", 50)
    second = await client.list_available_podcast_episodes(
        "pod1", 50, offset=50
    )

    assert first["total"] == 60
    assert first["count"] == 50
    assert first["has_more"] is True
    assert first["next_offset"] == 50
    assert first["episodes"][0]["title"] == "Episode 60"
    assert first["episodes"][-1]["title"] == "Episode 11"

    assert second["count"] == 10
    assert second["total"] == 60
    assert second["has_more"] is False
    assert second["next_offset"] is None
    assert [entry["title"] for entry in second["episodes"]] == [
        f"Episode {index}" for index in range(10, 0, -1)
    ]
    assert all(
        FEED_ID_RE.fullmatch(entry["id"])
        for entry in first["episodes"] + second["episodes"]
    )


async def test_available_search_and_page_bounds_never_reach_backend(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport)

    for query in ("", "  ", "x" * 201, "bad\nquery", 5):
        with pytest.raises(ABSInvalidRequest):
            await client.list_available_podcast_episodes(
                "pod1", query=query  # type: ignore[arg-type]
            )
    for order in ("Oldest", "", None, 1):
        with pytest.raises(ABSInvalidRequest):
            await client.list_available_podcast_episodes(
                "pod1", order=order  # type: ignore[arg-type]
            )
    for offset in (-1, 100001, True, "0", 1.0):
        with pytest.raises(ABSInvalidRequest):
            await client.list_available_podcast_episodes(
                "pod1", offset=offset  # type: ignore[arg-type]
            )

    assert requests == []


# --- Selected episode deletion -------------------------------------------


def _delete_routes(
    item: dict[str, Any],
    episode_ids: list[str],
    *,
    fail: dict[str, httpx.Response] | None = None,
    after: Callable[[str], None] | None = None,
    podcast_id: str = "pod1",
    library_id: str = "lib1",
) -> Routes:
    handler = _delete_handler(item, fail=fail, after=after)
    routes: Routes = {
        ("GET", f"/api/items/{podcast_id}"): lambda request: httpx.Response(
            200, json=item
        ),
        ("GET", f"/api/libraries/{library_id}"): _podcast_library(library_id),
    }
    for episode_id in episode_ids:
        routes[
            ("DELETE", f"/api/podcasts/{podcast_id}/episode/{episode_id}")
        ] = handler
    return routes


async def test_delete_selected_episodes_in_caller_order(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", 1000),
            _episode("ep2", 2000),
            _episode("ep3", 3000),
        ]
    )
    routes = _delete_routes(item, ["ep1", "ep2", "ep3"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1",
        ["ep2", "ep1", "ep3"],
        expected_fingerprint=fingerprint,
    )

    assert result["podcast_id"] == "pod1"
    assert result["status"] == "completed"
    assert result["requested_count"] == 3
    assert result["deleted_count"] == 3
    assert result["deleted_episode_ids"] == ["ep2", "ep1", "ep3"]
    assert result["filesystem_removal"] == "not_verified"
    assert "may already have been applied" in result["note"]
    assert "filesystem removal is not verified" in result["note"]

    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert [request.url.path for request in deletes] == [
        "/api/podcasts/pod1/episode/ep2",
        "/api/podcasts/pod1/episode/ep1",
        "/api/podcasts/pod1/episode/ep3",
    ]
    assert all(request.url.params.get("hard") == "1" for request in deletes)
    assert not any(
        "/api/files" in path for _, path in _method_paths(requests)
    )

    rendered = json.dumps(result)
    for forbidden in (
        "cdn.example",
        "/podcasts",
        "fingerprint",
        "http",
        BACKEND_SECRET,
    ):
        assert forbidden not in rendered


async def test_delete_selected_prevalidation_rejects_with_zero_deletes(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000), _episode("ep2", 2000)]
    )
    routes = _delete_routes(item, ["ep1", "ep2"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    invalid: list[object] = [
        [],
        ["ep1", "ep1"],
        [f"ep{index}" for index in range(26)],
        ("ep1",),
        [123],
        [None],
        [""],
        ["a/b"],
    ]
    for episode_ids in invalid:
        with pytest.raises(ABSInvalidRequest):
            await client.delete_podcast_episodes(
                "pod1",
                episode_ids,  # type: ignore[arg-type]
                expected_fingerprint=fingerprint,
            )

    with pytest.raises(ABSNotFound, match="episode not found"):
        await client.delete_podcast_episodes(
            "pod1", ["ep9"], expected_fingerprint=fingerprint
        )
    with pytest.raises(ABSNotFound, match="episode not found"):
        await client.delete_podcast_episodes(
            "pod1",
            ["feed_" + "a" * 64],
            expected_fingerprint=fingerprint,
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_selected_refuses_active_queue_and_stale_fingerprint(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", 1000)])
    item["episodeDownloadsQueued"] = [
        _queue_entry("gx", "https://cdn.example/x.mp3")
    ]
    routes = _delete_routes(item, ["ep1"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="queued or active"):
        await client.delete_podcast_episodes(
            "pod1", ["ep1"], expected_fingerprint=fingerprint
        )

    item.pop("episodeDownloadsQueued")
    with pytest.raises(ABSInvalidRequest, match="retry with current settings"):
        await client.delete_podcast_episodes(
            "pod1", ["ep1"], expected_fingerprint="0" * 64
        )

    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.parametrize("case", ["episode-path", "item-path"])
async def test_delete_selected_prevalidates_all_paths_before_first_delete(
    podcast_factory: Any, case: str
) -> None:
    episodes = [_episode("ep1", 1000), _episode("ep2", 2000)]
    item = _podcast(episodes=episodes)
    if case == "episode-path":
        episodes[1]["audioFile"] = {
            "metadata": {"path": "/podcasts/other/evil.mp3"}
        }
    else:
        item["path"] = "/podcasts"

    routes = _delete_routes(item, ["ep1", "ep2"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="not safe to delete"):
        await client.delete_podcast_episodes(
            "pod1", ["ep1", "ep2"], expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_selected_stops_on_first_failure_without_retry(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode("ep1", 1000),
            _episode("ep2", 2000),
            _episode("ep3", 3000),
        ]
    )
    routes = _delete_routes(
        item,
        ["ep1", "ep2", "ep3"],
        fail={"ep2": httpx.Response(500, text=BACKEND_SECRET)},
    )
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["ep1", "ep2", "ep3"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "partial"
    assert result["requested_count"] == 3
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "unavailable" in result["error"]
    assert result["filesystem_removal"] == "not_verified"
    assert "may already have been applied" in result["note"]
    assert BACKEND_SECRET not in json.dumps(result)

    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert [request.url.path for request in deletes] == [
        "/api/podcasts/pod1/episode/ep1",
        "/api/podcasts/pod1/episode/ep2",
    ]


async def test_delete_selected_ambiguous_oversize_failure_is_not_retried(
    podcast_factory: Any,
) -> None:
    item = _podcast(episodes=[_episode("ep1", 1000)])
    oversized = b'{"episode": "' + b"z" * (4 * 1024 * 1024 + 1) + b'"}'
    routes = _delete_routes(
        item,
        ["ep1"],
        fail={
            "ep1": httpx.Response(
                200,
                content=oversized,
                headers={"Content-Type": "application/json"},
            )
        },
    )
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["ep1"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "partial"
    assert result["deleted_count"] == 0
    assert result["deleted_episode_ids"] == []
    assert result["failed_episode_id"] == "ep1"
    assert "size limit" in result["error"]
    assert "zzzz" not in json.dumps(result)
    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert len(deletes) == 1


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("policy", "changed during deletion"),
        ("queue", "queued or active"),
        ("item-path", "changed during deletion"),
    ],
)
async def test_delete_selected_detects_external_change_after_first_ack(
    podcast_factory: Any, case: str, message: str
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000), _episode("ep2", 2000)]
    )

    def after(episode_id: str) -> None:
        if case == "policy":
            item["media"]["maxEpisodesToKeep"] = 7
        elif case == "queue":
            item["episodesDownloading"] = [
                _queue_entry("gx", "https://cdn.example/x.mp3")
            ]
        else:
            item["path"] = "/podcasts/other"

    routes = _delete_routes(item, ["ep1", "ep2"], after=after)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["ep1", "ep2"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert message in result["error"]
    assert "fingerprint" not in json.dumps(result)
    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert len(deletes) == 1


async def test_delete_selected_late_unsafe_path_reports_partial_honestly(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000), _episode("ep2", 2000)]
    )
    library_reads = 0

    def library_route(request: httpx.Request) -> httpx.Response:
        nonlocal library_reads
        library_reads += 1
        payload = _podcast_library("lib1")
        if library_reads >= 3:
            # Same folder ID and unchanged item path, but the folder root
            # moved between the two per-target revalidations.
            payload["folders"] = [
                {
                    "id": "folder1",
                    "fullPath": "/different-root",
                    "libraryId": "lib1",
                }
            ]
        return httpx.Response(200, json=payload)

    handler = _delete_handler(item)
    routes: Routes = {
        ("GET", "/api/items/pod1"): lambda request: httpx.Response(
            200, json=item
        ),
        ("GET", "/api/libraries/lib1"): library_route,
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): handler,
        ("DELETE", "/api/podcasts/pod1/episode/ep2"): handler,
    }
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["ep1", "ep2"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "partial"
    assert result["requested_count"] == 2
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "this target was not deleted" in result["error"]
    assert "Earlier acknowledged deletions" in result["error"]
    assert "no files were changed" not in result["error"]
    assert result["filesystem_removal"] == "not_verified"
    assert library_reads == 3

    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert [request.url.path for request in deletes] == [
        "/api/podcasts/pod1/episode/ep1"
    ]
    assert [episode["id"] for episode in item["media"]["episodes"]] == [
        "ep2"
    ]


async def test_delete_selected_rejects_changed_safe_episode_path(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000), _episode("ep2", 2000)]
    )

    def after(episode_id: str) -> None:
        if episode_id != "ep1":
            return
        for episode in item["media"]["episodes"]:
            if episode["id"] == "ep2":
                # Same native ID, same safe item directory, different file.
                episode["audioFile"] = {
                    "metadata": {"path": "/podcasts/pod1/renamed.mp3"}
                }

    routes = _delete_routes(item, ["ep1", "ep2"], after=after)
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["ep1", "ep2"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["deleted_episode_ids"] == ["ep1"]
    assert result["failed_episode_id"] == "ep2"
    assert "changed during deletion" in result["error"]
    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert [request.url.path for request in deletes] == [
        "/api/podcasts/pod1/episode/ep1"
    ]


async def test_delete_selected_rejects_ambiguous_duplicate_native_rows(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[_episode("ep1", 1000), _episode("ep1", 2000)]
    )
    routes = _delete_routes(item, ["ep1"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    with pytest.raises(ABSResponseError, match="ambiguous"):
        await client.delete_podcast_episodes(
            "pod1", ["ep1"], expected_fingerprint=fingerprint
        )

    assert not any(request.method == "DELETE" for request in requests)


async def test_delete_selected_allows_distinct_ids_sharing_feed_identity(
    podcast_factory: Any,
) -> None:
    item = _podcast(
        episodes=[
            _episode(
                "epA", 1000, guid="same", url="https://cdn.example/same.mp3"
            ),
            _episode(
                "epB", 2000, guid="same", url="https://cdn.example/same.mp3"
            ),
        ]
    )
    routes = _delete_routes(item, ["epA", "epB"])
    transport, requests = _make_transport(routes)
    client = podcast_factory(transport)
    fingerprint = (await client.get_podcast_state("pod1"))["fingerprint"]

    result = await client.delete_podcast_episodes(
        "pod1", ["epA"], expected_fingerprint=fingerprint
    )

    assert result["status"] == "completed"
    assert result["deleted_episode_ids"] == ["epA"]
    deletes = [
        request for request in requests if request.method == "DELETE"
    ]
    assert [request.url.path for request in deletes] == [
        "/api/podcasts/pod1/episode/epA"
    ]
    assert [episode["id"] for episode in item["media"]["episodes"]] == [
        "epB"
    ]


async def test_new_podcast_mutations_require_writes_enabled(
    podcast_factory: Any,
) -> None:
    transport, requests = _make_transport({})
    client = podcast_factory(transport, writes=False)
    anchor = "feed_" + "a" * 64

    calls = [
        lambda: client.download_podcast_episodes("pod1", [anchor]),
        lambda: client.download_episodes_from("pod1", anchor, 5),
        lambda: client.delete_podcast_episodes(
            "pod1", ["ep1"], expected_fingerprint="f" * 64
        ),
    ]
    for call in calls:
        with pytest.raises(ABSNotAllowed, match="disabled"):
            await call()

    assert requests == []
