# Open WebUI Self-Hosted Stack

A self-hosted AI chat interface with extended capabilities, powered by [Open WebUI](https://github.com/open-webui/open-webui) and additional services.

## Services Included

| Service | Description | Port |
|---------|-------------|------|
| **open-webui** | Main UI for AI conversations | 3001 |
| **mcpo** | MCP server gateway (Trilium, filesystem, search) | internal |
| **searxng** | Privacy-respecting search engine | 8081 |
| **exa-loader** | External web loader: clean URL fetch via Exa `/contents` | internal |
| **tika** | Document extraction & preprocessing | internal |
| **kokoro** | TTS engine (Kokoro-FastAPI, OpenAI-compatible) | internal |

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

**Available MCP servers:**
- **filesystem**: Access to `/stacks` directory (mounted read-only)
- **trilium**: Integration with Trilium note-taking app (requires `TRILIUM_API_KEY`)
- **searxng**: Web search via the SearXNG service
- **trainlocks**: Training log integration

### SearXNG (`searxng/`)

Privacy-respecting metasearch engine. Configuration is in [`searxng/config/settings.yml`](searxng/config/settings.yml ).

### Web search & page fetching (`exa-loader/`)

Web search uses **Exa** (`web.search.engine=exa`, key in `/opt/secrets/exa.key`).
Automatic search bypasses Open WebUI's own web loader and embedding stage
(`web.search.bypass_web_loader=true`, `web.search.bypass_embedding_and_retrieval=true`,
`exa_max_content_length=8000`, `result_count=8`): Exa's cleaned results go straight
to the model — no second fetch and no lossy local embed/retrieve hop. Document
RAG (`rag.*`) is unaffected.

URL *fetches* would otherwise use Open WebUI's built-in `safe_web` loader, which
is `BeautifulSoup(html).get_text()` over the whole DOM and therefore returns
navigation/sidebar/footer boilerplate. `exa-loader` reuses the same Exa key to
return clean article text instead, wired via these DB keys:

| Key | Value |
|---|---|
| `web.loader.engine` | `external` |
| `web.loader.external_web_loader_url` | `http://exa-loader:8080/contents` |
| `web.loader.external_web_loader_api_key` | token in `/opt/secrets/exa-loader.token` |

Secrets live outside the repo (gitignored): `/opt/secrets/exa.key`,
`/opt/secrets/exa-loader.token`. The service is internal-only (no published
ports). Set `EXA_TEXT_MAX_CHARS` in `compose.yml` to cap fetched page length
(unset = full page). These retrieval/loader keys are read from the DB on every
request, so changes apply **without** an Open WebUI restart.

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
`simple` (which applies a ~1.15 `shift`). Resolution is 1024×1024 (the Comfy-Org
template default); the Turbo card documents 2048-scale presets.

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
| `image_generation.steps` | unused for Turbo — the schedule is fixed by `ManualSigmas`; left at `8` for the UI. Restore to `40` when rolling back to the base. |
| `*.comfyui.base_url` | `http://${LAN_IP}:8189` (lifecycle proxy) |
| `*.comfyui.api_key` | empty |

Rollback to the base model: point the two `*.comfyui.workflow` keys at
`qwen_image_2_1_{t2i,edit}_api.json`, set both `model` keys to
`qwen_image_2.1_int8_convrot.safetensors`, and set `image_generation.steps` to `40`.
Then restart Open WebUI.

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
- edit (Turbo): `image`→4, `prompt`→5, `model`/`unet_name`→1, `width`/`height`→7, `seed`→6 (`noise_seed`)

Neither maps `steps`: the Turbo schedule is fixed by `ManualSigmas`, and
`SamplerCustom` has no `steps` input. The base workflows
(`qwen_image_2_1_{t2i,edit}_api.json`, rollback) still use `KSampler`, where
generation maps `steps`/`seed`→6 and edit maps `seed`→6.

PE workflows (`prompt` goes to the rewrite node, not `TextEncodeQwenImage21`):

- PE generation: `prompt`→2, `negative_prompt`→6, `model`/`unet_name`→3, `width`/`height`/`n`→7, `steps`/`seed`→8
- PE edit: `image`→3, `prompt`→2, `model`/`unet_name`→4, `width`/`height`→8, `seed`→9

(PE node ids: CLIPLoader PE=1, rewrite=2, then UNET=3/4, generation
CLIPLoader, VAE, `TextEncodeQwenImage21`, `EmptyLatentImage`, `KSampler`,
`VAEDecode`, `SaveImage`, `PreviewAny`. See the JSON files.)

Two gotchas, both of which fail with a generic 400 if violated:

- The **edit** node map must **not** include `negative_prompt`: `ComfyUIEditImageForm` has no such field and the node-map applier dereferences it unconditionally.
- The **edit** node map must **not** include `steps`: the edit request path does not send it, so mapping it writes `None` and ComfyUI rejects the prompt. (The Turbo edit workflow takes its schedule from `ManualSigmas`; the base edit workflow's own default, 40, applies.)
- The Turbo workflows seed through `SamplerCustom`, whose input is named `noise_seed` — so the `seed` map entry must use `"key": "noise_seed"` (the base `KSampler` uses `"key": "seed"`).

Changing the DB directly requires an Open WebUI restart — image config is read
into memory at startup.

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
│   └── config.json           # Generated from template (gitignored, contains secrets)
├── searxng/
│   └── config/
│       └── settings.yml      # SearXNG configuration
├── exa-loader/              # External web loader (clean URL fetch via Exa /contents)
│   ├── app.py               # stdlib HTTP adapter
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
