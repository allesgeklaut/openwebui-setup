"""Configuration validation and secret-handling tests."""

from __future__ import annotations

import traceback
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import pytest

from audiobookshelf_mcp.config import (
    DEFAULT_ALLOWED_HOSTS,
    MAX_TIMEZONE_NAME_LENGTH,
    MIN_MCP_TOKEN_LENGTH,
    Config,
    ConfigError,
    load_config,
)


def _secret(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _valid_env(tmp_path: Path) -> dict[str, str]:
    return {
        "ABS_URL": "http://abs.local:8080",
        "ABS_ALLOWED_LIBRARY_IDS": "lib1,lib2",
        "ABS_API_KEY_FILE": _secret(tmp_path, "abs-key", "backend-key\n"),
        "MCP_AUTH_TOKEN_FILE": _secret(tmp_path, "mcp-token", "t" * 32 + "\n"),
    }


def test_loads_valid_config_and_defaults(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    env["ABS_URL"] = "http://abs.local:8080/base/"
    env["ABS_ALLOWED_LIBRARY_IDS"] = "lib1, lib-2,lib1"

    config = load_config(env)

    assert config.abs_url == "http://abs.local:8080/base"
    assert config.api_key == "backend-key"
    assert config.mcp_auth_token == "t" * 32
    assert config.allowed_library_ids == frozenset({"lib1", "lib-2"})
    assert config.host == "0.0.0.0"
    assert config.port == 8000
    assert config.allowed_hosts == DEFAULT_ALLOWED_HOSTS
    assert config.allowed_origins == ()
    assert config.abs_server_timezone is None

    rendered = repr(config)
    assert "backend-key" not in rendered
    assert "t" * 32 not in rendered
    assert "abs.local" not in rendered


def test_custom_network_settings(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    env.update(
        {
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": "9000",
            "MCP_ALLOWED_HOSTS": "mcp.internal:9000,[::1]:*",
            "MCP_ALLOWED_ORIGINS": "https://ui.example:443,http://localhost:3000/",
        }
    )

    config = load_config(env)

    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.allowed_hosts == ("mcp.internal:9000", "[::1]:*")
    assert config.allowed_origins == (
        "https://ui.example:443",
        "http://localhost:3000",
    )


def test_url_with_ipv6_host_is_accepted(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    env["ABS_URL"] = "https://[::1]:8443/"

    assert load_config(env).abs_url == "https://[::1]:8443"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://abs.local",
        "http://user:pass@abs.local",
        "http://abs.local?x=1",
        "http://abs.local#fragment",
        "http:///missing-host",
        "http://abs.local:0",
        "http://abs.local:99999",
        "http://abs.local/bad\\path",
        "http://abs.local/../escape",
        "http://abs.local/path\nx",
        "http://abs.local:12x",
        "http://abs.local/\u00e9",
    ],
)
def test_invalid_abs_url_is_rejected(tmp_path: Path, url: str) -> None:
    env = _valid_env(tmp_path)
    env["ABS_URL"] = url

    with pytest.raises(ConfigError):
        load_config(env)


def test_missing_abs_url_is_rejected(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    del env["ABS_URL"]

    with pytest.raises(ConfigError, match="ABS_URL is required"):
        load_config(env)


@pytest.mark.parametrize(
    "library_ids",
    ["", "lib1,,lib2", "../lib1", "lib.1", "lib/1", "lib 1", "lib%2F1", "x" * 129],
)
def test_invalid_library_allowlist_is_rejected(
    tmp_path: Path, library_ids: str
) -> None:
    env = _valid_env(tmp_path)
    env["ABS_ALLOWED_LIBRARY_IDS"] = library_ids

    with pytest.raises(ConfigError):
        load_config(env)


def test_missing_library_allowlist_is_rejected(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    del env["ABS_ALLOWED_LIBRARY_IDS"]

    with pytest.raises(ConfigError, match="ABS_ALLOWED_LIBRARY_IDS is required"):
        load_config(env)


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("api-missing", None, "ABS_API_KEY_FILE is required"),
        ("api-nonexistent", "missing-file", "could not be read"),
        ("api-empty", "", "is empty"),
        ("api-blank", " \n\t", "is empty"),
        ("api-spaces", "has spaces", "invalid characters"),
        ("api-multiline", "line1\nline2", "invalid characters"),
    ],
)
def test_invalid_api_key_file_is_rejected(
    tmp_path: Path,
    name: str,
    content: str | None,
    message: str,
) -> None:
    env = _valid_env(tmp_path)
    if content is None:
        del env["ABS_API_KEY_FILE"]
    elif content == "missing-file":
        env["ABS_API_KEY_FILE"] = str(tmp_path / "does-not-exist")
    else:
        env["ABS_API_KEY_FILE"] = _secret(tmp_path, name, content)

    with pytest.raises(ConfigError, match=message):
        load_config(env)


def test_non_utf8_secret_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "binary-secret"
    path.write_bytes(b"\xff\xfe\x00secret")
    env = _valid_env(tmp_path)
    env["ABS_API_KEY_FILE"] = str(path)

    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(env)


def test_oversized_secret_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "huge-secret"
    path.write_text("x" * 9000, encoding="utf-8")
    env = _valid_env(tmp_path)
    env["ABS_API_KEY_FILE"] = str(path)

    with pytest.raises(ConfigError, match="too large"):
        load_config(env)


def test_mcp_token_minimum_length(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    env["MCP_AUTH_TOKEN_FILE"] = _secret(tmp_path, "short-token", "x" * 31)

    with pytest.raises(ConfigError, match=f"at least {MIN_MCP_TOKEN_LENGTH}"):
        load_config(env)

    env["MCP_AUTH_TOKEN_FILE"] = _secret(
        tmp_path, "exact-token", "x" * MIN_MCP_TOKEN_LENGTH
    )
    assert len(load_config(env).mcp_auth_token) == MIN_MCP_TOKEN_LENGTH


@pytest.mark.parametrize("port", ["0", "65536", "-1", "not-a-number", ""])
def test_invalid_port_is_rejected(tmp_path: Path, port: str) -> None:
    env = _valid_env(tmp_path)
    env["MCP_PORT"] = port

    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_config(env)


@pytest.mark.parametrize(
    "allowed_hosts",
    [
        "",
        " ",
        "http://audiobookshelf-mcp:8000",
        "audiobookshelf-mcp:8000/path",
        "audiobookshelf-mcp:99999",
        "bad host:8000",
    ],
)
def test_invalid_allowed_hosts_are_rejected(
    tmp_path: Path, allowed_hosts: str
) -> None:
    env = _valid_env(tmp_path)
    env["MCP_ALLOWED_HOSTS"] = allowed_hosts

    with pytest.raises(ConfigError, match="MCP_ALLOWED_HOSTS"):
        load_config(env)


@pytest.mark.parametrize(
    "origin",
    [
        "ui.example",
        "https://ui.example/path",
        "https://user@ui.example",
        "https://ui.example?x=1",
        "https://ui.example#fragment",
        "https://ui.example:99999",
        "*",
    ],
)
def test_invalid_allowed_origins_are_rejected(
    tmp_path: Path, origin: str
) -> None:
    env = _valid_env(tmp_path)
    env["MCP_ALLOWED_ORIGINS"] = origin

    with pytest.raises(ConfigError, match="MCP_ALLOWED_ORIGINS"):
        load_config(env)


def test_secret_errors_do_not_contain_secret_values(tmp_path: Path) -> None:
    secret_value = "THIS-MUST-NOT-LEAK-IN-AN-ERROR"
    env = _valid_env(tmp_path)
    env["MCP_AUTH_TOKEN_FILE"] = _secret(
        tmp_path, "leaky-token", secret_value
    )

    with pytest.raises(ConfigError) as raised:
        load_config(env)

    assert secret_value not in str(raised.value)


def test_podcast_writes_are_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(_valid_env(tmp_path))

    assert config.podcast_writes_enabled is False


def test_config_constructed_directly_defaults_to_read_only() -> None:
    config = Config(
        abs_url="http://abs.test",
        api_key="backend-key",
        mcp_auth_token="t" * 32,
        allowed_library_ids=frozenset({"lib1"}),
    )

    assert config.podcast_writes_enabled is False
    assert config.abs_server_timezone is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("  true  ", True),
        ("false", False),
        ("FALSE", False),
        ("False", False),
        ("\tfalse\n", False),
    ],
)
def test_podcast_writes_accept_case_insensitive_booleans(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    env = _valid_env(tmp_path)
    env["ABS_ENABLE_PODCAST_WRITES"] = raw

    assert load_config(env).podcast_writes_enabled is expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "1",
        "0",
        "yes",
        "no",
        "on",
        "off",
        "enabled",
        "disabled",
        "truthy",
        "tru",
        "true,false",
        "true false",
        "true1",
    ],
)
def test_invalid_podcast_writes_values_fail_closed(tmp_path: Path, raw: str) -> None:
    env = _valid_env(tmp_path)
    env["ABS_ENABLE_PODCAST_WRITES"] = raw

    with pytest.raises(
        ConfigError, match="ABS_ENABLE_PODCAST_WRITES must be true or false"
    ):
        load_config(env)


def test_invalid_podcast_writes_value_is_not_echoed(tmp_path: Path) -> None:
    raw = "do-not-echo-this-value"
    env = _valid_env(tmp_path)
    env["ABS_ENABLE_PODCAST_WRITES"] = raw

    with pytest.raises(ConfigError) as raised:
        load_config(env)

    assert raw not in str(raised.value)


def test_server_timezone_defaults_to_none(tmp_path: Path) -> None:
    assert load_config(_valid_env(tmp_path)).abs_server_timezone is None


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_server_timezone_is_none(tmp_path: Path, raw: str) -> None:
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = raw

    assert load_config(env).abs_server_timezone is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UTC", "UTC"),
        ("Europe/Vienna", "Europe/Vienna"),
        ("America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
        ("  Europe/Vienna  ", "Europe/Vienna"),
        ("\tUTC\n", "UTC"),
    ],
)
def test_valid_server_timezone_is_trimmed_and_accepted(
    tmp_path: Path, raw: str, expected: str
) -> None:
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = raw

    assert load_config(env).abs_server_timezone == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Mars/Olympus_Mons",
        "localtime",
        "posixrules",
        "posix/UTC",
        "right/UTC",
        "/etc/localtime",
        "../etc/passwd",
        "Europe/../etc/passwd",
        "Europe/Vienna/../../etc/passwd",
        "Europe//Vienna",
        "Europe/Vienna/",
        "Europe/Vienna\x00",
        "Europe/Vienna\tBerlin",
        "Europe/Vienna Berlin",
        "A" * (MAX_TIMEZONE_NAME_LENGTH + 1),
    ],
)
def test_invalid_server_timezone_is_rejected_without_echo(
    tmp_path: Path, raw: str
) -> None:
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = raw

    with pytest.raises(ConfigError, match="ABS_SERVER_TIMEZONE") as raised:
        load_config(env)

    assert raw.strip() not in str(raised.value)


def test_too_long_server_timezone_uses_the_length_message(tmp_path: Path) -> None:
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = "A" * (MAX_TIMEZONE_NAME_LENGTH + 1)

    with pytest.raises(ConfigError, match="at most 100 characters"):
        load_config(env)


@pytest.mark.parametrize(
    "error_type", [ZoneInfoNotFoundError, ValueError, TypeError]
)
def test_unavailable_server_timezone_is_a_sanitized_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    def _reject(_key: str) -> None:
        raise error_type("no time zone data")

    monkeypatch.setattr("audiobookshelf_mcp.config.ZoneInfo", _reject)
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = "Europe/Vienna"

    with pytest.raises(ConfigError, match="valid IANA time zone") as raised:
        load_config(env)

    assert "Europe/Vienna" not in str(raised.value)


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_zoneinfo_oserror_is_a_sanitized_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError],
) -> None:
    private_path = str(tmp_path / "private-tzdata" / "Europe" / "Vienna")

    def _reject(_key: str) -> None:
        raise error_type(13, "permission denied", private_path)

    monkeypatch.setattr("audiobookshelf_mcp.config.ZoneInfo", _reject)
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = "Europe/Vienna"

    with pytest.raises(ConfigError, match="valid IANA time zone") as raised:
        load_config(env)

    assert private_path not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(raised.value))
    assert private_path not in formatted
    assert error_type.__name__ not in formatted


@pytest.mark.parametrize("raw", [123, b"Europe/Vienna", ["UTC"]])
def test_non_string_server_timezone_is_rejected_without_echo(
    tmp_path: Path, raw: object
) -> None:
    env = _valid_env(tmp_path)
    env["ABS_SERVER_TIMEZONE"] = raw  # type: ignore[assignment]

    with pytest.raises(ConfigError, match="ABS_SERVER_TIMEZONE") as raised:
        load_config(env)

    assert repr(raw) not in str(raised.value)
