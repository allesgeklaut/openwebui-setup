"""Configuration validation and secret-handling tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from audiobookshelf_mcp.config import (
    DEFAULT_ALLOWED_HOSTS,
    MIN_MCP_TOKEN_LENGTH,
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
