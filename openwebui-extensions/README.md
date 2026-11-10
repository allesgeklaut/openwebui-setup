# Repo-managed Open WebUI extensions

This whole directory is mounted **read-only** at `/app/backend/extensions`.
Compose adds that directory to `PYTHONPATH`, so runtime packages placed directly
here can be imported without adding more mounts or per-package import paths.
Only trusted, administrator-reviewed Python code belongs here.

- `chat_image_retrieval/` — shared runtime implementation of the image filter/tool.
- `chat-images/` — native registration entries, tests and
  [installation/deployment documentation](chat-images/README.md).
- `searxng_image_search/` — runtime implementation of the image-search tool.
- `searxng-images/` — its registration entry, tests and
  [installation/review documentation](searxng-images/README.md).

Mounting code does **not** register or activate extensions. Native Function/Tool
entries are stored by Open WebUI in its database and import the runtime package.
No upstream application files are replaced or patched.

For package updates, restart `open-webui`. For mount/environment changes,
recreate it with `docker compose up -d --no-deps --force-recreate open-webui`.
Keep both this repository and Open WebUI data in backups.
