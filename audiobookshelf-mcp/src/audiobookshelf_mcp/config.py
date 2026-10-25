"""Typed, fail-closed configuration for the Audiobookshelf MCP server."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_ALLOWED_HOSTS = (
    "audiobookshelf-mcp:8000",
    "localhost:8000",
    "127.0.0.1:8000",
)
MIN_MCP_TOKEN_LENGTH = 32
MAX_SECRET_FILE_BYTES = 8192
MAX_TIMEZONE_NAME_LENGTH = 100

# Audiobookshelf library and item IDs are opaque, but never contain URL syntax.
_RESOURCE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_SECRET_VALUE_RE = re.compile(r"\A[\x21-\x7e]+\Z")
_HOSTNAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_IPV6_RE = re.compile(r"\A[0-9A-Fa-f:.]+\Z")
_BIND_HOST_RE = re.compile(r"\A[A-Za-z0-9._:-]+\Z")
_HOST_ENTRY_RE = re.compile(
    r"\A(?P<host>[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}|\*))?\Z"
)

# IANA zone keys are "/"-separated path-like names; reject anything that could
# refer to a file outside the tz database (absolute paths, dot segments).
_TIMEZONE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._+-]+\Z")
# Files shipped next to the tz database that are not IANA zone names.
_NON_IANA_TIME_ZONE_KEYS = frozenset({"localtime", "posixrules"})
_NON_IANA_TIME_ZONE_PREFIXES = frozenset({"posix", "right"})
_INVALID_TIMEZONE_MESSAGE = "ABS_SERVER_TIMEZONE must be a valid IANA time zone name."


class ConfigError(ValueError):
    """Configuration is missing or invalid. Messages never contain secret values."""


@dataclass(frozen=True)
class Config:
    """Validated runtime settings.

    Secret values and the backend URL are excluded from the generated repr so an
    accidental debug log cannot disclose them.
    """

    abs_url: str = field(repr=False)
    api_key: str = field(repr=False)
    mcp_auth_token: str = field(repr=False)
    allowed_library_ids: frozenset[str]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    allowed_origins: tuple[str, ...] = ()
    podcast_writes_enabled: bool = False
    abs_server_timezone: str | None = None


def is_valid_resource_id(value: object) -> bool:
    """Return True for the conservative ID shape accepted in URLs and tool args."""
    return isinstance(value, str) and _RESOURCE_ID_RE.fullmatch(value) is not None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load and validate configuration from the environment.

    Credentials are read only from the ``*_FILE`` variables; direct token
    environment variables are intentionally unsupported. Any invalid setting
    raises :class:`ConfigError` so the process fails closed before serving.
    """
    values = os.environ if env is None else env

    abs_url = _validate_abs_url(_required(values, "ABS_URL"))
    allowed_library_ids = _parse_library_ids(
        _required(values, "ABS_ALLOWED_LIBRARY_IDS")
    )
    api_key = _read_secret(values, "ABS_API_KEY_FILE", min_length=1)
    mcp_auth_token = _read_secret(
        values, "MCP_AUTH_TOKEN_FILE", min_length=MIN_MCP_TOKEN_LENGTH
    )
    host = _parse_host(values)
    port = _parse_port(values)
    allowed_hosts = _parse_allowed_hosts(values)
    allowed_origins = _parse_allowed_origins(values)
    podcast_writes_enabled = _parse_podcast_writes(values)
    abs_server_timezone = _parse_server_timezone(values)

    return Config(
        abs_url=abs_url,
        api_key=api_key,
        mcp_auth_token=mcp_auth_token,
        allowed_library_ids=allowed_library_ids,
        host=host,
        port=port,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        podcast_writes_enabled=podcast_writes_enabled,
        abs_server_timezone=abs_server_timezone,
    )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required.")
    return value


def _validate_abs_url(raw: str) -> str:
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in raw):
        raise ConfigError("ABS_URL must contain only printable ASCII characters.")
    if "\\" in raw:
        raise ConfigError("ABS_URL must not contain backslashes.")

    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ConfigError("ABS_URL is not a valid URL.") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise ConfigError("ABS_URL must use http or https.")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("ABS_URL must not contain user information.")
    if parts.query or parts.fragment:
        raise ConfigError("ABS_URL must not contain a query or fragment.")
    if not hostname or not (_HOSTNAME_RE.fullmatch(hostname) or _IPV6_RE.fullmatch(hostname)):
        raise ConfigError("ABS_URL must contain a valid host.")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("ABS_URL contains an invalid port.")

    path = parts.path.rstrip("/")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise ConfigError("ABS_URL must not contain dot path segments.")

    host_part = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host_part = f"{host_part}:{port}"
    return urlunsplit((parts.scheme.lower(), host_part, path, "", ""))


def _parse_library_ids(raw: str) -> frozenset[str]:
    entries = [entry.strip() for entry in raw.split(",")]
    if not entries or any(not entry for entry in entries):
        raise ConfigError(
            "ABS_ALLOWED_LIBRARY_IDS must be a non-empty comma-separated list."
        )
    if any(not is_valid_resource_id(entry) for entry in entries):
        raise ConfigError("ABS_ALLOWED_LIBRARY_IDS contains an invalid library ID.")
    return frozenset(entries)


def _read_secret(env: Mapping[str, str], name: str, *, min_length: int) -> str:
    path = env.get(name, "").strip()
    if not path:
        raise ConfigError(f"{name} is required.")
    try:
        with open(path, "rb") as secret_file:
            data = secret_file.read(MAX_SECRET_FILE_BYTES + 1)
    except (OSError, ValueError):
        raise ConfigError(f"{name} could not be read.") from None
    if len(data) > MAX_SECRET_FILE_BYTES:
        raise ConfigError(f"{name} is too large.")

    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ConfigError(f"{name} must be UTF-8 text.") from None
    if len(value) < min_length:
        if min_length > 1:
            raise ConfigError(
                f"{name} must contain at least {min_length} characters."
            )
        raise ConfigError(f"{name} is empty.")
    if _SECRET_VALUE_RE.fullmatch(value) is None:
        raise ConfigError(f"{name} contains invalid characters.")
    return value


def _parse_host(env: Mapping[str, str]) -> str:
    host = env.get("MCP_HOST", DEFAULT_HOST).strip()
    if not host or _BIND_HOST_RE.fullmatch(host) is None:
        raise ConfigError("MCP_HOST must be a valid bind host.")
    return host


def _parse_podcast_writes(env: Mapping[str, str]) -> bool:
    """Read the podcast write opt-in, failing closed on anything but true/false."""
    raw = env.get("ABS_ENABLE_PODCAST_WRITES")
    if raw is None:
        return False
    if not isinstance(raw, str):
        raise ConfigError("ABS_ENABLE_PODCAST_WRITES must be true or false.")
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConfigError("ABS_ENABLE_PODCAST_WRITES must be true or false.")


def _parse_server_timezone(env: Mapping[str, str]) -> str | None:
    """Read the optional Audiobookshelf server time zone for reporting.

    Unset or blank values yield ``None`` so existing deployments keep working.
    Supplied values must be plain IANA zone keys resolvable by :class:`ZoneInfo`.
    Errors never echo the supplied value.
    """
    raw = env.get("ABS_SERVER_TIMEZONE")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(_INVALID_TIMEZONE_MESSAGE)
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_TIMEZONE_NAME_LENGTH:
        raise ConfigError(
            f"ABS_SERVER_TIMEZONE must be at most {MAX_TIMEZONE_NAME_LENGTH} characters."
        )
    if any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value
    ):
        raise ConfigError(_INVALID_TIMEZONE_MESSAGE)

    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or _TIMEZONE_SEGMENT_RE.fullmatch(segment) is None
        for segment in segments
    ):
        raise ConfigError(_INVALID_TIMEZONE_MESSAGE)
    if (
        value in _NON_IANA_TIME_ZONE_KEYS
        or segments[0] in _NON_IANA_TIME_ZONE_PREFIXES
    ):
        raise ConfigError(_INVALID_TIMEZONE_MESSAGE)

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        raise ConfigError(_INVALID_TIMEZONE_MESSAGE) from None
    return value


def _parse_port(env: Mapping[str, str]) -> int:
    raw = env.get("MCP_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw, 10)
    except ValueError:
        raise ConfigError("MCP_PORT must be an integer.") from None
    if not 1 <= port <= 65535:
        raise ConfigError("MCP_PORT must be between 1 and 65535.")
    return port


def _parse_allowed_hosts(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("MCP_ALLOWED_HOSTS")
    if raw is None:
        return DEFAULT_ALLOWED_HOSTS
    entries = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    if not entries:
        raise ConfigError("MCP_ALLOWED_HOSTS must contain at least one host.")
    for entry in entries:
        match = _HOST_ENTRY_RE.fullmatch(entry)
        if match is None:
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid host entry.")
        port_part = match.group("port")
        if (
            port_part is not None
            and port_part != "*"
            and not 1 <= int(port_part) <= 65535
        ):
            raise ConfigError("MCP_ALLOWED_HOSTS contains an invalid port.")
    return entries


def _parse_allowed_origins(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("MCP_ALLOWED_ORIGINS", "")
    normalized: list[str] = []
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            parts = urlsplit(entry)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            raise ConfigError("MCP_ALLOWED_ORIGINS contains an invalid origin.") from None
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ConfigError("MCP_ALLOWED_ORIGINS contains an invalid origin.")
        host_part = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            host_part = f"{host_part}:{port}"
        normalized.append(urlunsplit((parts.scheme.lower(), host_part, "", "", "")))
    return tuple(normalized)
