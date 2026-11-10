# SearXNG Image Search — repo-managed tool

Native Workspace Tool `searxng_image_search` for Open WebUI 0.13.x.
`requests` and Pydantic 2 are already installed; no extra service or credentials.

## Source of truth

- `../searxng_image_search/__init__.py`: implementation and valve schema.
- `tool.py`: native registration entry; imports `Tools` from the mounted package.
- `test_searxng_images.py`: offline regression tests.

The existing extension root is mounted read-only at `/app/backend/extensions`
and is on `PYTHONPATH`. **Mounting the package alone does not replace the
existing database entry.** The one-time registration step below is required.

## Behaviour

The tool still exposes `image_search(query, count=0, safesearch=-1,
time_range="")`. It calls the same SearXNG instance at `http://searxng:8080`,
with `categories=images` and `format=json`. Defaults remain four images,
moderate safesearch and a 20-second HTTP timeout; output is capped at eight
images. No engines are forced: SearXNG's **image-category** defaults apply,
not its general/news engines. Time filters can exclude unsupported engines.

Only administrators should edit code or the trusted SearXNG URL valve. The
backend fetches SearXNG, not result images/pages; the browser contacts image
hosts when rendering, exposing its IP. HTTP(S) links and captions are sanitized,
but remote addresses/content are not trusted or guaranteed safe. Safesearch
depends on engine support. Output is Markdown, not proof of model vision or
image licensing. Responses are limited to 2 MiB/200 candidates; the HTTP timeout
is per connect/read, not a total deadline. Redirects are rejected.

## Tests

Run the offline suite against the existing mounted package and real dependencies:

```sh
docker exec -e PYTHONPATH=/app/backend/extensions open-webui \
  python -m unittest discover -s /app/backend/extensions/searxng-images -p 'test_*.py' -v
git diff --check
```

## One-time installation (administrator)

1. In **Workspace → Tools**, edit the existing **SearXNG Image Search** tool and
   replace its code with `tool.py`. Keep the same ID, access permissions,
   activation and model attachments. Do not delete/recreate it. Save normally so
   Open WebUI regenerates metadata/specs through its supported application path.
   Do not write the database directly.
2. Restart `open-webui` to clear imported-module and tool caches. No Compose
   recreation is needed: the extension directory is already mounted.
3. Check the registered code equals `tool.py`, the mounted implementation imports,
   and the registered tool still exposes only `image_search`. In a private test
   chat, select the tool, ask for an image and verify inline rendering/source links.

For later **implementation-only updates**, deploy the Git changes and restart
`open-webui`; the adapter does not need to be pasted again. Adapter/manifest
changes require explicitly updating the saved entry too. Git manages the code,
not Open WebUI's permissions, activation or per-installation valves.

Keep this package present while the import-based registration is active.
