# SearXNG Image Search — repo-managed tool

Migrates the native Workspace Tool `searxng_image_search` (page-only version
0.2.0) to a small registration adapter and a Git-managed runtime package.
Targets Open WebUI 0.13.x; `requests` and Pydantic 2 are already installed.
No new service, credentials, dependencies or Compose changes are needed.

## Source of truth

- `../searxng_image_search/__init__.py`: implementation and valve schema.
- `tool.py`: native registration entry; imports `Tools` from the mounted package.
- `test_searxng_images.py`: offline regression tests.
- [REVIEW.md](REVIEW.md): pre-commit findings, checks and remaining acceptance tests.

The existing extension root is mounted read-only at `/app/backend/extensions`
and is on `PYTHONPATH`. **Mounting the package alone does not replace the
existing database entry.** The one-time registration step below is required.

## Behaviour and review

The tool still exposes `image_search(query, count=0, safesearch=-1,
time_range="")`. It calls the same SearXNG instance at `http://searxng:8080`,
with `categories=images` and `format=json`. Defaults remain four images,
moderate safesearch and a 20-second HTTP timeout; output is capped at eight
images. No engines are forced: SearXNG's **image-category** defaults apply,
not its general/news engines. Time filters can exclude unsupported engines.

Reviewed changes from the old tool:

- Validate valve ranges and require an HTTP(S) base URL without credentials,
  query or fragment. This URL is administrator-controlled and trusted; internal
  Docker addresses are intentionally allowed. Do not grant untrusted users
  permission to edit code or server-level valves.
- Send query parameters through the HTTP client's encoding, not string formatting.
  Reject redirects rather than following the search service to another target.
- Bound the decompressed JSON response to 2 MiB and scan at most 200 results.
  Validate response/result shapes; ignore malformed entries and duplicates.
- Require an actual `img_src`; a source webpage is no longer rendered as though
  it were an image. Accept HTTP(S) image/source links only; reject credentials,
  controls and malformed URLs. Escape captions and encode Markdown URL delimiters.
- Return sanitized errors instead of internal URLs or exception details.
- Remove the unsupported promise that `/image_search` is automatically registered
  as a slash command. Native tool selection and model tool calling are unchanged.

The backend fetches **only the configured SearXNG**, not result images/pages.
Displaying external images makes the browser contact those hosts, exposing its
IP and subject to the browser's privacy/mixed-content policies. URLs, captions,
image contents and source pages remain untrusted and can contain prompt
injections. HTTP(S)-only URL validation is not a private-address/content safety
filter. Safesearch depends on each engine's support. This tool returns Markdown,
not a guarantee that the model visually inspected the image or verified its license.
The HTTP timeout is a connect/read timeout, not a total wall-clock deadline.

## Tests and pre-install checks

Run the offline suite against the existing mounted package and real dependencies:

```sh
docker exec -e PYTHONPATH=/app/backend/extensions open-webui \
  python -m unittest discover -s /app/backend/extensions/searxng-images -p 'test_*.py' -v
git diff --check
```

Before installation, also verify that Open WebUI's native tool loader can load
`tool.py` and exposes **only** `image_search`, with the same arguments/defaults.
An explicit live smoke search may contact SearXNG and its upstream engines;
offline tests do not. Do not log chat content, credentials or image payloads.

## One-time installation (administrator)

1. Optionally export the existing `searxng_image_search` tool through the Workspace
   UI, including its valves/access settings. Keep any export private, outside Git.
2. In **Workspace → Tools**, edit the existing **SearXNG Image Search** tool and
   replace its code with `tool.py`. Keep the same ID, access permissions,
   activation and model attachments. Do not delete/recreate it. Save normally so
   Open WebUI regenerates metadata/specs through its supported application path.
   Do not write the database directly.
3. Restart `open-webui` to clear imported-module and tool caches. No Compose
   recreation is needed: the extension directory is already mounted.
4. Check the registered code equals `tool.py`, the mounted implementation imports,
   and the registered tool still exposes only `image_search`. In a private test
   chat, select the tool, ask for an image and verify inline rendering/source links.

For later **implementation-only updates**, deploy the Git changes and restart
`open-webui`; the adapter does not need to be pasted again. Adapter/manifest
changes require explicitly updating the saved entry too. Git manages the code,
not Open WebUI's permissions, activation or per-installation valves.

For rollback, restore the previous saved tool code through the UI and restart,
or revert the package in Git and restart if the adapter remains compatible.
Back up both this repo and Open WebUI data. Do not deploy a branch without this
package while the import-based registration is active.
