"""Bounded podcast reads and writes on top of the fixed Audiobookshelf client.

All operations use the installed Audiobookshelf 2.37.2 native endpoints:

* ``GET /api/items/{id}?expanded=1`` (plus ``include=downloads`` for queue state)
* ``GET /api/libraries/{id}`` and ``GET /api/libraries/{id}/items``
* ``POST /api/podcasts/feed``
* ``POST /api/podcasts``
* ``POST /api/podcasts/{id}/download-episodes``
* ``PATCH /api/items/{id}/media``
* ``DELETE /api/podcasts/{id}/episode/{episodeId}?hard=1``
* ``DELETE /api/items/{id}?hard=1``

Discovery notes for 2.37.2 (read from the installed server source; no live
mutation was performed):

* Feed fetching and writing require an admin-owned API key.
* A podcast item's downloaded episodes are the only entries in
  ``media.episodes``. There is no separate public episode list.
* ``DELETE`` with ``hard=1`` acknowledges the database operation but logs and
  ignores filesystem-removal failures. An item delete removes the entire
  ``item.path`` recursively, so this adapter validates, before every hard
  delete, that the item is a direct child of the owning configured library
  folder and that each targeted episode file is inside that item directory.
  Backend paths are never accepted from a caller and are never returned.
  Symlinks are not visible through this API; this is not a claim of filesystem
  isolation. Actual Audiobookshelf egress filtering (including its SSRF filter
  flags) remains a deployment prerequisite; the public-URL preflight here is
  defense in depth, not a substitute.
* An over-size DELETE response can arrive after the backend applied the
  mutation. Callers receive one fixed sanitized error and no retry is attempted.
* ``maxEpisodesToKeep`` is applied natively only after an *automatic* episode
  download. The native retention path attempts to remove the oldest dated
  episode's audio file and only proceeds with database cleanup if that file
  removal succeeds; it does not trim an existing backlog when the policy
  changes. Immediate trimming here uses the native per-episode delete endpoint
  with ``hard=1``; that endpoint can still acknowledge a database delete after
  a logged file-removal failure, so filesystem removal is not verified.
* Retention does not consider listened state.
* ``GET /checknew`` and ``GET /clear-queue`` mutate state and are never used.
* A destructive delete or trim is refused while this podcast has queued or
  active downloads. Clearing or cancelling the native queue would itself be an
  unrequested mutation, and deleting underneath the native worker can race.

Disabling automatic downloads PATCHes only ``autoDownloadEpisodes: false``. The
existing schedule and per-check limit (including the native ``0`` unlimited
value) are preserved instead of being rewritten or rejected.

Concurrency limitation: the process-local lock serializes this service's own
writes, and the fingerprint is re-read under that lock. It is not an atomic
compare-and-swap against an Audiobookshelf administrator, the native scheduler,
or another process; those can change the item between the check and the write.
A read-back after a mutation can also be ambiguous if the backend times out
after applying it. Nothing is retried blindly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from .client import (
    ABSClient,
    ABSClientError,
    ABSInvalidRequest,
    ABSNotAllowed,
    ABSNotFound,
    ABSResponseError,
    _duration_seconds,
    _sanitize_text,
)
from .config import is_valid_resource_id
from .podcast_guards import ensure_public_url, validate_schedule

MAX_PODCAST_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EPISODE_LIST_LIMIT = 50
DEFAULT_EPISODE_LIST_LIMIT = 25
MAX_DOWNLOAD_COUNT = 25
MAX_NEW_EPISODES = 25
MAX_KEEP_EPISODES = 1000
MAX_TRIM_DELETIONS = 100
DUPLICATE_PAGE_SIZE = 100
MAX_DUPLICATE_PAGES = 10
MAX_EPISODE_TITLE_LENGTH = 300
MAX_PODCAST_TITLE_LENGTH = 300
MAX_AUTHOR_LENGTH = 120
MAX_SCHEDULE_LENGTH = 100

PODCAST_NOT_FOUND = "Podcast not found or not permitted."
WRITES_DISABLED = "Podcast write operations are disabled."
STALE_FINGERPRINT = "Podcast changed; retry with current settings."
_FEED_UNAVAILABLE = "Podcast has no usable RSS feed URL."
_DOWNLOADS_ACTIVE = (
    "Podcast has queued or active downloads; wait for them to finish before "
    "deleting or trimming."
)
_UNSAFE_ITEM_PATH = "Podcast path is not safe to delete; no files were changed."
_UNSAFE_EPISODE_PATH = (
    "Podcast episode path is not safe to delete; no files were changed."
)
_ENCLOSURE_ENCODING_INVALID = (
    "Podcast enclosure URL has invalid percent encoding."
)
_UNEXPECTED = "Audiobookshelf returned an unexpected response."

_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# One process-wide lock serializes every podcast mutation performed here. It
# deliberately does not coordinate with external Audiobookshelf writers.
_MUTATION_LOCK = asyncio.Lock()


class PodcastClient(ABSClient):
    """Fixed podcast read/write adapter; never exposes generic API input."""

    async def get_podcast_state(self, podcast_id: str) -> dict[str, Any]:
        """Return safe podcast policy/download state plus a stale-check digest."""
        item = await self._load_podcast(podcast_id)
        return self._state(item)

    async def list_podcast_episodes(
        self, podcast_id: str, limit: int = DEFAULT_EPISODE_LIST_LIMIT
    ) -> dict[str, Any]:
        """Return downloaded episodes newest first, with undated episodes last."""
        self._require_episode_limit(limit)
        item = await self._load_podcast(podcast_id)

        projected: list[tuple[int, str, dict[str, Any]]] = []
        for episode in self._downloaded_episodes(item):
            published_ms = _epoch_ms(episode.get("publishedAt"))
            sort_ms = published_ms if published_ms is not None else -1
            projected.append(
                (
                    sort_ms,
                    episode["id"],
                    {
                        "id": episode["id"],
                        "title": _sanitize_text(
                            episode.get("title"), MAX_EPISODE_TITLE_LENGTH
                        ),
                        "published_at": _iso_timestamp(published_ms),
                        "duration_seconds": _duration_seconds(
                            episode.get("duration")
                        ),
                    },
                )
            )

        projected.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        episodes = [entry[2] for entry in projected[:limit]]
        return {"podcast_id": podcast_id, "episodes": episodes, "count": len(episodes)}

    async def add_podcast(self, library_id: str, feed_url: str) -> dict[str, Any]:
        """Create a podcast in an allowed podcast library from a validated feed.

        The caller never supplies a folder or path. A directory named only by a
        fresh UUID is created under the library's first configured folder, so a
        feed title cannot become a path component.
        """
        self._require_writes_enabled()
        self._require_library(library_id)
        feed_url = await ensure_public_url(feed_url)

        async with _MUTATION_LOCK:
            library = await self._get_podcast_library(library_id)
            folder_id, folder_path = _first_folder(library)

            duplicate = await self._find_duplicate_feed(library_id, feed_url)
            if duplicate is not None:
                return {
                    "podcast_id": duplicate["podcast_id"],
                    "library_id": library_id,
                    "title": duplicate["title"],
                    "status": "already_exists",
                }

            metadata = await self._fetch_feed_metadata(feed_url)
            title = _sanitize_text(metadata.get("title"), MAX_PODCAST_TITLE_LENGTH)
            if not title:
                raise ABSResponseError("Podcast feed has no usable title.")
            author = _sanitize_text(metadata.get("author"), MAX_AUTHOR_LENGTH)

            safe_metadata: dict[str, Any] = {"title": title, "feedUrl": feed_url}
            if author:
                safe_metadata["author"] = author

            payload = {
                "libraryId": library_id,
                "folderId": folder_id,
                "path": f"{folder_path}/{uuid4()}",
                "media": {
                    "metadata": safe_metadata,
                    "autoDownloadEpisodes": False,
                },
                "tags": [],
            }
            data = await self._request_json(
                "POST",
                "api/podcasts",
                json_body=payload,
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
            )
            if not isinstance(data, dict):
                raise ABSResponseError(_UNEXPECTED)

            podcast_id = data.get("id")
            if (
                not is_valid_resource_id(podcast_id)
                or data.get("id") != podcast_id
                or data.get("libraryId") != library_id
                or data.get("mediaType") != "podcast"
            ):
                raise ABSResponseError(_UNEXPECTED)

            media = data.get("media")
            returned_metadata = media.get("metadata") if isinstance(media, dict) else None
            returned_title = _sanitize_text(
                returned_metadata.get("title") if isinstance(returned_metadata, dict) else None,
                MAX_PODCAST_TITLE_LENGTH,
            )
            return {
                "podcast_id": podcast_id,
                "library_id": library_id,
                "title": returned_title or title,
                "status": "created",
            }

    async def download_latest_episodes(
        self, podcast_id: str, count: int = 10
    ) -> dict[str, Any]:
        """Queue up to ``count`` recent, not-already-known episodes for download.

        The status is ``queued``, never ``completed``: Audiobookshelf downloads
        asynchronously. No feed URL, enclosure URL, or file path is returned.
        """
        self._require_writes_enabled()
        if type(count) is not int or not 1 <= count <= MAX_DOWNLOAD_COUNT:
            raise ABSInvalidRequest(
                f"Count must be an integer between 1 and {MAX_DOWNLOAD_COUNT}."
            )

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            media = item["media"]
            metadata = media.get("metadata")
            feed_url = metadata.get("feedUrl") if isinstance(metadata, dict) else None
            if not isinstance(feed_url, str) or not feed_url:
                raise ABSResponseError(_FEED_UNAVAILABLE)
            feed_url = await ensure_public_url(feed_url)

            blocked = self._blocked_episode_keys(item)
            podcast = await self._fetch_podcast(feed_url)
            candidates = _feed_candidates(podcast)

            selected: list[dict[str, Any]] = []
            selected_keys: set[str] = set()
            for candidate in candidates:
                keys = _episode_keys(candidate["guid"], candidate["url"])
                if keys & blocked or keys & selected_keys:
                    continue
                selected.append(candidate)
                selected_keys.update(keys)
                if len(selected) >= count:
                    break

            if not selected:
                return {
                    "podcast_id": podcast_id,
                    "status": "nothing_new",
                    "queued_count": 0,
                    "episodes": [],
                }

            # Validate every enclosure before sending any download request.
            typed_episodes: list[dict[str, Any]] = []
            for candidate in selected:
                enclosure_url = await ensure_public_url(candidate["url"])
                _require_native_download_url(enclosure_url)
                typed_episodes.append(
                    {
                        "title": candidate["title"],
                        "guid": candidate["guid"],
                        "publishedAt": candidate["published"],
                        "enclosure": {
                            "url": enclosure_url,
                            "type": candidate["type"],
                            "length": candidate["length"],
                        },
                    }
                )

            await self._request_json(
                "POST",
                f"api/podcasts/{podcast_id}/download-episodes",
                json_body=typed_episodes,
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
                allow_empty=True,
            )
            return {
                "podcast_id": podcast_id,
                "status": "queued",
                "queued_count": len(typed_episodes),
                "episodes": [
                    {
                        "title": episode["title"],
                        "published_at": _iso_timestamp(episode["publishedAt"]),
                    }
                    for episode in typed_episodes
                ],
            }

    async def set_podcast_schedule(
        self,
        podcast_id: str,
        enabled: bool,
        schedule: str,
        max_new_episodes: int,
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Update native automatic-download policy and return the read-back state.

        The server applies this immediately when the tool is called; there is no
        approval or elicitation flow here. The fingerprint is only a state fence
        against a stale instant read. Enabling also revalidates the podcast's
        existing feed URL, since the native scheduler will fetch it later.
        Disabling PATCHes only ``autoDownloadEpisodes: false`` and preserves the
        existing schedule and per-check limit, including the native ``0``
        unlimited value. A disable never clears the download queue or deletes
        files.
        """
        self._require_writes_enabled()
        if type(enabled) is not bool:
            raise ABSInvalidRequest("Enabled must be a boolean.")
        if not isinstance(schedule, str):
            raise ABSInvalidRequest("Schedule must be a string.")
        if type(max_new_episodes) is not int:
            raise ABSInvalidRequest("Max new episodes must be an integer.")
        expected = _require_fingerprint(expected_fingerprint)

        payload: dict[str, Any]
        if enabled:
            if not 1 <= max_new_episodes <= MAX_NEW_EPISODES:
                raise ABSInvalidRequest(
                    "Max new episodes must be an integer between "
                    f"1 and {MAX_NEW_EPISODES}."
                )
            payload = {
                "autoDownloadEpisodes": True,
                "autoDownloadSchedule": validate_schedule(schedule.strip()),
                "maxNewEpisodesToDownload": max_new_episodes,
            }
        else:
            payload = {"autoDownloadEpisodes": False}

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            current = self._state(item)
            _require_unchanged(current, expected)

            if enabled:
                media = item["media"]
                metadata = media.get("metadata")
                feed_url = (
                    metadata.get("feedUrl") if isinstance(metadata, dict) else None
                )
                if not isinstance(feed_url, str) or not feed_url:
                    raise ABSResponseError(_FEED_UNAVAILABLE)
                # The scheduler fetches this URL later; reapply the URL guard
                # now rather than silently scheduling a non-public feed.
                await ensure_public_url(feed_url)

            await self._request_json(
                "PATCH",
                f"api/items/{podcast_id}/media",
                json_body=payload,
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
                allow_empty=True,
            )
            updated = await self._load_podcast(podcast_id)
            return self._state(updated)

    async def set_podcast_retention(
        self,
        podcast_id: str,
        keep: int,
        trim_existing: bool,
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Set native retention and optionally delete the oldest surplus now.

        The server applies this immediately when the tool is called; there is no
        approval or elicitation flow here. The fingerprint is only a state fence
        against a stale instant read. A trim is bounded to 100 deletions per call
        and is refused before the policy PATCH if it would need more. Deletion
        stops at the first failure and reports the honest partial count; there
        is no rollback or retry.
        """
        self._require_writes_enabled()
        if type(keep) is not int or not 0 <= keep <= MAX_KEEP_EPISODES:
            raise ABSInvalidRequest(
                f"Keep must be an integer between 0 and {MAX_KEEP_EPISODES}."
            )
        if type(trim_existing) is not bool:
            raise ABSInvalidRequest("Trim existing must be a boolean.")
        if trim_existing and keep == 0:
            raise ABSInvalidRequest(
                "Cannot trim existing episodes with an unlimited retention policy."
            )
        expected = _require_fingerprint(expected_fingerprint)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            current = self._state(item)
            _require_unchanged(current, expected)

            plan: list[str] = []
            plan_episodes: list[dict[str, Any]] = []
            if trim_existing:
                if self._queue_entries(item):
                    raise ABSResponseError(_DOWNLOADS_ACTIVE)
                downloaded = self._downloaded_episodes(item)
                newest_first = sorted(
                    downloaded,
                    key=lambda episode: (
                        _epoch_ms(episode.get("publishedAt")) or -1,
                        episode["id"],
                    ),
                    reverse=True,
                )
                # Deletion order is oldest first for deterministic, bounded work.
                plan_episodes = list(reversed(newest_first[keep:]))
                if len(plan_episodes) > MAX_TRIM_DELETIONS:
                    raise ABSInvalidRequest(
                        "Retention trim would delete more than "
                        f"{MAX_TRIM_DELETIONS} episodes; narrow the policy or "
                        "run it again with a smaller trim."
                    )
                if plan_episodes:
                    library = await self._get_podcast_library(
                        current["library_id"]
                    )
                    item_parts = _validate_podcast_item_path(item, library)
                    for episode in plan_episodes:
                        _validate_episode_path(episode, item_parts)
                    plan = [episode["id"] for episode in plan_episodes]

            await self._request_json(
                "PATCH",
                f"api/items/{podcast_id}/media",
                json_body={"maxEpisodesToKeep": keep},
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
                allow_empty=True,
            )

            deleted_count = 0
            status = "updated"
            error: str | None = None
            if trim_existing:
                status = "completed"
                for episode_id in plan:
                    try:
                        await self._delete_episode(podcast_id, episode_id)
                    except ABSClientError as exc:
                        status = "partial"
                        error = str(exc)
                        break
                    deleted_count += 1

            outcome: dict[str, Any] = {
                "podcast_id": podcast_id,
                "library_id": current["library_id"],
                "keep_episodes": keep,
                "trim_existing": trim_existing,
                "deleted_count": deleted_count,
                "status": status,
            }
            if trim_existing:
                outcome["filesystem_removal"] = "not_verified"
                outcome["note"] = (
                    "Deleted counts are acknowledged Audiobookshelf "
                    "operations; filesystem removal is not verified."
                )
            else:
                outcome["note"] = (
                    "Existing episodes are not removed immediately. "
                    "Audiobookshelf applies this limit during future automatic "
                    "downloads; it does not reduce an existing over-limit backlog."
                )
            if error is not None:
                outcome["error"] = error

            try:
                updated = await self._load_podcast(podcast_id)
            except ABSClientError:
                # The policy write may have succeeded; do not retry, and do not
                # hide the count from a read-back failure.
                updated = None
            outcome["state"] = self._state(updated) if updated is not None else None
            return outcome

    async def delete_podcast_episode(
        self,
        podcast_id: str,
        episode_id: str,
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Delete one downloaded episode that belongs to the validated podcast.

        Backend file removal is requested with ``hard=1`` but is not verified.
        """
        self._require_writes_enabled()
        if not is_valid_resource_id(podcast_id):
            raise ABSInvalidRequest("Invalid podcast ID.")
        if not is_valid_resource_id(episode_id):
            raise ABSInvalidRequest("Invalid episode ID.")
        expected = _require_fingerprint(expected_fingerprint)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            _require_unchanged(self._state(item), expected)
            if self._queue_entries(item):
                raise ABSResponseError(_DOWNLOADS_ACTIVE)
            episode = next(
                (
                    candidate
                    for candidate in self._downloaded_episodes(item)
                    if candidate["id"] == episode_id
                ),
                None,
            )
            if episode is None:
                raise ABSNotFound("Podcast episode not found.")

            library = await self._get_podcast_library(item["libraryId"])
            item_parts = _validate_podcast_item_path(item, library)
            _validate_episode_path(episode, item_parts)

            await self._delete_episode(podcast_id, episode_id)
            return {
                "podcast_id": podcast_id,
                "episode_id": episode_id,
                "status": "deleted",
                "deleted_count": 1,
                "filesystem_removal": "not_verified",
                "note": (
                    "Audiobookshelf acknowledged the delete; filesystem "
                    "removal is not verified."
                ),
            }

    async def delete_podcast(
        self, podcast_id: str, *, expected_fingerprint: str
    ) -> dict[str, Any]:
        """Delete a validated podcast item via the native hard-delete endpoint.

        Never deletes books or libraries. Backend file removal is requested but
        is not verified.
        """
        self._require_writes_enabled()
        if not is_valid_resource_id(podcast_id):
            raise ABSInvalidRequest("Invalid podcast ID.")
        expected = _require_fingerprint(expected_fingerprint)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            current = self._state(item)
            _require_unchanged(current, expected)
            if self._queue_entries(item):
                raise ABSResponseError(_DOWNLOADS_ACTIVE)

            library = await self._get_podcast_library(current["library_id"])
            _validate_podcast_item_path(item, library)

            await self._request_json(
                "DELETE",
                f"api/items/{podcast_id}",
                params={"hard": "1"},
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
                allow_empty=True,
            )
            return {
                "podcast_id": podcast_id,
                "library_id": current["library_id"],
                "status": "deleted",
                "filesystem_removal": "not_verified",
                "note": (
                    "Audiobookshelf acknowledged the item delete; filesystem "
                    "removal is not verified."
                ),
            }

    async def _load_podcast(self, podcast_id: str) -> dict[str, Any]:
        """Fetch and validate one expanded podcast item, or fail generically.

        ``include=downloads`` is always requested so callers can fail closed on
        destructive operations while the native worker is active and so the
        state-fence fingerprint can cover queue changes. Queue details are never
        returned to callers.
        """
        if not is_valid_resource_id(podcast_id):
            raise ABSInvalidRequest("Invalid podcast ID.")

        try:
            data = await self._request_json(
                "GET",
                f"api/items/{podcast_id}",
                params={"expanded": "1", "include": "downloads"},
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
            )
        except (ABSNotFound, ABSResponseError):
            raise ABSNotAllowed(PODCAST_NOT_FOUND) from None

        if not isinstance(data, dict) or data.get("id") != podcast_id:
            raise ABSNotAllowed(PODCAST_NOT_FOUND)
        library_id = data.get("libraryId")
        if (
            not isinstance(library_id, str)
            or library_id not in self._allowed_library_ids
            or data.get("mediaType") != "podcast"
            or not isinstance(data.get("media"), dict)
        ):
            raise ABSNotAllowed(PODCAST_NOT_FOUND)
        return data

    async def _get_podcast_library(self, library_id: str) -> dict[str, Any]:
        """Fetch the exact allowed library and require its native type to be podcast."""
        data = await self._request_json(
            "GET",
            f"api/libraries/{library_id}",
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
        )
        if (
            not isinstance(data, dict)
            or data.get("id") != library_id
            or data.get("mediaType") != "podcast"
        ):
            raise ABSNotAllowed("Podcast library not found or not permitted.")
        return data

    async def _find_duplicate_feed(
        self, library_id: str, feed_url: str
    ) -> dict[str, Any] | None:
        """Scan bounded minified pages for the same normalized feed URL."""
        target = _normalize_feed_url(feed_url)
        if target is None:
            raise ABSInvalidRequest("Invalid feed URL.")

        fetched = 0
        for page in range(MAX_DUPLICATE_PAGES):
            data = await self._request_json(
                "GET",
                f"api/libraries/{library_id}/items",
                params={
                    "minified": "1",
                    "limit": str(DUPLICATE_PAGE_SIZE),
                    "page": str(page),
                },
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
            )
            if not isinstance(data, dict):
                raise ABSResponseError(_UNEXPECTED)
            results = data.get("results")
            total = data.get("total")
            if (
                not isinstance(results, list)
                or type(total) is not int
                or total < 0
                or total > DUPLICATE_PAGE_SIZE * MAX_DUPLICATE_PAGES
            ):
                raise ABSResponseError(
                    "Could not complete the duplicate podcast check."
                )

            for raw in results:
                if not isinstance(raw, dict):
                    raise ABSResponseError(_UNEXPECTED)
                item_id = raw.get("id")
                if (
                    not is_valid_resource_id(item_id)
                    or raw.get("libraryId") != library_id
                    or not isinstance(raw.get("media"), dict)
                ):
                    raise ABSResponseError(_UNEXPECTED)
                if raw.get("mediaType") != "podcast":
                    continue
                metadata = raw["media"].get("metadata")
                if not isinstance(metadata, dict):
                    raise ABSResponseError(_UNEXPECTED)
                if _normalize_feed_url(metadata.get("feedUrl")) == target:
                    return {
                        "podcast_id": item_id,
                        "title": _sanitize_text(
                            metadata.get("title"), MAX_PODCAST_TITLE_LENGTH
                        ),
                    }

            fetched += len(results)
            if fetched >= total:
                return None
            if not results:
                raise ABSResponseError(
                    "Could not complete the duplicate podcast check."
                )

        raise ABSResponseError("Could not complete the duplicate podcast check.")

    async def _fetch_podcast(self, feed_url: str) -> dict[str, Any]:
        data = await self._request_json(
            "POST",
            "api/podcasts/feed",
            json_body={"rssFeed": feed_url},
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
        )
        if not isinstance(data, dict) or not isinstance(data.get("podcast"), dict):
            raise ABSResponseError(_UNEXPECTED)
        return data["podcast"]

    async def _fetch_feed_metadata(self, feed_url: str) -> dict[str, Any]:
        podcast = await self._fetch_podcast(feed_url)
        metadata = podcast.get("metadata")
        if not isinstance(metadata, dict):
            raise ABSResponseError(_UNEXPECTED)
        return metadata

    def _state(self, item: Mapping[str, Any]) -> dict[str, Any]:
        media = item["media"]
        library_id = item["libraryId"]
        item_id = item["id"]

        schedule_value = media.get("autoDownloadSchedule")
        schedule = (
            _sanitize_text(schedule_value, MAX_SCHEDULE_LENGTH)
            if isinstance(schedule_value, str)
            else ""
        )
        auto_download = media.get("autoDownloadEpisodes") is True
        max_new = _policy_int(media.get("maxNewEpisodesToDownload"))
        keep = _policy_int(media.get("maxEpisodesToKeep"))
        downloaded_ids = sorted(
            episode["id"] for episode in self._downloaded_episodes(item)
        )
        queue_entries = self._queue_entries(item)
        queue_keys = sorted(
            key
            for entry in queue_entries
            for key in _episode_keys(entry.get("guid"), entry.get("url"))
        )
        metadata = media.get("metadata")
        feed_url_hash = _hash_feed_url(
            metadata.get("feedUrl") if isinstance(metadata, dict) else None
        )

        digest = hashlib.sha256(
            json.dumps(
                {
                    "media_type": item.get("mediaType"),
                    "item_id": item_id,
                    "library_id": library_id,
                    "auto_download": auto_download,
                    "schedule": schedule,
                    "max_new_episodes": max_new,
                    "keep_episodes": keep,
                    "downloaded_episode_ids": downloaded_ids,
                    "downloads_active": bool(queue_entries),
                    "queue_keys": queue_keys,
                    "feed_url_sha256": feed_url_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

        return {
            "podcast_id": item_id,
            "library_id": library_id,
            "auto_download": auto_download,
            "schedule": schedule,
            "max_new_episodes": max_new,
            "keep_episodes": keep,
            "downloaded_count": len(downloaded_ids),
            "fingerprint": digest,
        }

    def _downloaded_episodes(self, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        media = item.get("media")
        if not isinstance(media, dict):
            raise ABSNotAllowed(PODCAST_NOT_FOUND)
        episodes = media.get("episodes")
        if episodes is None:
            return []
        if not isinstance(episodes, list):
            raise ABSResponseError(_UNEXPECTED)

        downloaded: list[dict[str, Any]] = []
        for episode in episodes:
            if not isinstance(episode, dict) or not is_valid_resource_id(
                episode.get("id")
            ):
                raise ABSResponseError(_UNEXPECTED)
            downloaded.append(episode)
        return downloaded

    def _queue_entries(self, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return validated queued/current download entries; never their URLs."""
        entries: list[dict[str, Any]] = []
        for field in ("episodeDownloadsQueued", "episodesDownloading"):
            value = item.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ABSResponseError(_UNEXPECTED)
            for entry in value:
                if not isinstance(entry, dict):
                    raise ABSResponseError(_UNEXPECTED)
                entries.append(entry)
        return entries

    def _blocked_episode_keys(self, item: Mapping[str, Any]) -> set[str]:
        keys: set[str] = set()
        for episode in self._downloaded_episodes(item):
            keys.update(
                _episode_keys(episode.get("guid"), _episode_enclosure_url(episode))
            )
        for entry in self._queue_entries(item):
            keys.update(_episode_keys(entry.get("guid"), entry.get("url")))
        return keys

    async def _delete_episode(self, podcast_id: str, episode_id: str) -> None:
        await self._request_json(
            "DELETE",
            f"api/podcasts/{podcast_id}/episode/{episode_id}",
            params={"hard": "1"},
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
            allow_empty=True,
        )

    def _require_writes_enabled(self) -> None:
        if self._podcast_writes_enabled is not True:
            raise ABSNotAllowed(WRITES_DISABLED)

    def _require_episode_limit(self, limit: object) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_EPISODE_LIST_LIMIT:
            raise ABSInvalidRequest(
                f"Limit must be an integer between 1 and {MAX_EPISODE_LIST_LIMIT}."
            )


def _first_folder(library: Mapping[str, Any]) -> tuple[str, str]:
    folders = library.get("folders")
    if not isinstance(folders, list) or not folders:
        raise ABSResponseError("Podcast library has no configured folder.")
    folder = folders[0]
    if not isinstance(folder, dict):
        raise ABSResponseError("Podcast library has no configured folder.")

    folder_id = folder.get("id")
    full_path = folder.get("fullPath")
    folder_library_id = folder.get("libraryId")
    if (
        not is_valid_resource_id(folder_id)
        or (
            folder_library_id is not None
            and folder_library_id != library.get("id")
        )
    ):
        raise ABSResponseError("Podcast library has no usable folder.")

    parts = _normalized_abs_parts(full_path)
    if parts is None:
        raise ABSResponseError("Podcast library has no usable folder.")
    return folder_id, "/" + "/".join(parts)


def _normalized_abs_parts(value: object) -> tuple[str, ...] | None:
    """Parse an absolute POSIX path into plain, already-normalized components.

    Root, relative paths, backslashes, control bytes, empty components, ``.``
    and ``..`` are all rejected. Nothing is normalized by collapsing a
    traversal; such a path is simply refused.
    """
    if not isinstance(value, str) or len(value) > 4096:
        return None
    if "\\" in value or not value.startswith("/"):
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    trimmed = value.rstrip("/")
    if not trimmed:
        return None
    parts = trimmed.split("/")[1:]
    if any(not part or part in {".", ".."} for part in parts):
        return None
    return tuple(parts)


def _library_folder_by_id(
    library: Mapping[str, Any], folder_id: object
) -> dict[str, Any] | None:
    folders = library.get("folders")
    if not isinstance(folders, list):
        return None
    for folder in folders:
        if not isinstance(folder, dict) or folder.get("id") != folder_id:
            continue
        folder_library_id = folder.get("libraryId")
        if folder_library_id is not None and folder_library_id != library.get("id"):
            return None
        return folder
    return None


def _validate_podcast_item_path(
    item: Mapping[str, Any], library: Mapping[str, Any]
) -> tuple[str, ...]:
    """Require the item directory to be a direct child of its owning folder.

    This is the fence before an item-wide hard delete, which removes
    ``item.path`` recursively. A library-root path, a nested path, a traversal,
    or a folder that does not belong to the returned podcast library is refused
    without exposing any path text.
    """
    folder = _library_folder_by_id(library, item.get("folderId"))
    if folder is None:
        raise ABSResponseError(_UNSAFE_ITEM_PATH)
    folder_parts = _normalized_abs_parts(folder.get("fullPath"))
    item_parts = _normalized_abs_parts(item.get("path"))
    if folder_parts is None or item_parts is None:
        raise ABSResponseError(_UNSAFE_ITEM_PATH)
    if (
        len(item_parts) != len(folder_parts) + 1
        or item_parts[: len(folder_parts)] != folder_parts
    ):
        raise ABSResponseError(_UNSAFE_ITEM_PATH)
    return item_parts


def _validate_episode_path(
    episode: Mapping[str, Any], item_parts: tuple[str, ...]
) -> None:
    """Require a downloaded episode file to sit strictly inside the item dir."""
    audio_file = episode.get("audioFile")
    metadata = audio_file.get("metadata") if isinstance(audio_file, dict) else None
    file_parts = _normalized_abs_parts(
        metadata.get("path") if isinstance(metadata, dict) else None
    )
    if (
        file_parts is None
        or len(file_parts) <= len(item_parts)
        or file_parts[: len(item_parts)] != item_parts
    ):
        raise ABSResponseError(_UNSAFE_EPISODE_PATH)


def _is_utf8_text(value: str | None) -> bool:
    """Return whether a backend string is encodable as strict UTF-8."""
    if value is None:
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _feed_candidates(podcast: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return usable feed episodes, skipping entries that cannot be encoded.

    An entry is skipped when its guid, enclosure type, or enclosure length is
    not strict UTF-8: such a lone surrogate would crash JSON serialization of
    the download request after the backend response was already accepted.
    """
    episodes = podcast.get("episodes")
    if not isinstance(episodes, list):
        raise ABSResponseError(_UNEXPECTED)

    candidates: list[dict[str, Any]] = []
    for raw in episodes:
        if not isinstance(raw, dict):
            raise ABSResponseError(_UNEXPECTED)
        enclosure = raw.get("enclosure")
        if enclosure is not None and not isinstance(enclosure, dict):
            raise ABSResponseError(_UNEXPECTED)
        url = enclosure.get("url") if isinstance(enclosure, dict) else None
        title = _sanitize_text(raw.get("title"), MAX_EPISODE_TITLE_LENGTH)
        published = _epoch_ms(raw.get("publishedAt"))
        if not title or not isinstance(url, str) or not url.strip() or published is None:
            # Feed entries without a playable enclosure or valid date cannot be
            # ordered or downloaded safely; they are not silently guessed.
            continue

        enclosure_type = (
            enclosure.get("type") if isinstance(enclosure.get("type"), str) else None
        )
        raw_length = enclosure.get("length")
        if isinstance(raw_length, bool) or not isinstance(raw_length, (str, int)):
            enclosure_length = None
        else:
            enclosure_length = str(raw_length)

        guid = raw.get("guid")
        normalized_guid = (
            guid.strip() if isinstance(guid, str) and guid.strip() else None
        )
        if (
            not _is_utf8_text(normalized_guid)
            or not _is_utf8_text(enclosure_type)
            or not _is_utf8_text(enclosure_length)
        ):
            # Backend feed metadata that is not strict UTF-8 cannot be sent in
            # the download request; treat it like any other unusable entry.
            continue

        candidates.append(
            {
                "title": title,
                "guid": normalized_guid,
                "published": published,
                "url": url.strip(),
                "type": enclosure_type,
                "length": enclosure_length,
            }
        )

    candidates.sort(
        key=lambda candidate: (
            candidate["published"],
            candidate["guid"] or candidate["url"],
        ),
        reverse=True,
    )
    return candidates


def _require_native_download_url(url: object) -> None:
    """Reject percent encoding that native ``decodeURIComponent`` would throw on.

    ``PodcastEpisodeDownload.setData`` calls ``decodeURIComponent(url)`` without
    a guard after the download POST has been accepted. This is a native
    download contract, not a general URL policy: every percent escape must be a
    hex pair and the decoded bytes must be strict UTF-8. The fixed error never
    contains the URL.
    """
    if not isinstance(url, str) or _MALFORMED_PERCENT_RE.search(url):
        raise ABSInvalidRequest(_ENCLOSURE_ENCODING_INVALID)
    try:
        unquote(url, errors="strict")
    except UnicodeDecodeError:
        raise ABSInvalidRequest(_ENCLOSURE_ENCODING_INVALID) from None


def _episode_keys(guid: object, url: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(guid, str) and guid.strip():
        keys.add("guid:" + guid.strip())
    if isinstance(url, str) and url.strip():
        cleaned = url.strip()
        keys.add("url:" + cleaned)
        unquoted = unquote(cleaned)
        if unquoted != cleaned:
            keys.add("url:" + unquoted)
    return keys


def _episode_enclosure_url(episode: Mapping[str, Any]) -> str | None:
    enclosure = episode.get("enclosure")
    if not isinstance(enclosure, dict):
        return None
    url = enclosure.get("url")
    return url if isinstance(url, str) else None


def _hash_feed_url(value: object) -> str | None:
    """Hash a raw feed URL for stale detection without returning or logging it."""
    if not isinstance(value, str) or not value.strip():
        return None
    return hashlib.sha256(
        value.strip().encode("utf-8", "surrogatepass")
    ).hexdigest()


def _normalize_feed_url(value: object) -> str | None:
    """Normalize only scheme/host case, default port, and fragment.

    Paths and query strings are compared exactly so private feed tokens are
    never conflated. This is exact normalized-URL matching, not alias merging.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None

    host = hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path, parts.query, ""))


def _epoch_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except (OverflowError, ValueError):
        return None
    return int(value)


def _iso_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _policy_int(value: object) -> int:
    if type(value) is int and 0 <= value <= 1_000_000:
        return value
    return 0


def _require_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ABSInvalidRequest("Invalid expected fingerprint.")
    return value


def _require_unchanged(current: Mapping[str, Any], expected: str) -> None:
    if current["fingerprint"] != expected:
        raise ABSInvalidRequest(STALE_FINGERPRINT)
