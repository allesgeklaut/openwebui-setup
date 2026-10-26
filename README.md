# Open WebUI Self-Hosted Stack

A self-hosted AI chat interface with extended capabilities, powered by [Open WebUI](https://github.com/open-webui/open-webui) and additional services.

## Services Included

| Service | Description | Port |
|---------|-------------|------|
| **open-webui** | Main UI for AI conversations | 3001 |
| **mcpo** | MCP server gateway (Trilium, filesystem, deep-research, …) | internal |
| **searxng** | Privacy-respecting search engine | 8081 |
| **research-search** | Web search + URL fetch via the Mistral "Web Scout" agent | internal |
| **tika** | Document extraction & preprocessing | internal |
| **audiobookshelf-mcp** | Audiobookshelf podcast connector (authenticated MCP for Open WebUI) | internal |

Voice chat (TTS/STT) and RAG embeddings are **not** separate services: they
route through litellm to Mistral (Voxtral TTS + transcription, Mistral Embed).
Web search and URL fetch **are** a service (research-search), but the actual
searching happens in a Mistral agent — see
[Web search & page fetching](#web-search--page-fetching-research-search).

## Prerequisites

- Docker & Docker Compose installed
- Ollama running on your network (default: `http://<your-LAN-IP>:11434`)
- Cloudflare Access configured for OAuth/SSO (optional)

## Setup

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your actual values (API keys, URLs, etc.)
```

See [`.env.example`](.env.example ) for all available options and descriptions.

### 2. Start Services

```bash
./start.sh
```

This script:
1. Generates `mcpo/config.json` from the template with your `.env` values
2. Starts all Docker services in detached mode

### Alternative Manual Steps

If you prefer manual control:

```bash
# Generate config (first time only, or after .env changes)
./start.sh

# Or just start containers without regenerating config
docker compose up -d
```

## Configuration

### MCP Servers (`mcpo/`)

The `mcpo` service connects to various Model Context Protocol servers. The configuration is generated from [`mcpo/template_config.json`](mcpo/template_config.json ) using your environment variables.

MCP runtime packages have exact direct versions in [`mcpo/npm/package.json`](mcpo/npm/package.json)
and [`mcpo/requirements.txt`](mcpo/requirements.txt). The template uses `run-npx.sh`
and `uvx --with-requirements` so these tracked manifests are the version source of
truth. Dependabot checks both manifests and the three selected container images
weekly. MCPO currently publishes commit tags rather than release-version tags;
its image updates may require manual tag selection. Review the PRs rather than
automatically merging them. Version tags can still be replaced by their publisher.
These pins do not
lock all transitive dependencies or remove existing dependency advisories.
After changing the template, `./start.sh` regenerates the secret config and
starts/recreates the stack. Do not edit the generated secret config directly.

**Available MCP servers:**
- **filesystem**: Access to `/stacks` directory (mounted read-only)
- **trilium**: Integration with Trilium note-taking app (requires `TRILIUM_API_KEY`)
- **searxng**: Web search via the SearXNG service
- **trainlocks**: Training log integration
- **deep-research**: Deep web research tool fronting the "Deep Researcher"
  Mistral Studio agent (`web_search_premium`). One tool,
  `deep_research(question)`: runs several refined searches with page opens and
  returns a structured, cited report (30–120 s, ~$0.10–0.30 per call).
  The same server is also registered in `opencode.json`, so coding agents
  can call it. Both `MISTRAL_API_KEY` and the agent id come from
  `/opt/secrets/mistral.env` (start.sh's config renderer for mcpo; the script's
  file fallback for opencode).

### SearXNG (`searxng/`)

Privacy-respecting metasearch engine. Configuration is in [`searxng/config/settings.yml`](searxng/config/settings.yml ).

### Audiobookshelf MCP (`audiobookshelf-mcp/`)

Authenticated Streamable-HTTP MCP connector for Audiobookshelf podcast
management, built from [`audiobookshelf-mcp/`](audiobookshelf-mcp/) and mounted
into Open WebUI as a private tool server. Full tool, safety, and configuration
documentation: [`audiobookshelf-mcp/README.md`](audiobookshelf-mcp/README.md).

The Compose service starts with the stack, so a fresh deployment needs these
prerequisites first:

- the external network `audiobookshelf_default` (the network of an existing
  Audiobookshelf Compose deployment; adjust `compose.yml` if yours differs),
- secret files `/opt/secrets/audiobookshelf-mcp/abs_api_key` and
  `/opt/secrets/audiobookshelf-mcp/mcp_token`,
- `ABS_ALLOWED_LIBRARY_IDS` set to your podcast library ID(s) in the local,
  gitignored `.env` (see `.env.example`; the service refuses to start with it
  unset).

The repository's `compose.yml` enables podcast write tools for this service;
make sure that is intended before deploying. Register the MCP endpoint
(`http://audiobookshelf-mcp:8000/mcp`) as a private tool-server connection in
Open WebUI, restricted to the intended account.

### Web search & page fetching (`research-search/`)

All web access goes through **Mistral** (EU) — Exa was dropped for privacy,
and there is no direct HTTPS fetching anywhere in this stack. The
`research-search` service serves both of Open WebUI's `external` web
contracts from one process, backed by the "Web Scout" Mistral Studio agent
(`web_search`):

- **Search** (`web.search.engine=external`): the scout runs the query (up to
  3 refined searches), this service harvests the raw results —
  `[{link, title, snippet}]`, SERP-style only. The agent never answers the
  question: synthesis stays with whichever chat model is in use, so the
  research quality composes with every model. When a result matters, the
  chat model requests the full page via the fetch loader.
- **Fetch** (`web.loader.engine=external`): every URL goes through the scout
  agent, which opens the page — target sites see Mistral's fetchers, never
  this stack, and there is no outbound-fetch/SSRF surface here. Pages capped
  at 32K chars by Mistral's tool channel are flagged
  (`metadata.truncated` + note in `page_content`). Only if the open never
  happens — including refused opens, where the fetcher reports
  `can_open=false` (robots.txt-banned sites like instagram.com) — do the
  page's own SERP snippets serve as text; with no SERP entry either, the
  fetch 502s rather than returning the refusal as page text.

Wired via these DB keys:

| Key | Value |
|---|---|
| `web.search.engine` | `external` |
| `web.search.external_web_search_url` | `http://research-search:8080/search` |
| `web.search.external_web_search_api_key` | token in `/opt/secrets/research-search.token` |
| `web.loader.engine` | `external` |
| `web.loader.external_web_loader_url` | `http://research-search:8080/fetch` |
| `web.loader.external_web_loader_api_key` | token in `/opt/secrets/research-search.token` |

Cost per search-enabled chat turn: ~$0.03–0.09 for the search plus ~$0.06
per fetched page (each fetch is a scout conversation). Details, config and
limits: [`research-search/README.md`](research-search/README.md).

Pre-change backup of all `web.search.*` / `web.loader.*` rows (incl. the
Exa key): `data/backups/config-web-backup-2026-10-20.json`. Rollback: stop
open-webui, restore the rows, `git revert` the exa-loader removal commit,
`docker compose up -d`.

## Audio & RAG embeddings

Voice chat and document embeddings route through litellm (same gateway and
key as chat) to Mistral's audio/embedding APIs — no local Kokoro/Whisper/
sentence-transformers anymore:

- **TTS**: Voxtral TTS (`voxtral-tts` on litellm, ~$0.016/1k chars). The
  TTS Voice field in OpenWebUI accepts readable names, thanks to the
  `VoxtralVoiceAlias` callback in the litellm stack (`custom_callbacks.py`),
  which rewrites them to the UUIDs Mistral actually accepts:
  - `auto` (current default): language routing — German text is spoken by
    **Jo** (the only native German preset), anything else by **Jane** (en-GB).
    OpenWebUI splits replies on punctuation, so detection runs per chunk and
    mixed-language answers use the right voice per sentence; ambiguous
    chunks default to Jo.
  - `jo` / `de`, `jane` / `en`, and mood variants: `jane-sarcasm`,
    `paul-cheerful`, `oliver-curious`, `marie-happy`, … (full map in
    `custom_callbacks.py`).
  - Mistral Studio display names ("Jane") do **not** work — only UUIDs,
    readable aliases (above), or OpenAI names (`alloy`, `nova`, …, which
    litellm maps to English presets; `nova`/`shimmer` are both Jane-Sarcasm).
- **STT**: Voxtral batch transcription (`voxtral-stt`, $0.003/min) — better
  German accuracy than the previous local Whisper `base`.
- **Embeddings**: Mistral Embed (`mistral-embed`, $0.10/M tokens, 1024-dim),
  a large quality jump over the previous local all-MiniLM (384-dim).

These are Open WebUI ConfigVars: the `webui.db` `config` rows win over the
compose env on an existing install (the env matches for fresh deploys). The
rows changed in place (backup: `data/backups/config-audio-rag-backup-20261019.json`):

| Key | Value |
|---|---|
| `audio.tts.openai.api_base_url` / `audio.stt.openai.api_base_url` / `rag.openai.api_base_url` | `http://192.168.0.46:4000/v1` |
| `audio.tts.model` / `audio.stt.model` | `voxtral-tts` / `voxtral-stt` |
| `audio.stt.engine` / `rag.embedding_engine` | `openai` |
| `audio.tts.voice` | `auto` (readable names/UUIDs also work — see above) |
| `rag.embedding_model` | `mistral-embed` |
| `*.api_key` | the litellm master key |

**Important**: the embedding model changed (384→1024 dims), so existing
documents/knowledge bases need re-embedding — Admin → Documents → **Reindex**
(or "Re-sync" per knowledge base). Until then, retrieval over old uploads
errors; new uploads work immediately.

Rollback to the local setup: restore the backup JSON rows above (engine `""`
for STT/RAG, `sentence-transformers/all-MiniLM-L6-v2` as embedding model,
kokoro TTS values), `git revert` the kokoro service removal in `compose.yml`,
and reindex again.

### Image generation & editing (`workflows/`)

Open WebUI's native ComfyUI engine drives the local ComfyUI stack
(`/opt/stacks/comfyui`) running **Qwen-Image-2.1** for both generation and
editing. The active config lives in Open WebUI's config DB
(`data/webui.db`, key/value `config` table), which is gitignored — so the
workflows are mirrored here for reproducibility:

- [`workflows/qwen_image_2_1_t2i_api.json`](workflows/qwen_image_2_1_t2i_api.json) — text-to-image (plain)
- [`workflows/qwen_image_2_1_edit_api.json`](workflows/qwen_image_2_1_edit_api.json) — image edit (plain)
- [`workflows/qwen_image_2_1_pe_t2i_api.json`](workflows/qwen_image_2_1_pe_t2i_api.json) — text-to-image with the Prompt Enhancer
- [`workflows/qwen_image_2_1_pe_edit_api.json`](workflows/qwen_image_2_1_pe_edit_api.json) — image edit with the Prompt Enhancer

- [`workflows/qwen_image_2_1_turbo_t2i_api.json`](workflows/qwen_image_2_1_turbo_t2i_api.json) — text-to-image, Turbo UNet, 8 steps (**active in Open WebUI**)
- [`workflows/qwen_image_2_1_turbo_edit_api.json`](workflows/qwen_image_2_1_turbo_edit_api.json) — image edit, Turbo UNet, 8 steps (**active in Open WebUI**)
- [`workflows/qwen_image_2_1_turbo_pe_t2i_api.json`](workflows/qwen_image_2_1_turbo_pe_t2i_api.json) — text-to-image, Turbo UNet + Prompt Enhancer
- [`workflows/qwen_image_2_1_turbo_pe_edit_api.json`](workflows/qwen_image_2_1_turbo_pe_edit_api.json) — image edit, Turbo UNet + Prompt Enhancer

All use the int8 text encoder (`qwen3vl_8b_int8_convrot`). The Turbo UNet
(`qwen_image_2.1_turbo_int8_convrot.safetensors`, from `Comfy-Org/Qwen-Image-2.1`)
has no encoder of its own and shares the same Qwen3-VL encoder. The Turbo
checkpoint documents **CFG 1** and an **8-step schedule baked into the model**;
the Turbo workflows reproduce both (see below).
A single warm A/B run (seed 7, one prompt) took ~24–28 s versus ~80 s for the base
at 40 steps — one uncontrolled sample, not a benchmark.
The `_pe_` workflows (base and Turbo) prepend the official Qwen-Image-2.1 Prompt
Enhancer (Qwen3.5-VL-9B; see `../comfyui/README.md`).

**Turbo sampling schedule.** The official checkpoint stores its own 8-step sigma
schedule (`Qwen-Image-2.1-Turbo/model_index.json`, shift 1.0), and its card says
other schedules have not been evaluated. `KSampler` cannot load that schedule, so
the Turbo workflows use a `ManualSigmas` node carrying the checkpoint's list
(`1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0`)
into `SamplerCustom` (euler). That is the documented schedule, not ComfyUI's own
`simple` (which applies a ~1.15 `shift`).

Comfy-Org has since published official Turbo templates
(`Comfy-Org/workflow_templates`, `image_qwen_image_2_1_turbo_{t2i,image_edit}.json`)
using the byte-identical sigma list, euler, cfg 1, and the same int8/VAE files —
the day-one integration here matches them. Generation keeps the native 2K pairing
(2048×2048 latent + encode `resolution` 2048, mirrored by
`image_generation.size`). Edits follow the official template: the sampling
latent comes from `TextEncodeQwenImage21`'s `latent` output, which is sized to
the input image (within the encode node's `resolution` budget — see the
resolution note under the node maps), so the output preserves the input's
aspect ratio; the UI size box feeds that budget through its width.

**Open WebUI uses the plain Turbo workflows**
(`qwen_image_2_1_turbo_{t2i,edit}_api.json`). It already has its own
prompt-rewrite step (`image_generation.prompt.enable`), so adding the PE would
double-enhance and cost an extra 70–340 s per image. The PE is opt-in
(`enhance=True`) in the ComfyUI MCP server, which otherwise also runs Turbo. To
switch Open WebUI to the PE anyway, point the `*.comfyui.workflow` keys at the
`turbo_pe` files, use the PE node maps below, and consider disabling Open
WebUI's own rewrite so the PE is the single enhancer.

Both `image_generation.comfyui.base_url` and `images.edit.comfyui.base_url`
point at `http://${LAN_IP}:8189` — the ComfyUI lifecycle proxy in the litellm
stack, **not** ComfyUI's own `:8188`. The proxy starts the `comfyui` container
on demand (the first image after idle costs ~8 s to start the container plus
~40–60 s for the first model load) and stops it after 15 minutes idle, so the
GPU is not held when nobody is making images. The `COMFYUI_BASE_URL` env default is overridden by
the DB value. See `../litellm/README.md`. The `runpod-bridge` service is
retained but is no longer used for image work.

DB keys to restore (Admin → Settings → Images, or the `config` table directly):

| Key | Value |
|---|---|
| `image_generation.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_t2i_api.json` |
| `images.edit.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_edit_api.json` |
| `image_generation.model` / `images.edit.model` | `qwen_image_2.1_turbo_int8_convrot.safetensors` |
| `image_generation.size` | `2048x2048` |
| `images.edit.size` | `1024x1024` — its **width** is written into the encode node's `resolution` (input-image budget); request `size` overrides it per edit |
| `image_generation.steps` | unused for Turbo — the schedule is fixed by `ManualSigmas`; left at `8` for the UI. Restore to `40` when rolling back to the base. |
| `*.comfyui.base_url` | `http://${LAN_IP}:8189` (lifecycle proxy) |
| `*.comfyui.api_key` | empty |

Rollback to the base model: point the two `*.comfyui.workflow` keys at
`qwen_image_2_1_{t2i,edit}_api.json`, set both `model` keys to
`qwen_image_2.1_int8_convrot.safetensors`, set the two `*.size` keys back to
`1024x1024`, and set `image_generation.steps` to `40`. Then restart Open WebUI.

Storage note when writing the `config` table directly: `*.comfyui.workflow` is
a JSON **string** whose content is the workflow file above, whereas
`*.comfyui.nodes` is a raw JSON **array**. Double-encoding `nodes` (or leaving
`workflow` unquoted) still loads but fails at generation time.

Precedence note: Open WebUI **overrides** the workflow's UNet node with
`image_generation.model` / `images.edit.model`, so the `unet_name` inside the
workflow file is effectively decorative — changing it alone does nothing.
Update the `model` key as well.

Node maps (`*.comfyui.nodes`); ids refer to the active **Turbo** workflows above.

- generation (Turbo): `prompt`→4, `negative_prompt`→4, `model`/`unet_name`→1, `width`/`height`/`n`→5, `seed`→6 (`noise_seed`)
- edit (Turbo): `image`→4 then the reference slots 12..20 positionally, `prompt`→5, `model`/`unet_name`→1, `seed`→6 (`noise_seed`), `width`→5 with `"key": "resolution"` (the size box's width is the edit resolution budget; height is ignored)

Neither maps `steps`: the Turbo schedule is fixed by `ManualSigmas`, and
`SamplerCustom` has no `steps` input. The base workflows
(`qwen_image_2_1_{t2i,edit}_api.json`, rollback) still use `KSampler`, where
generation maps `steps`/`seed`→6 and edit maps `seed`→6.

**Multi-reference editing.** Qwen-Image-2.1 edits accept up to **10 images**
(target + 9 references) and its editing is reference-based generation — the
model composes new scenes from reference people/products (its showcase
includes a group photo from six portrait references). The chat `edit_image`
tool takes `image_urls` as a **list** plus an optional `size`, and
`/api/v1/images/edit` accepts `"image": [...]`, so this works end-to-end:
address references in the prompt as `<image2>` .. `<image10>` (`<image1>` is
the target). The edit workflows carry ten `LoadImage` slots; unfilled slots
keep the sentinel `multi_ref_unused.png`, and the ComfyUI lifecycle proxy
strips those nodes (and links to them) from every `POST /prompt` before
ComfyUI validates it (`../litellm/comfyui_proxy/prompt_clean.py` — Open
WebUI's applier can write filenames into nodes but can neither create nor
remove nodes). The ComfyUI MCP `edit_image` tool exposes the same thing via
its `reference_images` parameter.

PE workflows (`prompt` goes to the rewrite node, not `TextEncodeQwenImage21`):

- PE generation: `prompt`→2, `negative_prompt`→6, `model`/`unet_name`→3, `width`/`height`/`n`→7, seed→8 (`noise_seed` for the Turbo `SamplerCustom`; `seed`+`steps` for the base `KSampler`)
- PE edit: `image`→3 then reference slots 16..24 (Turbo) / 14..22 (base), `prompt`→2, `model`/`unet_name`→4, seed→9 (same key rule as PE generation)

(PE node ids: CLIPLoader PE=1, rewrite=2, then UNET=3/4, generation
CLIPLoader, VAE, `TextEncodeQwenImage21`, `EmptyLatentImage` (t2i only),
sampler, `VAEDecode`, `SaveImage`, `PreviewAny`, then the reference slots.
See the JSON files.)

Generation keeps `TextEncodeQwenImage21.resolution` at the workflow's baked
2048 (native 2K pairing); the node map does not set it, so changing the
generation size in the UI only rewrites `EmptyLatentImage` (`width`/`height`,
node 5) — use the baked size, or change `resolution` in the workflow (and the
DB workflow) to match. For editing, the map wires `width` into the encode
node's `resolution` (the input-image size budget: aspect is preserved, ~1 MP
at 1024, native 2K at 2048 — a 2048-budget edit takes ~7 min on the 16 GB
card, so the default is 1024), with `images.edit.size` as the fallback when a
request sends no size.

Node-map gotchas — violating any of these fails image requests with a generic
400:

- The **edit** node map must **not** include `negative_prompt`: `ComfyUIEditImageForm` has no such field and the node-map applier dereferences it unconditionally.
- The **edit** node map must **not** include `steps`: the edit request path does not send it, so mapping it writes `None` and ComfyUI rejects the prompt. (The Turbo edit workflow takes its schedule from `ManualSigmas`; the base edit workflow's own default, 40, applies.)
- The edit map's `width` entry must use `"key": "resolution"` (the edit workflows sample from the encode node's latent, so there is no `EmptyLatentImage` canvas to size), and `images.edit.size` must stay non-empty: the applier writes `payload.width` unconditionally, so an empty size means `None` lands in `resolution` and ComfyUI rejects the prompt.
- The Turbo workflows seed through `SamplerCustom`, whose input is named `noise_seed` — so the `seed` map entry must use `"key": "noise_seed"` (the base `KSampler` uses `"key": "seed"`).

Changing the DB directly does **not** require an Open WebUI restart: since
v0.12.4 the images router reads its config per request
(`get_image_config()` → `Config.get_many()` queries the config table on every
call), so changes apply immediately — the browser only needs a reload to
refresh the settings UI.

**ChatGPT subscription adapter (alternative).** Between 2026-10-18 and 2026-10-26
Open WebUI's image engines were switched to the `chatgpt-images` shim
(`../litellm/chatgpt_images/`). The shim is retained as an alternative: run its
`configure_webui.py` to switch to it again. To switch back to ComfyUI, restore
the keys per this section — or, for the exact adapter-era rows,
`data/backups/config-image-gen-comfyui-backup-20261026.json` (secret-bearing,
gitignored; contains the gateway key, delete once no longer needed).

## Ports

| Port | Service | Access |
|------|---------|--------|
| 3001 | Open WebUI | External (browser) |
| 8081 | SearXNG | External (browser) |

Other services run internally and are not exposed.

## File Structure

```
├── compose.yml          # Docker Compose configuration
├── .env.example         # Environment variables template
├── .env                 # Your actual environment variables (gitignored)
├── start.sh             # Helper script to generate config & start services
├── mcpo/
│   ├── template_config.json  # MCP config template with ${VAR} placeholders
│   ├── config.json           # Generated from template (gitignored, contains secrets)
│   ├── deep-research/        # Deep Research MCP server (Mistral Studio agent + premium search)
│   ├── trainlocks/           # Training log MCP server
│   └── trilium/              # Trilium MCP integration
├── searxng/
│   └── config/
│       └── settings.yml      # SearXNG configuration
├── research-search/         # Web search + URL fetch via the Mistral "Web Scout" agent
│   ├── app.py               # stdlib HTTP adapter (search + fetch contracts)
│   ├── test_app.py          # unit tests for the parsing helpers
│   ├── Dockerfile
│   └── README.md
├── audiobookshelf-mcp/      # Audiobookshelf podcast MCP connector (17 tools, writes opt-in)
│   ├── src/audiobookshelf_mcp/
│   ├── tests/
│   ├── Dockerfile
│   └── README.md
├── workflows/               # ComfyUI API workflows for Open WebUI (Qwen-Image-2.1)
│   ├── qwen_image_2_1_t2i_api.json
│   ├── qwen_image_2_1_edit_api.json
│   ├── qwen_image_2_1_pe_t2i_api.json   # + Prompt Enhancer
│   ├── qwen_image_2_1_pe_edit_api.json  # + Prompt Enhancer
│   ├── qwen_image_2_1_turbo_t2i_api.json   # Turbo UNet, 8 steps (active)
│   ├── qwen_image_2_1_turbo_edit_api.json  # Turbo UNet, 8 steps (active)
│   ├── qwen_image_2_1_turbo_pe_t2i_api.json   # Turbo + Prompt Enhancer
│   └── qwen_image_2_1_turbo_pe_edit_api.json  # Turbo + Prompt Enhancer
└── data/                    # Open WebUI data & cache (gitignored)
```

## Troubleshooting

### MCP server not connecting?

Check the generated config:
```bash
cat mcpo/config.json
```

Verify environment variables are loaded:
```bash
docker logs mcpo --tail 50
```

### Regenerate config after `.env` changes

```bash
./start.sh
```

## License

This project is provided as-is. Open WebUI and its dependencies have their own licenses.
