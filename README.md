# Open WebUI Self-Hosted Stack

A self-hosted AI chat interface with extended capabilities, powered by [Open WebUI](https://github.com/open-webui/open-webui) and additional services.

## Services Included

| Service | Description | Port |
|---------|-------------|------|
| **open-webui** | Main UI for AI conversations | 3001 |
| **open-terminal** | Persistent terminal with repo-managed OpenCode configuration | internal |
| **mcpo** | MCP server gateway (Trilium, filesystem, deep-research, …) | internal |
| **searxng** | Privacy-respecting search engine | 8081 |
| **research-search** | Optional Mistral Web Scout search/fetch backend | internal |
| **tika** | Document extraction & preprocessing | internal |
| **audiobookshelf-mcp** | Audiobookshelf podcast connector (authenticated MCP for Open WebUI) | internal |

Voice chat and RAG embeddings route through LiteLLM to Mistral. Web search uses
SearXNG; page loading uses Open WebUI's default HTTP loader. RunPod is an optional
Compose profile, not part of the default deployment.

## Prerequisites

- Docker & Docker Compose installed
- Ollama running on your network (default: `http://<your-LAN-IP>:11434`)
- Cloudflare Access configured for OAuth/SSO (optional)

## Setup

### 1. Configure Environment

```bash
cp .env.example .env
# Set installation URLs, agent/library IDs and other non-secret values.
```

Use [`.env.example`](.env.example) for configuration fields. Keep actual
credentials under `/opt/secrets`, not in Git. Required mounts and external
networks are listed in `compose.yml`; this stack assumes the companion
LiteLLM, Trilium, Audiobookshelf and shared MCP services already exist.

`start.sh` reads `webui.env`, `trainlocks.env`, `mistral.env` and `trilium.token`
from `/opt/secrets`. Other services mount their own key/token files there.
The SearXNG signing secret is `/opt/secrets/searxng.secret` (mode `600`).

### 2. Start Services

```bash
./start.sh
```

This script:
1. Renders `mcpo/template_config.json` to `/opt/secrets/mcpo-config.json`.
2. Starts the default Docker Compose services.

Run `./start.sh` again after template/environment changes. Use
`docker compose up -d` when the generated configuration is already current.
Do not print or commit the generated MCPO config: it contains credentials.

## Configuration

### Native tools and filters

The [extensions directory](openwebui-extensions/README.md) is mounted read-only
on Python's import path. Native Open WebUI entries contain small import adapters;
the implementation lives in Git. New installations need explicit registration.
Package updates need an `open-webui` restart; mount changes need recreation.

- [Chat Image Retrieval](openwebui-extensions/chat-images/README.md): retain the
  latest uploaded image set; retrieve older current-chat images after approval.
- [SearXNG Image Search](openwebui-extensions/searxng-images/README.md): inline
  images and source links from SearXNG's image category.

### MCP Servers (`mcpo/`)

The `mcpo` service connects to various Model Context Protocol servers. The configuration is generated from [`mcpo/template_config.json`](mcpo/template_config.json ) using your environment variables.

Direct package versions are pinned in [`mcpo/npm/package.json`](mcpo/npm/package.json)
and [`mcpo/requirements.txt`](mcpo/requirements.txt); transitive dependencies
are not fully locked. Dependabot checks the manifests and selected images.
MCPO uses a commit-tagged image, so image upgrades may need manual selection.

**Available MCP servers:**
- **filesystem**: Access to `/stacks` directory (mounted read-only)
- **trilium**: The pinned `triliumnext-mcp` package, with its token read by
  `start.sh` from `/opt/secrets/trilium.token`.
- **searxng**: Web search via the shared `mcp-searxng` stack — supergateway
  serves the pinned `mcp-searxng` package over Streamable HTTP on the
  `stacks-shared` docker network; mcpo proxies it remotely
  (`/opt/stacks/mcp-searxng`)
- **trainlocks**: Training log integration
- **deep-research**: Deep web research tool fronting the "Deep Researcher"
  Mistral Studio agent (`web_search_premium`). One tool,
  `deep_research(question)`: runs several refined searches with page opens and
  returns a structured, cited report. Calls are billed and can take minutes.
  The same server is also registered in `opencode.json`, so coding agents
  can call it. Both `MISTRAL_API_KEY` and the agent id come from
  `/opt/secrets/mistral.env` (start.sh's config renderer for mcpo; the script's
  file fallback for opencode).

### SearXNG (`searxng/`)

Privacy-respecting metasearch engine. Configuration is in [`searxng/config/settings.yml`](searxng/config/settings.yml).

Search fans out across default engines, then SearXNG merges, deduplicates and
ranks the combined results. Open WebUI takes the top 10 overall, not 10 per
engine. In native tool-calling mode the model chooses which pages to fetch;
the forced search pipeline fetches pages through the configured loader.

API backbone (default-enabled; subject to upstream quotas/availability):

- `exaapi` — the keyed Exa API (key from `/opt/secrets/exa.key`), the primary
  source.
- `exa keyless` — Exa's MCP endpoint (`mcp.exa.ai`), the same keyless mode
  OpenCode v2's WebSearch uses.
- `tinyfish keyless` / `tinyfish news` — TinyFish's MCP endpoint
  (`agent.tinyfish.ai/mcp`), web and news indexes.

Opt-in engines (registered but off the default fan-out; select with their
shortcut, e.g. `!scout`, or flip `disabled: false` to make them default):

- `web scout` (`!scout`) — Mistral-agent search via `research-search`.
  Each call is billed and slower than an API search. Authentication uses
  `/opt/secrets/research-search.token`.
- `tavily keyless` (`!tvl`) — Tavily's `X-Tavily-Access-Mode: keyless` REST
  mode, with a small shared quota that can return HTTP 429 quickly.

General searches also include Wikipedia and best-effort scrapers: DuckDuckGo,
Startpage, Brave and Mojeek. CAPTCHA/access-denied suspension is one day; 429
suspension is one minute (in memory, reset on restart). General Bing/Google
engines are off; other categories have their own defaults. News defaults are
`exaapi` and `tinyfish news`.

Open WebUI uses `http://searxng:8080/search?q=<query>`, JSON format and a result
count of 10. Its default page loader fetches directly: fetched sites see this
server's IP, and JavaScript-only pages may yield little content. Configure
search/loader settings through Open WebUI's administration UI, not direct SQL.

`searxng/engines/` holds the four small SearXNG engine modules; compose mounts
them individually into the image's `searx/engines/` directory (SearXNG loads an
engine by file name, and mounting a directory would shadow the bundled engines).
Note: SearXNG exits at startup if an engine module fails to *import*, so after a
`searxng/searxng:latest` image upgrade, check the modules still load (a quick
`/search?q=test&format=json` smoke test) — a missing host file only skips the
engine, but a broken import is fatal.

The shared MCP server (`/opt/stacks/mcp-searxng`) and trade-sentinel reach this
instance at `http://searxng:8080` over `stacks-shared`. MCPO exposes that MCP
server to Open WebUI; its package version is managed in the companion stack.

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

### Optional Web Scout backend (`research-search/`)

The [research-search service](research-search/README.md) provides Mistral-agent
search for SearXNG's opt-in `!scout` engine and an optional external page loader.
It is not Open WebUI's default search or loader. Each call starts a billed
agent conversation; use it explicitly when wanted. Its bearer token lives in
`/opt/secrets/research-search.token`.

## Audio & RAG embeddings

Voice chat and document embeddings use the same LiteLLM gateway/key as chat:

- **TTS:** `voxtral-tts`. Voice `auto` uses Jo for German, Jane for English,
  with ambiguous chunks defaulting to Jo. Aliases (`jo`/`de`, `jane`/`en`,
  mood variants) are resolved by LiteLLM's `VoxtralVoiceAlias` callback;
  Mistral Studio display names are not voice IDs.
- **STT:** `voxtral-stt`.
- **Embeddings:** `mistral-embed`, 1024 dimensions.

Persisted Open WebUI settings override Compose defaults on existing installs.
Use the administration UI to change them:

| Key | Value |
|---|---|
| `audio.tts.openai.api_base_url` / `audio.stt.openai.api_base_url` / `rag.openai.api_base_url` | `http://<LAN_IP>:4000/v1` |
| `audio.tts.model` / `audio.stt.model` | `voxtral-tts` / `voxtral-stt` |
| `audio.stt.engine` / `rag.embedding_engine` | `openai` |
| `audio.tts.voice` | `auto` (readable names/UUIDs also work — see above) |
| `rag.embedding_model` | `mistral-embed` |
| `*.api_key` | the litellm master key |

After changing embedding models/dimensions, reindex existing documents and
knowledge bases through Open WebUI before using their stored embeddings.

### Image generation & editing (`workflows/`)

Open WebUI's ComfyUI engine drives the local stack (`../comfyui`) on
**Qwen-Image-2.1-Turbo** for generation and editing, via the lifecycle proxy
`http://<LAN_IP>:8189` (details in `../litellm/README.md`). The live config is
the `config` table of the gitignored `data/webui.db`; the workflow files here
are its reproducible mirror. Active in Open WebUI:
`qwen_image_2_1_turbo_{t2i,edit}_api.json`. The base variants remain available
as alternatives.

Settings (Admin → Settings → Images; stored under these configuration keys):

| Key | Value |
|---|---|
| `image_generation.engine` / `images.edit.engine` | `comfyui` |
| `image_generation.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_t2i_api.json` |
| `images.edit.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_edit_api.json` |
| `image_generation.model` / `images.edit.model` | `qwen_image_2.1_turbo_int8_convrot.safetensors` |
| `image_generation.size` | `2048x2048` (generation canvas; if changed, also change the workflow's encode `resolution`) |
| `images.edit.size` | `1536x1536` — its **width** becomes the edit resolution budget; a request's `size` overrides it |
| `*.comfyui.base_url` | `http://<LAN_IP>:8189` (lifecycle proxy, not `:8188`) |
| `*.comfyui.api_key` | empty |

Node maps (`*.comfyui.nodes`; ids per the active Turbo workflows):

- generation: `prompt`/`negative_prompt`→4, `model`/`unet_name`→1, `width`/`height`/`n`→5, `seed`→6 (`noise_seed`)
- edit: `image`→4 then reference slots 12..20 positionally, `prompt`→5, `model`→1, `seed`→6 (`noise_seed`), `width`→5 (`resolution`)

For edits, `size` is a **pixel-budget control**, not an exact output canvas:
only its width is mapped to Qwen's `resolution`; height is ignored. Use a square
value such as `1536x1536` to request about 2.36 MP. The actual dimensions preserve
the first reference image's aspect ratio and are rounded to multiples of 32.

**Multi-reference editing.** Qwen-Image-2.1 edits accept up to 10 images
(target + references) and are reference-based generation; the chat
`edit_image` tool passes `image_urls` lists end-to-end. Address references as
`<image2>`..`<image10>` (`<image1>` is the target). Unfilled workflow slots
carry the sentinel `multi_ref_unused.png`, which the lifecycle proxy strips
along with its unused alpha-join helper from `POST /prompt`
(`../litellm/comfyui_proxy/prompt_clean.py`); the ComfyUI
MCP exposes the same via `reference_images`.

Each edit image's RGB and mask outputs are recombined with `JoinImageWithAlpha`
before Qwen encoding, preserving input transparency. Active Turbo editing now
defaults to a 1536 resolution budget for finer detail, at greater latency and
memory cost; the base rollback workflow remains at 1024.

Use the administration UI/API, not direct database writes. The model setting
overrides the workflow's `unet_name`; keep both consistent. Do not map edit
`negative_prompt` or `steps` (the edit request sends neither). The edit
`width`→`resolution` mapping requires a non-empty edit size. Turbo uses
`SamplerCustom.noise_seed`; the base workflows use `KSampler.seed` and `steps`.

Rollback to the base model: point the two workflow keys at
`qwen_image_2_1_{t2i,edit}_api.json`, set both models to
`qwen_image_2.1_int8_convrot.safetensors`, `image_generation.size` to
`1024x1024`, `image_generation.steps` to `40`, `images.edit.size` to
`1024x1024`, and swap both node maps to
the base variants (the ids/keys above are Turbo-specific): generation adds
`steps`→6 and seeds via `"key": "seed"` (not `noise_seed`); edit points
`image` at `["4", "10", ..., "18"]` and also seeds via `"key": "seed"`,
keeping `prompt`→5, `model`→1 and `width`→5 (`resolution`) unchanged.

**Alternatives:** the `runpod` Compose profile enables the optional
[RunPod bridge](runpod-bridge/README.md). The companion `chatgpt-images` adapter
(`../litellm/chatgpt_images/`) can also serve both image engines; its
`configure_webui.py` switches the Open WebUI configuration.

## Ports

| Port | Service | Access |
|------|---------|--------|
| 3001 | Open WebUI | Loopback; remote access through the configured proxy |
| 8081 | SearXNG | Published on the host |

Other services run internally and are not exposed.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail 50 mcpo searxng open-webui
```

For MCP configuration changes, rerun `./start.sh`; do not edit the generated
secret file. For native extension updates, restart `open-webui`. Keep logs and
private configuration out of public issue reports.

## License

This project is provided as-is. Open WebUI and its dependencies have their own licenses.
