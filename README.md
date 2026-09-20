# Open WebUI Self-Hosted Stack

A self-hosted AI chat interface with extended capabilities, powered by [Open WebUI](https://github.com/open-webui/open-webui) and additional services.

## Services Included

| Service | Description | Port |
|---------|-------------|------|
| **open-webui** | Main UI for AI conversations | 3001 |
| **mcpo** | MCP server gateway (Trilium, filesystem, search) | internal |
| **searxng** | Privacy-respecting search engine | 8081 |
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

### Image generation & editing (`workflows/`)

Open WebUI's native ComfyUI engine drives the local ComfyUI stack
(`/opt/stacks/comfyui`) running **Qwen-Image-2.1** for both generation and
editing. The active config lives in Open WebUI's config DB
(`data/webui.db`, key/value `config` table), which is gitignored — so the
workflows are mirrored here for reproducibility:

- [`workflows/qwen_image_2_1_t2i_api.json`](workflows/qwen_image_2_1_t2i_api.json) — text-to-image
- [`workflows/qwen_image_2_1_edit_api.json`](workflows/qwen_image_2_1_edit_api.json) — image edit

Both `image_generation.comfyui.base_url` and `images.edit.comfyui.base_url`
point at `http://${LAN_IP}:8188` (the `COMFYUI_BASE_URL` env default is
overridden by the DB value). The `runpod-bridge` service is retained but is no
longer used for image work.

DB keys to restore (Admin → Settings → Images, or the `config` table directly):

| Key | Value |
|---|---|
| `image_generation.comfyui.workflow` | `workflows/qwen_image_2_1_t2i_api.json` |
| `images.edit.comfyui.workflow` | `workflows/qwen_image_2_1_edit_api.json` |
| `image_generation.model` / `images.edit.model` | `qwen_image_2.1_int8_convrot.safetensors` |
| `*.comfyui.base_url` | `http://${LAN_IP}:8188` |
| `*.comfyui.api_key` | empty |

Storage note when writing the `config` table directly: `*.comfyui.workflow` is
a JSON **string** whose content is the workflow file above, whereas
`*.comfyui.nodes` is a raw JSON **array**. Double-encoding `nodes` (or leaving
`workflow` unquoted) still loads but fails at generation time.

Node maps (`*.comfyui.nodes`); ids refer to the workflows above:

- generation: `prompt`→4, `negative_prompt`→4, `model`/`unet_name`→1, `width`/`height`/`n`→5, `steps`/`seed`→6
- edit: `image`→4, `prompt`→5, `model`/`unet_name`→1, `width`/`height`→7, `seed`→6

Two gotchas, both of which fail with a generic 400 if violated:

- The **edit** node map must **not** include `negative_prompt`: `ComfyUIEditImageForm` has no such field and the node-map applier dereferences it unconditionally.
- The **edit** node map must **not** include `steps`: the edit request path does not send it, so mapping it writes `None` and ComfyUI rejects the prompt. The workflow's own default (25) applies.

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
├── workflows/               # ComfyUI API workflows for Open WebUI (Qwen-Image-2.1)
│   ├── qwen_image_2_1_t2i_api.json
│   └── qwen_image_2_1_edit_api.json
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
