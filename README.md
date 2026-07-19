# Open WebUI Self-Hosted Stack

A self-hosted AI chat interface with extended capabilities, powered by [Open WebUI](https://github.com/open-webui/open-webui) and additional services.

## Services Included

| Service | Description | Port |
|---------|-------------|------|
| **open-webui** | Main UI for AI conversations | 3001 |
| **mcpo** | MCP server gateway (Trilium, filesystem, fetch) | internal |
| **searxng** | Privacy-respecting search engine | 8081 |
| **tika** | Document extraction & preprocessing | internal |

## Prerequisites

- Docker & Docker Compose installed
- Ollama running on your network (default: `http://192.168.0.46:11434`)
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
- **filesystem**: Access to `/stacks` directory (read-only)
- **trilium**: Integration with Trilium note-taking app (requires `TRILIUM_API_KEY`)
- **fetch**: Web content fetching

### SearXNG (`searxng/`)

Privacy-respecting metasearch engine. Configuration is in [`searxng/config/settings.yml`](searxng/config/settings.yml ).

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
