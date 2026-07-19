#!/usr/bin/env python3
"""
Trilium MCP Server - Model Context Protocol server for Trilium Notes
Provides tools to create, read, search, list, and modify notes in Trilium via ETAPI.

Default note type is "code" with MIME "text/x-markdown", since Markdown code notes
are the primary use case for this server.
"""

import os
import sys
import json
import logging
import asyncio
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

# MCP imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Error: mcp package not found. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("trilium-mcp")

# Configuration from environment
TRILIUM_URL = os.getenv("TRILIUM_URL", "http://trilium:8080")
TRILIUM_API_KEY = os.getenv("TRILIUM_API_KEY", "")

# Defaults for note creation, since Markdown code notes are the primary use case
DEFAULT_NOTE_TYPE = "code"
DEFAULT_MIME = "text/x-markdown"

if not TRILIUM_API_KEY:
    logger.warning("TRILIUM_API_KEY not set. Set it in your environment or compose file.")


class TriliumClient:
    """HTTP client for Trilium ETAPI."""

    def __init__(self, base_url: str, api_key: str):
        # base_url should be the host+port only, e.g. http://192.168.0.46:9910
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    def _request(self, method: str, path: str, data: Optional[Dict] = None,
                 params: Optional[Dict] = None) -> Dict:
        """Make a JSON HTTP request to the ETAPI (for metadata / structured endpoints)."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        body = json.dumps(data).encode('utf-8') if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req) as response:
                if response.status == 204:
                    return {}
                content = response.read().decode('utf-8')
                if not content:
                    return {}
                return json.loads(content) if content.startswith('{') or content.startswith('[') else {"raw": content}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            raise Exception(f"Trilium API error {e.code}: {error_body}")
        except urllib.error.URLError as e:
            raise Exception(f"Connection error: {str(e)}")

    def _request_raw(self, method: str, path: str, text_body: Optional[str] = None) -> str:
        """Make a raw text HTTP request (for note content endpoints, which are not JSON)."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "text/plain"
        }
        body = text_body.encode('utf-8') if text_body is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req) as response:
                if response.status == 204:
                    return ""
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            raise Exception(f"Trilium API error {e.code}: {error_body}")
        except urllib.error.URLError as e:
            raise Exception(f"Connection error: {str(e)}")

    def create_note(self, parent_id: str, title: str, content: str,
                    note_type: str = DEFAULT_NOTE_TYPE,
                    mime: Optional[str] = None) -> str:
        """Create a new note. Returns the note ID.

        Defaults to a 'code' note with MIME 'text/x-markdown', so the AI
        writes raw Markdown source by default rather than rich text.
        """
        note_data = {
            "parentNoteId": parent_id,
            "title": title,
            "type": note_type,
            "content": content
        }
        if mime:
            note_data["mime"] = mime
        elif note_type == "code":
            note_data["mime"] = DEFAULT_MIME
        result = self._request("POST", "/etapi/create-note", note_data)
        return result.get("note", {}).get("noteId", "unknown")

    def get_note(self, note_id: str) -> Dict:
        """Get a note's metadata by ID."""
        return self._request("GET", f"/etapi/notes/{note_id}")

    def get_note_content(self, note_id: str) -> str:
        """Get the raw content of a note."""
        return self._request_raw("GET", f"/etapi/notes/{note_id}/content")

    def search_notes(self, query: str, limit: int = 10) -> List[Dict]:
        """Search for notes by title or content using ETAPI search syntax."""
        params = {
            "search": query,
            "orderBy": "title",   # required for 'limit' to actually apply
            "limit": str(limit)
        }
        result = self._request("GET", "/etapi/notes", params=params)
        return result.get("results", [])

    def list_notes(self, parent_id: str = "root") -> List[Dict]:
        """List direct child notes of a given parent note (default: root)."""
        parent = self._request("GET", f"/etapi/notes/{parent_id}")
        child_ids = parent.get("childNoteIds", [])
        children = []
        for cid in child_ids:
            try:
                child = self._request("GET", f"/etapi/notes/{cid}")
                children.append({
                    "noteId": child.get("noteId", cid),
                    "title": child.get("title", "Untitled"),
                    "type": child.get("type", "unknown"),
                    "mime": child.get("mime", "")
                })
            except Exception as e:
                logger.warning(f"Could not fetch child note {cid}: {e}")
        return children

    def update_note_content(self, note_id: str, content: str) -> None:
        """Update a note's content (raw text)."""
        self._request_raw("PUT", f"/etapi/notes/{note_id}/content", content)

    def update_note(self, note_id: str, title: Optional[str] = None) -> None:
        """Update note metadata."""
        data = {}
        if title is not None:
            data["title"] = title
        if data:
            self._request("PATCH", f"/etapi/notes/{note_id}", data)

    def delete_note(self, note_id: str) -> None:
        """Delete a note."""
        self._request("DELETE", f"/etapi/notes/{note_id}")

    def get_day_note(self, date: Optional[str] = None) -> str:
        """Get or create today's day note. Returns note ID."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        result = self._request("POST", f"/etapi/calendar/days/{date}")
        return result.get("noteId", "")


# Initialize MCP server
app = Server("trilium-mcp")
client = TriliumClient(TRILIUM_URL, TRILIUM_API_KEY)


@app.list_tools()
async def list_tools() -> List[Tool]:
    """List available Trilium tools."""
    return [
        Tool(
            name="create_note",
            description=(
                "Create a new note in Trilium. Returns the note ID. "
                "By default creates a 'code' note with MIME 'text/x-markdown', "
                "so the content is stored as raw Markdown source rather than rich text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "ID of the parent note (use 'root' for top-level, or get a specific parent from search/list). Use the day note for journal entries."
                    },
                    "title": {"type": "string", "description": "Title of the new note"},
                    "content": {"type": "string", "description": "Content of the note. For the default Markdown code note, write plain Markdown source here."},
                    "note_type": {
                        "type": "string",
                        "description": "Type of note: 'code' (raw Markdown/plain text, default), 'text' (rich text/HTML), 'relationMap', 'search'",
                        "default": "code",
                        "enum": ["code", "text", "relationMap", "search"]
                    },
                    "mime": {
                        "type": "string",
                        "description": "MIME type, mainly relevant for code notes. Defaults to 'text/x-markdown' when note_type is 'code'.",
                        "default": "text/x-markdown"
                    }
                },
                "required": ["parent_id", "title", "content"]
            }
        ),
        Tool(
            name="get_note",
            description="Get a note by its ID. Returns title, type, mime, and metadata.",
            inputSchema={
                "type": "object",
                "properties": {"note_id": {"type": "string", "description": "The ID of the note to retrieve"}},
                "required": ["note_id"]
            }
        ),
        Tool(
            name="get_note_content",
            description="Get the content/body of a note by its ID.",
            inputSchema={
                "type": "object",
                "properties": {"note_id": {"type": "string", "description": "The ID of the note to read"}},
                "required": ["note_id"]
            }
        ),
        Tool(
            name="list_notes",
            description="List the direct child notes of a given parent note. Use 'root' to list top-level notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "ID of the parent note whose children to list (default: 'root')",
                        "default": "root"
                    }
                }
            }
        ),
        Tool(
            name="search_notes",
            description="Search for notes by title or content. Returns a list of matching notes with their IDs and titles.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term to find in note titles or content"},
                    "limit": {"type": "integer", "description": "Maximum number of results to return (default: 10)", "default": 10}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="update_note_content",
            description="Replace the content of an existing note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The ID of the note to update"},
                    "content": {"type": "string", "description": "New content for the note (will replace existing content)"}
                },
                "required": ["note_id", "content"]
            }
        ),
        Tool(
            name="update_note_title",
            description="Update the title of an existing note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The ID of the note to update"},
                    "title": {"type": "string", "description": "New title for the note"}
                },
                "required": ["note_id", "title"]
            }
        ),
        Tool(
            name="append_to_note",
            description="Append content to an existing note (preserves existing content).",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The ID of the note to append to"},
                    "content": {"type": "string", "description": "Content to append to the note"}
                },
                "required": ["note_id", "content"]
            }
        ),
        Tool(
            name="delete_note",
            description="Delete a note by its ID. WARNING: This is permanent!",
            inputSchema={
                "type": "object",
                "properties": {"note_id": {"type": "string", "description": "The ID of the note to delete"}},
                "required": ["note_id"]
            }
        ),
        Tool(
            name="get_today_note",
            description="Get or create today's journal note. Useful for daily logging or journal entries.",
            inputSchema={"type": "object", "properties": {}}
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle tool calls."""
    try:
        if name == "create_note":
            note_id = client.create_note(
                parent_id=arguments["parent_id"],
                title=arguments["title"],
                content=arguments["content"],
                note_type=arguments.get("note_type", DEFAULT_NOTE_TYPE),
                mime=arguments.get("mime")
            )
            return [TextContent(
                type="text",
                text=f"Note created successfully!\nNote ID: {note_id}\nTitle: {arguments['title']}\nParent: {arguments['parent_id']}\nType: {arguments.get('note_type', DEFAULT_NOTE_TYPE)}"
            )]

        elif name == "get_note":
            note = client.get_note(arguments["note_id"])
            return [TextContent(type="text", text=json.dumps(note, indent=2))]

        elif name == "get_note_content":
            content = client.get_note_content(arguments["note_id"])
            return [TextContent(type="text", text=content)]

        elif name == "list_notes":
            parent_id = arguments.get("parent_id", "root")
            children = client.list_notes(parent_id)
            if not children:
                return [TextContent(type="text", text=f"No child notes found under '{parent_id}'")]
            formatted = f"Notes under '{parent_id}' ({len(children)}):\n\n"
            for note in children:
                mime_info = f", mime: {note['mime']}" if note.get('mime') else ""
                formatted += f"- **{note['title']}** (ID: {note['noteId']}, type: {note['type']}{mime_info})\n"
            return [TextContent(type="text", text=formatted)]

        elif name == "search_notes":
            results = client.search_notes(query=arguments["query"], limit=arguments.get("limit", 10))
            if not results:
                return [TextContent(type="text", text=f"No notes found matching '{arguments['query']}'")]
            formatted = f"Found {len(results)} note(s):\n\n"
            for note in results:
                formatted += f"- **{note.get('title', 'Untitled')}** (ID: {note.get('noteId', 'unknown')})\n"
            return [TextContent(type="text", text=formatted)]

        elif name == "update_note_content":
            client.update_note_content(note_id=arguments["note_id"], content=arguments["content"])
            return [TextContent(type="text", text=f"Note {arguments['note_id']} content updated successfully")]

        elif name == "update_note_title":
            client.update_note(note_id=arguments["note_id"], title=arguments["title"])
            return [TextContent(type="text", text=f"Note {arguments['note_id']} title updated to '{arguments['title']}'")]

        elif name == "append_to_note":
            existing = client.get_note_content(arguments["note_id"])
            new_content = existing + "\n\n" + arguments["content"] if existing else arguments["content"]
            client.update_note_content(note_id=arguments["note_id"], content=new_content)
            return [TextContent(type="text", text=f"Content appended to note {arguments['note_id']}")]

        elif name == "delete_note":
            client.delete_note(arguments["note_id"])
            return [TextContent(type="text", text=f"Note {arguments['note_id']} deleted")]

        elif name == "get_today_note":
            note_id = client.get_day_note()
            return [TextContent(type="text", text=f"Today's note ID: {note_id}")]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error in tool {name}: {str(e)}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Run the MCP server."""
    logger.info(f"Starting Trilium MCP server")
    logger.info(f"Trilium URL: {TRILIUM_URL}")
    logger.info(f"API Key configured: {'Yes' if TRILIUM_API_KEY else 'No'}")
    logger.info(f"Default note type: {DEFAULT_NOTE_TYPE}, default mime: {DEFAULT_MIME}")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

