# Selective chat image retrieval

Repo-managed native Open WebUI **Filter + Tool**, targeting v0.13.0. No extra
service, credentials, outbound HTTP client, or database writes. Installing Python
extensions grants server-side code execution: only administrators should manage
these files and their registration.

## Behaviour

- Keep images from the latest **saved user message containing images**, including
  on text-only follow-ups. Up to eight images in that set, 10 MiB per source.
  Latest-set uploads referenced by bare file ID, file URL or resolved base64 all
  survive; ordinary `files` metadata is preserved too, so photo uploads and image
  editing inputs keep working.
- Omit earlier image content parts and historical tool-result images, leaving
  text intact. The database and original attachments are never modified.
- **Animated-GIF sampling, for every model using this filter:** replace each
  animated GIF in the latest saved image set with up to **four chronological
  JPEG frames**, at most 1536 px on the longest edge. This does not add vision
  capability to text-only models. Samples are spread across playback time using
  frame delays, including the first and last frames when the configured maximum
  is at least two. Short animations send only their available frames. Missing or
  zero delays use 100 ms; labels show the selected frames' actual start timestamps,
  not idealized target times. GIF disposal and transparency are composited before
  resizing; transparency is flattened onto white and original metadata discarded.
- Each sampled frame has a GIF/group number and timestamp. A request-only note
  explains that these are **samples, not the complete animation**: motion, brief
  events, flicker and loop seams can be missed. Converted `files` entries describe
  JPEG stills without the original GIF ID, filename or nested record. Stored GIFs
  and chat display are untouched. Source URLs, bare IDs and resolved bytes match;
  original or already-sampled tool-loop messages normalize idempotently, including
  repeated-pixel frames and GIFs sharing a first frame. Unrecognized original GIF
  payloads fail safe by being pruned, not forwarded.
- GIF decoding is limited to **512 source frames**, **25 million pixels per
  frame**, and a conservative **128 million decoded-pixel work budget** covering
  two passes per GIF. Over-budget, malformed or inaccessible sources reject the
  request rather than silently forwarding the original animation. The latest
  image set may contain at most **16 images after expansion** by default; excess
  produces a clear error suggesting fewer GIFs or fewer samples.
- Single-frame GIFs remain unchanged. Retrieval of an **older GIF** still returns
  one resized first-frame JPEG after approval; the retrieval tool does not expose
  the original animation. **New current-response tool images remain unchanged**
  and are not subject to latest-upload expansion or its image-count cap.
- Include a bounded manifest (last 100 references) with message/image positions.
  Untrusted filenames, captions and URLs are not copied into the system manifest.
- `retrieve_chat_image(image_ref)` requires explicit browser confirmation before
  loading an older image. This confirmation is required even in full tool mode.
  Missing/disconnected clients, non-boolean approvals and timeouts deny access.
- Return a metadata-free JPEG, maximum edge 1536 px, for actual visual inspection.
  PNG/JPEG/WebP/GIF accepted, first frame only, maximum 25 million source pixels.
- Limit retrieval attempts to three per response (including declined attempts),
  serialize concurrent calls, and recheck ownership/branch after confirmation.
- The `request` hook runs again during tool loops: the latest image set, approved
  retrievals, and **new current-response tool images** survive. New tool output
  is verified against the owned response's server-persisted results/attachments;
  a synthetic `user` role alone does not grant an exception. Previously saved
  results on a resumed response are history, not newly generated images.
- Current image-generation outputs are passed through **unchanged**: no resizing,
  JPEG conversion, deduplication, or retrieval-specific 10 MiB source limit.
  Generation JSON, current image-file metadata and UI events remain intact.
  Identical-pixel regenerations are preserved too. Saved generated images stay
  visible; their older multimodal payloads can still be omitted on future turns.
- Historical-image pruning runs in worker threads, hashes base64 representations
  **without decoding** them, and caches repeated URI identities within each pass.
  Per-pass work is capped at 256 MiB of unique image URI characters and 2048
  multimodal image parts; oversize histories raise an explicit error rather than
  silently discarding current images. Current response image-source discovery is
  capped at 64 sources, with a separate 256 MiB resolution-work budget per update.
  Already processed tool results are not reloaded on every tool round.
- Historical duplicate payloads are sent once. A later request must obtain
  approval again for older-image retrieval; current tool execution retains its
  normal Open WebUI permissions and approval policy.

### Sampling valves and upgrade behaviour

| Valve | Default | Allowed values | Purpose |
| --- | --- | --- | --- |
| `gif_max_frames` | `4` | `1`–`4` | Maximum samples per latest-set animated GIF; `1` explicitly sends only the first frame. |
| `max_expanded_images` | `16` | `1`–`32` | Maximum latest-set image count after sampling, including normal images. Excess is rejected, never silently truncated. |

The old `gif_first_frame_models` valve is obsolete and ignored, including when
present in saved registration settings. Sampling now applies across providers.
The static registration adapters have not changed; package-only deployment still
requires just a restart. Adjust valves through the usual Open WebUI function
settings if needed; do not change tool permissions to control sampling.

### Large images and multiple normal uploads

- Multiple PNG/JPEG/WebP uploads in one message form **one retained image set**:
  keep all of them, not just the last image. The source limit is eight images,
  each at most 10 MiB. Text-only follow-ups retain the same set. A later
  image-bearing user message replaces it; older payloads then require retrieval.
- Ordinary latest-set images and their metadata are **not resized or recompressed
  by this filter**. A file over 10 MiB is rejected before decoding/resizing; the
  filter does not rescue oversized sources by loading them without a bound.
  Pixel/work limits above apply to GIF sampling (and the existing single-image
  renderer used by retrieval), not a guaranteed normal-image resolution cap.
- Open WebUI offers optional **Settings > Interface > Image Compression** and
  configurable dimensions. Its documented built-in default is off; compression
  depends on UI/admin configuration and the upload path, not an unconditional
  backend resize. See [official interface defaults](https://docs.openwebui.com/features/administration/interface-defaults/).
  This extension does not alter that setting or recover animation already lost
  by browser-side compression before upload.
- Provider image-count, resolution, request-size and context limits still apply.
  The expansion cap is not a provider-token budget and does not cap fresh tool
  images or approved retrievals. Consider provider limits before raising it.

## Access boundary and limitations

The tool accepts an opaque `message-id:image-number` reference, **not** a file
ID, user ID, chat ID, path or URL. The authenticated user and chat/message IDs
come from Open WebUI's injected server context. Every lookup uses the owned-chat
helper and walks only the ancestors of the current response. No sibling branches,
other chats, other users, shared non-owned chats, or admin ownership bypass.
Attachment-file lookups additionally require file ownership. Local reads are
rooted under `UPLOAD_DIR`, reject symlinks at every path component, and enforce
file type/size limits. Embedded base64 images in owned chat messages also work.
Native raster attachments can have `type: file` and `url` equal to a bare upload
ID; these use the same owned-file lookup as `/api/v1/files/{id}/content` routes.
Neither form accepts arbitrary filesystem paths or fetches external URLs.

This initial version intentionally does **not** support external image URLs,
absolute upload URLs, S3/cloud-storage paths, temporary/unsaved chats, assistant
generated-image retrieval, or arbitrary markdown images. Unsupported latest
images fail closed rather than silently sending all history. Retrieval errors
are sanitized. There are no per-user/global caches or image payload logs.

The extension itself never writes chat/file records. Open WebUI's normal
tool-result plumbing may save the returned resized image as an additional
attachment; the original is still untouched. These saved tool copies are also
excluded from later provider requests unless their bytes are authorized again.

Keeping the latest image set still sends those images on follow-ups, as normal
Open WebUI does. Approval covers **older-image retrieval**, not initial upload.
Image contents remain untrusted input and can contain prompt injections; this
extension cannot eliminate that model-level risk. It also cannot guarantee a
provider capacity error will disappear.

Open WebUI resolves stored image URLs before inlet filters. Therefore this
extension reduces the **provider payload**, not necessarily Open WebUI's own
historical image-loading cost. It never fetches arbitrary URLs itself.

## Installation (explicit, scoped rollout)

1. Run the tests below. Review the code before installing it.
2. Apply the Compose read-only mount (requires recreating `open-webui`):
   ```sh
   docker compose up -d --no-deps open-webui
   ```
   The whole `openwebui-extensions/` directory is mounted read-only at
   `/app/backend/extensions`, which is on `PYTHONPATH`. Runtime packages live
   directly under that root; `chat_image_retrieval` is shared by both entries.
   Do not install third-party requirements: Pillow is already in this image.
3. As an administrator, create a **Filter Function** using the contents of
   `filter.py`, and a **Workspace Tool** using `tool.py`. Keep both private.
   Suggested IDs: `chat_image_retrieval_filter`, `chat_image_retrieval`.
4. Enable the filter, **not globally**. Attach/select it and the tool together on
   one private test model/chat. Use a vision-capable model and **native function
   calling**. Keep the model's Vision capability enabled.
5. Perform the acceptance checks below before enabling it elsewhere. Disable the
   filter and deselect the tool together to revert to ordinary image handling.

The two small entry files contain frontmatter and a plain import of the shared
package. They are native registration adapters, not standalone implementations.
There is no hot reload, module-cache manipulation, or upstream Open WebUI patch.

- **Package-only updates:** update the repo and run
  `docker compose restart open-webui` to start a fresh Python process.
- **Compose mount/environment changes:** run
  `docker compose up -d --no-deps --force-recreate open-webui`; a plain restart
  does not apply Compose changes.
- **Entry changes:** explicitly update the two registered contents from
  `filter.py` / `tool.py` and restart. This is only needed when those entries
  actually change (imports, registration metadata or the native interface), not
  for every package update.

Registration stays explicit; mounting a package does not activate it. Back up
both the repository and Open WebUI data. A database-only export omits the
implementation. Keep the extension code present on the deployed branch; do not
switch to a branch that lacks it while these registrations are active.

## Tests

From the repository root, with Python 3.12+ and Pillow:

```sh
PYTHONPATH=openwebui-extensions python3 -m unittest discover -s openwebui-extensions/chat-images -p 'test_*.py' -v
docker compose config --quiet
```

For a synthetic integration smoke test in the running Open WebUI container
(temporary files only; no provider requests, user-data reads or registration):

```sh
python3 openwebui-extensions/chat-images/container_smoke.py
```

After deployment, verify the **actual mounted package** and read the two
registered entries for exact agreement with the repo (no database writes or
provider calls; all chat/file adapters still use synthetic fixtures):

```sh
python3 openwebui-extensions/chat-images/container_smoke.py --installed
```

### Acceptance checks before wider rollout

Use a **new saved private test chat**, not a sensitive existing conversation.

1. Upload one clearly labelled test image, then a different one in a later turn.
2. Ask about the latest image: confirm only the newest set is in the outbound
   provider request. Do not log base64 payloads or credentials.
3. Ask to inspect the earlier image: confirm the browser approval appears. Deny
   it first; no older-image payload should be sent.
4. Repeat and approve. Confirm a real image content part reaches the provider,
   and that the model can identify a visual feature not supplied in text.
5. Send a text-only follow-up: the retrieved older image must be absent again.
6. Attempt sibling-branch, other-chat and other-user references: deny all. Test
   revocation/deletion while the approval dialog is open.
7. Check tool-approval resume, compaction, regeneration and the actual provider's
   vision/tool format. A resumed server request intentionally requires fresh
   image authorization; fail closed if the filter context is unavailable.
8. Generate/edit an image using the normal image-generation tool: verify the UI
   image and JSON result remain intact. If a tool returns multimodal image parts,
   verify those reach the provider unchanged, including identical-pixel repeats.
    Verify a later turn still omits older payloads without deleting displayed images.
9. Upload a synthetic GIF with at least six distinct frames and varying delays:
   verify up to four timestamped JPEG parts, first/last coverage, one sampling
   disclosure and no original GIF file tag. Repeat with a non-Mistral model and
   a tool-call round; labels and images must not accumulate. Four samples do not
   prove the model has seen the complete animation.
10. Upload several ordinary images together and follow with text only: keep the
    full latest set unchanged. Try a source over 10 MiB, more than eight sources,
    and GIFs exceeding the expansion/work budget: expect clear errors, not a
    fallback that replays all originals.

Unit and synthetic runtime tests do not substitute for these browser/provider
checks. Provider checks incur normal inference costs and are not automated here.
