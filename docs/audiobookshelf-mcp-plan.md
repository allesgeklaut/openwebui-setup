# Audiobookshelf MCP server — project plan

Status: Round 1 (read-only) was implemented and independently verified locally
on 2026-10-24; Round 2 (podcast management) was implemented and independently
verified locally the same day. Round 2 is committed at `c58b107` with podcast
writes explicitly enabled, and the connector is registered privately through
OpenWebUI's application database; the user has confirmed the read-only OpenWebUI
path works. Round 3 (the approved episode-selection extension) is committed at
`0a1b962`; its full suite passed 579 tests in 2.30 s on Python 3.14.6, and it
was the previous fifteen-tool release. The review hardening is committed at
`0d15a14` and is the current release: its candidate image was built from the
reviewed source hashes, scanned, promoted to `audiobookshelf-mcp:local`, and
only the connector was recreated. It is healthy, started
`2026-10-25T11:52:27Z` (12:52 Vienna CET), and serves sixteen tools with the
active `ABS_SERVER_TIMEZONE=Europe/Vienna` assertion. The fresh pre-deployment
suite passed 716 tests in 2.74 s; the earlier 2.76 s run and the 579-test
Round 3 evidence are historical. The required client, server, and config reviews
are complete with findings fixed and none blocking, and the optional
installed-client HTTP compatibility check passed against a synthetic backend.
The installed OpenWebUI MCPClient adapter, using the persisted production
connection, verified discovery of the sixteen closed-schema tool specs, delivery
of the initialize discovery instructions, and an authenticated read-only
`list_libraries` call; the OpenWebUI frontend/model-chat workflow and
model-in-the-loop behavior remain unverified. Trivy reported zero fixable
HIGH/CRITICAL findings for the candidate, with 44 unfixed HIGH findings across
8 CVEs (0 critical) still present in the full scan. No live media write is
claimed, and this documentation update does not change credentials, services,
or media. The implementation commit is not pushed; push and broader deployment
require separate, explicit authorization that has not been granted.

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
no live media mutation is claimed here. Round 3 is committed as `0a1b962`,
verified against synthetic backends and harmless live reads only, and was the
previous fifteen-tool deployment; its review hardening followed and is now the
current release. Round 3's installed-client HTTP compatibility check passed
against a synthetic backend and remains part of that historical evidence. After
the hardening review, the user authorized the implementation commit and a
connector-only restart; both are complete, and `0d15a14` is running as a healthy
sixteen-tool deployment. Real write behavior has not been exercised and remains
default-off outside this private deployment.

Implementation principles: KISS, DRY, YAGNI. Start with a small authenticated
read-only server and tests; add podcast writes only after the API/permission and
security gates pass. Do not build speculative approval infrastructure, plugins,
or a separate scheduler.

Delivery at the committed `c58b107` baseline: `list_libraries`, `search_library`,
and `get_item`, plus eight podcast tools (eleven total), with the Docker build,
locked dependencies, tests, and CI/Dependabot. Round 3, committed as `0a1b962`,
adds `list_available_podcast_episodes`, `download_podcast_episodes`,
`download_episodes_from`, and `delete_podcast_episodes`, and extends
`list_podcast_episodes`, for fifteen tools (three read-only, twelve podcast);
that was the previous release. The review hardening, committed at `0d15a14`, is
the current release at sixteen tools (three read-only, thirteen podcast): it
adds `list_podcasts`, strict input rejection, annotations, settings/state
fields, and the safety fixes described below. The required reviews are complete
with findings fixed and none blocking, and the optional installed-client HTTP
compatibility check passed against a synthetic backend. The
connector's Compose service already exists from Round 2; neither Round 3 nor the
hardening needs a new root Compose service or `start.sh` change. Both keep the
same writes flag; the hardening adds only the optional
`ABS_SERVER_TIMEZONE=Europe/Vienna` operator assertion to the existing service,
which is committed and active.

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
on an invalid write flag. Locked runtime dependency audit passed. The current
`0d15a14` candidate has zero known fixable HIGH/CRITICAL findings in the dated
Trivy 0.76.1 database. A full scan still reports 8 unfixed upstream CVEs across
44 HIGH package findings (0 critical); these are documented, not a claim of zero
vulnerabilities. CI and weekly Dependabot updates are configured; GitHub-hosted
execution awaits an authorized push. Round 2 was subsequently committed as
`c58b107` (superseding
the earlier "uncommitted" note). Round 3 was committed as `0a1b962`; its final
candidate passed 579 tests in 2.30 s on Python 3.14.6 (123 podcast-client and 64
protocol tests; earlier tests unchanged). That was the previous fifteen-tool
release. The review hardening is committed at `0d15a14`; its final synthetic
suite passed 716 tests in 2.76 s on Python 3.14.6 (218 podcast,
73 server, 112 config, 313 unchanged client/guard), and a fresh pre-deployment
run passed 716 tests in 2.74 s. The suite covered independent
oracles for trim drift, latest-preflight scope changes, and unknown-flag
rejection. No dependencies or lockfiles changed in the hardening; a fresh
`pip-audit` 2.10.1 run reported no known vulnerabilities. A 17,280-case sample
of connector-accepted crons (10 minute forms × 8 hour × 6 day × 6 month ×
6 weekday) also passed the installed native pure validator.
Config independent review found one LOW-severity sanitized I/O gap
(`OSError`), now fixed, with 112 config tests verified. The independent server
review passed with no severity findings (the 73 server tests plus raw-HTTP and
direct unknown-key,
strict boolean/numeric, auth-bound, fingerprinted-state, and cron/note/
annotation probes). The independent client review passed except one LOW
duplicate-kept-prefix ambiguity, reproduced and fixed (+2 tests); the review's
F1 follow-up is closed with no new findings or policy-only regression,
218 podcast tests pass, and a primary duplicate-probe rerun (`keep` 2 and 5)
gave a sanitized
ambiguity error with zero mutations and unchanged policy. All required client,
server, and config reviews are complete with findings fixed and none blocking;
the optional installed-client HTTP compatibility check passed against a
synthetic backend (installed MCP 1.27.2 client inside the open-webui container
to a temporary `172.31.0.1`-bound gateway; `httpx.MockTransport` backend with
25 fake-host requests, 114/114 client and 25/25 host checks, artifact
`/tmp/opencode/abs-followup-integration/probe-result.json`; primary verified
the artifact and cleanup; that probe performed no build, scan, promotion,
restart, or real media write). Round 3 results are historical and are not
claimed to revalidate the hardening release. The
installed OpenWebUI MCP client 1.27.2 discovered all 15 Round 3 tools over real
HTTP against a temporary
synthetic backend and passed paging, selected/window, partial-delete, and
redaction checks; all mutating checks were synthetic, and no real download,
delete, policy, or subscription call was made. Runtime verification confirmed
UID `1000:1000`, read-only root
and secret mounts, `0600` user-owned credentials, `cap_drop: ALL` with
`no-new-privileges`, no privileged mode or published ports, the expected
networks and allowed podcast library, and writes enabled.

Current deployment (2026-10-25): the `0d15a14` candidate was built from the
reviewed source hashes, scanned, promoted to `audiobookshelf-mcp:local` (digest
`sha256:459d05ad97623726798faf850b8a82eb65294900dfcc83726d5fb7a80edb7452`),
and only `audiobookshelf-mcp` was recreated with
`docker compose up --no-deps --no-build --force-recreate --wait`. It is healthy,
started `2026-10-25T11:52:27Z` (12:52 Vienna CET), and serves sixteen tools;
Audiobookshelf and OpenWebUI were not restarted. Two separate read-only checks
passed after deployment. First, the installed OpenWebUI application's MCPClient
adapter, using the persisted production connection, discovered the sixteen
closed-schema tool specs, received the initialize discovery instructions, and
completed an authenticated read-only `list_libraries`; that check covered
discovery and `list_libraries` only. Second, connector-local authenticated
reads — direct to the connector, not through the OpenWebUI adapter — were
`list_libraries` → `list_podcasts` (`limit` 1, `page` 0; 1 of 8 podcasts) →
`get_podcast_settings`, which reported `Europe/Vienna` with integer
queued/active counts. No fingerprint field, media write, feed fetch, download,
or policy change was performed. The frontend/model-chat selection workflow,
model-in-the-loop behavior, real download completion, and filesystem effects
remain unverified.

## Goal

Build a small, security-focused Audiobookshelf MCP server that connects directly
to OpenWebUI over Streamable HTTP. Support library browsing, bounded podcast
subscription discovery, podcast subscriptions, episode downloads, download
schedules, retention settings, and bounded current-feed episode selection. Use
Audiobookshelf's native scheduler rather than introducing another scheduler.

Round 3 adds feed-scoped episode listing, selected and fixed-window downloads,
and bounded bulk deletion; it is committed at `0a1b962` and was the previous
release. The review hardening, committed at `0d15a14` and now deployed, adds
`list_podcasts` and the safety/usability changes below. Neither release
changes media unless a user invokes a write tool; this documentation work
changes none.

The initial implementation did not change running services, account permissions,
secrets, subscriptions, or existing media. The later connector-only hardening
recreation is described above and did not change credentials or media.

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
approved names below. The review hardening adds a sixteenth tool,
`list_podcasts`. Treat all names as stable tool identities.

| Tool | Purpose | Initial guard |
| --- | --- | --- |
| `list_libraries` | List permitted libraries | Read-only |
| `search_library` | Search books/podcasts with bounded pagination | Read-only |
| `get_item` | Compact metadata for one permitted item | Read-only |
| `list_podcasts` | Enumerate podcast subscriptions in one allowed library | Podcast group only; allowed podcast library; native page 0–1000, limit 1–50; minified reads; no feed URLs |
| `list_podcast_episodes` | List downloaded episodes with title filtering and offset paging | Podcast group; bounded list 1–50, offset 0–100000; undated last |
| `list_available_podcast_episodes` | List valid, dated current-feed episodes with an audio enclosure URL and opaque feed IDs | Podcast group (admin-only feed fetch); 4 MiB fail-closed bound; not an archive; no remote pagination |
| `get_podcast_settings` | Current schedule, limits, retention, settings counts, and asserted server timezone | Podcast group only; non-mutating; settings counts are current state, not completion history |
| `add_podcast` | Subscribe using an RSS URL in an allowed podcast library | Explicit user request; URL validation; duplicate detection |
| `download_latest_episodes` | Queue the newest N not already downloaded/queued/active | Bounded count and queue; no implicit full-feed download; final scope/feed/blocked recompute after URL preflight |
| `download_podcast_episodes` | Queue individually selected current-feed episodes | 1–25 unique feed IDs; URL-preflight remaining targets before one request; skip downloaded/queued with `skipped_episodes`; no retry |
| `download_episodes_from` | Queue a fixed chronological window anchored at one feed episode | 1–25 inclusive window; anchor plus newer episodes only; no backfill; stale anchor fails; `skipped_episodes` |
| `set_podcast_schedule` | Enable/disable auto-download and configure frequency/new-episode limit | Validated settings and read-back; no preview tool; no approval prompt |
| `set_podcast_retention` | Set native retention and optionally trim existing episodes | Direct execution when writes enabled; per-step guarded bounded deletion; policy PATCH may apply before zero deletes |
| `delete_podcast_episode` | Remove a downloaded episode and its associated data | Podcast/library validation; no extra confirmation; deletion behavior unchanged; strict schema/annotations changed with the review hardening |
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

The user approved this extension with “yes do that, orchestrate.” Round 3 is
committed at `0a1b962`: its full suite passed 579 tests, and its image served
fifteen tools (three read-only, twelve podcast) until the review hardening
replaced it. The tool gate remains
`ABS_ENABLE_PODCAST_WRITES=true`; default read-only mode exposes exactly three
tools. No actual media change is authorized or claimed for this implementation
unless separately authorized. The following contract is the committed Round 3
baseline; the hardening deltas below are committed at `0d15a14` and deployed,
with synthetic validation and the required client/server/config reviews passed
(findings fixed, none blocking) and the optional installed-client HTTP
compatibility check passed against a synthetic backend.

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
  applied; there is no rollback, and filesystem removal is `not_verified`. Round
  3 left `delete_podcast_episode` and `download_latest_episodes` as they were;
  the later review hardening changes latest's final recomputation.
  `queued_count` keeps its submitted-request semantics.
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

### Round 3 review hardening (2026-10-25; committed `0d15a14`, deployed)

The user accepted “sound good” to fix three safety findings and small
AI-usability gaps, then authorized the implementation commit and a
connector-only restart; both are complete. The hardening is committed at
`0d15a14`, was built from the reviewed source hashes, scanned, promoted, and
deployed only for `audiobookshelf-mcp`; it is healthy and serves sixteen tools.
Its final synthetic suite passed 716 tests in 2.76 s on Python 3.14.6 (fresh
pre-deployment rerun 716 tests in 2.74 s); the required client, server, and
config reviews are complete
with findings fixed and none blocking, and the optional installed-client HTTP
compatibility check passed against a synthetic backend. Review also corrected post-PATCH/final-state partial accounting,
immediate-deletion notes, the omitted schedule instruction, and impossible
list-page totals. The release is sixteen tools when writes are enabled (three
read-only, thirteen podcast) and three when disabled.

- `list_podcasts(library_id, limit=25, page=0)` enumerates subscriptions in one
  allowed podcast library through bounded, minified native library-item page
  reads (`limit` 1–50, `page` 0–1000). It is the supported way to discover
  podcast IDs without a guessed search query. It returns `library_id`,
  `podcasts` (`id`, `title`, `downloaded_count` as an integer or `null`),
  `count`, `total`, `page`, `limit`, `has_more`, `next_page`, and optionally
  `note`; `next_page` is null at the end. At the maximum page with more rows
  remaining, `has_more` becomes false and `note` explains the bound instead of
  returning an unusable cursor. It exposes no generic API, no feed or enclosure
  URL, and no full-library fetch. The existing episode-listing `offset`
  contract (0–100000, page size 1–50) is unchanged.
- `get_podcast_settings` adds `queued_download_count`,
  `active_download_count`, and `server_timezone` (string or `null`).
  `downloaded_count`, `queued_download_count`, and `active_download_count` are
  current backend counts at read time, not completion/failure history and not a
  download result's submitted-request `queued_count`. The timezone is the
  optional `ABS_SERVER_TIMEZONE` operator assertion, not backend autodiscovery;
  when unset it is unknown and must not be inferred from the connector,
  container, or host zone. The Compose service sets
  `Europe/Vienna`, matching the verified native Audiobookshelf zone; that
  assertion is active in the running deployment and was reported by a live
  read-only `get_podcast_settings` call. It does not change the backend zone.
- Selected-download and window results add `skipped_episodes`, each with `id`,
  `title`, `published_at`, and `reason` (`already_downloaded` or
  `already_queued`). `id` is the same opaque feed selector accepted by the
  download tools, not a native episode ID. Selected/queued/skipped counts and
  the no-backfill window behavior are unchanged.
- `download_latest_episodes` now applies the selected tools' final scope,
  feed-identity, and blocked-state recomputation after URL preflight and before
  its single POST. The goal is to reduce a scope/queue race, not to introduce
  atomicity or completion tracking.
- Retention trim rejects duplicate native IDs anywhere in the downloaded set —
  kept (non-target) episodes and zero-surplus cases included — with a sanitized
  ambiguity error before planning or the policy PATCH; the earlier target-only
  check could miss kept duplicates and over-delete unique episodes. Selected
  deletion keeps target-only duplicate semantics, and policy-only retention is
  unchanged. It prevalidates every target's podcast/library identity, path,
  downloaded membership, and queue state before its own policy PATCH. Each
  deletion step then rechecks scope, state, queue, path, and identity, and
  accounts separately for the policy write and its acknowledged deletions. It
  stops at the first failure and reports partial `deleted_count`,
  `deleted_episode_ids`, `failed_episode_id` when a failed target is known,
  status, read-back state, and the error. A read-back-only failure can report
  partial with all DELETE acknowledgements and no failed ID. There is no retry,
  rollback, or filesystem verification. The policy PATCH may apply even when zero deletions follow, so
  a zero-delete result does not prove that no write occurred. There is no
  atomic compare-and-swap guarantee: native Audiobookshelf administrators, its
  scheduler, or another server process can still race the final checks.
- Every tool input schema sets `additionalProperties: false`, and unknown
  arguments are rejected at runtime before any backend call. `dry_run`, `hard`,
  `override`, `confirm`, and unexpected keyword arguments are errors, never
  silent destructive defaults. No dry-run or approval flag exists; stateless
  direct execution and the no-extra-approval policy are preserved.
- The server advertises truthful MCP annotations (read-only/destructive
  hints). They are client hints only, not permissions or a security boundary.
  The installed OpenWebUI client drops annotations and typed output schemas but
  forwards the initialize instructions, so essential guidance remains in tool
  descriptions. A broad typed-output-schema rewrite is optional/deferred and is
  not claimed as done.
- Model guidance: list current state with `get_podcast_settings` or the episode
  listings rather than claiming a download completed from a submitted request;
  use `list_podcasts` for discovery; disclose that positive retention plus an
  automatic schedule can delete unlistened episodes later; do not add an
  approval prompt.

The hardening leaves secret separation, library allowlisting, container
hardening, the SSRF caveat, NFS/symlink path limitations, and the 4 MiB/10-second
feed bounds unchanged. Scripted workflow and synthetic tests do not prove
model-in-the-loop behavior or real media writes. The post-deployment live checks
were read-only.

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
- Every tool input schema sets `additionalProperties: false`; the runtime also
  rejects unknown keys before any backend call. `dry_run`, `hard`, `confirm`,
  and similar flags are rejected rather than treated as overrides. MCP
  annotations are truthful client hints only, not permissions, authorization,
  or a security boundary.
- `list_podcasts` is scoped to one allowed podcast library, uses bounded native
  page reads, and returns no feed/enclosure URLs or generic API surface.
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
  lock before applying; there is no atomic compare-and-swap guarantee, and an
  external Audiobookshelf administrator, its scheduler, or another server
  process can still race the final check.
- For bulk deletion, prevalidate every selected native ID, membership, path, and
  queue state before the first DELETE, revalidate while deleting, and stop at the
  first failure. Report acknowledged counts/IDs and the failed ID; an ambiguous
  timeout may already have applied the operation. Do not retry or claim rollback.
- Retention trim follows the same per-step discipline: prevalidate all targets
  before its own policy PATCH, recheck scope/state/queue/path/identity before
  each DELETE, and account separately for the policy write and acknowledged
  deletions. The policy can apply even when zero deletions follow; stop at the
  first failure and report partial counts/IDs and `failed_episode_id` when a
  failed target is known. No retry, rollback, or filesystem verification.
- Explain known immediate deletion impact and report partial failures honestly;
  never promise rollback or blindly retry a potentially successful mutation.
  Do not claim that files were physically erased, that a backup exists, or that
  deleted media can be restored.
- Podcast downloads are asynchronous. Native batch download POST returns 200
  immediately, may silently deduplicate queued URLs, and may fail individual
  items later. `queued_count` counts submitted requests, not verified acceptance
  or completion. Existing queue work runs ahead of the new batch.
  `download_latest_episodes` now applies the same final scope, feed-identity,
  and blocked-state recomputation as the selected tools after URL preflight,
  reducing but not eliminating a queue race.
- A per-episode DELETE returns the full updated item, so the 4 MiB response cap
  can fail after the deletion has already applied. Treat that ID as ambiguous,
  stop the batch, and do not retry. Skipping downloaded/queued entries does not
  imply remaining files are complete or playable.
- Do not describe feed IDs as authentication or as HMAC/secret-backed. They are
  podcast-bound selectors; every call refetches membership and revalidates scope.
- Explicitly disclose that setting retention permits future automatic episode
  removals under that policy, not just one immediate deletion. A positive `keep`
  plus an automatic-download schedule can delete unlistened episodes later;
  Audiobookshelf ignores listened state. Model tool calls can therefore be
  destructive without another user prompt, and no approval prompt is added.

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
  `delete_podcast_episode` deletion behavior unchanged; the later review
  hardening adds latest's final recomputation.
- Keep the read-only mode at three tools and the writes group at twelve podcast
  tools (fifteen total). Keep stateless HTTP without confirmations,
  application-level OpenWebUI ACL with admin override, token/connector
  separation, read-only root, narrow read-only mounts, and UID `1000` in Compose
  unchanged.

Gate: met on the committed Round 3 candidate at `0a1b962`. The full suite passed
579 tests in 2.30 s on Python 3.14.6, covering the bounds, ordering,
filter-before-paging, 4 MiB/10-second fail-closed behavior for the unpaginated
native feed, opaque/stale selector IDs, native-versus-feed rejection, membership
resolution and URL preflight before a single POST, submitted queue counts,
no-backfill windows, the full-item DELETE reply exceeding the cap after
application, and bulk-delete prevalidation/partial/ambiguous results. Native
queue FIFO/dedupe behavior is source-verified, not exercised against a live
queue. Tool count is three in read-only mode and fifteen with writes enabled;
that build ran locally until the review hardening replaced it. No real
download/delete/policy/subscription call or frontend mutation workflow is
claimed, and writes remain default-off outside the explicit private deployment.

### 8. Round 3 review hardening (approved 2026-10-25; deployed)

- Add `list_podcasts(library_id, limit=25, page=0)` with allowed-podcast-library
  scope, native page 0–1000/limit 1–50 minified reads, nullable
  `downloaded_count`, and page/total/next-page reporting; no guessed search
  query, generic API, or feed URL.
- Add `queued_download_count`, `active_download_count`, and `server_timezone` to
  settings. The timezone is the optional `ABS_SERVER_TIMEZONE` operator
  assertion, not autodiscovery; Compose's `Europe/Vienna` value is committed
  and active in the running connector.
- Add `skipped_episodes` feed selectors/reasons to selected and window download
  results; apply final scope/feed-identity/blocked recomputation to
  `download_latest_episodes` after URL preflight.
- Rework retention trim for per-step scope/state/queue/path/identity
  revalidation, all-target prevalidation before the policy PATCH, separate
  accounting for the policy write and acknowledged deletions, honest partial
  results, and no retry/rollback/filesystem verification. Keep the documented
  absence of atomic CAS and native-actor race.
- Apply `additionalProperties: false` to every tool and reject unknown keys at
  runtime before backend calls; reject `dry_run`/`hard`/`confirm` rather than
  falling back to a destructive default. Add truthful MCP annotations as hints
  only, and keep essential guidance in descriptions because the installed
  OpenWebUI client drops annotations/output schema but forwards initialize
  instructions. Typed output schemas remain optional/deferred.

Gate: implementation, safety review, and synthetic validation passed — the
716-test synthetic suite and the required client, server, and config reviews
are complete with findings fixed and none blocking. The optional
installed-client HTTP compatibility check passed against a synthetic backend.
The gate was not by itself deploy approval; a separate commit-and-restart
authorization was granted, and the release is now committed at `0d15a14`,
deployed as the healthy sixteen-tool connector, and checked with read-only
production calls. Prior Round 3 evidence remains historical and must not be
reported as validating this sixteen-tool deployment's HTTP integration. The
installed OpenWebUI MCPClient adapter covered discovery of the sixteen
closed-schema specs, the initialize discovery instructions, and an
authenticated read-only `list_libraries`; connector-local reads covered
`list_podcasts` and `get_podcast_settings`. No frontend chat turn was exercised.

## Required tests

- Authentication failures, invalid Host/Origin, malformed/oversized requests,
  unexpected protocol versions, rate limits, and concurrent operations.
- Allowed-library enforcement for every tool, including forged IDs and stale
  resources; insufficient backend permissions fail cleanly.
- Malformed RSS, private-address URLs, redirects, credential-bearing URLs,
  oversized feeds/results, and hostile descriptions; test actual backend fetches.
- Duplicate subscriptions/downloads, already-downloaded episodes, sorting,
  partial failures, timeouts, and bounded queues.
- Podcast discovery: `list_podcasts` only for an allowed podcast library,
  1–50/0–1000 page bounds, native paging, `count`/`total`/`has_more`/
  `next_page` (null at end), nullable `downloaded_count`, and no feed URL,
  generic API, or guessed-query fallback.
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
  downloaded/queued entries with `skipped_episodes` feed-selector IDs, titles,
  publication times, and reasons, selection-order preservation,
  `queued`/`nothing_new`, selected/queued/skipped counts, submitted-request
  rather than per-item acceptance semantics, native silent-dedupe/async-failure
  handling, existing-queue-ahead behavior, and no retry.
- Window downloads: inclusive anchor, `count - 1` newer episodes, chronological
  order, deterministic ties, skipping inside the window without backfill,
  `skipped_episodes` entries, fewer-than-count near the newest end,
  stale/removed anchors, and URL-preflight failure (not actual audio
  availability).
- Bulk deletion: 1–25 unique native IDs only, library/podcast membership,
  downloaded state, path safety, active/queued download guard before first
  DELETE, revalidation while deleting, stop at first failure, partial counts and
  IDs, failed ID, ambiguous-applied treatment including an oversized full-item
  DELETE reply after application, no retry/rollback, filesystem `not_verified`,
  and unchanged single-deletion behavior.
- Tool gating and count: exactly three read-only tools without the flag; sixteen
  with writes enabled (thirteen podcast); strict `additionalProperties: false`
  and runtime unknown-key rejection before backend calls, including
  `dry_run`/`hard`/`confirm`; native and feed ID spaces reject each other;
  `download_latest_episodes` final scope/feed/blocked recomputation; truthful
  annotations as hints only.
- Schedule enable/disable, the narrow five-field cron grammar (integers, `*`,
  `*/N`, comma lists; no ranges/names; 15-minute minimum spacing; weekday
  0=Sunday–6=Saturday, e.g. `0 8 * * 1,2,3,4,5`), timezone/DST, existing queue
  behavior, and no scheduler dependency on an active chat.
- Retention boundaries (including zero semantics), listened/unlistened behavior,
  per-step scope/state/queue/path/identity guards, whole-set duplicate-ID
  rejection before planning/PATCH (kept prefix and zero-surplus included),
  all-target prevalidation
  before the policy PATCH, policy-applied-with-zero-deletes accounting,
  partial counts/IDs and `failed_episode_id` when a target failed, concurrent
  state changes, direct execution without elicitation, no CAS guarantee, and
  filesystem `not_verified`.
- Settings state fields (`queued_download_count`, `active_download_count`, and
  nullable `server_timezone`), unset/unknown timezone behavior without
  inference, invalid timezone rejection, and `ABS_SERVER_TIMEZONE` as an
  operator assertion rather than autodiscovery.
- Config validation/redaction, secret rotation and permissions, non-root/read-
  only runtime, no public port, and no runtime dependency installation.
- End-to-end OpenWebUI tool discovery, calls, errors, and access visibility;
  use Playwright artifact paths required by repository policy when browser testing.
  The Round 3 candidate was exercised through the installed client over real
  HTTP against a synthetic backend; the actual frontend selection workflow and
  live mutations remain unverified. The review hardening's synthetic
  suite and required reviews passed, its optional installed-client HTTP
  compatibility check passed against a synthetic backend, and it is now
  deployed. The installed OpenWebUI MCPClient adapter over the persisted
  production connection verified sixteen-tool discovery with closed schemas,
  the initialize instructions, and an authenticated read-only `list_libraries`;
  connector-local reads separately covered `list_podcasts` and
  `get_podcast_settings`. Do not infer application-chat, model-in-the-loop, or
  live-write integration from those checks.

The committed baseline `c58b107` passed 510 tests. Round 3, committed as
`0a1b962`, passed 579 tests in 2.30 s on Python 3.14.6, including 123
podcast-client and 64 protocol tests and quoted/unquoted enclosure URL matching.
Those historical results cover the previous fifteen-tool release only. The
sixteen-tool review hardening's final synthetic suite passed 716 tests (fresh
pre-deployment rerun 2.74 s) and its required
client/server/config reviews are complete with findings fixed and none
blocking, and its optional installed-client HTTP compatibility check passed
against a synthetic backend; post-deployment read-only checks verified the
installed OpenWebUI adapter's sixteen-tool discovery and `list_libraries` plus
connector-local `list_podcasts` and `get_podcast_settings` reads. No actual
model evaluation should be inferred. Native queue FIFO/dedupe behavior is
source-verified, not measured against a live queue. No actual model-in-the-loop
evaluation, real download/delete/policy/subscription call, or frontend mutation
workflow is claimed.

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
   no extra confirmation, and the existing writes gate. It is committed at
   `0a1b962` and was the previous fifteen-tool private deployment; live write
   validation remains separate.
7. Review hardening resolved (2026-10-25): add the sixteenth tool
   `list_podcasts`, settings/state fields, strict unknown-key rejection,
   truthful annotations, `skipped_episodes`, latest final recomputation, and
   per-step retention guards. Keep 3-tool read-only mode, 16-tool writes mode,
   no dry-run/approval mechanism, and the existing writes gate. It is committed
   at `0d15a14` and deployed to the private connector. The user authorized the
   commit and connector restart only; push and broader deployment require
   separate, explicit authorization that has not been granted.

Definition of done: approved tools work through direct authenticated MCP;
least-privilege access and destructive-action boundaries are demonstrated;
dependency/image maintenance is configured; credentials stay out of the repo,
model context, and logs; deployment and recovery procedures are documented.
Round 3 met its final 579-test gate, reports submitted queue counts and
partial/ambiguous mutations honestly, and was deployed locally from its verified
image as the previous release. The review hardening's implementation, reviews,
testing, and optional HTTP check are complete (716 tests; findings fixed, none
blocking; the installed-client HTTP compatibility check passed against a
synthetic backend); it is committed at `0d15a14` and deployed as the healthy
sixteen-tool connector after the authorized commit and connector-only restart.
The installed OpenWebUI MCPClient adapter over the persisted production
connection verified discovery of the sixteen closed-schema specs, delivery of
the initialize instructions, and an authenticated read-only `list_libraries`;
no frontend chat turn was exercised. Push and broader deployment require
separate, explicit authorization and have not been authorized. Actual OpenWebUI
application-chat/frontend selection,
model-in-the-loop behavior, real download completion, and filesystem effects
remain unverified.
