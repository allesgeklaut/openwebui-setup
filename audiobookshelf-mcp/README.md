# audiobookshelf-mcp

Minimal, read-only Audiobookshelf MCP server. It speaks authenticated
Streamable HTTP directly (no MCPO, template renderer, or `start.sh` wrapper) and
exposes exactly three tools:

- `list_libraries` — allowed libraries only, with ID, name, and media type.
- `search_library(library_id, query, limit)` — books and podcasts in one allowed
  library; `query` is 1–200 printable characters, `limit` is 1–25 (default 10).
  Only `book` and `podcast` item matches whose `libraryId` equals the requested
  library are returned; author, series, tag, and episode-only match categories
  are not.
- `get_item(item_id)` — compact metadata for one item whose backend
  `libraryId` is allowed. An expanded item that cannot be read safely
  (oversized or malformed) is reported as not found or not permitted.

Responses contain only a compact projection (`id`, `library_id`, `media_type`,
`title`, `authors`, `narrators`, `series`, `published_year`,
`duration_seconds`). Backend paths, image/feed/enclosure URLs, descriptions, and
raw API payloads are never returned.

### Bounded responses (as of 2026-10-24)

Backend responses are capped at 1 MiB. Expanded large items and large search
responses can therefore be rejected. Searches consume only the `book` and
`podcast` item match arrays; author, series, tag, and episode-only result
categories are not consumed or returned. `get_item` can check the item's library
only after reading a payload that fits the cap, so a missing, inaccessible,
oversized, or malformed item all produce the same "not found or not permitted"
tool error.

This milestone is read-only by design. It does not implement podcast
subscriptions, downloads, schedules, retention, approvals, feed fetching, or
any other mutation. Do not add generic proxy or arbitrary-HTTP tools.

## Requirements

- Python 3.14 (the repository requires `>=3.14`; local tests use uv-managed
  3.14.6, and the runtime image uses Python 3.14.9).
- [uv](https://docs.astral.sh/uv/) for locking, syncing, and running.
- Audiobookshelf. The request shapes were verified against Audiobookshelf
  `2.37.2` source. Only these read endpoints are used:
  - `GET /api/libraries`
  - `GET /api/libraries/{id}/search?q=...&limit=...`
  - `GET /api/items/{id}?expanded=1`

  Other Audiobookshelf GET routes include mutating operations (for example
  check-for-new-episodes and clear-queue), so they are deliberately absent.

## Configuration

All credentials are file-backed; there are intentionally no direct
`ABS_API_KEY` or `MCP_AUTH_TOKEN` environment variables.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ABS_URL` | yes | — | Fixed backend base URL (`http`/`https`, no userinfo, query, or fragment). Use the Audiobookshelf origin, normally without `/api`. |
| `ABS_ALLOWED_LIBRARY_IDS` | yes | — | Non-empty comma-separated library ID allowlist. These IDs are also enforced against backend search results and item `libraryId` values. |
| `ABS_API_KEY_FILE` | yes | — | File containing the Audiobookshelf API key. |
| `MCP_AUTH_TOKEN_FILE` | yes | — | File containing the MCP bearer token; at least 32 characters. |
| `MCP_HOST` | no | `0.0.0.0` | Bind address. |
| `MCP_PORT` | no | `8000` | Bind port. |
| `MCP_ALLOWED_HOSTS` | no | `audiobookshelf-mcp:8000,localhost:8000,127.0.0.1:8000` | Exact `Host` allowlist. Update this if the service name or port changes. |
| `MCP_ALLOWED_ORIGINS` | no | empty | Exact `Origin` allowlist. Empty rejects every request that carries an `Origin`; requests without `Origin` (OpenWebUI server-to-server) are accepted. |

Startup fails closed on missing, empty, oversized, malformed, or unreadable
secret files and on short MCP tokens. Secret contents are never logged or
included in error messages.

The Audiobookshelf API key inherits the permissions of its owning user; it is
not independently scoped. For this foundation, create a dedicated non-admin
user that can access only the intended libraries, then have an administrator
create an API key on behalf of that restricted user. API-key management is
admin-only in Audiobookshelf 2.37.2, so the restricted account cannot mint its
own key. Add-podcast and download APIs are also admin-only, so future write
features require a separate security decision rather than broadening this key.

## Run locally

```bash
cd audiobookshelf-mcp
uv sync --frozen
ABS_URL=http://audiobookshelf:80 \
ABS_ALLOWED_LIBRARY_IDS=library-id-1,library-id-2 \
ABS_API_KEY_FILE=/run/secrets/abs_api_key \
MCP_AUTH_TOKEN_FILE=/run/secrets/mcp_token \
uv run --frozen audiobookshelf-mcp
```

The MCP endpoint is `http://audiobookshelf-mcp:8000/mcp`; authenticate with
`Authorization: Bearer <MCP_AUTH_TOKEN_FILE contents>`. The server is stateless
and does not treat any session ID as authentication. Authentication is enforced
for every method on `/mcp`, using a constant-time comparison of the configured
shared token. DNS-rebinding protection validates `Host` and `Origin`; no CORS
middleware or wildcard origin is enabled.

## Docker

The image contains only the locked third-party dependencies in `/app/.venv`
plus the application source in `/app/src`. Dependencies are installed
wheel-only (`--no-build`) from the committed `uv.lock`; there is no dynamic
dependency resolution, and the project itself is never built, installed, or
packaged as a wheel. It runs as UID `10001` with no runtime package downloads
or installs. `PYTHONPATH=/app/src` makes the source importable, and the image
entrypoint is `python -m audiobookshelf_mcp.server`. The local console script
from `pyproject.toml` remains available for `uv run` development but is not
used in the image. It is digest-pinned to `python:3.14.9-slim`; the unused
base-image `pip`, its shims, and the bundled `ensurepip` wheel (plus the
vulnerable packages pip vendors) are removed at build time. Build:

```bash
docker build -t audiobookshelf-mcp .
```

Run it only on a private network, with no published host port and with narrow,
read-only secret mounts (no real credentials in the repo):

```bash
docker run --name audiobookshelf-mcp \
  --read-only --cap-drop=ALL --security-opt no-new-privileges \
  --network audiobookshelf-internal \
  -v /opt/secrets/abs_api_key:/run/secrets/abs_api_key:ro \
  -v /opt/secrets/mcp_token:/run/secrets/mcp_token:ro \
  -e ABS_URL=http://audiobookshelf:80 \
  -e ABS_ALLOWED_LIBRARY_IDS=library-id-1 \
  -e ABS_API_KEY_FILE=/run/secrets/abs_api_key \
  -e MCP_AUTH_TOKEN_FILE=/run/secrets/mcp_token \
  audiobookshelf-mcp
```

The mounted files must be readable by UID/GID `10001` and should be readable
only by that identity (for example `chown 10001:10001` and mode `0400`). Do not
publish port 8000 or attach the service to an untrusted network; a Docker
network does not authenticate callers or restrict egress by itself.

## Tests

```bash
cd audiobookshelf-mcp
uv sync --frozen
uv run --frozen pytest
```

Local development installs the project (and may use build tooling); the
production image deliberately does neither.

Tests cover configuration and secret redaction, authenticated Streamable HTTP
(including failed authentication on every `/mcp` verb), strict tool-argument
validation, Host/Origin and request-body limits, the in-process MCP tool
lifecycle, cross-library search and item rejection, bounded item lookup, the
1 MiB response cap, redirects, timeouts, oversized responses, and backend
error-body redaction. They use synthetic in-process backends; no live
Audiobookshelf or OpenWebUI instance is needed.

## Deployment notes

- OpenWebUI `0.12.3` bundles MCP client `1.27.2`. This server was smoke-tested
  with that client over live HTTP (protocol `2025-11-25`, no session ID, all
  three tools callable), so it remains compatible. That client has known
  client-side security advisories: do not downgrade this server to match it;
  upgrade the client where possible. The server uses patched `mcp` `1.30.0`
  with an explicit 1 MiB request-body limit.
- Keep the MCP connection private to the intended owner. The shared backend API
  key is not per-user authentication, and tool results may be sent to an
  external model provider by the chat client.
- Schedule/retention semantics are not implemented and must not be inferred
  from API fields in this milestone.
- The runtime base is digest-pinned `python:3.14.9-slim`. On Debian 13.7 (scan
  as of 2026-10-24) it has no fixable HIGH/CRITICAL findings under the CI Trivy
  gate (`--ignore-unfixed`); 44 upstream unfixed HIGH advisories remain in base
  packages with no fixed version available. Re-scan weekly and upgrade the
  digest when Debian publishes fixes; do not suppress the gate.
