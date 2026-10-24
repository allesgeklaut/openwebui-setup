"""Shared fixtures for the Audiobookshelf MCP tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from audiobookshelf_mcp.config import Config

TEST_MCP_TOKEN = "test-client-token-" + "a" * 24
BACKEND_API_KEY = "backend-secret-api-key"


@pytest.fixture
def config_factory() -> Callable[..., Config]:
    def make(**overrides: object) -> Config:
        values: dict[str, object] = {
            "abs_url": "http://abs.test",
            "api_key": BACKEND_API_KEY,
            "mcp_auth_token": TEST_MCP_TOKEN,
            "allowed_library_ids": frozenset({"lib1", "lib2"}),
            "host": "0.0.0.0",
            "port": 8000,
            "allowed_hosts": ("audiobookshelf-mcp:8000", "127.0.0.1:8000"),
            "allowed_origins": (),
        }
        values.update(overrides)
        return Config(**values)  # type: ignore[arg-type]

    return make
