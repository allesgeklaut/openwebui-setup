# Trilium MCP Server

A Model Context Protocol (MCP) server that allows Open WebUI models to interact with your Trilium Notes instance.

## Features

The server exposes the following tools to your models:

- **`create_note`** - Create new notes with title and content
- **`get_note`** - Get note metadata by ID
- **`get_note_content`** - Read the content of a note
- **`search_notes`** - Search for notes by title or content
- **`update_note_content`** - Replace a note's content
- **`update_note_title`** - Update a note's title
- **`append_to_note`** - Append content to an existing note (preserves existing content)
- **`delete_note`** - Delete a note (permanent!)
- **`get_today_note`** - Get or create today's journal note

## Setup

### 1. Create Trilium API Key

1. Open your Trilium Notes instance
2. Go to **Settings** → **General**
3. Find **API Key** section
4. Click to generate a new key
5. Copy the key

### 2. Add API Key to Environment

Add the API key to your environment (e.g., in a `.env` file or your shell):

```bash
export TRILIUM_API_KEY="your-trilium-api-key-here"
```

Or add it to your Docker environment file.

### 3. Restart MCPo Service

```bash
docker compose restart mcpo
```

Or restart the entire webui stack:
```bash
docker compose -f /opt/stacks/webui/compose.yml restart
```

## Usage in Open WebUI

Once set up, your models can interact with Trilium. Example prompts:

- *"Create a note in Trilium titled 'Meeting Notes' with today's discussion"*
- *"Search my Trilium notes for 'docker setup'"*
- *"Append this to my Trilium note [note_id]"*
- *"Add this conversation to today's journal note in Trilium"*

## Notes

- The server uses `urllib` (no external HTTP dependencies needed)
- All operations respect Trilium's authentication via API key
- The `append_to_note` tool is useful for accumulating information in journal/log notes
- Use `get_today_note` to get the note ID for daily journal entries
- For creating notes, you'll need a parent note ID (use 'root' for top-level, or search for an existing note first)

## Troubleshooting

- **"API Key not set"**: Make sure `TRILIUM_API_KEY` is set in the environment
- **"Connection error"**: Verify Trilium is running and accessible at the configured URL
- **Tools not appearing in Open WebUI**: Restart the MCPo container and check logs: `docker logs mcpo`

## Security

- Keep your API key secure - don't commit it to version control
- The MCP server runs inside Docker, so it only has network access to your Trilium container
- All operations are authenticated and logged
