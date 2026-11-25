# Automatic ComfyUI video delivery

Native global OWUI filter, verified on v0.13.5. It leaves the MCP poster and
download URLs intact, but automatically imports the MP4 into owned chat storage
when the response finishes. The model receives a notice that the video is saved
as an attachment and is told to show it with OWUI's native `display_file` tool
exactly once, falling back to the download link if display fails. The filter
itself adds no player markup; presentation is the native display tool's alone.
For GIF generation it attaches the source MP4; GIF delivery is unchanged.

## Model guidance

OWUI normally adds “Here are the images from the tool results above. Please
analyze them.” after tool images. For a generation-only request this can divert
the model into unsolicited inspection or regeneration.

The request filter replaces that exact synthetic text with neutral preview
context only when it follows an authorized current video tool result and every
image matches that result's owned poster(s), including resolved base64 versions.
It does not rewrite genuine earlier user prompts, explicit analysis requests,
mixed/unrelated image batches, still-image or GIF results.

Guidance tells the model to deliver the existing attachment/download link, ask
before regenerating, and not claim to have watched a clip from its poster. The
attachment notice lists which URLs are not yet displayed (owned server-side
`display_file` results mark them displayed) and instructs one native `display_file`
per pending attachment, with the download link as the fallback. For requested
inspection/editing, use the returned HTTP URL in the terminal and sample/process
the clip rather than search for host-only output paths.
These are instructions, not a tool-execution permission gate; a fresh
external-model chat remains the behavioral acceptance check.

Guidance and attachment notices extend existing system text rather than append
separate system messages: OWUI v0.13.3's Responses converter keeps only the last
system message. Existing policy and poster bytes are preserved.

## Boundaries

- Sources must come from paired, completed `comfyui_generate_video`,
  `comfyui_generate_video_from_image`, or `comfyui_generate_gif` results in the
  server-side current assistant response. Saved chat ownership is checked before
  exact chat/message-matched live response buffers are read.
- Incoming model messages, arbitrary URLs/paths, unpaired results, and other
  chats/responses do not authorize file reads.
- Only production filenames directly under the read-only
  `/app/backend/comfyui-media` mount are accepted. Symlinks/traversal are rejected;
  each file must have an MP4 signature, at most 32 MiB, four sources per response.
- Uses native upload/storage, owned file records, chat persistence and
  `chat:message:files` events. Request-local locking/caching and saved source
  metadata prevent duplicates during normal tool rounds/resumes. Separate
  simultaneous requests for the same assistant message are not transactionally
  deduplicated.
- Failures retain the original URL/poster and do not fail generation. The source
  file is not moved/deleted; OWUI stores a separate copy with normal chat access.
- No upstream OWUI files or image-history pruning behavior are changed.

## Rendering notes (historical)

Earlier versions appended a block-markup player (`<video>` newline native-file
URL newline `</video>`) to legacy `content` and Responses `output`. On v0.13.3,
`{{VIDEO_FILE_ID_uuid}}` expands to an empty src-style element that renders as
text; inline `<video>URL</video>` also renders as text. That fallback was
browser-verified but has been retired in favor of the native `display_file`
presentation; the filter now never appends player markup.

## Installation/update

Compose mounts production outputs read-only and the extension source directory.
Apply mount changes, register through native OWUI APIs, then refresh caches:

```sh
docker compose up -d --no-deps --force-recreate open-webui
docker exec -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-media/install.py
docker restart open-webui
```

Registration ID: `comfyui_media`, active and global. To disable, toggle this
function off in OWUI administration; the existing MCP result links still work.
Runtime package updates require a restart. No additional network service,
credentials, broad host mount, or model calls are needed.

## Verification

From `webui/`:

```sh
PYTHONPATH=openwebui-extensions python3 -B -m unittest discover \
  -s openwebui-extensions/comfyui-media -p 'test_*.py' -v
docker exec -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-media/guidance_smoke.py
docker exec -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-media/container_smoke.py
```

`guidance_smoke.py` uses the installed serializers and isolated mocked database
reads/live buffers to reproduce the synthetic prompt and verify corrected final
Responses input. It preserves original user/system text and poster bytes, uses
temporary files only, and makes no chat/file database writes or provider calls.

The installed test creates one private synthetic chat and native uploaded copy
of an existing production clip. It checks real storage, ownership, live-stream
provenance, events, persistence, resume/idempotence, and the registered filter
pipeline without provider inference. It retains only that test chat for browser
verification; remove it and its uploaded copy afterwards:

```sh
docker exec -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-media/container_smoke.py --cleanup
```

Requires the verified fixture
`ltx25_mcp_b80fde435f_1024x1024_49f_00001_.mp4` in the production output folder.
