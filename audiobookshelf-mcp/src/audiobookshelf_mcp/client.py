"""Bounded HTTP adapter for the fixed Audiobookshelf backend.

The base class exposes only read-only operations. A private, status- and
size-bounded request helper exists for the fixed podcast operations in
:mod:`audiobookshelf_mcp.podcasts`; no tool can supply a method, path, header,
or arbitrary body.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from typing import Any

import httpx

from .config import Config, is_valid_resource_id

MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_CONNECTIONS = 4
MAX_SEARCH_LIMIT = 25
MAX_QUERY_LENGTH = 200
_MAX_NAMES = 10
_MAX_TEXT_LENGTH = 300
_MAX_NAME_LENGTH = 120


class ABSClientError(Exception):
    """Sanitized error safe to surface to an MCP client.

    Messages are fixed strings and never contain the backend URL, response
    bodies, headers, or credentials.
    """


class ABSInvalidRequest(ABSClientError):
    """A caller supplied an argument outside the supported bounds."""


class ABSNotAllowed(ABSClientError):
    """The requested resource is outside the configured library allowlist."""


class ABSAuthError(ABSClientError):
    """The backend rejected the configured API key."""


class ABSNotFound(ABSClientError):
    """The backend has no such resource, or it may not be disclosed."""


class ABSUnavailable(ABSClientError):
    """The backend could not be reached or did not answer in time."""


class ABSResponseError(ABSClientError):
    """The backend returned a redirect, an error status, or malformed data."""


class ABSClient:
    """Fixed Audiobookshelf read adapter with a private bounded request core.

    Only the read-only endpoints used by the tools are public here. The
    backend base URL, credential, redirect policy, timeout, response cap, and
    connection limits are fixed at construction; no tool can supply a host,
    path, method, or headers.
    """

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed_library_ids = config.allowed_library_ids
        # Read by the podcast write adapter as defense in depth. The read-only
        # operations never consult it.
        self._podcast_writes_enabled = config.podcast_writes_enabled
        self._client = httpx.AsyncClient(
            base_url=f"{config.abs_url}/",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=2,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._requests = asyncio.Semaphore(MAX_CONNECTIONS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_libraries(self) -> list[dict[str, Any]]:
        """Return compact metadata for allowed libraries only."""
        data = await self._get_json("api/libraries")
        libraries = data.get("libraries")
        if not isinstance(libraries, list):
            raise ABSResponseError("Audiobookshelf returned an unexpected response.")

        result: list[dict[str, Any]] = []
        for raw in libraries:
            if not isinstance(raw, dict):
                continue
            library_id = raw.get("id")
            if (
                not is_valid_resource_id(library_id)
                or library_id not in self._allowed_library_ids
            ):
                continue
            media_type = _sanitize_text(raw.get("mediaType"), 20)
            result.append(
                {
                    "id": library_id,
                    "name": _sanitize_text(raw.get("name"), _MAX_TEXT_LENGTH),
                    "media_type": (
                        media_type if media_type in {"book", "podcast"} else "unknown"
                    ),
                }
            )
        return result

    async def search_library(
        self, library_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Search one allowed library and return at most ``limit`` compact items."""
        self._require_library(library_id)
        query = _validate_query(query)
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ABSInvalidRequest(
                f"Limit must be an integer between 1 and {MAX_SEARCH_LIMIT}."
            )

        data = await self._get_json(
            f"api/libraries/{library_id}/search",
            params={"q": query, "limit": str(limit)},
        )
        if "book" not in data and "podcast" not in data:
            raise ABSResponseError("Audiobookshelf returned an unexpected response.")

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("book", "podcast"):
            matches = data.get(key, [])
            if matches is None:
                continue
            if not isinstance(matches, list):
                raise ABSResponseError("Audiobookshelf returned an unexpected response.")
            for raw in matches:
                projected = self._project_search_result(raw, library_id)
                if projected is None or projected["id"] in seen:
                    continue
                seen.add(projected["id"])
                result.append(projected)
                if len(result) >= limit:
                    return result
        return result

    async def get_item(self, item_id: str) -> dict[str, Any]:
        """Return one allowed item, checking its library before returning data.

        A payload that cannot be read safely is reported exactly like an
        inaccessible or missing item, rather than exposing a distinct
        size/parse error.
        """
        if not is_valid_resource_id(item_id):
            raise ABSInvalidRequest("Invalid item ID.")

        try:
            data = await self._get_json(
                f"api/items/{item_id}", params={"expanded": "1"}
            )
        except (ABSNotFound, ABSResponseError):
            raise ABSNotAllowed("Item not found or not permitted.") from None

        library_id = data.get("libraryId")
        if (
            not isinstance(library_id, str)
            or library_id not in self._allowed_library_ids
        ):
            raise ABSNotAllowed("Item not found or not permitted.")

        projected = _project_item(data, library_id)
        if projected is None:
            raise ABSNotAllowed("Item not found or not permitted.")
        return projected

    def _require_library(self, library_id: str) -> None:
        if not is_valid_resource_id(library_id):
            raise ABSInvalidRequest("Invalid library ID.")
        if library_id not in self._allowed_library_ids:
            raise ABSNotAllowed("Library not found or not permitted.")

    def _project_search_result(
        self, raw: object, requested_library_id: str
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        # Current Audiobookshelf responses wrap the item in ``libraryItem``.
        # Accepting a direct item as well keeps this adapter tolerant of the
        # same payload without a wrapper.
        wrapped = raw.get("libraryItem")
        item = wrapped if isinstance(wrapped, dict) else raw

        # A search result must identify the requested allowed library. A
        # missing, malformed, other-library, or unlisted libraryId is treated
        # as untrusted and dropped rather than inheriting the requested ID.
        library_id = item.get("libraryId")
        if (
            not isinstance(library_id, str)
            or library_id != requested_library_id
            or library_id not in self._allowed_library_ids
        ):
            return None
        return _project_item(item, library_id)

    async def _get_json(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        data = await self._request_json("GET", path, params=params)
        if data is None:  # pragma: no cover - GET never allows an empty body
            raise ABSResponseError("Audiobookshelf returned an unexpected response.")
        return data

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: object | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        """Perform one fixed-method request and return its JSON object.

        ``method`` and ``path`` are always module constants or built from a
        validated ID by a hardcoded public method; neither is ever a tool
        argument. GET accepts only 200. Write methods accept only 200, 201, and
        204, and may return ``None`` when ``allow_empty`` is set because some
        native Audiobookshelf mutations answer with an empty or plain-text 200.

        The outer timeout bounds the whole operation (connecting, waiting for
        connection capacity, headers, and streaming), while httpx's timeout
        separately bounds each network phase.
        """
        allowed_statuses = (200,) if method == "GET" else (200, 201, 204)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._requests:
                    async with self._client.stream(
                        method,
                        path,
                        params=params,
                        json=json_body,
                    ) as response:
                        if response.status_code not in allowed_statuses:
                            raise _status_error(response.status_code)
                        body = await _read_bounded(response, max_response_bytes)
        except ABSClientError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ABSUnavailable("Audiobookshelf request timed out.") from None
        except (TypeError, UnicodeError):
            # A backend-supplied value that cannot be serialized must never
            # escape as an internal encoding exception.
            raise ABSResponseError(
                "Audiobookshelf returned an unexpected response."
            ) from None
        except httpx.HTTPError:
            raise ABSUnavailable("Audiobookshelf is unavailable.") from None

        if allow_empty and not body.strip():
            return None
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            if allow_empty:
                return None
            raise ABSResponseError(
                "Audiobookshelf returned an unexpected response."
            ) from None
        if not isinstance(data, dict):
            if allow_empty:
                return None
            raise ABSResponseError("Audiobookshelf returned an unexpected response.")
        return data


async def _read_bounded(
    response: httpx.Response, max_bytes: int = MAX_RESPONSE_BYTES
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            pass
        else:
            if declared_length > max_bytes:
                raise ABSResponseError(
                    "Audiobookshelf response exceeded the size limit."
                )

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ABSResponseError("Audiobookshelf response exceeded the size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _status_error(status_code: int) -> ABSClientError:
    if status_code == 401:
        return ABSAuthError("Audiobookshelf rejected the configured API key.")
    # 403 and 404 deliberately share one generic message so an inaccessible
    # library or item is indistinguishable from a nonexistent one.
    if status_code in {403, 404}:
        return ABSNotFound("Requested Audiobookshelf resource was not found.")
    if status_code >= 500:
        return ABSUnavailable("Audiobookshelf is unavailable.")
    return ABSResponseError("Audiobookshelf returned an unexpected response.")


def _validate_query(query: object) -> str:
    if not isinstance(query, str):
        raise ABSInvalidRequest("Invalid search query.")
    query = query.strip()
    if (
        not query
        or len(query) > MAX_QUERY_LENGTH
        or any(not char.isprintable() for char in query)
    ):
        raise ABSInvalidRequest(
            f"Query must be 1-{MAX_QUERY_LENGTH} printable characters."
        )
    return query


def _project_item(item: Mapping[str, Any], library_id: str) -> dict[str, Any] | None:
    item_id = item.get("id")
    if not is_valid_resource_id(item_id):
        return None

    media = item.get("media")
    if not isinstance(media, dict):
        media = {}
    metadata = media.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    media_type = item.get("mediaType")
    if media_type not in {"book", "podcast"}:
        media_type = "unknown"

    authors = (
        _names(metadata.get("authors"))
        or _names(metadata.get("authorName"))
        or _names(metadata.get("author"))
    )
    narrators = _names(metadata.get("narrators")) or _names(
        metadata.get("narratorName")
    )
    series = _names(metadata.get("series")) or _names(metadata.get("seriesName"))

    return {
        "id": item_id,
        "library_id": library_id,
        "media_type": media_type,
        "title": _sanitize_text(metadata.get("title"), _MAX_TEXT_LENGTH),
        "authors": authors,
        "narrators": narrators,
        "series": series,
        "published_year": _published_year(metadata.get("publishedYear")),
        "duration_seconds": _duration_seconds(media.get("duration")),
    }


def _names(value: object) -> list[str]:
    if isinstance(value, str):
        text = _sanitize_text(value, _MAX_NAME_LENGTH)
        return [text] if text else []
    if not isinstance(value, list):
        return []

    names: list[str] = []
    for entry in value:
        name = entry.get("name") if isinstance(entry, dict) else entry
        text = _sanitize_text(name, _MAX_NAME_LENGTH)
        if text and text not in names:
            names.append(text)
        if len(names) >= _MAX_NAMES:
            break
    return names


def _sanitize_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char if char.isprintable() else " " for char in value)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 3].rstrip() + "..."
    return cleaned


def _published_year(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 9999 else None
    if (
        isinstance(value, str)
        and len(value) == 4
        and value.isascii()
        and value.isdigit()
    ):
        year = int(value)
        if 1000 <= year <= 9999:
            return year
    return None


def _duration_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # JSON allows arbitrarily large integers; converting such an int to a
        # float for isfinite() raises. Treat it as absent, not a duration.
        return None
    if not finite or value <= 0:
        return None
    return int(value)
