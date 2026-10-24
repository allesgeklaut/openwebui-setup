"""Conservative guards for podcast schedules and remote feed/enclosure URLs.

Schedule policy
---------------
Audiobookshelf 2.37.2 validates ``autoDownloadSchedule`` with its vendored
node-cron parser. That parser accepts five- or six-field expressions and, after
its own range/name conversions, reduces each field to digits, ``*``, and
``*/N``. This module accepts only a narrow, explicit subset of five-field
expressions:

* bare integers within the field bounds,
* ``*``,
* ``*/N`` with ``1 <= N <= field span``,
* comma-separated lists of bare integers.

Ranges (``1-5``), month/weekday names, ``?``, ``L``, and mixed forms such as
``*,5`` or ``1,*/5`` are rejected even where node-cron might tolerate them.
Weekday 7 is rejected in every form because Audiobookshelf rewrites it to 0
before validating, which turns ``*/7`` into the invalid ``*/0``. Anything
accepted here is also accepted by Audiobookshelf's validator; the reverse is
intentionally not true. This is not a cron engine: it does not expand dates,
model timezones, or schedule work.

The interval policy is deliberately conservative and heuristic. The set of
minute values in the minute field must be at least 15 minutes apart, including
the wrap from ``:59`` to ``:00`` of the next hour (and therefore across hour,
day, and month boundaries, which this check does not otherwise model). Thus
``*/15``, ``*/20``, ``*/30``, and a single minute are accepted while ``*``,
``*/1``, and ``*/5`` are rejected. The rule may reject schedules that would
still be sparse in practice; that is an accepted trade-off for bounding how
often the backend can be made to fetch feeds and download episodes. The
effective timezone remains Audiobookshelf's concern.

URL/SSRF boundary
-----------------
``validate_public_url`` is syntax-only. ``ensure_public_url`` adds one bounded
DNS resolution (five-second timeout) and requires every returned address to be
globally routable and not private, reserved, multicast, unspecified, loopback,
link-local, site-local, or scoped. Both reject literal non-public addresses,
credentials, fragments, backslashes, non-ASCII/control/whitespace characters,
non-default ports, and URLs over 8192 characters. Feed URLs are applied only to
remote RSS feed and enclosure URLs, never to ``ABS_URL``.

This preflight is defense-in-depth only. It performs no HTTP request, follows
no redirect, and pins nothing: DNS answers can change between this check and
Audiobookshelf's actual fetch, so it cannot by itself prevent DNS rebinding.
Audiobookshelf must enforce egress restrictions and re-check every resolved
address for each feed fetch, enclosure download, and redirect target. Do not
treat a successful call here as proof of SSRF safety. Error messages are fixed
strings and never include the URL, its host, its path, or its query token.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import NoReturn
from urllib.parse import SplitResult, urlsplit

from .client import ABSInvalidRequest

MAX_SCHEDULE_LENGTH = 100
MIN_SCHEDULE_INTERVAL_MINUTES = 15
MAX_URL_LENGTH = 8192
DNS_TIMEOUT_SECONDS = 5.0
MAX_DNS_RECORDS = 64

_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # weekday (0 = Sunday); 7 is rejected as non-portable
)
_WEEKDAY_INDEX = 4
_INT_RE = re.compile(r"\A[0-9]+\Z")
_STEP_RE = re.compile(r"\A\*/([0-9]+)\Z")
_LABEL_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

_SCHEDULE_MESSAGE = (
    "Invalid podcast schedule: expected a five-field cron expression "
    "running at least 15 minutes apart."
)
_INVALID_URL_MESSAGE = "Invalid remote URL."
_HOST_NOT_PUBLIC_MESSAGE = "Remote URL host is not publicly routable."
_HOST_UNRESOLVED_MESSAGE = "Remote URL host could not be verified."


def validate_schedule(schedule: str) -> str:
    """Validate a five-field Audiobookshelf download schedule.

    Raises :class:`ABSInvalidRequest` with a fixed message for anything outside
    the conservative subset documented in the module docstring. Returns the
    schedule with its five fields joined by single spaces.
    """
    if not isinstance(schedule, str):
        _deny_schedule()
    if not schedule or len(schedule) > MAX_SCHEDULE_LENGTH:
        _deny_schedule()

    fields = schedule.split(" ")
    if len(fields) != 5:
        _deny_schedule()

    expanded: list[tuple[int, ...]] = []
    for index, field in enumerate(fields):
        low, high = _FIELD_BOUNDS[index]
        if index == _WEEKDAY_INDEX and "7" in field:
            # Audiobookshelf rewrites every "7" in the weekday field to "0"
            # before expanding steps, so ``*/7`` becomes ``*/0`` and fails its
            # own validation. Literal 7 is also outside our weekday bounds.
            _deny_schedule()
        expanded.append(_expand_field(field, low, high))

    _require_minute_spacing(expanded[0])
    return " ".join(fields)


def validate_public_url(url: str) -> str:
    """Validate the syntax of a remote feed or enclosure URL.

    This performs no DNS lookup and no network request; see
    :func:`ensure_public_url` for the preflight address check. Returns the URL
    unchanged so path and query tokens are preserved byte for byte.
    """
    _parse_public_url(url)
    return url


async def ensure_public_url(url: str) -> str:
    """Validate a remote URL and preflight every address its host resolves to.

    The check is bounded by :data:`DNS_TIMEOUT_SECONDS` and rejects mixed
    public/private answers. It is stateless defense in depth, not a rebinding
    defense; Audiobookshelf must re-check egress on the actual fetch.
    """
    parts = _parse_public_url(url)
    hostname = parts.hostname
    if hostname is None:  # pragma: no cover - _parse_public_url guarantees this
        _deny_url(_INVALID_URL_MESSAGE)

    if _literal_ip(hostname) is not None:
        # A literal address was already checked by _parse_public_url; asking
        # the resolver again would add nothing and may query the network.
        return url

    scheme = parts.scheme.lower()
    port = parts.port if parts.port is not None else _default_port(scheme)

    try:
        async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
            entries = await asyncio.get_running_loop().getaddrinfo(hostname, port)
    except (TimeoutError, OSError, UnicodeError, ValueError):
        raise ABSInvalidRequest(_HOST_UNRESOLVED_MESSAGE) from None

    _require_public_answers(entries)
    return url


def _expand_field(field: str, low: int, high: int) -> tuple[int, ...]:
    """Expand one field to its absolute values using Audiobookshelf semantics."""
    if field == "*":
        return tuple(range(low, high + 1))

    if field.startswith("*/"):
        match = _STEP_RE.fullmatch(field)
        if match is None:
            _deny_schedule()
        step = int(match.group(1))
        if not 1 <= step <= high - low + 1:
            _deny_schedule()
        # node-cron converts ``*`` to the full range and keeps values divisible
        # by the step, so ``*/N`` starts at zero for fields that include it.
        return tuple(value for value in range(low, high + 1) if value % step == 0)

    values: list[int] = []
    for item in field.split(","):
        if _INT_RE.fullmatch(item) is None:
            _deny_schedule()
        value = int(item)
        if not low <= value <= high:
            _deny_schedule()
        values.append(value)
    return tuple(values)


def _require_minute_spacing(minutes: tuple[int, ...]) -> None:
    unique = sorted(set(minutes))
    if len(unique) < 2:
        return
    gaps = [later - earlier for earlier, later in zip(unique, unique[1:])]
    gaps.append(60 - unique[-1] + unique[0])
    if min(gaps) < MIN_SCHEDULE_INTERVAL_MINUTES:
        _deny_schedule()


def _parse_public_url(url: object) -> SplitResult:
    """Validate URL syntax and literal addresses, returning the parsed parts."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        _deny_url(_INVALID_URL_MESSAGE)
    for char in url:
        if not 0x21 <= ord(char) <= 0x7E:
            _deny_url(_INVALID_URL_MESSAGE)
    if "\\" in url or "#" in url:
        _deny_url(_INVALID_URL_MESSAGE)

    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        _deny_url(_INVALID_URL_MESSAGE)

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        _deny_url(_INVALID_URL_MESSAGE)
    if parts.username is not None or parts.password is not None:
        _deny_url(_INVALID_URL_MESSAGE)
    if parts.netloc.endswith(":"):
        _deny_url(_INVALID_URL_MESSAGE)
    if port is not None and port != _default_port(scheme):
        _deny_url(_INVALID_URL_MESSAGE)

    if "%" in hostname:
        # Reject percent-encoded hosts and IPv6 zone identifiers.
        _deny_url(_INVALID_URL_MESSAGE)

    if ":" in hostname:
        if not (parts.netloc.startswith("[") and "]" in parts.netloc):
            _deny_url(_INVALID_URL_MESSAGE)
        literal = _literal_ip(hostname)
        if not isinstance(literal, ipaddress.IPv6Address):
            _deny_url(_INVALID_URL_MESSAGE)
        if not _is_public_ip(literal):
            _deny_url(_HOST_NOT_PUBLIC_MESSAGE)
    else:
        literal = _literal_ip(hostname)
        if literal is not None:
            if not _is_public_ip(literal):
                _deny_url(_HOST_NOT_PUBLIC_MESSAGE)
        else:
            _validate_hostname(hostname)

    return parts


def _validate_hostname(hostname: str) -> None:
    name = hostname[:-1] if hostname.endswith(".") else hostname
    if not name or len(name) > 253:
        _deny_url(_INVALID_URL_MESSAGE)
    for label in name.split("."):
        if _LABEL_RE.fullmatch(label) is None:
            _deny_url(_INVALID_URL_MESSAGE)


def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if getattr(ip, "scope_id", None) is not None:
        return False
    if getattr(ip, "is_site_local", False):
        return False
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_reserved
        and not ip.is_multicast
        and not ip.is_unspecified
        and not ip.is_loopback
        and not ip.is_link_local
    )


def _require_public_answers(entries: object) -> None:
    if not isinstance(entries, (list, tuple)) or not entries:
        _deny_url(_HOST_UNRESOLVED_MESSAGE)
    if len(entries) > MAX_DNS_RECORDS:
        _deny_url(_HOST_UNRESOLVED_MESSAGE)

    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 5:
            _deny_url(_HOST_UNRESOLVED_MESSAGE)
        family, sockaddr = entry[0], entry[4]
        if family not in {socket.AF_INET, socket.AF_INET6}:
            _deny_url(_HOST_UNRESOLVED_MESSAGE)
        if not isinstance(sockaddr, (list, tuple)) or len(sockaddr) < 2:
            _deny_url(_HOST_UNRESOLVED_MESSAGE)

        address = sockaddr[0]
        if not isinstance(address, str) or "%" in address:
            _deny_url(_HOST_NOT_PUBLIC_MESSAGE)
        if family == socket.AF_INET6 and len(sockaddr) > 3 and sockaddr[3]:
            _deny_url(_HOST_NOT_PUBLIC_MESSAGE)

        ip = _literal_ip(address)
        if ip is None:
            _deny_url(_HOST_UNRESOLVED_MESSAGE)
        if not _is_public_ip(ip):
            _deny_url(_HOST_NOT_PUBLIC_MESSAGE)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _deny_schedule() -> NoReturn:
    raise ABSInvalidRequest(_SCHEDULE_MESSAGE) from None


def _deny_url(message: str) -> NoReturn:
    raise ABSInvalidRequest(message) from None
