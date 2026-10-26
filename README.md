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

Open WebUI's ComfyUI engine drives the local stack (`../comfyui`) on
**Qwen-Image-2.1-Turbo** for generation and editing, via the lifecycle proxy
`http://${LAN_IP}:8189` (details in `../litellm/README.md`). The live config is
the `config` table of the gitignored `data/webui.db`; the workflow files here
are its reproducible mirror. Active in Open WebUI:
`qwen_image_2_1_turbo_{t2i,edit}_api.json`. The base variants are the rollback
path; the `_pe_` variants (Prompt Enhancer) belong to the ComfyUI MCP only —
Open WebUI already rewrites prompts itself, so PE there would double-enhance.

DB keys (Admin → Settings → Images, or the `config` table directly — applies
live, no restart, reload the browser):

| Key | Value |
|---|---|
| `image_generation.engine` / `images.edit.engine` | `comfyui` |
| `image_generation.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_t2i_api.json` |
| `images.edit.comfyui.workflow` | `workflows/qwen_image_2_1_turbo_edit_api.json` |
| `image_generation.model` / `images.edit.model` | `qwen_image_2.1_turbo_int8_convrot.safetensors` |
| `image_generation.size` | `2048x2048` (generation canvas; if changed, also change the workflow's encode `resolution`) |
| `images.edit.size` | `1024x1024` — its **width** becomes the edit resolution budget; a request's `size` overrides it |
| `*.comfyui.base_url` | `http://${LAN_IP}:8189` (lifecycle proxy, not `:8188`) |
| `*.comfyui.api_key` | empty |

Node maps (`*.comfyui.nodes`; ids per the active Turbo workflows):

- generation: `prompt`/`negative_prompt`→4, `model`/`unet_name`→1, `width`/`height`/`n`→5, `seed`→6 (`noise_seed`)
- edit: `image`→4 then reference slots 12..20 positionally, `prompt`→5, `model`→1, `seed`→6 (`noise_seed`), `width`→5 (`resolution`)

**Multi-reference editing.** Qwen-Image-2.1 edits accept up to 10 images
(target + references) and are reference-based generation; the chat
`edit_image` tool passes `image_urls` lists end-to-end. Address references as
`<image2>`..`<image10>` (`<image1>` is the target). Unfilled workflow slots
carry the sentinel `multi_ref_unused.png`, which the lifecycle proxy strips
from `POST /prompt` (`../litellm/comfyui_proxy/prompt_clean.py`); the ComfyUI
MCP exposes the same via `reference_images`.

Gotchas when writing the `config` table directly (violations fail as generic
400s; non-JSON values crash every image request):

- Every `config.value` is a **JSON column**: direct SQL must store JSON — plain strings need quotes (`'"comfyui"'`, not `comfyui`); raw text crashes every image request inside `get_image_config` before any traffic reaches the proxy. (Raw integers are tolerated by the ORM's SQLite JSON dialect.)
- `*.comfyui.workflow` is a JSON **string** (double-encoded); `*.comfyui.nodes` is a raw JSON **array**.
- The `model` keys override the workflow's `unet_name` — update the key, not just the file.
- The edit node map must not map `negative_prompt` or `steps` (the edit request sends neither; `None` gets written).
- The edit `width`→`resolution` mapping needs a non-empty `images.edit.size`.
- Turbo seeds via `SamplerCustom` (`noise_seed`); the base `KSampler` uses `seed` + `steps`.

Rollback to the base model: point the two workflow keys at
`qwen_image_2_1_{t2i,edit}_api.json`, set both models to
`qwen_image_2.1_int8_convrot.safetensors`, `image_generation.size` to
`1024x1024` and `image_generation.steps` to `40`.

**ChatGPT adapter (alternative).** The `chatgpt-images` shim
(`../litellm/chatgpt_images/`) can serve both engines instead; run its
`configure_webui.py` to switch. The adapter-era rows (incl. its gateway key)
are backed up at `data/backups/config-image-gen-comfyui-backup-20261026.json`
(gitignored; delete once obsolete).

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
