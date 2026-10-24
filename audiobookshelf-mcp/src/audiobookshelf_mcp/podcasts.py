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

Feed catalogue selectors are opaque hashes of
``(podcast_id, guid, enclosure URL, publication ms)``. They are not cached
authority: every selector-based download (``download_podcast_episodes`` and
``download_episodes_from``) reloads the allowed podcast, refetches its stored
feed, and resolves each selector against the current catalogue. After the
enclosure URL preflight, the item is reloaded once more to confirm the same
podcast, folder, item path, and stored feed URL and to drop entries that became
downloaded or queued meanwhile. That final read is followed by the native POST,
so an external writer can still race the request; no atomic compare-and-swap
exists. The latest-episode tool picks its own candidates instead of resolving
caller selectors, so this extra late fence applies only to selector-based
downloads. Multi-delete re-reads before every DELETE and compares the target
episode's identity and file metadata with the prevalidated snapshot because the
state fingerprint does not cover paths or enclosure metadata.
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
    _validate_query,
)
from .config import is_valid_resource_id
from .podcast_guards import ensure_public_url, validate_schedule

MAX_PODCAST_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EPISODE_LIST_LIMIT = 50
DEFAULT_EPISODE_LIST_LIMIT = 25
MAX_EPISODE_OFFSET = 100000
MAX_SELECTED_EPISODES = 25
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
_UNKNOWN_EPISODE_SELECTION = (
    "Unknown or stale podcast episode selection; refresh the catalogue."
)
_CHANGED_DURING_DELETE = (
    "Podcast changed during deletion; stopped without retrying."
)
_CHANGED_BEFORE_DOWNLOAD = (
    "Podcast changed before download could be queued; nothing was queued."
)
_AMBIGUOUS_EPISODE_ROWS = (
    "Podcast episode data is ambiguous; no files were changed."
)
_UNSAFE_DURING_DELETE = (
    "Podcast path or episode file became unsafe during deletion; this target "
    "was not deleted. Earlier acknowledged deletions, if any, remain."
)
_FEED_CATALOGUE_NOTE = (
    "Catalogue lists metadata-eligible entries from the current RSS feed; "
    "enclosure URL preflight occurs only on download; not a complete archive."
)
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
_FEED_SELECTOR_RE = re.compile(r"\Afeed_[0-9a-f]{64}\Z")

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
        self,
        podcast_id: str,
        limit: int = DEFAULT_EPISODE_LIST_LIMIT,
        *,
        query: str | None = None,
        order: str = "newest",
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded page of downloaded episodes.

        ``query`` filters on a case-insensitive title substring. ``order`` is
        ``newest`` or ``oldest``; undated episodes always sort last. ``count``
        is the size of this page and ``total`` is the filtered catalogue size.
        """
        self._require_episode_limit(limit)
        query = _require_episode_query(query)
        order = _require_episode_order(order)
        offset = _require_episode_offset(offset)
        item = await self._load_podcast(podcast_id)

        projected: list[dict[str, Any]] = []
        for episode in self._downloaded_episodes(item):
            published_ms = _epoch_ms(episode.get("publishedAt"))
            projected.append(
                {
                    "id": episode["id"],
                    "published_ms": published_ms,
                    "view": {
                        "id": episode["id"],
                        "title": _sanitize_text(
                            episode.get("title"), MAX_EPISODE_TITLE_LENGTH
                        ),
                        "published_at": _iso_timestamp(published_ms),
                        "duration_seconds": _duration_seconds(
                            episode.get("duration")
                        ),
                    },
                }
            )

        views = _order_downloaded_views(projected, order)
        views = _filter_titles(views, query)
        page, paging = _paginate_views(views, offset, limit)
        return {
            "podcast_id": podcast_id,
            "episodes": page,
            "count": len(page),
            **paging,
        }

    async def list_available_podcast_episodes(
        self,
        podcast_id: str,
        limit: int = DEFAULT_EPISODE_LIST_LIMIT,
        *,
        query: str | None = None,
        order: str = "newest",
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded page of metadata-eligible current feed entries.

        The feed URL is taken only from the reloaded allowed podcast item; a
        caller never supplies it. IDs are opaque, podcast-bound selectors, not
        cached authority: every download resolves them against a freshly
        fetched feed. Listing only skips entries without a usable title, a
        valid publication date, or a non-empty enclosure URL, and entries whose
        guid or enclosure metadata is not strict UTF-8. Public URL and
        percent-encoding checks are deferred until an episode is actually
        selected for download, so this is the current feed, not a complete
        archive.
        """
        self._require_episode_limit(limit)
        query = _require_episode_query(query)
        order = _require_episode_order(order)
        offset = _require_episode_offset(offset)
        item = await self._load_podcast(podcast_id)
        podcast = await self._fetch_stored_feed(item)

        catalogue = _feed_catalogue(podcast_id, podcast)
        downloaded_index = _downloaded_match_index(
            self._downloaded_episodes(item)
        )
        queued_keys = _queue_keys(self._queue_entries(item))

        views: list[dict[str, Any]] = []
        for entry in _order_catalogue(catalogue, order):
            keys = _episode_keys(entry["guid"], entry["url"])
            matched: set[str] = set()
            for key in keys:
                matched.update(downloaded_index.get(key, ()))
            view: dict[str, Any] = {
                "id": entry["id"],
                "title": entry["title"],
                "published_at": _iso_timestamp(entry["published"]),
                "downloaded": bool(matched),
                "queued": bool(keys & queued_keys),
            }
            if len(matched) == 1:
                view["downloaded_episode_id"] = next(iter(matched))
            views.append(view)

        views = _filter_titles(views, query)
        page, paging = _paginate_views(views, offset, limit)
        return {
            "podcast_id": podcast_id,
            "episodes": page,
            "count": len(page),
            **paging,
            "note": _FEED_CATALOGUE_NOTE,
        }

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
            blocked = self._blocked_episode_keys(item)
            podcast = await self._fetch_stored_feed(item)
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
            typed_episodes = await self._queue_candidates(
                podcast_id, selected
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

    async def download_podcast_episodes(
        self, podcast_id: str, episode_ids: list[str]
    ) -> dict[str, Any]:
        """Queue selected current-feed episodes in the caller's order.

        Every selector is resolved against a freshly reloaded podcast and feed
        under the mutation lock; one unknown or stale selector rejects the
        whole selection before any download is queued. Already downloaded or
        queued episodes are skipped without backfilling. ``queued`` means
        submitted to the asynchronous native queue, never completed.
        """
        self._require_writes_enabled()
        selectors = _require_feed_selectors(episode_ids)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            podcast = await self._fetch_stored_feed(item)
            catalogue = _feed_catalogue(podcast_id, podcast)
            by_selector = {entry["id"]: entry for entry in catalogue}

            selected: list[dict[str, Any]] = []
            for selector in selectors:
                entry = by_selector.get(selector)
                if entry is None:
                    raise ABSInvalidRequest(_UNKNOWN_EPISODE_SELECTION)
                selected.append(entry)

            queued = await self._queue_selected_episodes(
                podcast_id, selected, item
            )
            return _queue_result(
                podcast_id, len(selected), queued, len(selected) - len(queued)
            )

    async def download_episodes_from(
        self, podcast_id: str, start_episode_id: str, count: int = 10
    ) -> dict[str, Any]:
        """Queue a fixed chronological window starting at an anchor episode.

        The window is the anchor plus the next ``count - 1`` current-feed
        episodes in publication order (ascending, opaque ID tie-break), so the
        anchor is the oldest entry in the window. The window is fixed before
        skipping: already downloaded or queued entries inside it are skipped
        without backfilling, and a window near the end of the feed may contain
        fewer than ``count`` entries. An anchor that is not in the freshly
        fetched feed is rejected; there is no fallback or latest-episode
        guess. ``queued`` means submitted to the asynchronous native queue,
        never completed.
        """
        self._require_writes_enabled()
        if type(count) is not int or not 1 <= count <= MAX_SELECTED_EPISODES:
            raise ABSInvalidRequest(
                f"Count must be an integer between 1 and {MAX_SELECTED_EPISODES}."
            )
        anchor = _require_feed_selector(start_episode_id)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            podcast = await self._fetch_stored_feed(item)
            catalogue = _feed_catalogue(podcast_id, podcast)
            chronological = _order_catalogue(catalogue, "oldest")
            start = next(
                (
                    index
                    for index, entry in enumerate(chronological)
                    if entry["id"] == anchor
                ),
                None,
            )
            if start is None:
                raise ABSInvalidRequest(_UNKNOWN_EPISODE_SELECTION)

            window = chronological[start : start + count]
            queued = await self._queue_selected_episodes(
                podcast_id, window, item
            )
            return _queue_result(
                podcast_id, len(window), queued, len(window) - len(queued)
            )

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

    async def delete_podcast_episodes(
        self,
        podcast_id: str,
        episode_ids: list[str],
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Delete selected downloaded episodes one at a time, without retries.

        All targets, the item path, and every target episode path are
        validated before the first DELETE. Before each DELETE the podcast is
        reloaded and compared with the state expected after only the deletions
        already acknowledged here, and the target's identity and file metadata
        must still match the prevalidated snapshot; an external change stops
        the run. A failure stops immediately with the honest partial count.
        There is no rollback and no retry, and filesystem removal is not
        verified.
        """
        self._require_writes_enabled()
        targets = _require_native_episode_ids(episode_ids)
        expected = _require_fingerprint(expected_fingerprint)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            _require_unchanged(self._state(item), expected)
            if self._queue_entries(item):
                raise ABSResponseError(_DOWNLOADS_ACTIVE)

            episodes = self._downloaded_episodes(item)
            by_id = {episode["id"]: episode for episode in episodes}
            if any(target not in by_id for target in targets):
                raise ABSNotFound("Podcast episode not found.")
            row_counts: dict[str, int] = {}
            for episode in episodes:
                row_counts[episode["id"]] = (
                    row_counts.get(episode["id"], 0) + 1
                )
            if any(row_counts.get(target, 0) > 1 for target in targets):
                # The native hard delete removes every row sharing this ID.
                raise ABSResponseError(_AMBIGUOUS_EPISODE_ROWS)

            library = await self._get_podcast_library(item["libraryId"])
            item_parts = _validate_podcast_item_path(item, library)
            snapshots: dict[str, tuple[Any, ...]] = {}
            for target in targets:
                episode = by_id[target]
                _validate_episode_path(episode, item_parts)
                snapshots[target] = _episode_identity(episode)
            item_snapshot = (item.get("path"), item.get("folderId"))

            deleted: list[str] = []
            removed: set[str] = set()
            failed_episode_id: str | None = None
            error: str | None = None
            for target in targets:
                try:
                    await self._require_delete_ready(
                        podcast_id,
                        item,
                        removed,
                        target,
                        snapshots[target],
                        item_snapshot,
                    )
                    await self._delete_episode(podcast_id, target)
                except ABSClientError as exc:
                    failed_episode_id = target
                    error = str(exc)
                    break
                deleted.append(target)
                removed.add(target)

            return _deletion_result(
                podcast_id, targets, deleted, failed_episode_id, error
            )

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
        keys.update(_queue_keys(self._queue_entries(item)))
        return keys

    async def _fetch_stored_feed(
        self, item: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Fetch the item's stored feed URL after the public-URL guard."""
        feed_url = await ensure_public_url(_stored_feed_url(item))
        return await self._fetch_podcast(feed_url)

    def _split_known_episodes(
        self, item: Mapping[str, Any], entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop already downloaded or queued entries, preserving order."""
        blocked = self._blocked_episode_keys(item)
        return [
            entry
            for entry in entries
            if not (_episode_keys(entry["guid"], entry["url"]) & blocked)
        ]

    async def _typed_candidates(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate every enclosure, returning native-shaped download entries."""
        typed_episodes: list[dict[str, Any]] = []
        for candidate in candidates:
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
        return typed_episodes

    async def _post_downloads(
        self, podcast_id: str, typed_episodes: list[dict[str, Any]]
    ) -> None:
        """Send one native download request, or nothing when empty."""
        if not typed_episodes:
            return
        await self._request_json(
            "POST",
            f"api/podcasts/{podcast_id}/download-episodes",
            json_body=typed_episodes,
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
            allow_empty=True,
        )

    async def _queue_candidates(
        self, podcast_id: str, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate every enclosure, then submit one native queue request."""
        typed_episodes = await self._typed_candidates(candidates)
        await self._post_downloads(podcast_id, typed_episodes)
        return typed_episodes

    async def _queue_selected_episodes(
        self,
        podcast_id: str,
        selected: list[dict[str, Any]],
        item: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Queue a fixed selection after preflight, re-reading just before POST.

        Process-local locking cannot stop an external Audiobookshelf writer.
        The re-read narrows the window in which the stored feed URL or the
        podcast identity can change, and drops entries that became downloaded
        or queued during URL preflight without backfilling. A final
        read-then-write race remains because the native API offers no
        compare-and-swap.
        """
        to_queue = self._split_known_episodes(item, selected)
        if not to_queue:
            return []
        typed_episodes = await self._typed_candidates(to_queue)

        probe = await self._load_podcast(podcast_id)
        self._require_download_identity(probe, item)
        blocked = self._blocked_episode_keys(probe)

        queued: list[dict[str, Any]] = []
        typed_queued: list[dict[str, Any]] = []
        for entry, typed in zip(to_queue, typed_episodes, strict=True):
            if _episode_keys(entry["guid"], entry["url"]) & blocked:
                continue
            queued.append(entry)
            typed_queued.append(typed)
        await self._post_downloads(podcast_id, typed_queued)
        return queued

    def _require_download_identity(
        self, probe: Mapping[str, Any], item: Mapping[str, Any]
    ) -> None:
        """Refuse to queue when the reloaded podcast moved or changed feeds."""
        if (
            probe.get("id") != item.get("id")
            or probe.get("libraryId") != item.get("libraryId")
            or probe.get("folderId") != item.get("folderId")
            or probe.get("path") != item.get("path")
            or _stored_feed_url(probe) != _stored_feed_url(item)
        ):
            raise ABSResponseError(_CHANGED_BEFORE_DOWNLOAD)

    async def _require_delete_ready(
        self,
        podcast_id: str,
        baseline: Mapping[str, Any],
        removed: set[str],
        target: str,
        snapshot: tuple[Any, ...],
        item_snapshot: tuple[object, object],
    ) -> None:
        """Fail closed unless one delete target still matches its fence.

        The expected state is the initial item minus only the episodes this
        call has already deleted. The target episode must still carry the same
        identity and file metadata as when it was prevalidated. Paths are
        revalidated on every iteration because the state fingerprint does not
        cover them.
        """
        probe = await self._load_podcast(podcast_id)
        if self._queue_entries(probe):
            raise ABSResponseError(_DOWNLOADS_ACTIVE)
        if self._state(probe) != self._state_without_episodes(baseline, removed):
            raise ABSResponseError(_CHANGED_DURING_DELETE)
        if (probe.get("path"), probe.get("folderId")) != item_snapshot:
            raise ABSResponseError(_CHANGED_DURING_DELETE)

        matches = [
            candidate
            for candidate in self._downloaded_episodes(probe)
            if candidate["id"] == target
        ]
        if not matches:
            raise ABSNotFound("Podcast episode not found.")
        episode = matches[0]
        if _episode_identity(episode) != snapshot:
            raise ABSResponseError(_CHANGED_DURING_DELETE)
        library = await self._get_podcast_library(probe["libraryId"])
        try:
            item_parts = _validate_podcast_item_path(probe, library)
            _validate_episode_path(episode, item_parts)
        except ABSResponseError:
            # Prevalidation accepted these paths; a late refusal means folder
            # or file metadata changed, so this target was not deleted.
            raise ABSResponseError(_UNSAFE_DURING_DELETE) from None

    def _state_without_episodes(
        self, item: Mapping[str, Any], removed: set[str]
    ) -> dict[str, Any]:
        """Project state as if only the acknowledged deletions had happened."""
        media = item["media"]
        episodes = media.get("episodes")
        expected = {
            **item,
            "media": {
                **media,
                "episodes": [
                    episode
                    for episode in (
                        episodes if isinstance(episodes, list) else []
                    )
                    if episode.get("id") not in removed
                ],
            },
        }
        return self._state(expected)

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


def _stored_feed_url(item: Mapping[str, Any]) -> str:
    """Return the item's stored feed URL or a fixed safe error."""
    media = item.get("media")
    metadata = media.get("metadata") if isinstance(media, dict) else None
    feed_url = metadata.get("feedUrl") if isinstance(metadata, dict) else None
    if not isinstance(feed_url, str) or not feed_url:
        raise ABSResponseError(_FEED_UNAVAILABLE)
    return feed_url


def _require_episode_query(value: object) -> str | None:
    if value is None:
        return None
    return _validate_query(value)


def _require_episode_order(value: object) -> str:
    if not isinstance(value, str) or value not in {"newest", "oldest"}:
        raise ABSInvalidRequest("Order must be 'newest' or 'oldest'.")
    return value


def _require_episode_offset(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_EPISODE_OFFSET:
        raise ABSInvalidRequest(
            f"Offset must be an integer between 0 and {MAX_EPISODE_OFFSET}."
        )
    return value


def _filter_titles(
    episodes: list[dict[str, Any]], query: str | None
) -> list[dict[str, Any]]:
    """Filter a projected list on a case-insensitive title substring."""
    if query is None:
        return episodes
    needle = query.casefold()
    return [
        episode
        for episode in episodes
        if needle in episode["title"].casefold()
    ]


def _paginate_views(
    episodes: list[dict[str, Any]], offset: int, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Slice one page and return its additive paging metadata."""
    total = len(episodes)
    page = episodes[offset : offset + limit]
    count = len(page)
    has_more = offset + count < total
    return page, {
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + count if has_more else None,
    }


def _order_downloaded_views(
    entries: list[dict[str, Any]], order: str
) -> list[dict[str, Any]]:
    """Order downloaded episode projections; undated entries always last."""
    reverse = order == "newest"
    dated = [entry for entry in entries if entry["published_ms"] is not None]
    undated = [entry for entry in entries if entry["published_ms"] is None]
    dated.sort(
        key=lambda entry: (entry["published_ms"], entry["id"]),
        reverse=reverse,
    )
    undated.sort(key=lambda entry: entry["id"], reverse=reverse)
    return [entry["view"] for entry in dated + undated]


def _feed_selector(podcast_id: str, candidate: Mapping[str, Any]) -> str:
    """Return a stable opaque selector bound to the podcast and entry identity.

    Title and feed order are deliberately excluded; a GUID, enclosure URL, or
    publication-date change produces a different selector. Binding the podcast
    ID means a selector from another podcast never resolves here.
    """
    canonical = json.dumps(
        [
            podcast_id,
            candidate["guid"] or "",
            candidate["url"],
            candidate["published"],
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "feed_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _feed_catalogue(
    podcast_id: str, podcast: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Build a deduped feed catalogue ordered only by entry content.

    Sorting and deduplication happen on content, not feed order, so the
    surviving entry of a duplicate pair, its selector, and membership do not
    depend on the feed order or on a caller's requested browse order.
    """
    entries: list[dict[str, Any]] = []
    for candidate in _feed_candidates(podcast):
        entry = dict(candidate)
        entry["id"] = _feed_selector(podcast_id, candidate)
        entries.append(entry)
    entries.sort(key=lambda entry: (-entry["published"], entry["id"]))

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        keys = _episode_keys(entry["guid"], entry["url"])
        if keys & seen:
            continue
        seen.update(keys)
        unique.append(entry)
    return unique


def _order_catalogue(
    entries: list[dict[str, Any]], order: str
) -> list[dict[str, Any]]:
    """Order catalogue entries by publication, then opaque selector."""
    if order == "newest":
        return sorted(
            entries, key=lambda entry: (-entry["published"], entry["id"])
        )
    return sorted(
        entries, key=lambda entry: (entry["published"], entry["id"])
    )


def _downloaded_match_index(
    episodes: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Index native downloaded episodes by the shared GUID/URL keys."""
    index: dict[str, set[str]] = {}
    for episode in episodes:
        for key in _episode_keys(
            episode.get("guid"), _episode_enclosure_url(episode)
        ):
            index.setdefault(key, set()).add(episode["id"])
    return index


def _queue_keys(entries: list[dict[str, Any]]) -> set[str]:
    """Return the GUID/URL keys of queued or active downloads."""
    keys: set[str] = set()
    for entry in entries:
        keys.update(_episode_keys(entry.get("guid"), entry.get("url")))
    return keys


def _episode_identity(episode: Mapping[str, Any]) -> tuple[object, ...]:
    """Return identity and file fields that must not drift before a delete."""
    audio_file = episode.get("audioFile")
    metadata = (
        audio_file.get("metadata") if isinstance(audio_file, dict) else None
    )
    return (
        episode.get("guid"),
        _episode_enclosure_url(episode),
        _epoch_ms(episode.get("publishedAt")),
        metadata.get("path") if isinstance(metadata, dict) else None,
    )


def _require_feed_selector(value: object) -> str:
    if not isinstance(value, str) or _FEED_SELECTOR_RE.fullmatch(value) is None:
        raise ABSInvalidRequest("Invalid podcast episode selection.")
    return value


def _require_feed_selectors(value: object) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= MAX_SELECTED_EPISODES:
        raise ABSInvalidRequest(
            "Episode IDs must be a list of 1-"
            f"{MAX_SELECTED_EPISODES} feed episode IDs."
        )
    selectors: list[str] = []
    seen: set[str] = set()
    for entry in value:
        selector = _require_feed_selector(entry)
        if selector in seen:
            raise ABSInvalidRequest("Episode IDs must not contain duplicates.")
        seen.add(selector)
        selectors.append(selector)
    return selectors


def _require_native_episode_ids(value: object) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= MAX_SELECTED_EPISODES:
        raise ABSInvalidRequest(
            "Episode IDs must be a list of 1-"
            f"{MAX_SELECTED_EPISODES} downloaded episode IDs."
        )
    episode_ids: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not is_valid_resource_id(entry):
            raise ABSInvalidRequest("Invalid episode ID.")
        if entry in seen:
            raise ABSInvalidRequest("Episode IDs must not contain duplicates.")
        seen.add(entry)
        episode_ids.append(entry)
    return episode_ids


def _queue_result(
    podcast_id: str,
    selected_count: int,
    queued_entries: list[dict[str, Any]],
    skipped_count: int,
) -> dict[str, Any]:
    """Project one heterogeneous, asynchronous queue submission."""
    return {
        "podcast_id": podcast_id,
        "status": "queued" if queued_entries else "nothing_new",
        "selected_count": selected_count,
        "queued_count": len(queued_entries),
        "skipped_count": skipped_count,
        "episodes": [
            {
                "id": entry["id"],
                "title": entry["title"],
                "published_at": _iso_timestamp(entry["published"]),
            }
            for entry in queued_entries
        ],
        "note": (
            "Queued counts download requests submitted by this call, not "
            "completed downloads; Audiobookshelf downloads asynchronously "
            "and may deduplicate requests."
        ),
    }


def _deletion_result(
    podcast_id: str,
    requested: list[str],
    deleted: list[str],
    failed_episode_id: str | None,
    error: str | None,
) -> dict[str, Any]:
    """Project a bounded multi-delete, honestly reporting partial work."""
    result: dict[str, Any] = {
        "podcast_id": podcast_id,
        "status": "partial" if failed_episode_id is not None else "completed",
        "requested_count": len(requested),
        "deleted_count": len(deleted),
        "deleted_episode_ids": list(deleted),
        "filesystem_removal": "not_verified",
        "note": (
            "Acknowledged Audiobookshelf deletes only; filesystem removal is "
            "not verified. A failed delete request may already have been "
            "applied and is not retried."
        ),
    }
    if failed_episode_id is not None:
        result["failed_episode_id"] = failed_episode_id
        result["error"] = error
    return result


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
