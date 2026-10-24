# audiobookshelf-mcp

Minimal Audiobookshelf MCP server. It speaks authenticated Streamable HTTP
directly (no MCPO, template renderer, or `start.sh` wrapper) and is stateless
in both modes.

- **Read-only (default):** exactly three tools, with no podcast mutation.
- **Podcast writes (opt-in):** setting `ABS_ENABLE_PODCAST_WRITES=true` adds
  eight podcast tools (eleven total). The server remains stateless; there is no
  session store, approval flow, or confirmation token.

Writes stay off unless explicitly enabled. A default deployment registers no
podcast subscription, download, schedule, retention, or delete tool.

## Tools

### Read-only tools (always registered)

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `list_libraries` | — | Allowed libraries only, with ID, name, and media type. |
| `search_library` | `library_id`, `query`, `limit` (1–25, default 10) | Books and podcasts in one allowed library. `query` is 1–200 printable characters. Only `book` and `podcast` item matches whose `libraryId` equals the requested library are returned; author, series, tag, and episode-only categories are not. |
| `get_item` | `item_id` | Compact metadata for one item whose backend `libraryId` is allowed. An expanded item that cannot be read safely (oversized or malformed) is reported as not found or not permitted. |

### Podcast tools (registered only when `ABS_ENABLE_PODCAST_WRITES=true`)

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `get_podcast_settings` | `podcast_id` | Current auto-download flag, schedule, per-check limit, retention limit, and downloaded count for an allowed podcast. |
| `list_podcast_episodes` | `podcast_id`, `limit` (optional, 1–50, default 25) | Downloaded episodes newest first (undated last), projected to `id`, `title`, `published_at`, and `duration_seconds`. |
| `add_podcast` | `library_id`, `feed_url` | Subscribe to a validated RSS feed in an allowed podcast library. An exact duplicate returns `already_exists`. New items use an internally generated UUID folder and start with auto-download off. |
| `download_latest_episodes` | `podcast_id`, `count` (optional, 1–25, default 10) | Queue recent episodes that are not already downloaded or queued. Returns `queued` or `nothing_new`, never `completed`; downloads are asynchronous. |
| `set_podcast_schedule` | `podcast_id`, `enabled`, `schedule` (optional), `max_new_episodes` (optional) | Enable with a validated five-field cron and 1–25 new episodes. Omitted values reuse current settings, except that an empty cron becomes hourly and an unlimited `0` limit becomes 3. Disabling requires both omitted, preserves old cron/limit, and does not clear the queue or delete files. |
| `set_podcast_retention` | `podcast_id`, `keep`, `trim_existing` (optional, default `false`) | Set retention to 0–1000 episodes (0 = unlimited). Only an explicit `trim_existing=true` deletes surplus episodes now, capped at 100. |
| `delete_podcast_episode` | `podcast_id`, `episode_id` | Hard-delete one downloaded episode; removes its database record. Physical file wipe is not verified. |
| `delete_podcast` | `podcast_id` | Hard-delete the item; the backend recursively targets its stored directory and removes its database record. Physical file wipe is not verified. |

All eight are restricted to allowed podcast libraries: item tools require an
item whose backend `libraryId` is allowed and whose `mediaType` is `podcast`,
while `add_podcast` requires the target library itself to be a podcast library.
Books, libraries, and items outside the allowlist are rejected as not found or
not permitted. Tool results contain only a compact projection; backend paths,
folder IDs, `fullPath` values, image/feed/enclosure URLs, descriptions, and raw
API payloads are never returned.

## Destructive operations execute on call

There is no confirmation prompt, approval step, confirmation flag, or token.
The tool schemas contain no `confirm` or `approved` field. A model-invoked call
is sufficient to perform these actions:

- `delete_podcast` and `delete_podcast_episode` are hard deletes. The podcast
  variant recursively targets the entire stored item directory and removes the
  item database record, including listening progress and playlist references;
  the episode variant removes one downloaded episode's record.
- `set_podcast_retention` with `trim_existing=true` changes policy and deletes
  up to 100 surplus episodes immediately.

Audiobookshelf may log a physical-file error and still return HTTP 200. A
`deleted` result therefore means the backend accepted the mutation and removed
the database record; the server does not and cannot verify that every physical
file was erased. Delete results report `filesystem_removal: not_verified`
rather than asserting success. Deleted audio, progress, and playlist
memberships may be unrecoverable. The server does not create backups, retry, or
roll back. A trim stops at the first error and reports an honest partial count;
a timeout after a write may leave the outcome ambiguous. The server exposes no
queue-cancel or clear-queue operation: deleting or trimming is refused while the
podcast has queued or active downloads.

Native `DELETE /api/items/{id}?hard=1` is the recursive item-directory delete
path: it targets the entire stored item directory. Audiobookshelf allows an item
path equal to the library root, including for externally created items, so an
unguarded hard delete could remove the whole library directory. The server
rejects deletion unless the item directory is a direct child of the configured
library folder; the root, an outside path, a nested path, or an unverifiable
path fails closed. Episode deletion is likewise constrained to the item folder.
This is path validation, not a filesystem sandbox: symlinks, mounts, and files
placed in a shared podcast folder can still be affected, and deleting a podcast
removes other contents of its folder.

Retention and scheduling also commit future automatic behavior. Enabling
automatic downloads under a positive `keep` value permits recurring automatic
episode deletion by Audiobookshelf. A retention change alone does not shrink an
existing over-limit library.

The podcast scope prevents these tools from deleting books or libraries, but it
is not a credential boundary: write mode uses an instance-wide Audiobookshelf
admin key (see [Credentials](#credentials-and-permissions)). Treat any content
that can steer the model as able to trigger these tools while writes are
enabled. Policy and delete writes re-read their target under a process-local
mutation lock and abort if the state changed, but that lock does not coordinate
with an Audiobookshelf administrator, its scheduler, or another server process.

## Scheduling and retention semantics

- Schedules are five-field cron expressions containing only integers, `*`,
  `*/N`, and comma-separated integers. Ranges, month/weekday names, `?`, `L`,
  `W`, `#`, and six-field forms are rejected. Minute values must be at least 15
  minutes apart (including the hour wrap); this is a conservative heuristic,
  and Audiobookshelf's timezone applies. Timezone and DST behavior were not
  live-verified.
- `max_new_episodes` is 1–25 when supplied. On enable, omitted `schedule` or
  `max_new_episodes` reuse current values, with two bounded exceptions: an empty
  schedule becomes hourly `0 * * * *`, and an unlimited current `0` limit
  becomes 3. On disable, both arguments must be omitted; the stored cron and
  per-check limit (including `0`) are preserved, and the queue and files are
  untouched.
- Enabling re-runs the remote-URL preflight against the podcast's existing feed
  URL; a pre-existing feed that fails the guard cannot be scheduled.
- `keep` is 0–1000, where `0` means unlimited. `trim_existing=true` cannot be
  combined with `0`. An immediate trim deletes the oldest surplus episodes and
  is refused before the policy write if it would exceed 100 deletions.
- Audiobookshelf's automatic retention ignores listened state and removes at
  most one episode per automatic download. It therefore does not shrink an
  existing over-limit backlog by itself. Missing publication dates are ignored
  by the native limit.

## Bounded responses and remote URLs

The read-only GET cap remains 1 MiB. Podcast and feed operations use a separate
4 MiB response cap; an oversized response fails safely rather than being
truncated. The `add_podcast` duplicate check is bounded to 10 pages of 100
minified items. It compares normalized feed URLs exactly (scheme/host case,
default port, and fragment only); paths and queries must match, and aliases or
redirects are not merged. `download_latest_episodes` deduplicates against
existing downloaded, queued, and active entries by native GUID and enclosure
URL.

Feed and enclosure URLs are accepted only for `http` on port 80 or `https` on
port 443, without user information, fragments, backslashes, or control/
non-ASCII characters. Literal IP addresses must be public, and every DNS answer
for a hostname must be public, checked with a 5-second timeout. New podcast
items are created under an internally generated UUID folder; callers cannot
supply a path, and no cover image is fetched. Library folder IDs and `fullPath`
values used for creation and deletion checks are internal and are never
returned.

This URL preflight is defense in depth, not a complete SSRF control: it makes
no HTTP request, follows no redirect, and does not prevent DNS rebinding or
DNS changes between the check and the real fetch. Audiobookshelf's native SSRF
filter and network egress controls are required for the actual feed fetch,
enclosure download, redirect target, and scheduled future fetch. Read-only
inspection of the running Audiobookshelf 2.37.2 configuration found
`DISABLE_SSRF_REQUEST_FILTER`, `EXP_PROXY_SUPPORT`, and
`SSRF_REQUEST_FILTER_WHITELIST` unset, with the native SSRF filter active. That
verifies configuration only, not a public redirect, a proxy bypass, or full
SSRF safety. Native axios may honor `HTTP(S)_PROXY`, so before enabling writes
the operator must verify a trusted proxy and egress path and restrict outbound
access. No generic proxy, arbitrary-HTTP, user-administration, or filesystem
tools are exposed.

## Requirements

- Python 3.14 (the repository requires `>=3.14`; local tests use uv-managed
  3.14.6, and the runtime image uses Python 3.14.9).
- [uv](https://docs.astral.sh/uv/) for locking, syncing, and running.
- Audiobookshelf. The request shapes were verified against Audiobookshelf
  `2.37.2` source. The read-only endpoints are:
  - `GET /api/libraries`
  - `GET /api/libraries/{id}/search?q=...&limit=...`
  - `GET /api/items/{id}?expanded=1`

  With writes enabled, the server additionally uses `GET /api/libraries/{id}`,
  `GET /api/libraries/{id}/items`,
  `GET /api/items/{id}?expanded=1&include=downloads`, `POST /api/podcasts/feed`,
  `POST /api/podcasts`, `POST /api/podcasts/{id}/download-episodes`,
  `PATCH /api/items/{id}/media`,
  `DELETE /api/podcasts/{id}/episode/{episodeId}?hard=1`, and
  `DELETE /api/items/{id}?hard=1`.

  Other Audiobookshelf GET routes include mutating operations (for example
  check-for-new-episodes and clear-queue), so they are deliberately absent.

## Configuration

All credentials are file-backed; there are intentionally no direct
`ABS_API_KEY` or `MCP_AUTH_TOKEN` environment variables.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ABS_URL` | yes | — | Fixed backend base URL (`http`/`https`, no userinfo, query, or fragment). Use the Audiobookshelf origin, normally without `/api`. |
| `ABS_ALLOWED_LIBRARY_IDS` | yes | — | Non-empty comma-separated library ID allowlist. These IDs are also enforced against backend search results, item `libraryId` values, and every podcast operation. |
| `ABS_API_KEY_FILE` | yes | — | File containing the Audiobookshelf API key. Read-only mode uses a restricted key; write mode requires an admin-owned key (see below). |
| `ABS_ENABLE_PODCAST_WRITES` | no | `false` | `true` registers the eight podcast tools. Only case-insensitive `true`/`false` is accepted; any other value fails startup. |
| `MCP_AUTH_TOKEN_FILE` | yes | — | File containing the MCP bearer token; at least 32 characters. |
| `MCP_HOST` | no | `0.0.0.0` | Bind address. |
| `MCP_PORT` | no | `8000` | Bind port. |
| `MCP_ALLOWED_HOSTS` | no | `audiobookshelf-mcp:8000,localhost:8000,127.0.0.1:8000` | Exact `Host` allowlist. Update this if the service name or port changes. |
| `MCP_ALLOWED_ORIGINS` | no | empty | Exact `Origin` allowlist. Empty rejects every request that carries an `Origin`; requests without `Origin` (OpenWebUI server-to-server) are accepted. |

Startup fails closed on missing, empty, oversized, malformed, or unreadable
secret files, on short MCP tokens, and on invalid write-flag values. Secret
contents are never logged or included in error messages.

### Credentials and permissions

The Audiobookshelf API key inherits the permissions of its owning user; it is
not independently scoped. For read-only mode, create a dedicated non-admin
user that can access only the intended libraries, then have an administrator
create an API key on behalf of that restricted user. API-key management is
admin-only in Audiobookshelf 2.37.2, so the restricted account cannot mint its
own key.

Podcast add, download, schedule, retention, and delete APIs are also admin-only
in Audiobookshelf 2.37.2. Write mode therefore requires an admin-owned key, and
the user accepts that risk. Keep that key exclusively in this connector in a
narrow, read-only `/opt/secrets` mount, and give OpenWebUI a distinct MCP bearer
token. The admin credential is still instance-wide: if stolen, it can affect all
books, users, and settings. Library allowlists and tool scoping constrain this
server's behavior, but they do not scope the underlying key. Do not reuse the
MCP bearer token as the Audiobookshelf key. No root Compose service or
`start.sh` change is part of this project.

## Run locally

```bash
cd audiobookshelf-mcp
uv sync --frozen
ABS_URL=http://audiobookshelf:80 \
ABS_ALLOWED_LIBRARY_IDS=library-id-1,library-id-2 \
ABS_API_KEY_FILE=/run/secrets/abs_api_key \
ABS_ENABLE_PODCAST_WRITES=false \
MCP_AUTH_TOKEN_FILE=/run/secrets/mcp_token \
uv run --frozen audiobookshelf-mcp
```

For writes, set `ABS_ENABLE_PODCAST_WRITES=true` and point `ABS_API_KEY_FILE`
at the admin-owned key described above. The MCP endpoint is
`http://audiobookshelf-mcp:8000/mcp`; authenticate with
`Authorization: Bearer <MCP_AUTH_TOKEN_FILE contents>`. The server is stateless
in both modes; no session ID is issued or treated as authentication.
Authentication is enforced for every method on `/mcp`, using a constant-time
comparison of the configured shared token. DNS-rebinding protection validates
`Host` and `Origin`; no CORS middleware or wildcard origin is enabled.

## Docker

The image contains only the locked third-party dependencies in `/app/.venv`
plus the application source in `/app/src`. Dependencies are installed
wheel-only (`--no-build`) from the committed `uv.lock`; there is no dynamic
dependency resolution, and the project is never built with Hatchling, installed,
or packaged as a wheel. It runs as UID `10001` with no runtime package downloads
or installs. `PYTHONPATH=/app/src` makes the source importable, and the image
entrypoint is `python -m audiobookshelf_mcp.server`. The local console script
from `pyproject.toml` remains available for `uv run` development but is not
used in the image. It is digest-pinned to `python:3.14.9-slim`; the unused
base-image `pip`, its shims, and the bundled `ensurepip` wheel (plus the
vulnerable packages pip vendors) are removed at build time. Build:

```bash
docker build -t audiobookshelf-mcp .
```

The default example keeps podcast writes off. Run it only on a private network,
with no published host port and with narrow, read-only secret mounts (no real
credentials in the repo):

```bash
docker run --name audiobookshelf-mcp \
  --read-only --cap-drop=ALL --security-opt no-new-privileges \
  --network audiobookshelf-internal \
  -v /opt/secrets/abs_api_key:/run/secrets/abs_api_key:ro \
  -v /opt/secrets/mcp_token:/run/secrets/mcp_token:ro \
  -e ABS_URL=http://audiobookshelf:80 \
  -e ABS_ALLOWED_LIBRARY_IDS=library-id-1 \
  -e ABS_API_KEY_FILE=/run/secrets/abs_api_key \
  -e ABS_ENABLE_PODCAST_WRITES=false \
  -e MCP_AUTH_TOKEN_FILE=/run/secrets/mcp_token \
  audiobookshelf-mcp
```

Podcast writes are a separate, explicit opt-in and must never be enabled by a
default example or a shared deployment change. The opt-in variant replaces the
read key with the admin-owned key described above and adds the write flag:

```bash
  -v /opt/secrets/abs_admin_api_key:/run/secrets/abs_admin_api_key:ro \
  -e ABS_ENABLE_PODCAST_WRITES=true \
  -e ABS_API_KEY_FILE=/run/secrets/abs_admin_api_key
```

Use that variant only after completing the credential, egress, and
Audiobookshelf SSRF checks and establishing backups. It is documentation of the
opt-in, not an approval to deploy.

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

Tests use synthetic in-process Audiobookshelf backends and do not require a
live Audiobookshelf or OpenWebUI instance. Coverage includes configuration and
secret redaction, authenticated Streamable HTTP (including failed
authentication on every `/mcp` verb), strict tool-argument validation,
Host/Origin and request-body limits, the MCP tool lifecycle, library/search/
item scoping, bounded responses, redirects, timeouts, backend error-body
redaction, podcast reads, feed duplicate and download deduplication, cron and
remote-URL guards, retention/trim calculation, queue-active refusal, and
partial-failure reporting. Only synthetic/protocol tests exist for the podcast
writes: no deployed-browser end-to-end test or live Audiobookshelf mutation has
been run, and no test count or final acceptance is claimed. The suite does not
prove recursive filesystem deletion behavior or physical file removal.

## Deployment notes

- OpenWebUI `0.12.3` bundles MCP client `1.27.2`. The three-tool read-only
  baseline was previously smoke-tested with that client over live HTTP
  (protocol `2025-11-25`, no session ID), so it remains compatible. The eight
  podcast write tools have not been tested end-to-end through the installed
  OpenWebUI frontend. That client has known client-side security advisories: do
  not downgrade this server to match it; upgrade the client where possible. The
  server uses patched `mcp` `1.30.0` with an explicit 1 MiB request-body limit.
- Keep the MCP connection private to the intended owner. The shared backend API
  key is not per-user authentication, and tool results may be sent to an
  external model provider by the chat client.
- Once enabled, schedules and automatic retention run inside Audiobookshelf,
  independently of OpenWebUI, the model, or an active chat. Disabling a chat or
  the MCP server does not stop them.
- The SSRF flag check was a read-only inspection of the running Audiobookshelf
  2.37.2 configuration. No live feed redirect, proxy bypass, or timezone/DST
  behavior has been tested.
- Writes remain off by default. Enabling them requires the admin-key risk
  acceptance described above; a compromise of OpenWebUI or the MCP bearer token
  can invoke destructive tools without a human confirmation step.
- This round includes no deployment, live Audiobookshelf mutation or credential
  use, push, new commit, or hosted CI run. No final scan or acceptance is
  claimed.
- The runtime base is digest-pinned `python:3.14.9-slim`. On Debian 13.7 (scan
  dated 2026-10-24) it has no fixable HIGH/CRITICAL findings under the CI Trivy
  gate (`--ignore-unfixed`); 44 upstream unfixed HIGH findings across 8 CVEs
  remain in base packages with no fixed version available. The final scan is
  still pending. Re-scan weekly and upgrade the digest when Debian publishes
  fixes; do not suppress the gate.
