# Audiobookshelf MCP server — project plan

Status: first read-only milestone implemented and independently verified locally
(2026-10-24). Not deployed or connected to real credentials. Live deployment and
changes to accounts, credentials, or media are not part of this milestone.

Implementation principles: KISS, DRY, YAGNI. Start with a small authenticated
read-only server and tests; add podcast writes only after the API/permission and
security gates pass. Do not build speculative approval infrastructure, plugins,
or a separate scheduler.

Current delivery includes only `list_libraries`, `search_library`, and
`get_item`, plus a Docker build, locked dependencies, tests, and CI/Dependabot.
Podcast additions, downloads, settings changes, and deployment remain unfinished.
There is no root Compose service or `start.sh` change in this milestone.

Initial limitation: backend responses are capped at 1 MiB before projection.
Expanded large podcasts/audiobooks or search results can therefore fail safely;
this milestone does not promise arbitrary archive sizes. Item response-size/
parse failures must not reveal metadata or distinguish inaccessible large items
from other unavailable items. Search consumes only book/podcast item matches,
not separate author, series, tag, or episode-only search categories. Retain these
bounds rather than adding speculative endpoints or parser dependencies.

Verification: 163 service tests passed on Python 3.14; the image runs Python
3.14.9, non-root, with a read-only root filesystem and no runtime package manager.
Direct HTTP interoperability with OpenWebUI's exact MCP client 1.27.2 was tested
against synthetic data. Locked runtime dependency audit passed; the final image
has zero known fixable HIGH/CRITICAL findings in the dated Trivy database. A full
scan still reports 8 unfixed upstream CVEs across 44 HIGH package findings; these
are documented, not a claim of zero vulnerabilities. CI and weekly Dependabot
updates are configured; GitHub-hosted execution awaits an authorized push.

## Goal

Build a small, security-focused Audiobookshelf MCP server that connects directly
to OpenWebUI over Streamable HTTP. Support library browsing and podcast
subscriptions, episode downloads, download schedules, and retention settings.
Use Audiobookshelf's native scheduler rather than introducing another scheduler.

The initial implementation does not change running services, account permissions,
secrets, subscriptions, or existing media.

## Scope and architecture

```text
OpenWebUI -- authenticated Streamable HTTP --> audiobookshelf-mcp
                                               |
                                               +-- restricted API --> Audiobookshelf

Trusted human approval --> exact pending destructive change (if enabled)
Audiobookshelf --> podcast feeds / enclosure downloads / native scheduler
```

- Python 3.14 is the baseline for this new service; validate dependency support
  and run tests on that version. Use a current patched container tag plus digest.
- Prefer the official Python MCP SDK's bundled server helpers if they meet our
  needs; avoid adding a separate framework dependency without a concrete benefit.
  Review the dependency tree and authentication support before pinning versions.
- Standalone Docker service in this repository; no MCPO dependency or changes
  to existing MCPO connectors.
- Proposed endpoint: `http://audiobookshelf-mcp:8000/mcp`, reachable only on the
  intended Docker network. No host port, public proxy route, or internet ingress.
- Dedicated Audiobookshelf account/API key; restrict libraries and permissions
  to the minimum supported by the installed release. Verify restrictions with
  actual API requests; do not assume permission granularity exists.
- Separate high-entropy credential for OpenWebUI-to-MCP authentication. Initial
  deployment is single-owner: shared backend credentials are not per-user auth.
- Configure the service directly through Docker Compose environment variables
  and mounted secret files; no generated JSON, template renderer, or `start.sh`
  changes. MCPO's rendering workaround is not needed for this application.
- Keep credentials under `/opt/secrets` on the host. Prefer narrow read-only
  mounts (or Compose file-backed secrets), with application-supported `_FILE`
  settings pointing to the container paths. Docker does not implement arbitrary
  `_FILE` settings automatically; the application must read those files.
- Non-secret settings belong in Compose environment values. An `env_file` can
  supply runtime environment variables if needed; do not confuse that with
  Compose `${...}` interpolation, which has separate input rules. Prefer file
  mounts for credentials to avoid storing their values in container environment
  metadata. File-backed Compose secrets are not automatically encrypted at rest.
- Stateless HTTP transport where supported. Do not treat an MCP session ID as
  authentication. Persistent approval state, if needed, is a separate concern.

Local API access does not imply local AI processing. External chat models may
receive tool results. No embedding, transcription, or full-library indexing is
needed for the initial toolset; do not automatically send library contents to
embedding providers.

## Proposed tools

Names are provisional; finalize against the installed Audiobookshelf API.

| Tool | Purpose | Initial guard |
| --- | --- | --- |
| `list_libraries` | List permitted libraries | Read-only |
| `search_library` | Search books/podcasts with bounded pagination | Read-only |
| `get_item` | Compact metadata for one permitted item | Read-only |
| `list_podcast_episodes` | Downloaded episodes and available feed episodes, distinguished clearly | Read-only; fetching feeds needs URL controls |
| `get_podcast_settings` | Current schedule, limits, timezone interpretation | Read-only |
| `add_podcast` | Subscribe using an RSS URL in an allowed podcast library | Explicit user request; URL validation; duplicate detection |
| `download_episodes` | Download explicit episode IDs or the newest N | Bounded count and queue; no implicit full-feed download |
| `set_podcast_schedule` | Enable/disable auto-download and configure frequency/new-episode limit | Validated settings; preview and read-back |
| `preview_podcast_retention` | Preview a proposed retention policy | No mutation; clearly state deletion uncertainty |
| `apply_podcast_retention` | Apply an approved retention change | Disabled until trusted human approval is verified |

Examples to support:

- “Add this podcast from its RSS URL.”
- “Download its newest 10 episodes now.”
- “Check every day at 06:00 and download up to 10 new episodes per check.”
- “Keep only 10 episodes.”
- “Disable automatic downloads.”

Downloading the newest N episodes now, limiting new downloads per scheduled
check, and retaining N downloaded episodes are different operations. Confirm
ordering, already-downloaded handling, in-progress downloads, and listened/
unlistened retention behavior before promising exact results. Disabling
auto-download must not silently delete files or claim to cancel queued jobs.

Out of scope initially: playback control, user/server administration, generic
API proxying, arbitrary URLs/HTTP methods, shell execution, filesystem access,
book uploads, explicit episode/item deletion, progress editing, and other service
integrations (Paperless, Nextcloud, Gmail, iCloud).

## Security requirements

### Access and transport

- Authenticate initialization, tool listing/calls, and every other MCP request;
  fail closed on missing/invalid credentials. Use the framework's reviewed auth
  implementation rather than inventing a protocol.
- Validate Host/Origin where appropriate; do not enable permissive CORS. Check
  compatibility with OpenWebUI server-to-server requests without an Origin.
- Limit request/body sizes, concurrent requests, download batches, and response
  sizes; enforce timeouts, pagination limits, and rate limits.
- Restrict each item/tool call to allowed libraries server-side, including IDs
  supplied directly rather than discovered through search.
- Internal HTTP is acceptable only after reviewing the Docker trust boundary;
  use TLS if traffic crosses an untrusted network. A shared Docker network alone
  does not restrict egress or authenticate callers.
- Expose the connection privately to the owner in OpenWebUI, not all users or
  default model tools. Do not trust unsigned user-info headers as identity.

### API, RSS, and untrusted content

- Fixed Audiobookshelf base URL; validated typed arguments; allowlisted API
  operations. No model-controlled host, path traversal, or credential forwarding.
- Avoid redirects on authenticated API calls; never return credentials, raw
  backend error pages, private RSS query tokens, or secret-bearing URLs in logs.
- RSS titles/descriptions/episode metadata are untrusted data, not instructions.
  Keep outputs compact and avoid full descriptions unless requested. Labelling
  data is useful but is not a complete prompt-injection defense.
- Feed URLs and enclosure URLs can cause SSRF in Audiobookshelf itself. Inspect
  its fetching behavior, redirects, DNS resolution/rebinding, and private/link-
  local/loopback/metadata-address handling. Validating the initial URL in MCP
  alone is insufficient for future scheduled fetches.
- Decide whether a feed-domain allowlist and Audiobookshelf-side network egress
  restrictions are needed. Do not claim SSRF protection until the actual fetch
  path is tested; keep feed additions disabled if controls are insufficient.

### Retention and approval

- Retention is potentially destructive even if no delete tool is exposed.
- Preview current/proposed policy, affected podcast, and known deletion impact.
  A snapshot may become stale; confirm state again before applying.
- Approval must originate from an authenticated person, not a model-set boolean
  or a token returned to the model. Bind it to the exact action/arguments,
  expiration, and state; make it single-use and replay-resistant.
- Test MCP elicitation end-to-end in the installed OpenWebUI. Do not assume
  transport support means approval UI support. If unsupported, either design a
  separate trusted approval UI or leave retention changes to Audiobookshelf's UI.
- Explicitly disclose that approving retention permits future automatic episode
  removals under that policy, not just one immediate deletion.

### Container and supply chain

- Non-root runtime, read-only root filesystem, dropped capabilities,
  `no-new-privileges`, bounded tmpfs/resources, no Docker socket or media mounts.
- No production-time package downloads; build from committed reviewed source.
- Exact dependency versions in a hash-locked lockfile; pinned toolchain and
  base-image tag plus immutable digest. Keep runtime dependencies minimal.
- Dependabot configuration for the chosen Python manifest/lockfile, Dockerfile,
  and GitHub Actions. Verify supported lockfile updates and digest-update PRs;
  do not assume pinning enables alerts or covers every Compose image reference.
- Enable GitHub dependency graph and vulnerability alerts where supported.
- CI: tests, lint/type checks, dependency vulnerability audit, secret scan, and
  Trivy image/OS-package scan. Pin CI actions by full commit SHA and minimize
  workflow permissions. Never expose secrets to untrusted PR execution.
- Generate an SBOM and build provenance for published images; restrict publishing
  to trusted reviewed revisions. Use short-lived registry auth where supported.
- Scheduled scans catch newly disclosed issues in an unchanged build. No blanket
  automatic merge: review dependency/source changes as well as scan results.
- Document urgent patching, expiring narrowly justified scan exceptions,
  dependency compromise response, token rotation, and rollback limitations.

## Implementation phases and acceptance gates

### Verified discovery (installed Audiobookshelf 2.37.2)

- API keys inherit their owning user's permissions; they are not separately
  scoped. Use a restricted user-owned key for the read-only foundation.
- Podcast creation, feed fetching, and episode download APIs require an admin
  account in this release. Do not silently widen credentials for later features.
- Search returns `book`/`podcast` match arrays wrapping `libraryItem`; enforce
  library allowlists and a combined output limit, not just the backend limit.
- `/checknew` and `/clear-queue` are mutating GETs, not safe browsing APIs.
- Retention currently removes one oldest dated episode after each automatic
  episode download, regardless of listened state; changing the policy does not
  immediately trim the archive, and manual downloads do not enforce it. Zero
  means unlimited. Do not promise a strict immediate “keep latest N” invariant.
- Live OpenWebUI is 0.12.3 with Python MCP client 1.27.2; compatibility and client
  security review are needed before rollout. No live credentials were accessed.

### 1. Discovery and API contract

- Inspect installed Audiobookshelf version, deployment/networking, API key
  support, user permissions, timezone, and current podcast configuration.
- Repository currently declares OpenWebUI `v0.12.3`; verify the running version,
  direct Streamable HTTP authentication, tool responses, and approval support.
- Inspect version-matched Audiobookshelf source/API for feed creation, episode
  listing/downloading, settings updates, retention, and scheduler behavior.
  Upstream `Podcast.js` already exposes `autoDownloadEpisodes`,
  `autoDownloadSchedule`, `maxEpisodesToKeep`, and `maxNewEpisodesToDownload`;
  this is not proof of installed endpoint compatibility or deletion semantics.
- Agree allowed libraries, feed policy, download caps, scheduling timezone and
  DST behavior, account permissions, and acceptable external-model data exposure.

Gate: documented version-matched endpoint/permission matrix and confirmed scope.
No live mutations during discovery.

### 2. Read-only foundation

- Build typed config and a bounded, redacting Audiobookshelf client.
- Implement authenticated Streamable HTTP and the read-only toolset.
- Add unit tests with synthetic fixtures and an isolated test deployment.

Gate: OpenWebUI can browse only allowed libraries; unauthorized MCP requests and
cross-library item IDs fail; errors and logs do not leak credentials.

### 3. Podcast additions and downloads

- Implement approved feed handling, duplicate-safe podcast creation, and bounded
  download requests. Use supported backend controls rather than direct file I/O.
- Handle partial success, timeouts, and repeated calls without blindly retrying
  mutations or queuing duplicate downloads.

Gate: add a disposable test feed and download selected/newest episodes without
unexpected requests, downloads, permission escalation, or data loss.

### 4. Schedule and retention controls

- Implement native schedule enable/disable and per-check download limits;
  validate cron/frequency, minimum interval, timezone, and DST interpretation.
- Read settings back after changes; show effective values to the user.
- Implement retention preview, then enable application only after the trusted
  approval gate passes. Otherwise ship without retention writes.

Gate: schedules run in Audiobookshelf independently of OpenWebUI/MCP; disabling
works as documented; retention behavior and approval protections pass tests.

### 5. Hardened deployment and CI

- Add Docker build, lockfile, scans, Dependabot, and security/operations docs.
- Configure directly in Compose; leave `start.sh` and MCPO rendering unchanged.
  Implement typed environment settings and explicit secret-file loading; validate
  at startup, fail closed on missing credentials, and verify restrictive host
  permissions and service UID access. Document restart/recreation requirements
  for environment updates and file-based secret rotation, including bind-mount
  inode behavior when host files are replaced.
- Add Compose service, narrow secret mounts, health check without secrets, and
  restart policy. Avoid unnecessary persistent state or writable volumes.
- Configure the private direct MCP connection in OpenWebUI only after approval;
  no shared unauthenticated endpoint or credentials in tracked files.

Gate: clean reproducible build, reviewed scans, container hardening verified,
secret access tests pass, and no new externally reachable port.

### 6. Staged rollout and operations

- Back up relevant Audiobookshelf configuration/database before mutation tests;
  use a disposable podcast/library first, then read-only production access,
  then explicitly approved write capabilities.
- Recheck effective permissions from OpenWebUI, not just direct API tests.
- Verify restart/credential rotation, upstream downtime, queue limits, audit
  events, and disable/rollback procedure. Audit without storing podcast content
  or secrets by default.
- Document a kill switch: disable writes/connection and revoke credentials.
  Rolling back the connector cannot restore deleted episodes or undo downloads.

Gate: user accepts the demonstrated behavior before broader deployment.

## Required tests

- Authentication failures, invalid Host/Origin, malformed/oversized requests,
  unexpected protocol versions, rate limits, and concurrent operations.
- Allowed-library enforcement for every tool, including forged IDs and stale
  resources; insufficient backend permissions fail cleanly.
- Malformed RSS, private-address URLs, redirects, credential-bearing URLs,
  oversized feeds/results, and hostile descriptions; test actual backend fetches.
- Duplicate subscriptions/downloads, already-downloaded episodes, sorting,
  partial failures, timeouts, and bounded queues.
- Schedule enable/disable, invalid/too-frequent schedules, timezone/DST, existing
  queue behavior, and no scheduler dependency on an active chat.
- Retention boundaries (including zero semantics), listened/unlistened behavior,
  concurrent state changes, and expired/replayed/model-generated approvals.
- Config validation/redaction, secret rotation and permissions, non-root/read-
  only runtime, no public port, and no runtime dependency installation.
- End-to-end OpenWebUI tool discovery, calls, errors, access visibility, and
  approval flow; use Playwright artifact paths required by repository policy.

## Proposed file layout

```text
audiobookshelf-mcp/
  pyproject.toml
  uv.lock                       # proposed; verify selected tooling support
  Dockerfile
  README.md
  SECURITY.md
  src/audiobookshelf_mcp/
    server.py                   # transport/auth and tool registration
    config.py                   # typed settings and secret-file loading
    client.py                   # bounded, allowlisted API adapter
    tools/                      # browsing, podcasts, scheduling
    approvals.py                # only if a trusted flow is implemented
  tests/
.github/dependabot.yml
.github/workflows/audiobookshelf-mcp.yml
compose.yml                     # new service only when deployment is approved
/opt/secrets/...                # host-only, never committed
```

## Decisions required before implementation

1. Which Audiobookshelf libraries/account may the connector access?
2. Which feed domains are allowed, and how will backend fetches be restricted?
3. What download caps and scheduling timezone should apply?
4. Is native OpenWebUI approval reliable, or should retention remain UI-only?
5. May external chat providers receive returned library metadata, or is a local
   model required for this connection?

Definition of done: approved tools work through direct authenticated MCP;
least-privilege access and destructive-action boundaries are demonstrated;
dependency/image maintenance is configured; credentials stay out of the repo,
model context, and logs; deployment and recovery procedures are documented.
