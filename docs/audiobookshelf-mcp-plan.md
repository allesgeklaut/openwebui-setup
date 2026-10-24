# Audiobookshelf MCP server — project plan

Status: Round 1 (read-only) was implemented and independently verified locally
on 2026-10-24; Round 2 (podcast management) was implemented and independently
verified locally the same day. Round 2 is committed at `c58b107` with podcast
writes explicitly enabled, and the connector is registered privately through
OpenWebUI's application database; the user has confirmed the read-only OpenWebUI
path works. Round 3 (the approved episode-selection extension) is implemented,
independently reviewed, and verified on a final candidate with no remaining
implementation blockers: its full suite passed 579 tests in 2.30 s on Python
3.14.6, and the final image is running locally with fifteen tools. `HEAD` remains
`c58b107`; the six modified files (extension and these docs) are uncommitted and
unpushed. No live write mutation is claimed for Round 3, and this
documentation work does not change credentials, services, or media.

Round 2: podcast management implementation/testing is authorized on the existing
feature branch. The user accepts the admin-token tradeoff, with that credential
kept exclusively in the connector. Subscriptions, downloads, scheduling,
retention, episode deletion, and podcast deletion must remain restricted to
allowed podcast libraries. The user subsequently chose stateless HTTP without
extra confirmation: valid retention/deletion tool calls execute directly and may
remove files/history or authorize future automatic removals. Writes remain
default-off. No deployment, live media mutation, push, or new commit is authorized
by this round. Native elicitation research found stateful transport would be
needed for confirmation, but approval/session infrastructure is now out of scope.

Status update: Round 2 was subsequently committed as `c58b107` and enabled in
the private connector, and the read-only path was confirmed through OpenWebUI.
The "no deployment ... new commit" wording above describes the authorization at
that time, not the current state. The earlier direct-delete probe was test-backed;
no live media mutation is claimed here. Round 3 is verified against synthetic
backends and harmless live reads only; real write behavior has not been
exercised and remains default-off outside this private deployment.

Implementation principles: KISS, DRY, YAGNI. Start with a small authenticated
read-only server and tests; add podcast writes only after the API/permission and
security gates pass. Do not build speculative approval infrastructure, plugins,
or a separate scheduler.

Delivery at the committed `c58b107` baseline: `list_libraries`, `search_library`,
and `get_item`, plus eight podcast tools (eleven total), with the Docker build,
locked dependencies, tests, and CI/Dependabot. Round 3 adds
`list_available_podcast_episodes`, `download_podcast_episodes`,
`download_episodes_from`, and `delete_podcast_episodes`, and extends
`list_podcast_episodes`, for fifteen tools (three read-only, twelve podcast).
Round 3 is implemented, independently reviewed, and verified on the final
candidate. The connector's Compose service already exists from Round 2; Round 3
needs no new root Compose or `start.sh` change and keeps the same writes flag.

Initial limitation: backend responses are capped at 1 MiB before projection.
Expanded large podcasts/audiobooks or search results can therefore fail safely;
this milestone does not promise arbitrary archive sizes. Podcast/feed operations
use a separate 4 MiB cap, still applied to the whole native feed before
projection. Item response-size/parse failures must not reveal metadata or
distinguish inaccessible large items from other unavailable items. Search
consumes only book/podcast item matches, not separate author, series, tag, or
episode-only search categories. Retain these bounds rather than adding
speculative endpoints or parser dependencies.

Verification: 163 service tests passed on Python 3.14 for the read-only
foundation; Round 2 raised the suite to 510 tests before Round 3, all passing,
including independent probes. Both modes speak stateless Streamable HTTP: the
exact OpenWebUI MCP client 1.27.2 negotiated protocol 2025-11-25 with no session
ID against the writes-enabled server, listed the eleven baseline tools, and
executed a direct destructive delete without elicitation in the test setup. No
live media mutation is claimed. The image runs Python 3.14.9, non-root, offline
with a read-only root filesystem, no runtime package manager, and fails closed
on an invalid write flag. Locked runtime dependency audit passed; the final
image has zero known fixable HIGH/CRITICAL findings in the dated Trivy database.
A full scan still reports 8 unfixed upstream CVEs across 44 HIGH package
findings; these are documented, not a claim of zero vulnerabilities. CI and
weekly Dependabot updates are configured; GitHub-hosted execution awaits an
authorized push. Round 2 was subsequently committed as `c58b107` (superseding
the earlier "uncommitted" note). Round 3's final candidate passed 579 tests in
2.30 s on Python 3.14.6 (123 podcast-client and 64 protocol tests; earlier tests
unchanged). No dependencies or lockfiles changed, and locked `pip-audit`
reported no known vulnerabilities. The installed OpenWebUI MCP client 1.27.2
discovered all 15 tools over real HTTP against a temporary synthetic backend and
passed paging, selected/window, partial-delete, and redaction checks; all
mutating checks were synthetic, and no real download, delete, policy, or
subscription call was made. The persisted production connection also discovered
exactly 15 tools with owner-only access grants intact, and harmless offset-0 live
reads passed (paging beyond 50 is synthetic-only). Runtime verification
confirmed UID `1000:1000`, read-only root and secret mounts, `0600` user-owned
credentials, `cap_drop: ALL` with `no-new-privileges`,
no privileged mode or published ports, the expected networks and allowed podcast
library, and writes enabled. The frontend/model-chat selection workflow, real
download completion, and filesystem effects remain unverified.

## Goal

Build a small, security-focused Audiobookshelf MCP server that connects directly
to OpenWebUI over Streamable HTTP. Support library browsing and podcast
subscriptions, episode downloads, download schedules, retention settings, and
bounded current-feed episode selection. Use Audiobookshelf's native scheduler
rather than introducing another scheduler.

Round 3 adds feed-scoped episode listing, selected and fixed-window downloads,
and bounded bulk deletion. It changes no media unless a user invokes a write
tool; this documentation work changes none.

The initial implementation does not change running services, account permissions,
secrets, subscriptions, or existing media.

## Scope and architecture

```text
OpenWebUI -- authenticated Streamable HTTP --> audiobookshelf-mcp
                                               |
                                               +-- restricted API --> Audiobookshelf

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
- Stateless HTTP transport in both modes. No extra approval UI, approval tokens,
  or persistent session state. Podcast writes execute directly when called.

Local API access does not imply local AI processing. External chat models may
receive tool results. No embedding, transcription, or full-library indexing is
needed for the initial toolset; do not automatically send library contents to
embedding providers.

## Proposed tools

The baseline names are implemented; Round 3 keeps those names and adds the four
approved names below. Treat them as stable tool identities.

| Tool | Purpose | Initial guard |
| --- | --- | --- |
| `list_libraries` | List permitted libraries | Read-only |
| `search_library` | Search books/podcasts with bounded pagination | Read-only |
| `get_item` | Compact metadata for one permitted item | Read-only |
| `list_podcast_episodes` | List downloaded episodes with title filtering and offset paging | Podcast group; bounded list 1–50, offset 0–100000; undated last |
| `list_available_podcast_episodes` | List valid, dated current-feed episodes with an audio enclosure URL and opaque feed IDs | Podcast group (admin-only feed fetch); 4 MiB fail-closed bound; not an archive; no remote pagination |
| `get_podcast_settings` | Current schedule, limits, timezone interpretation | Read-only |
| `add_podcast` | Subscribe using an RSS URL in an allowed podcast library | Explicit user request; URL validation; duplicate detection |
| `download_latest_episodes` | Queue the newest N not already downloaded/queued/active | Bounded count and queue; no implicit full-feed download; unchanged |
| `download_podcast_episodes` | Queue individually selected current-feed episodes | 1–25 unique feed IDs; URL-preflight remaining targets before one request; skip downloaded/queued; no retry |
| `download_episodes_from` | Queue a fixed chronological window anchored at one feed episode | 1–25 inclusive window; anchor plus newer episodes only; no backfill; stale anchor fails |
| `set_podcast_schedule` | Enable/disable auto-download and configure frequency/new-episode limit | Validated settings; preview and read-back |
| `set_podcast_retention` | Set native retention and optionally trim existing episodes | Direct execution when writes enabled; bounded deletion batch |
| `delete_podcast_episode` | Remove a downloaded episode and its associated data | Podcast/library validation; no extra confirmation; unchanged |
| `delete_podcast_episodes` | Remove selected downloaded episodes | 1–25 unique native IDs; prevalidate membership/path/queue; stop on first failure; honest partial result; no rollback/retry |
| `delete_podcast` | Remove a podcast subscription, media, and associated data | Podcast/library validation; no extra confirmation |

Examples to support:

- “Add this podcast from its RSS URL.”
- “Download its newest 10 episodes now.”
- “Check every day at 06:00 and download up to 10 new episodes per check.”
- “Keep only 10 episodes.”
- “Disable automatic downloads.”
- “List available episodes matching `title`, oldest first, then download 10
  starting at that feed ID, inclusive.”
- “Download these available episodes individually” (feed IDs).
- “Delete these downloaded episodes individually” (native IDs).

Downloading the newest N episodes now, limiting new downloads per scheduled
check, and retaining N downloaded episodes are different operations. Confirm
ordering, already-downloaded handling, in-progress downloads, and listened/
unlistened retention behavior before promising exact results. Disabling
auto-download must not silently delete files or claim to cancel queued jobs.

### Round 3 — approved episode-selection extension (2026-10-24)

The user approved this extension with “yes do that, orchestrate.” It is
implemented, independently reviewed, and verified on the final candidate: the
full suite passed 579 tests, and the final image is running locally. The
committed eleven-tool baseline at `c58b107` is unchanged; the six modified files
remain uncommitted and unpushed. The tool gate remains
`ABS_ENABLE_PODCAST_WRITES=true`; the default read-only mode still exposes
exactly three tools, while writes-enabled mode exposes fifteen (three read-only,
twelve podcast). No actual media change is authorized or claimed for this
implementation unless separately authorized.

Contract:

- `list_podcast_episodes` keeps `podcast_id` and adds optional `query`,
  `order`, `limit`, and `offset`. `query` is a 1–200 printable-character,
  Unicode-casefolded substring of the title. `order` is `oldest` or `newest`
  (default). `limit` is 1–50 (default 25); `offset` is 0–100000 (default 0).
  Filtering happens before paging. Undated downloaded episodes sort last in both
  orders; equal timestamps use a deterministic episode-ID tie-break. The result
  reports `count` (page count), `total` (filtered total), the applied `offset`
  and `limit`, `has_more`, and `next_offset` (null when no next page). An offset
  past the matches is an empty page, not an error.
- `list_available_podcast_episodes` takes the same arguments and re-fetches the
  stored feed through Audiobookshelf each call. It returns metadata-eligible
  current-feed episodes (valid date plus a non-empty audio enclosure URL), with
  compact `id` (`feed_` plus a SHA-256 digest), `title`, `published_at`,
  `downloaded`, and `queued`; when exactly one downloaded match exists,
  `downloaded_episode_id` is also included. This is not a usability or
  reachability check; URL preflight applies only to selected/window downloads.
  Undated entries remain excluded as baseline even when they carry an enclosure
  URL; they are not added back by a title/enclosure match. It never returns URLs
  or GUIDs, does not probe audio availability, and does not promise the full
  archive. The native feed endpoint returns the whole feed with no pagination, so
  the unchanged 4 MiB cap and 10-second request timeout are all-or-nothing and
  can make an entire large catalogue unavailable. Paging
  is output-only, not remote pagination, and the feed may change between pages.
  The listing performs no mutation, but it stays in the writes group because the
  backend feed re-fetch is admin-only.
- `download_podcast_episodes` takes 1–25 unique feed IDs and resolves them against
  the requested podcast's current feed. Already downloaded or queued entries are
  skipped; every remaining enclosure URL must pass the URL preflight before one
  backend POST.
  It returns `queued` or `nothing_new` with selected, queued, and skipped counts,
  preserves selection order, and performs no retry. `selected_count` is the
  number of resolved targets in the selection/window before skipping,
  `queued_count` is the number of requests submitted, and `skipped_count` is the
  number filtered as already downloaded/queued. The native POST returns 200
  immediately and may silently deduplicate already-queued URLs or fail items
  asynchronously. `queued_count` is not verified per-item acceptance,
  completion, or file availability. The URL preflight checks public syntax, DNS,
  and percent encoding, not actual audio availability. Existing native queue
  work runs ahead of the new batch.
- `download_episodes_from` takes one feed anchor and `count` 1–25 (default 10),
  where count includes the anchor. The fixed window is the anchor plus the next
  `count - 1` newer episodes in chronological publication order, oldest to
  newest, with the deterministic ID tie-break. Already downloaded/queued
  episodes inside the window are skipped but occupy their slots; there is no
  backfill outside the window. Near the newest end, fewer episodes may remain.
  An unknown or stale anchor is rejected rather than guessed. Skipped entries
  are not revalidated; every remaining window entry must pass the URL preflight,
  or the call fails before queueing. Actual audio availability is learned only
  during the asynchronous download. Queue counts have the same submitted-request
  meaning as the selected-download tool.
- `delete_podcast_episodes` takes 1–25 unique native downloaded episode IDs, not
  feed IDs. It prevalidates library/podcast scope, downloaded membership, episode
  paths, and the absence of queued/active downloads before the first DELETE, then
  revalidates as it proceeds. It stops at the first failure and reports
  requested/acknowledged counts, acknowledged IDs, and the first failed ID. Each
  per-episode DELETE returns the full updated item, so the 4 MiB cap can trip
  after the deletion has already applied; that ID is ambiguous, the batch stops,
  and there is no retry. A timeout may leave an operation ambiguous and already
  applied; there is no rollback, and filesystem removal is `not_verified`. The
  existing `delete_podcast_episode` and `download_latest_episodes` tools are
  unchanged, including `queued_count`'s submitted-request semantics.
- The two ID spaces are not interchangeable: feed IDs are accepted only by the
  two feed-selection download tools; native IDs are required by both delete
  tools. A feed ID is a podcast-bound selector only — not an HMAC, secret,
  capability, token, session, or authorization grant. Membership is refetched on
  every call, so knowing a feed ID authorizes nothing. A stale or unknown feed ID
  fails rather than matching a title or URL.

Risk notes: bulk delete can remove up to 25 episodes without a confirmation
step, and selected/window downloads can submit up to 25 asynchronous download
requests. Native queue acknowledgement is immediate, may silently deduplicate
queued URLs, and may fail items later; submitted counts are not completion or
per-item acceptance proof, and pre-existing queue work runs first. Prevalidation
reduces partial failure but does not make a batch atomic or prove physical file
deletion. Per-episode DELETE replies can exceed the 4 MiB cap after the
change has applied, producing an ambiguous failed ID that must not be retried.
The feed ID is a podcast-bound selector, not a secret or authorization mechanism.
The existing SSRF caveat remains: the URL guard is defense in depth, not a
complete SSRF control, and Audiobookshelf's native filter and network egress
controls are still required. The server does not create backups and does not
verify or restore erased files.

Out of scope initially: playback control, user/server administration, generic
API proxying, arbitrary URLs/HTTP methods, shell execution, filesystem access,
book uploads, non-podcast deletion, progress editing, and other service
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
- The current connector is registered through OpenWebUI's application database
  and limited to the owner's account by application-level ACL checks
  (server-side access grants with administrator override). This is not a
  multi-user isolation boundary, and the shared MCP bearer token is not per-user
  authentication; it remains separate from the Audiobookshelf admin key.

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

### Retention and destructive execution

- Retention is potentially destructive even if no delete tool is exposed.
- The user explicitly chose direct execution without extra confirmation, to keep
  the connector stateless and simple. Do not add approval booleans, tokens, UI,
  databases, or elicitation/session infrastructure.
- Revalidate actual podcast/library identity and current state under the mutation
  lock before applying; an external Audiobookshelf actor can still race this.
- For bulk deletion, prevalidate every selected native ID, membership, path, and
  queue state before the first DELETE, revalidate while deleting, and stop at the
  first failure. Report acknowledged counts/IDs and the failed ID; an ambiguous
  timeout may already have applied the operation. Do not retry or claim rollback.
- Explain known immediate deletion impact and report partial failures honestly;
  never promise rollback or blindly retry a potentially successful mutation.
  Do not claim that files were physically erased, that a backup exists, or that
  deleted media can be restored.
- Podcast downloads are asynchronous. Native batch download POST returns 200
  immediately, may silently deduplicate queued URLs, and may fail individual
  items later. `queued_count` counts submitted requests, not verified acceptance
  or completion. Existing queue work runs ahead of the new batch.
- A per-episode DELETE returns the full updated item, so the 4 MiB response cap
  can fail after the deletion has already applied. Treat that ID as ambiguous,
  stop the batch, and do not retry. Skipping downloaded/queued entries does not
  imply remaining files are complete or playable.
- Do not describe feed IDs as authentication or as HMAC/secret-backed. They are
  podcast-bound selectors; every call refetches membership and revalidates scope.
- Explicitly disclose that setting retention permits future automatic episode
  removals under that policy, not just one immediate deletion. Model tool calls
  can therefore be destructive without another user prompt.

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
  immediately trim the archive, and manual downloads do not enforce it. An
  already-over-limit archive stays over limit (add one, delete at most one).
  Zero means unlimited. Optional bounded immediate trimming is a separate action.
  Do not promise a strict ongoing “keep latest N” invariant.
- Hard item deletion recursively removes the item's directory; reject a podcast
  located at its library folder root or outside the registered folder. Verify
  episode file paths remain inside the podcast directory before hard episode
  deletion. Backend success does not prove physical file removal: filesystem
  failures are logged while database deletion can continue. Reject destructive
  trimming/deletion during queued or active downloads instead of clearing queues.
- Native SSRF filter flags were checked without reading credentials:
  `DISABLE_SSRF_REQUEST_FILTER`, `EXP_PROXY_SUPPORT`, and
  `SSRF_REQUEST_FILTER_WHITELIST` are unset in the running instance. Redirect,
  proxy, and DNS-rebinding behavior is not proven by this configuration check;
  verify backend egress controls before rollout.
- Live OpenWebUI is 0.12.3 with Python MCP client 1.27.2; compatibility and client
  security review are needed before rollout. No live credentials were accessed
  during that discovery round; the later private deployment uses mounted secrets.

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
- Implement bounded retention application and optional immediate trimming with
  direct execution, as explicitly selected by the user. Keep writes default-off.

Gate: schedules run in Audiobookshelf independently of OpenWebUI/MCP; disabling
works as documented; retention behavior, podcast boundaries, and deletion bounds
pass tests without requiring elicitation or stateful sessions.

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
  no shared unauthenticated endpoint or credentials in tracked files. This is
  completed for the current private connection (Round 2 status above).

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

### 7. Episode-selection extension (Round 3, approved)

- Extend `list_podcast_episodes` with validated title filtering, `oldest`/
  `newest` ordering, 1–50 limits, 0–100000 offsets, undated-last ordering,
  filter-before-paging, and page/match/next-offset reporting.
- Add `list_available_podcast_episodes` over the current feed through
  Audiobookshelf, using opaque `feed_<sha256>` IDs, compact fields, the unchanged
  4 MiB fail-closed bound, and output-only paging. Keep undated entries excluded
  as baseline, do not expose URLs/GUIDs, and do not promise archive completeness.
  The native full feed has no pagination, so a 4 MiB/10-second failure can block
  the entire catalogue.
- Add `download_podcast_episodes` (1–25 unique feed IDs, remaining-target URL
  preflight before one POST, skip downloaded/queued, no retry) and
  `download_episodes_from` (1–25 inclusive chronological window anchored at one
  feed episode, no backfill, stale anchor fails). Document that native POST 200
  is immediate, may silently dedupe queued URLs or fail later, queue counts are
  submitted requests rather than per-item acceptance, and existing queue work
  runs first.
- Add `delete_podcast_episodes` (1–25 unique native IDs, all-target prevalidation
  and queue guard before first DELETE, revalidation, stop at first failure,
  honest partial counts/IDs, no retry or rollback, filesystem not verified).
  Account for the per-episode DELETE's full-item reply: the cap can fail after
  application, so the failed ID is ambiguous and must not be retried. Leave
  `delete_podcast_episode` and `download_latest_episodes` unchanged.
- Keep the read-only mode at three tools and the writes group at twelve podcast
  tools (fifteen total). Keep stateless HTTP without confirmations,
  application-level OpenWebUI ACL with admin override, token/connector
  separation, read-only root, narrow read-only mounts, and UID `1000` in Compose
  unchanged.

Gate: met on the final candidate. The full suite passed 579 tests in 2.30 s on
Python 3.14.6, covering the bounds, ordering, filter-before-paging,
4 MiB/10-second fail-closed behavior for the unpaginated native feed,
opaque/stale selector IDs, native-versus-feed rejection, membership resolution
and URL preflight before a single POST, submitted queue counts, no-backfill
windows, the full-item DELETE reply exceeding the cap after application, and
bulk-delete prevalidation/partial/ambiguous results. Native queue FIFO/dedupe
behavior is source-verified, not exercised against a live queue. Tool count is
three in read-only mode and fifteen with writes enabled; the final image is
running locally. No real download/delete/policy/subscription call or frontend
mutation workflow is claimed, and writes remain default-off outside the explicit
private deployment.

## Required tests

- Authentication failures, invalid Host/Origin, malformed/oversized requests,
  unexpected protocol versions, rate limits, and concurrent operations.
- Allowed-library enforcement for every tool, including forged IDs and stale
  resources; insufficient backend permissions fail cleanly.
- Malformed RSS, private-address URLs, redirects, credential-bearing URLs,
  oversized feeds/results, and hostile descriptions; test actual backend fetches.
- Duplicate subscriptions/downloads, already-downloaded episodes, sorting,
  partial failures, timeouts, and bounded queues.
- Listing filters and ordering: casefolded title substring, `oldest`/`newest`,
  undated-last behavior, deterministic timestamp ties, 1–50 limits, 0–100000
  offsets, filter-before-paging, `count`/`total`/`has_more`/`next_offset`
  plus applied offset/limit, empty offsets, and invalid query/order values.
- Available-feed listing: only metadata-eligible current-feed episodes (valid date
  plus a non-empty audio enclosure URL), undated entries excluded as baseline,
  downloaded/queued flags, opaque `feed_<sha256>` IDs with no URL/GUID leakage,
  stale/unknown IDs, non-secret selector behavior, unchanged 4 MiB/10-second
  fail-closed behavior for a native full feed with no pagination, output-only
  paging, and feed changes between calls.
- Selected downloads: 1–25 unique feed IDs, current-feed membership resolution of
  every feed ID and URL-preflight of every remaining enclosure URL before a
  single POST, no request when any target fails, duplicate IDs, skipping
  downloaded/queued entries, selection-order preservation, `queued`/`nothing_new`,
  selected/queued/skipped counts, submitted-request rather than per-item
  acceptance semantics, native silent-dedupe/async-failure handling,
  existing-queue-ahead behavior, and no retry.
- Window downloads: inclusive anchor, `count - 1` newer episodes, chronological
  order, deterministic ties, skipping inside the window without backfill,
  fewer-than-count near the newest end, stale/removed anchors, and URL-preflight
  failure (not actual audio availability).
- Bulk deletion: 1–25 unique native IDs only, library/podcast membership,
  downloaded state, path safety, active/queued download guard before first
  DELETE, revalidation while deleting, stop at first failure, partial counts and
  IDs, failed ID, ambiguous-applied treatment including an oversized full-item
  DELETE reply after application, no retry/rollback, filesystem `not_verified`,
  and unchanged single deletion.
- Tool gating and count: exactly three read-only tools without the flag; fifteen
  with writes enabled; no confirmation fields; native and feed ID spaces reject
  each other; `download_latest_episodes` remains unchanged.
- Schedule enable/disable, invalid/too-frequent schedules, timezone/DST, existing
  queue behavior, and no scheduler dependency on an active chat.
- Retention boundaries (including zero semantics), listened/unlistened behavior,
  concurrent state changes, direct execution without elicitation, and partial
  deletion results.
- Config validation/redaction, secret rotation and permissions, non-root/read-
  only runtime, no public port, and no runtime dependency installation.
- End-to-end OpenWebUI tool discovery, calls, errors, and access visibility;
  use Playwright artifact paths required by repository policy when browser testing.
  The final candidate was exercised through the installed client over real HTTP
  against a synthetic backend; the actual frontend selection workflow and live
  mutations remain unverified.

The committed baseline `c58b107` passed 510 tests. The final extension candidate
passed 579 tests in 2.30 s on Python 3.14.6, including 123 podcast-client and 64
protocol tests and quoted/unquoted enclosure URL matching. Native queue
FIFO/dedupe behavior is source-verified, not measured against a live queue. No
real download/delete/policy/subscription call or frontend mutation workflow is
claimed.

## Proposed file layout

```text
audiobookshelf-mcp/
  pyproject.toml
  uv.lock                       # committed locked dependencies
  Dockerfile
  README.md
  SECURITY.md                   # not present; security notes live in README
  src/audiobookshelf_mcp/
    server.py                   # transport/auth and tool registration
    config.py                   # typed settings and secret-file loading
    client.py                   # bounded, allowlisted API adapter
    podcasts.py                 # podcast read/write adapter
    podcast_guards.py           # cron, URL, and path guards
  tests/
.github/dependabot.yml
.github/workflows/audiobookshelf-mcp.yml
compose.yml                     # private connector service from Round 2
/opt/secrets/...                # host-only, never committed
```

## Decisions required before implementation

1. Which Audiobookshelf libraries/account may the connector access?
2. Which feed domains are allowed, and how will backend fetches be restricted?
3. What download caps and scheduling timezone should apply?
4. Confirmation policy resolved: stateless/direct execution, without extra prompts.
5. May external chat providers receive returned library metadata, or is a local
   model required for this connection?
6. Round 3 resolved: implement the episode-selection extension with fixed
   bounds, opaque podcast-bound feed selectors, no new HMAC/secret/session design,
   no extra confirmation, and the existing writes gate. Deployment is complete
   for the private connector; live write validation remains separate.

Definition of done: approved tools work through direct authenticated MCP;
least-privilege access and destructive-action boundaries are demonstrated;
dependency/image maintenance is configured; credentials stay out of the repo,
model context, and logs; deployment and recovery procedures are documented.
For Round 3, the extension passed its final 579-test gate, reports submitted
queue counts and partial/ambiguous mutations honestly, and is deployed locally
from the verified image. Actual frontend selection, real download completion,
and filesystem effects remain unverified.
