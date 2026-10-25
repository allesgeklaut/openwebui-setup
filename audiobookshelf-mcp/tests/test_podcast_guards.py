"""Tests for conservative podcast schedule and remote URL guards."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Sequence

import pytest

from audiobookshelf_mcp import podcast_guards
from audiobookshelf_mcp.client import ABSInvalidRequest
from audiobookshelf_mcp.podcast_guards import (
    ensure_public_url,
    validate_public_url,
    validate_schedule,
)


def _install_resolver(
    monkeypatch: pytest.MonkeyPatch,
    addresses: Sequence[str] = (),
    *,
    error: Exception | None = None,
    delay: float = 0.0,
) -> list[tuple[str, int]]:
    """Replace the running loop's getaddrinfo; never touches real DNS."""
    calls: list[tuple[str, int]] = []

    async def fake_getaddrinfo(host: str, port: int, **kwargs: object) -> list[object]:
        calls.append((host, port))
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        entries: list[object] = []
        for address in addresses:
            if ":" in address:
                entries.append(
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port, 0, 0),
                    )
                )
            else:
                entries.append(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port),
                    )
                )
        return entries

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    return calls


def _install_entries(
    monkeypatch: pytest.MonkeyPatch, entries: object
) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    async def fake_getaddrinfo(host: str, port: int, **kwargs: object) -> object:
        calls.append((host, port))
        return entries

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    return calls


# ---------------------------------------------------------------------------
# Schedule validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schedule",
    [
        "0 9 * * *",
        "30 6 * * 1",
        "*/15 * * * *",
        "*/20 3 * * 1",
        "*/30 0 * * *",
        "*/45 * * * *",
        "0,15,30,45 * * * *",
        "5,20,35,50 4 * * *",
        "10,50 * * * *",
        "59,14 * * * *",
        "59 23 31 12 6",
        "0 0 1 1 0",
        "59 0 31 12 6",
        "0 23 1 1 0",
        "15 */6 */2 */3 0,3,6",
        "0 0 1 1 */6",
    ],
)
def test_valid_schedules_are_returned_unchanged(schedule: str) -> None:
    assert validate_schedule(schedule) == schedule


@pytest.mark.parametrize(
    "schedule",
    [
        "",
        " ",
        "0",
        "0 9 * *",
        "0 9 * * * *",
        "0 9 * * * extra",
        "0  9 * * *",
        " 0 9 * * *",
        "0 9 * * * ",
        "0\t9 * * *",
        "0\n9 * * *",
        "0\u00a09 * * *",
        "0 9 * * *\x00",
        "0 9 * * *; rm -rf /",
        "0 9 * * * | true",
        "0 9-17 * * *",
        "0-30 9 * * *",
        "1-5 * * * *",
        "*/5-10 * * * *",
        "0 9 * JAN *",
        "0 9 * * MON",
        "0 9 * * mon",
        "0 9 * January *",
        "? 9 * * *",
        "0 9 ? * *",
        "0 9 L * *",
        "0 9 * * 5L",
        "0 9 15W * *",
        "0 9 * * 5#2",
        "60 9 * * *",
        "0 24 * * *",
        "0 9 0 * *",
        "0 9 32 * *",
        "0 9 * 0 *",
        "0 9 * 13 *",
        "0 9 * * 7",
        "0 9 * * 8",
        "0 0 1 1 */7",
        "0 0 1 1 */07",
        "-1 9 * * *",
        "0 -1 * * *",
        "0 9 * * -1",
        "0 9 * * *,",
        "0 9 * * ,",
        "*/0 * * * *",
        "*/ * * * *",
        "** * * * *",
        "*,5 * * * *",
        "5,* * * * *",
        "1,*/5 * * * *",
        "1/5 * * * *",
        "0 9 * * *" + "x" * 100,
        "\u0665 9 * * *",
    ],
)
def test_invalid_schedules_are_rejected_with_one_fixed_message(schedule: str) -> None:
    with pytest.raises(ABSInvalidRequest) as raised:
        validate_schedule(schedule)

    message = str(raised.value)
    assert message == (
        "Invalid podcast schedule: expected a five-field cron expression "
        "running at least 15 minutes apart."
    )


@pytest.mark.parametrize(
    "schedule",
    ["* * * * *", "*/1 * * * *", "*/5 * * * *", "*/10 * * * *", "*/14 * * * *"],
)
def test_dense_minute_schedules_are_rejected(schedule: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_schedule(schedule)


@pytest.mark.parametrize(
    "schedule",
    [
        "0,14 * * * *",
        "0,5 * * * *",
        "0,15,30,44 * * * *",
        "5,19,35,50 * * * *",
        "0,59 * * * *",
    ],
)
def test_minute_gaps_below_fifteen_minutes_are_rejected(schedule: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_schedule(schedule)


@pytest.mark.parametrize(
    "schedule",
    ["0 * * * *", "0,15 * * * *", "0,30 * * * *", "10,50 * * * *", "59,14 * * * *"],
)
def test_minute_gaps_of_fifteen_minutes_or_more_are_accepted(schedule: str) -> None:
    assert validate_schedule(schedule) == schedule


def test_schedule_extra_fields_and_whitespace_are_bounded() -> None:
    assert len(validate_schedule("*/15 * * * *")) == len("*/15 * * * *")
    for schedule in ["0 9 * * * *", "0 9 * *", "0 9 * * * ", " 0 9 * * *"]:
        with pytest.raises(ABSInvalidRequest):
            validate_schedule(schedule)


@pytest.mark.parametrize(
    "schedule", [None, 0, 1.5, b"0 9 * * *", ["0 9 * * *"], ("0 9 * * *",), True]
)
def test_non_string_schedules_are_rejected(schedule: object) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_schedule(schedule)  # type: ignore[arg-type]


def test_schedule_error_never_echoes_the_input() -> None:
    secret = "SCHEDULE-SECRET-TOKEN"
    with pytest.raises(ABSInvalidRequest) as raised:
        validate_schedule(f"0 9 * * * {secret}")

    assert secret not in str(raised.value)


# ---------------------------------------------------------------------------
# Remote URL syntax validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://feeds.example.com/podcast.xml",
        "https://feeds.example.com/podcast.xml?token=abc123",
        "http://feeds.example.com:80/podcast.xml",
        "https://feeds.example.com:443/podcast.xml",
        "HTTPS://FEEDS.EXAMPLE.COM/Feed.XML",
        "https://example.com",
        "https://example.com/feed?",
        "https://example.com./feed",
        "https://example.com/feed/@user",
        "https://example.com/a%20b/feed?token=a%2Fb",
        "https://example.com/feed?token=a%23b",
        "https://1.1.1.1/feed",
        "http://1.1.1.1:80/feed",
        "https://[2606:4700:4700::1111]/feed",
        "http://[2606:4700:4700::1111]:80/feed",
        "https://example.com/" + "a" * (8192 - len("https://example.com/")),
    ],
)
def test_valid_remote_urls_are_returned_unchanged(url: str) -> None:
    assert validate_public_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "example.com/feed",
        "/feed",
        "//example.com/feed",
        "ftp://example.com/feed",
        "file:///etc/passwd",
        "ws://example.com/feed",
        "javascript:alert(1)",
        "data:text/html,hello",
        "https:///feed",
        "https://",
        "https://example.com/" + "a" * (8192 - len("https://example.com/") + 1),
    ],
)
def test_relative_or_non_http_urls_are_rejected(url: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/feed",
        "https://user:pass@example.com/feed",
        "https://:pass@example.com/feed",
        "https://user:@example.com/feed",
        "https://example.com/feed#fragment",
        "https://example.com/feed?x=1#fragment",
        "https://example.com/feed#",
        "https://example.com\\@evil.example/feed",
        "https://example.com/feed\\",
        "https://example.com/fe ed",
        "https://example.com/feed\t",
        "https://example.com/feed\n",
        "https://example.com/feed\r",
        "https://example.com/feed\x00",
        "https://example.com/feed\x7f",
        "https://exa\u00e9mple.com/feed",
    ],
)
def test_unsafe_url_characters_and_userinfo_are_rejected(url: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:80/feed",
        "http://example.com:443/feed",
        "https://example.com:8080/feed",
        "https://example.com:0/feed",
        "https://example.com:65536/feed",
        "https://example.com:99999/feed",
        "https://example.com:/feed",
        "https://example.com:abc/feed",
        "https://example.com:80:90/feed",
        "https://[::1]:/feed",
    ],
)
def test_non_default_or_malformed_ports_are_rejected(url: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://-example.com/feed",
        "https://example-.com/feed",
        "https://example..com/feed",
        "https://.example.com/feed",
        "https://example.com../feed",
        "https://bad_host.example/feed",
        "https://exa%6dple.com/feed",
        "https://[example.com]/feed",
        "https://[::1]extra/feed",
        "https://" + "a" * 64 + ".example/feed",
        "https://" + ".".join(["a" * 63] * 4) + "/feed",
    ],
)
def test_invalid_hostnames_are_rejected(url: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",
        "http://127.1.2.3/feed",
        "http://10.0.0.1/feed",
        "http://172.16.0.1/feed",
        "http://192.168.1.1/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/feed",
        "http://100.64.0.1/feed",
        "http://192.0.2.1/feed",
        "http://224.0.0.1/feed",
        "http://255.255.255.255/feed",
        "http://[::1]/feed",
        "http://[::]/feed",
        "http://[fe80::1]/feed",
        "http://[fc00::1]/feed",
        "http://[fec0::1]/feed",
        "http://[2001:db8::1]/feed",
        "http://[::ffff:127.0.0.1]/feed",
        "http://[fe80::1%25eth0]/feed",
    ],
)
def test_non_public_literal_addresses_are_rejected(url: str) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url", [None, 0, b"https://example.com/feed", ["https://example.com/"]]
)
def test_non_string_urls_are_rejected(url: object) -> None:
    with pytest.raises(ABSInvalidRequest):
        validate_public_url(url)  # type: ignore[arg-type]


def test_url_errors_never_contain_the_url_or_its_tokens() -> None:
    secret = "UNIQUE-FEED-TOKEN-42"
    urls = [
        f"https://user:{secret}@example.com/feed?token={secret}",
        f"https://example.com/feed?token={secret}#{secret}",
        f"https://127.0.0.1/feed?token={secret}",
        f"https://example.com/{secret}\n",
        f"https://bad_host.example/feed?token={secret}",
        f"https://example.com:8080/{secret}",
    ]
    for url in urls:
        with pytest.raises(ABSInvalidRequest) as raised:
            validate_public_url(url)
        assert secret not in str(raised.value)
        assert "example.com" not in str(raised.value)


# ---------------------------------------------------------------------------
# Remote URL preflight DNS resolution
# ---------------------------------------------------------------------------


async def test_public_hostname_resolves_and_url_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_resolver(monkeypatch, ["93.184.216.34", "2606:4700:4700::1111"])
    url = "https://feeds.example.com/podcast.xml?token=keep-me"

    assert await ensure_public_url(url) == url
    assert calls == [("feeds.example.com", 443)]


@pytest.mark.parametrize(
    ("url", "expected_port"),
    [
        ("http://feeds.example.com/feed", 80),
        ("http://feeds.example.com:80/feed", 80),
        ("https://feeds.example.com/feed", 443),
        ("https://feeds.example.com:443/feed", 443),
    ],
)
async def test_resolution_uses_the_effective_port(
    monkeypatch: pytest.MonkeyPatch, url: str, expected_port: int
) -> None:
    calls = _install_resolver(monkeypatch, ["93.184.216.34"])

    assert await ensure_public_url(url) == url
    assert calls == [("feeds.example.com", expected_port)]


async def test_public_literal_address_skips_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_resolver(monkeypatch, ["127.0.0.1"])

    assert await ensure_public_url("https://1.1.1.1/feed") == "https://1.1.1.1/feed"
    assert await ensure_public_url("https://[2606:4700:4700::1111]/feed") == (
        "https://[2606:4700:4700::1111]/feed"
    )
    assert calls == []


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34", "10.0.0.5"],
        ["93.184.216.34", "::1"],
        ["10.0.0.5", "93.184.216.34"],
        ["93.184.216.34", "169.254.169.254"],
    ],
)
async def test_mixed_public_and_private_answers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    _install_resolver(monkeypatch, addresses)

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.5.4",
        "192.168.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "192.0.2.10",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fec0::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_any_non_public_resolved_address_is_rejected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _install_resolver(monkeypatch, [address])

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


@pytest.mark.parametrize(
    "entry",
    [
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("2606:4700:4700::1111%eth0", 443, 0, 0),
        ),
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("2606:4700:4700::1111", 443, 0, 2),
        ),
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("fe80::1%eth0", 443, 0, 1),
        ),
    ],
)
async def test_ipv6_zone_and_scope_identifiers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, entry: tuple[object, ...]
) -> None:
    _install_entries(monkeypatch, [entry])

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


async def test_decimal_and_octal_looking_hosts_are_denied_when_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_resolver(monkeypatch, ["127.0.0.1"])

    for host in ["2130706433", "0177.0.0.1", "0x7f.0.0.1"]:
        with pytest.raises(ABSInvalidRequest):
            await ensure_public_url(f"http://{host}/feed")

    assert [host for host, _ in calls] == ["2130706433", "0177.0.0.1", "0x7f.0.0.1"]


async def test_empty_dns_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, [])

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(-2, "Name or service not known"),
        OSError("resolver failed"),
        UnicodeError("bad host encoding"),
    ],
)
async def test_dns_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    secret = "DNS-FAILURE-TOKEN"
    _install_resolver(monkeypatch, error=error)

    with pytest.raises(ABSInvalidRequest) as raised:
        await ensure_public_url(f"https://feeds.example.com/feed?token={secret}")

    message = str(raised.value)
    assert message == "Remote URL host could not be verified."
    assert secret not in message
    assert "feeds.example.com" not in message


async def test_dns_timeout_is_bounded_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(podcast_guards, "DNS_TIMEOUT_SECONDS", 0.01)
    secret = "TIMEOUT-TOKEN"
    _install_resolver(monkeypatch, ["93.184.216.34"], delay=0.2)

    with pytest.raises(ABSInvalidRequest) as raised:
        await ensure_public_url(f"https://feeds.example.com/feed?token={secret}")

    message = str(raised.value)
    assert secret not in message
    assert "feeds.example.com" not in message


@pytest.mark.parametrize(
    "entries",
    [
        None,
        (),
        "not-a-list",
        [("short",)],
        [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", "/tmp/socket")],
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", "93.184.216.34")],
        [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("not-an-ip", 80),
            )
        ],
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (93, 80))],
    ],
)
async def test_malformed_resolver_answers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, entries: object
) -> None:
    _install_entries(monkeypatch, entries)

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


async def test_excessive_dns_answers_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(
        monkeypatch, ["93.184.216.34"] * (podcast_guards.MAX_DNS_RECORDS + 1)
    )

    with pytest.raises(ABSInvalidRequest):
        await ensure_public_url("https://feeds.example.com/feed")


async def test_invalid_urls_are_rejected_before_any_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_resolver(monkeypatch, ["93.184.216.34"])

    for url in [
        "ftp://feeds.example.com/feed",
        "https://user:pass@feeds.example.com/feed",
        "http://127.0.0.1/feed",
        "https://feeds.example.com:8080/feed",
        "https://feeds.example.com/feed#fragment",
    ]:
        with pytest.raises(ABSInvalidRequest):
            await ensure_public_url(url)

    assert calls == []
