"""Bounded podcast reads and writes on top of the fixed Audiobookshelf client.

All operations use the installed Audiobookshelf 2.37.2 native endpoints:

* ``GET /api/items/{id}?expanded=1`` (plus ``include=downloads`` for queue state)
* ``GET /api/libraries/{id}`` and ``GET /api/libraries/{id}/items``
* ``GET /api/me/progress`` and ``GET /api/me/progress/{libraryItemId}/{episodeId}``
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
feed, and resolves each selector against the current catalogue. After every
enclosure URL preflight, the item is reloaded once more to confirm the same
podcast, folder, item path, and stored feed URL and to drop entries that became
downloaded or queued meanwhile; the latest-episode tool shares that late fence.
Entries dropped there are reported as skipped, never backfilled. That final
read is followed by the native POST, so an external writer can still race the
request; no atomic compare-and-swap exists. Multi-delete and retention trims
re-read before every DELETE and compare the target episode's identity and file
metadata with the prevalidated snapshot because the state fingerprint does not
cover paths or enclosure metadata. A retention trim additionally requires the
post-PATCH read-back to equal the pre-PATCH baseline plus this call's own
``maxEpisodesToKeep`` value, with no active queue, before the first DELETE.

Browsing podcast items (``list_podcasts``) requests one minified native items
page from an allowlisted library whose native media type is podcast. It returns
only native item IDs, sanitized titles, and the native episode count when that
count is a valid non-negative integer; it never returns URLs, paths, or media
details and makes no remote media request. The backend page cursor is bounded;
this is the current upstream snapshot, not a complete archive.

Listening status comes from the connector's own Audiobookshelf API-key account:
one bounded ``GET /api/me/progress`` read joined to the downloaded episodes by
native episode ID. It is never another user's progress and never a live
playback session. A finished flag selected by ``delete_finished_podcast_episodes``
is re-read for that episode immediately before its DELETE; a record that is
missing or no longer finished skips only that episode, so progress which changes
between the recheck and the DELETE is not covered by any fence.
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
MAX_PODCAST_PAGE = 1000
MAX_PODCAST_LIST_LIMIT = 50
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
_NO_FINISHED_EPISODES = "No finished episodes found for the connector account."
_PROGRESS_RECORD_MISSING = "progress_record_missing"
_FINISHED_FLAG_CLEARED = "finished_flag_cleared"
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

    async def list_podcasts(
        self, library_id: str, limit: int = 25, *, page: int = 0
    ) -> dict[str, Any]:
        """Return one bounded page of podcasts from an allowed podcast library.

        The library must be allowlisted and natively typed ``podcast``. Exactly
        one native minified ``items`` page is requested; no caller-supplied
        filter, URL, remote media fetch, or follow-up page loop is involved.
        Only native item IDs, sanitized titles, and ``media.numEpisodes`` when
        that is a valid non-negative integer (otherwise ``null``) are returned.
        ``total`` is the backend's current count for the current upstream
        snapshot, not a promise of a complete archive. The native page cursor
        is bounded to ``MAX_PODCAST_PAGE``; if more rows exist at that bound,
        ``has_more`` is false, ``next_page`` is null, and ``note`` says the
        listing stopped at the bound instead of returning an unusable cursor.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_PODCAST_LIST_LIMIT:
            raise ABSInvalidRequest(
                f"Limit must be an integer between 1 and {MAX_PODCAST_LIST_LIMIT}."
            )
        if type(page) is not int or not 0 <= page <= MAX_PODCAST_PAGE:
            raise ABSInvalidRequest(
                f"Page must be an integer between 0 and {MAX_PODCAST_PAGE}."
            )
        self._require_library(library_id)
        await self._get_podcast_library(library_id)

        data = await self._request_json(
            "GET",
            f"api/libraries/{library_id}/items",
            params={
                "minified": "1",
                "limit": str(limit),
                "page": str(page),
            },
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
        )
        if not isinstance(data, dict):
            raise ABSResponseError(_UNEXPECTED)
        results = data.get("results")
        total = data.get("total")
        # One page plus a consistent count only: a non-empty page must start
        # before ``total``, a page starting before ``total`` must not be
        # silently empty, and a page may never carry rows beyond the remaining
        # total. Anything else would make the advertised cursor lie.
        if (
            not isinstance(results, list)
            or type(total) is not int
            or total < 0
            or len(results) > limit
            or len(results) > max(total - page * limit, 0)
            or bool(results) != (page * limit < total)
        ):
            raise ABSResponseError(_UNEXPECTED)

        podcasts: list[dict[str, Any]] = []
        for raw in results:
            if not isinstance(raw, dict):
                raise ABSResponseError(_UNEXPECTED)
            item_id = raw.get("id")
            media = raw.get("media")
            if (
                not is_valid_resource_id(item_id)
                or raw.get("libraryId") != library_id
                or raw.get("mediaType") != "podcast"
                or not isinstance(media, dict)
            ):
                raise ABSResponseError(_UNEXPECTED)
            metadata = media.get("metadata")
            if not isinstance(metadata, dict):
                raise ABSResponseError(_UNEXPECTED)
            podcasts.append(
                {
                    "id": item_id,
                    "title": _sanitize_text(
                        metadata.get("title"), MAX_PODCAST_TITLE_LENGTH
                    ),
                    "downloaded_count": _native_episode_count(
                        media.get("numEpisodes")
                    ),
                }
            )

        has_more = page * limit + len(podcasts) < total
        next_page: int | None = None
        note: str | None = None
        if has_more:
            if page + 1 <= MAX_PODCAST_PAGE:
                next_page = page + 1
            else:
                has_more = False
                note = (
                    "More podcasts exist beyond the maximum supported page; "
                    "this listing is the current upstream snapshot, not a "
                    "complete archive."
                )

        result: dict[str, Any] = {
            "library_id": library_id,
            "podcasts": podcasts,
            "count": len(podcasts),
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": has_more,
            "next_page": next_page,
        }
        if note is not None:
            result["note"] = note
        return result

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
        Every episode view carries ``listening_status`` (``finished``,
        ``in_progress``, or ``not_started``) from one bounded
        ``GET api/me/progress`` read for the connector's Audiobookshelf
        API-key account; it is not any other user's progress and is never a
        live playback claim.
        """
        self._require_episode_limit(limit)
        query = _require_episode_query(query)
        order = _require_episode_order(order)
        offset = _require_episode_offset(offset)
        item = await self._load_podcast(podcast_id)
        progress = await self._fetch_account_progress(podcast_id)

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
                        "listening_status": _listening_status(
                            progress.get(episode["id"])
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

        Selection scans the current feed newest first, skipping entries already
        downloaded or queued. After every enclosure is validated, the podcast is
        reloaded once and the same identity/folder/path/feed fence used by the
        selector downloads is applied; entries that became known meanwhile are
        dropped without backfilling. The status is ``queued``, never
        ``completed``: Audiobookshelf downloads asynchronously. No feed URL,
        enclosure URL, or file path is returned.
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
                return _nothing_new_result(podcast_id)

            # Validate every enclosure, then apply the shared late fence once
            # before the single native request. Late-known entries are dropped
            # without backfilling, so the batch may be smaller or empty.
            queued, typed_episodes, _late_skipped = (
                await self._finalize_download_batch(
                    podcast_id, item, selected
                )
            )
            await self._post_downloads(podcast_id, typed_episodes)
            if not queued:
                return _nothing_new_result(podcast_id)
            return {
                "podcast_id": podcast_id,
                "status": "queued",
                "queued_count": len(queued),
                "episodes": [
                    {
                        "title": episode["title"],
                        "published_at": _iso_timestamp(episode["published"]),
                    }
                    for episode in queued
                ],
            }

    async def download_podcast_episodes(
        self, podcast_id: str, episode_ids: list[str]
    ) -> dict[str, Any]:
        """Queue selected current-feed episodes in the caller's order.

        Every selector is resolved against a freshly reloaded podcast and feed
        under the mutation lock; one unknown or stale selector rejects the
        whole selection before any download is queued. Already downloaded or
        queued episodes are skipped without backfilling and reported in
        ``skipped_episodes``; ``episodes`` lists only submitted entries.
        ``queued`` means submitted to the asynchronous native queue, never
        completed.
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

            queued, skipped = await self._queue_selected_episodes(
                podcast_id, selected, item
            )
            return _queue_result(
                podcast_id, len(selected), queued, skipped
            )

    async def download_episodes_from(
        self, podcast_id: str, start_episode_id: str, count: int = 10
    ) -> dict[str, Any]:
        """Queue a fixed chronological window starting at an anchor episode.

        The window is the anchor plus the next ``count - 1`` current-feed
        episodes in publication order (ascending, opaque ID tie-break), so the
        anchor is the oldest entry in the window. The window is fixed before
        skipping: already downloaded or queued entries inside it are skipped
        without backfilling and reported in ``skipped_episodes``, and a window
        near the end of the feed may contain fewer than ``count`` entries. An
        anchor that is not in the freshly fetched feed is rejected; there is no
        fallback or latest-episode guess. ``queued`` means submitted to the
        asynchronous native queue, never completed.
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
            queued, skipped = await self._queue_selected_episodes(
                podcast_id, window, item
            )
            return _queue_result(
                podcast_id, len(window), queued, skipped
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
        and is refused before the policy PATCH if it would need more. Duplicate
        native episode IDs anywhere in the downloaded set are rejected before
        the PATCH, and all targets and paths are prevalidated; duplicates are
        never guessed at or deduplicated. After the PATCH the item is re-read
        and must match the pre-PATCH baseline plus this call's own
        ``maxEpisodesToKeep`` value with no active queue; each DELETE then
        re-reads the same fence, the library folder, and the target identity. A
        failure stops at once with the honest partial count; for
        ``trim_existing`` a malformed read-back or final state projection is
        reported as partial with ``state`` null rather than raised, while a
        policy-only update keeps its ``updated`` status and returns ``state``
        null; there is no rollback and no retry, the policy PATCH may already
        have been applied even when no episode DELETE is acknowledged, and
        filesystem removal is not verified.
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

            targets: list[str] = []
            snapshots: dict[str, tuple[Any, ...]] = {}
            item_snapshot: tuple[object, object] = (
                item.get("path"),
                item.get("folderId"),
            )
            if trim_existing:
                if self._queue_entries(item):
                    raise ABSResponseError(_DOWNLOADS_ACTIVE)
                downloaded = self._downloaded_episodes(item)
                # Retention slices by position, so a duplicate ID in the kept
                # prefix still consumes ``keep`` budget while a hard delete
                # removes every row sharing an ID. Never guess which duplicate
                # identity to keep or delete: reject before planning the trim.
                if any(
                    count > 1
                    for count in _episode_row_counts(downloaded).values()
                ):
                    raise ABSResponseError(_AMBIGUOUS_EPISODE_ROWS)
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
                targets = [episode["id"] for episode in plan_episodes]
                if targets:
                    # Full prevalidation before the policy write: a refused
                    # path or duplicate native row must not leave a changed
                    # policy behind.
                    snapshots, item_snapshot = (
                        await self._prevalidate_delete_targets(item, targets)
                    )

            await self._request_json(
                "PATCH",
                f"api/items/{podcast_id}/media",
                json_body={"maxEpisodesToKeep": keep},
                max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
                allow_empty=True,
            )

            deleted: list[str] = []
            failed_episode_id: str | None = None
            error: str | None = None
            status = "updated"
            if trim_existing:
                status = "completed"
                try:
                    post = await self._load_podcast(podcast_id)
                except ABSClientError as exc:
                    # The PATCH answered, so the policy may already be applied.
                    post = None
                    status = "partial"
                    failed_episode_id = targets[0] if targets else None
                    error = str(exc)
                if post is not None:
                    try:
                        error = self._retention_baseline_error(
                            post, item, keep, item_snapshot
                        )
                    except ABSClientError as exc:
                        # The policy PATCH already answered. A malformed or
                        # drifted read-back is a late verification failure and
                        # must return the honest partial outcome, not escape.
                        error = str(exc)
                    if error is None and targets:
                        try:
                            library = await self._get_podcast_library(
                                post["libraryId"]
                            )
                        except ABSClientError as exc:
                            error = str(exc)
                        else:
                            try:
                                _validate_podcast_item_path(post, library)
                            except ABSResponseError:
                                # The policy was already written, so this is a
                                # late refusal, not a "nothing changed" case.
                                error = _UNSAFE_DURING_DELETE
                    if error is None:
                        deleted, failed, delete_error = (
                            await self._delete_episode_batch(
                                podcast_id,
                                targets,
                                post,
                                snapshots,
                                item_snapshot,
                            )
                        )
                        if failed is not None:
                            failed_episode_id = failed
                            error = delete_error
                    if error is not None:
                        status = "partial"
                        if failed_episode_id is None and targets:
                            failed_episode_id = targets[0]

            # The final read-back is advisory: the policy PATCH and any DELETE
            # acknowledgements already happened. A malformed native payload or
            # an unprojectable state must not raise past the honest outcome.
            final_state: dict[str, Any] | None = None
            try:
                final_item = await self._load_podcast(podcast_id)
                final_state = self._state(final_item)
            except ABSClientError as exc:
                if trim_existing:
                    status = "partial"
                    if error is None:
                        # Every acknowledgement stands; only the read-back
                        # failed, so do not invent a failed episode target.
                        error = str(exc)

            outcome: dict[str, Any] = {
                "podcast_id": podcast_id,
                "library_id": current["library_id"],
                "keep_episodes": keep,
                "trim_existing": trim_existing,
                "deleted_count": len(deleted),
                "status": status,
            }
            if trim_existing:
                outcome["deleted_episode_ids"] = list(deleted)
                outcome["filesystem_removal"] = "not_verified"
                outcome["note"] = (
                    "The maxEpisodesToKeep policy may already have been applied "
                    "even when no episode DELETE was acknowledged. Deleted "
                    "counts are acknowledged Audiobookshelf operations; "
                    "filesystem removal is not verified. A failed delete "
                    "request may already have been applied and is not retried."
                )
                if failed_episode_id is not None:
                    outcome["failed_episode_id"] = failed_episode_id
            else:
                outcome["note"] = (
                    "Existing episodes are not removed immediately. "
                    "Audiobookshelf applies this limit during future automatic "
                    "downloads; it does not reduce an existing over-limit backlog."
                )
            outcome["state"] = final_state
            if error is not None:
                outcome["error"] = error
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

            snapshots, item_snapshot = await self._prevalidate_delete_targets(
                item, targets
            )
            deleted, failed_episode_id, error = await self._delete_episode_batch(
                podcast_id, targets, item, snapshots, item_snapshot
            )
            return _deletion_result(
                podcast_id, targets, deleted, failed_episode_id, error
            )

    async def delete_finished_podcast_episodes(
        self,
        podcast_id: str,
        limit: int = MAX_SELECTED_EPISODES,
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Delete up to ``limit`` downloaded episodes the account marked finished.

        Selection is one bounded snapshot of the connector's Audiobookshelf
        API-key account progress joined to the downloaded episodes, oldest
        ``publishedAt`` first with undated episodes last. Every target is
        prevalidated like a selected multi-delete, then its progress record is
        re-read immediately before its DELETE. A record that vanished (404) or
        is no longer strictly finished skips only that target with a fixed
        reason and does not stop the batch; any other failure stops the run with
        the honest partial count. There is no rollback and no retry, filesystem
        removal is not verified, and progress can change between the recheck
        and the DELETE, so the remaining count is a snapshot estimate.
        """
        self._require_writes_enabled()
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTED_EPISODES:
            raise ABSInvalidRequest(
                f"Limit must be an integer between 1 and {MAX_SELECTED_EPISODES}."
            )
        expected = _require_fingerprint(expected_fingerprint)

        async with _MUTATION_LOCK:
            item = await self._load_podcast(podcast_id)
            _require_unchanged(self._state(item), expected)
            if self._queue_entries(item):
                raise ABSResponseError(_DOWNLOADS_ACTIVE)

            progress = await self._fetch_account_progress(podcast_id)
            finished = [
                episode
                for episode in self._downloaded_episodes(item)
                if _is_finished(progress.get(episode["id"]))
            ]
            finished_episode_count = len(finished)
            targets = [
                episode["id"]
                for episode in _order_episodes_oldest(finished)[:limit]
            ]
            if not targets:
                return {
                    "podcast_id": podcast_id,
                    "status": "completed",
                    "requested_count": 0,
                    "deleted_count": 0,
                    "deleted_episode_ids": [],
                    "skipped_episode_ids": [],
                    "skipped": [],
                    "finished_episode_count": finished_episode_count,
                    "remaining_finished_count": 0,
                    "limit": limit,
                    "filesystem_removal": "not_verified",
                    "note": _NO_FINISHED_EPISODES,
                }

            snapshots, item_snapshot = await self._prevalidate_delete_targets(
                item, targets
            )
            deleted, skipped, failed_episode_id, error = (
                await self._delete_finished_episode_batch(
                    podcast_id, targets, item, snapshots, item_snapshot
                )
            )
            return _finished_deletion_result(
                podcast_id,
                targets,
                deleted,
                skipped,
                failed_episode_id,
                error,
                finished_episode_count,
                limit,
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

    async def _fetch_account_progress(
        self, podcast_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return this podcast's episode progress for the connector account.

        One bounded ``GET api/me/progress`` request covers every library item,
        so rows for other items (books, other podcasts) are ignored. A row
        scoped to this podcast must carry a non-empty string ``episodeId`` and a
        strict boolean ``isFinished``; anything else fails closed instead of
        being guessed at. The mapping is keyed by native episode ID and is used
        only for a fixed listening status or a strictly finished delete target.
        """
        data = await self._request_json(
            "GET",
            "api/me/progress",
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
        )
        if not isinstance(data, dict):
            raise ABSResponseError(_UNEXPECTED)
        records = data.get("mediaProgress")
        if not isinstance(records, list):
            raise ABSResponseError(_UNEXPECTED)

        progress: dict[str, dict[str, Any]] = {}
        for raw in records:
            if not isinstance(raw, dict):
                raise ABSResponseError(_UNEXPECTED)
            if raw.get("libraryItemId") != podcast_id:
                continue
            episode_id = raw.get("episodeId")
            if not isinstance(episode_id, str) or not episode_id:
                raise ABSResponseError(_UNEXPECTED)
            if type(raw.get("isFinished")) is not bool:
                raise ABSResponseError(_UNEXPECTED)
            progress[episode_id] = raw
        return progress

    async def _fetch_episode_progress(
        self, podcast_id: str, episode_id: str
    ) -> dict[str, Any]:
        """Read one episode's current progress record for the connector account.

        A missing record surfaces as :class:`ABSNotFound`, which the caller
        treats as a skip, never as permission to delete.
        """
        data = await self._request_json(
            "GET",
            f"api/me/progress/{podcast_id}/{episode_id}",
            max_response_bytes=MAX_PODCAST_RESPONSE_BYTES,
        )
        if not isinstance(data, dict):
            raise ABSResponseError(_UNEXPECTED)
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
        queued_entries = self._queue_field(item, "episodeDownloadsQueued")
        active_entries = self._queue_field(item, "episodesDownloading")
        queue_entries = queued_entries + active_entries
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
            "queued_download_count": len(queued_entries),
            "active_download_count": len(active_entries),
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

    def _queue_field(
        self, item: Mapping[str, Any], field: str
    ) -> list[dict[str, Any]]:
        """Return one validated native download queue; never its URLs."""
        value = item.get(field)
        if value is None:
            # Both native queue arrays are absent or empty when idle.
            return []
        if not isinstance(value, list):
            raise ABSResponseError(_UNEXPECTED)
        entries: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ABSResponseError(_UNEXPECTED)
            entries.append(entry)
        return entries

    def _queue_entries(self, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return validated queued plus active entries; never their URLs."""
        return self._queue_field(
            item, "episodeDownloadsQueued"
        ) + self._queue_field(item, "episodesDownloading")

    def _known_key_sets(
        self, item: Mapping[str, Any]
    ) -> tuple[set[str], set[str]]:
        """Return the GUID/URL keys of downloaded and queued episodes."""
        downloaded: set[str] = set()
        for episode in self._downloaded_episodes(item):
            downloaded.update(
                _episode_keys(
                    episode.get("guid"), _episode_enclosure_url(episode)
                )
            )
        return downloaded, _queue_keys(self._queue_entries(item))

    def _blocked_episode_keys(self, item: Mapping[str, Any]) -> set[str]:
        downloaded, queued = self._known_key_sets(item)
        return downloaded | queued

    async def _fetch_stored_feed(
        self, item: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Fetch the item's stored feed URL after the public-URL guard."""
        feed_url = await ensure_public_url(_stored_feed_url(item))
        return await self._fetch_podcast(feed_url)

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

    async def _queue_selected_episodes(
        self,
        podcast_id: str,
        selected: list[dict[str, Any]],
        item: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Queue a fixed selection after preflight, re-reading just before POST.

        Returns the submitted entries and the projected skipped entries.
        Already known entries are skipped during selection, then every
        remaining enclosure is validated before the shared late fence. Entries
        that became downloaded or queued during that preflight are skipped
        afterwards without backfilling. Process-local locking cannot stop an
        external Audiobookshelf writer; the re-read narrows the window in which
        the stored feed URL or the podcast identity can change, and a final
        read-then-write race remains because the native API offers no
        compare-and-swap.
        """
        downloaded, queued = self._known_key_sets(item)
        skipped: list[dict[str, Any]] = []
        to_queue: list[dict[str, Any]] = []
        for entry in selected:
            reason = _skip_reason(entry, downloaded, queued)
            if reason is None:
                to_queue.append(entry)
            else:
                skipped.append(_skipped_episode(entry, reason))
        if not to_queue:
            return [], skipped

        kept, typed_entries, late_skipped = await self._finalize_download_batch(
            podcast_id, item, to_queue
        )
        skipped.extend(
            _skipped_episode(entry, reason) for entry, reason in late_skipped
        )
        await self._post_downloads(podcast_id, typed_entries)
        return kept, skipped

    async def _finalize_download_batch(
        self,
        podcast_id: str,
        item: Mapping[str, Any],
        entries: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[tuple[dict[str, Any], str]],
    ]:
        """Validate every enclosure, then apply the shared late fence once.

        All public-URL and native encoding checks happen before the final read,
        so a refused enclosure never reaches the native POST. The re-read
        confirms that the podcast, folder, item path, and stored feed still
        match and recomputes the downloaded/queued keys. Entries known at that
        point are returned with their skip reason, are never submitted, and are
        not backfilled. A final read-then-write race remains.
        """
        typed_entries = await self._typed_candidates(entries)
        probe = await self._load_podcast(podcast_id)
        self._require_download_identity(probe, item)
        downloaded, queued = self._known_key_sets(probe)

        kept: list[dict[str, Any]] = []
        kept_typed: list[dict[str, Any]] = []
        skipped: list[tuple[dict[str, Any], str]] = []
        for entry, typed in zip(entries, typed_entries, strict=True):
            reason = _skip_reason(entry, downloaded, queued)
            if reason is not None:
                skipped.append((entry, reason))
                continue
            kept.append(entry)
            kept_typed.append(typed)
        return kept, kept_typed, skipped

    def _retention_baseline_error(
        self,
        probe: Mapping[str, Any],
        baseline: Mapping[str, Any],
        keep: int,
        item_snapshot: tuple[object, object],
    ) -> str | None:
        """Return a fixed error when the post-PATCH read is not our baseline.

        Only this call's own policy value may differ from the pre-PATCH item.
        Accepting a blind fresh read-back would let an external policy, feed,
        library, or queue change pass as if it were ours.
        """
        if self._queue_entries(probe):
            return _DOWNLOADS_ACTIVE
        if self._state(probe) != self._state_with_policy(baseline, keep):
            return _CHANGED_DURING_DELETE
        if (probe.get("path"), probe.get("folderId")) != item_snapshot:
            return _CHANGED_DURING_DELETE
        return None

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

    async def _prevalidate_delete_targets(
        self, item: Mapping[str, Any], targets: list[str]
    ) -> tuple[dict[str, tuple[Any, ...]], tuple[object, object]]:
        """Fully prevalidate one bounded delete batch before any mutation.

        Every target must be a current downloaded episode with exactly one
        native row (the hard delete removes every row sharing the ID), and both
        the item directory and every episode file must be safe. Returns the
        per-target identity snapshots and the item path/folder snapshot used by
        the per-DELETE fence.
        """
        episodes = self._downloaded_episodes(item)
        by_id = {episode["id"]: episode for episode in episodes}
        if any(target not in by_id for target in targets):
            raise ABSNotFound("Podcast episode not found.")
        row_counts = _episode_row_counts(episodes)
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
        return snapshots, (item.get("path"), item.get("folderId"))

    async def _delete_episode_batch(
        self,
        podcast_id: str,
        targets: list[str],
        baseline: Mapping[str, Any],
        snapshots: Mapping[str, tuple[Any, ...]],
        item_snapshot: tuple[object, object],
    ) -> tuple[list[str], str | None, str | None]:
        """Delete prevalidated targets one at a time, fenced before each DELETE.

        ``baseline`` is the item as it should look before any of these
        deletions; for a retention trim that is the post-PATCH read-back, so
        only this call's own policy change is part of the expectation. The
        expected state for an iteration is that baseline minus only the
        deletions acknowledged here. The first failure stops the batch with the
        honest partial count; nothing is retried or rolled back.
        """
        deleted: list[str] = []
        removed: set[str] = set()
        failed_episode_id: str | None = None
        error: str | None = None
        for target in targets:
            try:
                await self._require_delete_ready(
                    podcast_id,
                    baseline,
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
        return deleted, failed_episode_id, error

    async def _delete_finished_episode_batch(
        self,
        podcast_id: str,
        targets: list[str],
        baseline: Mapping[str, Any],
        snapshots: Mapping[str, tuple[Any, ...]],
        item_snapshot: tuple[object, object],
    ) -> tuple[list[str], list[dict[str, str]], str | None, str | None]:
        """Delete targets whose finished status survives a fresh recheck.

        The shared per-DELETE fence runs first and is unchanged. A fresh
        per-episode progress read then decides: a missing record or a record
        that is no longer strictly finished skips only that target and the batch
        continues, because the safety condition was never met for it. Any other
        failure — including the fence, the progress read, or the DELETE — stops
        the batch with the honest partial count; nothing is retried.
        """
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        removed: set[str] = set()
        failed_episode_id: str | None = None
        error: str | None = None
        for target in targets:
            try:
                await self._require_delete_ready(
                    podcast_id,
                    baseline,
                    removed,
                    target,
                    snapshots[target],
                    item_snapshot,
                )
            except ABSClientError as exc:
                failed_episode_id = target
                error = str(exc)
                break
            try:
                record = await self._fetch_episode_progress(podcast_id, target)
            except ABSNotFound:
                skipped.append({"id": target, "reason": _PROGRESS_RECORD_MISSING})
                continue
            except ABSClientError as exc:
                failed_episode_id = target
                error = str(exc)
                break
            if record.get("isFinished") is not True:
                skipped.append({"id": target, "reason": _FINISHED_FLAG_CLEARED})
                continue
            try:
                await self._delete_episode(podcast_id, target)
            except ABSClientError as exc:
                failed_episode_id = target
                error = str(exc)
                break
            deleted.append(target)
            removed.add(target)
        return deleted, skipped, failed_episode_id, error

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

        The expected state is the baseline item minus only the episodes this
        call has already deleted. The target episode must still carry the same
        identity and file metadata as when it was prevalidated. Scope, queue,
        and paths are revalidated on every iteration because the state
        fingerprint does not cover them.
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

    def _state_with_policy(
        self, item: Mapping[str, Any], keep: int
    ) -> dict[str, Any]:
        """Project state as if only this call's ``maxEpisodesToKeep`` changed."""
        media = item["media"]
        expected = {
            **item,
            "media": {**media, "maxEpisodesToKeep": keep},
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


def _order_episodes_oldest(
    episodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Order raw native episodes oldest first; undated entries always last."""
    dated: list[tuple[int, str, dict[str, Any]]] = []
    undated: list[tuple[str, dict[str, Any]]] = []
    for episode in episodes:
        published_ms = _epoch_ms(episode.get("publishedAt"))
        if published_ms is None:
            undated.append((episode["id"], episode))
        else:
            dated.append((published_ms, episode["id"], episode))
    dated.sort(key=lambda entry: (entry[0], entry[1]))
    undated.sort(key=lambda entry: entry[0])
    return [entry[2] for entry in dated] + [entry[1] for entry in undated]


def _is_finished(record: Mapping[str, Any] | None) -> bool:
    """Return whether a validated progress record is strictly finished."""
    return isinstance(record, dict) and record.get("isFinished") is True


def _listening_status(record: Mapping[str, Any] | None) -> str:
    """Project one validated progress record into a fixed listening status.

    Only a strict ``True`` counts as finished; a record counts as in progress
    only when ``currentTime`` is a non-boolean number greater than zero.
    """
    if record is None:
        return "not_started"
    if record.get("isFinished") is True:
        return "finished"
    current_time = record.get("currentTime")
    if (
        isinstance(current_time, (int, float))
        and not isinstance(current_time, bool)
        and current_time > 0
    ):
        return "in_progress"
    return "not_started"


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


def _episode_row_counts(
    episodes: list[dict[str, Any]],
) -> dict[str, int]:
    """Count native rows per episode ID; a hard delete removes every row."""
    counts: dict[str, int] = {}
    for episode in episodes:
        episode_id = episode["id"]
        counts[episode_id] = counts.get(episode_id, 0) + 1
    return counts


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
    skipped_episodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project one heterogeneous, asynchronous queue submission.

    ``episodes`` lists only submitted entries; every target skipped as already
    downloaded or queued is reported separately with feed-safe metadata.
    """
    return {
        "podcast_id": podcast_id,
        "status": "queued" if queued_entries else "nothing_new",
        "selected_count": selected_count,
        "queued_count": len(queued_entries),
        "skipped_count": len(skipped_episodes),
        "episodes": [
            {
                "id": entry["id"],
                "title": entry["title"],
                "published_at": _iso_timestamp(entry["published"]),
            }
            for entry in queued_entries
        ],
        "skipped_episodes": skipped_episodes,
        "note": (
            "Queued counts download requests submitted by this call, not "
            "completed downloads; Audiobookshelf downloads asynchronously "
            "and may deduplicate requests."
        ),
    }


def _nothing_new_result(podcast_id: str) -> dict[str, Any]:
    """Return the stable latest-episode result when nothing can be queued."""
    return {
        "podcast_id": podcast_id,
        "status": "nothing_new",
        "queued_count": 0,
        "episodes": [],
    }


def _skip_reason(
    entry: Mapping[str, Any],
    downloaded_keys: set[str],
    queued_keys: set[str],
) -> str | None:
    """Return the fixed skip reason for a feed entry, or None if unknown.

    A downloaded match wins over a queued match when both are present.
    """
    keys = _episode_keys(entry["guid"], entry["url"])
    if keys & downloaded_keys:
        return "already_downloaded"
    if keys & queued_keys:
        return "already_queued"
    return None


def _skipped_episode(
    entry: Mapping[str, Any], reason: str
) -> dict[str, Any]:
    """Project one skipped feed target without URLs, GUIDs, or native IDs."""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "published_at": _iso_timestamp(entry["published"]),
        "reason": reason,
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


def _finished_deletion_result(
    podcast_id: str,
    requested: list[str],
    deleted: list[str],
    skipped: list[dict[str, str]],
    failed_episode_id: str | None,
    error: str | None,
    finished_episode_count: int,
    limit: int,
) -> dict[str, Any]:
    """Project a finished-episode delete with honest snapshot counts."""
    note = (
        "Acknowledged Audiobookshelf deletes only; filesystem removal is not "
        "verified. A failed delete request may already have been applied and "
        "is not retried. Progress can change between the finished-status "
        "recheck and the DELETE, so remaining_finished_count is a snapshot "
        "estimate."
    )
    if finished_episode_count > len(requested):
        note += (
            " More finished episodes remain than this call's limit; check "
            "remaining_finished_count and call again."
        )
    result: dict[str, Any] = {
        "podcast_id": podcast_id,
        "status": "partial" if failed_episode_id is not None else "completed",
        "requested_count": len(requested),
        "deleted_count": len(deleted),
        "deleted_episode_ids": list(deleted),
        "skipped_episode_ids": [entry["id"] for entry in skipped],
        "skipped": list(skipped),
        "finished_episode_count": finished_episode_count,
        "remaining_finished_count": (
            finished_episode_count - len(deleted) - len(skipped)
        ),
        "limit": limit,
        "filesystem_removal": "not_verified",
        "note": note,
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


def _native_episode_count(value: object) -> int | None:
    """Return a native non-negative episode count, or None when not valid."""
    if type(value) is int and value >= 0:
        return value
    return None


def _require_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ABSInvalidRequest("Invalid expected fingerprint.")
    return value


def _require_unchanged(current: Mapping[str, Any], expected: str) -> None:
    if current["fingerprint"] != expected:
        raise ABSInvalidRequest(STALE_FINGERPRINT)
