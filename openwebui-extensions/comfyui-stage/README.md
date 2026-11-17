# ComfyUI Image Staging — repo-managed tool

Native Workspace Tool `comfyui_stage` for Open WebUI 0.13.3. It turns an image
attached to the current chat into a path that the ComfyUI MCP tools accept.
No shared mount, no MCP change, no database writes, no model-supplied paths.

## Why this tool exists

The ComfyUI MCP tools (`comfyui_edit_image`,
`comfyui_generate_video_from_image`) take paths on the ComfyUI host. MCP tool
arguments are JSON, so the model cannot forward the bytes of a chat attachment.
This tool resolves the attachment with Open WebUI's own ownership-checked file
APIs and uploads it through ComfyUI's documented `/api/upload/image` endpoint.
The returned `/comfyui/input/<name>` path is already where the MCP expects it,
so no second upload happens.

The model sees the image in the conversation; it does not need this tool to
inspect it, only to stage the bytes for ComfyUI.

## Source of truth

- `../comfyui_stage/__init__.py` — implementation and valve schema.
- `tool.py` — native registration entry; imports `Tools` from the mounted package.
- `install.py` — idempotent registration/update through OWUI's native tool model.
- `test_comfyui_stage.py` — offline regression tests.
- `container_smoke.py` — installed-runtime staging check.

The extension root is mounted read-only at `/app/backend/extensions` and is on
`PYTHONPATH`. Mounting the package alone does not replace the saved database
entry; the one-time registration below is required.

## Behaviour

`stage_chat_image(image_ref="")`:

- Resolves the latest user-uploaded image in the owned, saved conversation
  branch. GIFs, PNG, JPEG (including iPhone MPO photos) and WebP are accepted;
  images are validated and limited to 25 megapixels.
- `image_ref` optionally selects a 1-based index within that latest message,
  or an exact saved-image reference such as `<message_id>:1` when an earlier
  image from the branch is needed.
- Only local upload references and data URLs are read; external URLs are
  rejected. Ownership is checked by the `chat_image_retrieval` helpers.
- Uploads a fresh UUID-named copy to ComfyUI; existing chat storage is not
  modified, renamed, or deleted.
- Returns the exact `image_path` and a short instruction to pass it unchanged
  to `comfyui_edit_image` or `comfyui_generate_video_from_image`.
- Errors are generic and never include filesystem paths, database details, or
  image bytes.

The model should call `stage_chat_image` first and, after receiving its
result, call the MCP tool in a later tool call. Both calls emitted at once
would run in parallel and the second would not see the staged path.

## Configuration

The tool has two administrator valves:

| Valve | Default | Meaning |
|---|---|---|
| `COMFYUI_URL` | `COMFYUI_BASE_URL` env var | ComfyUI proxy base URL reachable from Open WebUI, from the `COMFYUI_BASE_URL` value in `.env` |
| `TIMEOUT` | `120` | Upload timeout in seconds |

The proxy URL is trusted infrastructure; no API key is used. The tool makes
only one outbound request, to `{COMFYUI_URL}/api/upload/image`.

## Tests

Run the offline suite against the mounted package and real dependencies:

```sh
docker exec -e PYTHONPATH=/app/backend/extensions open-webui \
  python -m unittest discover -s /app/backend/extensions/comfyui-stage -p 'test_*.py' -v
git diff --check
```

The optional installed-runtime check builds a synthetic saved chat with a real
owned upload, stages it through ComfyUI, rejects a foreign owner and an invalid
reference, and removes its synthetic chat/file. It creates one file under
`/opt/stacks/comfyui/input/`; the printed path must be deleted afterwards (the
script cannot reach the host mount):

```sh
docker exec -i -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-stage/container_smoke.py
rm -f /opt/stacks/comfyui/input/<printed-name>.png
```

## Registration

Registration and updates run through Open WebUI's native tool model, exactly
like the application's own create/update path. No raw SQL and no manual paste:

```sh
docker exec -i -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-stage/install.py
docker restart open-webui
```

`install.py` loads `tool.py` with `load_tool_module_by_id`, generates the specs
with `get_tool_specs`, and inserts or updates the `comfyui_stage` row. It
preserves the existing owner, access grants, activation and valves on update;
new rows are active and owned by the first admin. Re-running it is idempotent.

After registration:

1. Set `COMFYUI_URL` in the tool valves if `COMFYUI_BASE_URL` is not already
   correct, and keep `TIMEOUT` at its default unless uploads are slow.
2. Attach or select the tool for the models/chats that should stage images.
3. In a private chat, attach an image, ask to edit it or animate it, and
   confirm the model first stages the image (`/comfyui/input/<uuid>.png`) and
   then calls the ComfyUI MCP tool with that path.

Implementation-only updates are deployed through Git and a restart; the
adapter row does not need to be re-created unless `tool.py` changes. Git
manages the code, not Open WebUI permissions, activation, per-model
attachments, or valves.

Keep this package present while the import-based registration is active.
