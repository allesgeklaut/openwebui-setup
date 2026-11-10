# Pre-commit review — 2026-11-10

**Verdict:** no blocking findings remaining in the prepared code. Direct review,
not an independent second-agent review. Re-reviewed before installation; the
0.3.0 import adapter was installed through the authenticated native update API
on 2026-11-10. Review and installation were completed before committing.

## Scope

Reviewed the existing page-only implementation, migration adapter, runtime
package, tests, installation/rollback documentation and existing extension mount.
Original source SHA256:
`f782314ad5ffd533e6bf704bf1ecd17f4ef3b8a9a3d4e8c0117812b0122774db`.

## Findings addressed

- Source/image URLs could break out of Markdown destinations. URLs are now
  validated and Markdown delimiters encoded; caption controls/newlines and
  special characters are normalized/escaped.
- Unexpected JSON types/result types could raise uncaught errors. Shape checks
  and malformed-result skipping are covered by tests.
- HTTP errors included internal URL/exception details. Errors are now sanitized.
- No response-size bound, unvalidated valve ranges and redirects. Added a 2 MiB
  streamed response cap, valve validation and redirect rejection.
- A result webpage was used as an image fallback. Require `img_src` instead.
- Description claimed a slash command was automatically available without
  evidence of registration. Removed that promise; public tool method is unchanged.

## Verification

- New offline suite in the running Open WebUI container: **17 passed**.
- Existing chat-image extension suite: **86 passed**.
- Native `load_tool_module_by_id` loads the mounted package via the adapter;
  `get_tool_specs` exposes only `image_search` with `query` required and the same
  `count=0`, `safesearch=-1`, `time_range=""` defaults. Manifest version 0.3.0.
- Explicit live image search through that loaded instance returned two inline
  images using the configured SearXNG. No image URLs were fetched by this tool.
- Python syntax and `git diff --check` passed. No Compose changes required.
- Before installation, a read-only check confirmed the original 0.2.0 entry.
  The existing entry was then updated through the native API, preserving its
  ID, owner, name, activation, access grants, creation timestamp and valves.
  No tool was deleted/recreated and no model attachments were changed.
- The temporary private rollback export was removed at the operator's request.
  No direct database writes were used.
- After restarting Open WebUI: health HTTP 200, saved adapter exactly matches
  `tool.py`, the native loader resolves the mounted implementation with saved
  valves, and saved/generated specs agree. A live search through the actual
  saved registration returned two inline images. Both test suites passed again.

## Remaining acceptance checks

Check browser rendering in a private chat. Existing tests do not exercise a
model's tool selection or browser rendering. External image hosts and content
remain untrusted; see README for
privacy, safesearch and prompt-injection limitations.
