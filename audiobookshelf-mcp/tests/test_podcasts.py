"""Synthetic tests for bounded podcast reads, writes, and refusals.

Every backend response is local and synthetic. The URL guard is stubbed by
default so tests never resolve or contact a real host; the guard's own behavior
is tested with synthetic monkeypatches or in its dedicated test module.
"""

from __future__ import annotations

import json
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
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
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
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(200, text="OK"),
        ("DELETE", "/api/podcasts/pod1/episode/ep2"): httpx.Response(200, text="OK"),
        ("DELETE", "/api/podcasts/pod1/episode/ep3"): httpx.Response(200, text="OK"),
    }
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
    assert result["state"]["downloaded_count"] == 3


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

    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("GET", "/api/libraries/lib1"): _podcast_library("lib1"),
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
        ("DELETE", "/api/podcasts/pod1/episode/ep1"): httpx.Response(200, text="OK"),
        ("DELETE", "/api/podcasts/pod1/episode/ep2"): fail_ep2,
        ("DELETE", "/api/podcasts/pod1/episode/ep3"): httpx.Response(200, text="OK"),
    }
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
    assert "unavailable" in result["error"]
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
    routes: Routes = {
        ("GET", "/api/items/pod1"): item,
        ("PATCH", "/api/items/pod1/media"): {"updated": True},
    }
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
        "fingerprint",
    }
    assert state["downloaded_count"] == 2
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
    route_holder["item"] = queued
    with_queue = await client.get_podcast_state("pod1")
    assert with_queue["fingerprint"] != state["fingerprint"]


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
