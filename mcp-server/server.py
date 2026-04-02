"""
LifeRadar MCP Server — exposes LifeRadar API tools via MCP protocol.
Uses stdio transport for MCP clients. Background HTTP thread handles
Traefik health checks so the container stays alive.
"""
import os
import json
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

# MCP server implementation using mcp SDK
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# LifeRadar API base URL — use host.docker.internal to reach API container
LIFE_RADAR_API_URL = os.environ.get(
    "LIFE_RADAR_API_URL",
    "http://host.docker.internal:8000"
)

APP_NAME = "liferadar-mcp"
VERSION = "1.0.0"

server = Server(APP_NAME)


# ── Background HTTP health server for Traefik/Coolify health checks ───────────

class HealthHandler(BaseHTTPRequestHandler):
    """Minimal handler that responds 200 to all requests — keeps container alive."""

    def log_message(self, *args):
        pass  # silence default stderr logging

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "service": APP_NAME, "version": VERSION}).encode())

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_health_server(port: int = 8090):
    """Run a blocking HTTP server in a background thread."""
    srv = HTTPServer(("0.0.0.0", port), HealthHandler)
    srv.serve_forever()


# Start background health server before the async main() loop
health_thread = threading.Thread(target=run_health_server, args=(8090,), daemon=True)
health_thread.start()


# ── MCP tool definitions ──────────────────────────────────────────────────────

async def call_api(path: str, params: dict | None = None) -> list[dict]:
    """Make a GET request to the LifeRadar API and return parsed JSON."""
    url = f"{LIFE_RADAR_API_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params or {})
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return [data]
            else:
                return [{"result": str(data)}]
        except httpx.HTTPStatusError as e:
            return [{"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}]
        except httpx.ConnectError:
            return [{"error": f"Could not connect to LifeRadar API at {url}. Is the API running?"}]
        except Exception as e:
            return [{"error": str(e)}]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Declare the tools this MCP server exposes."""
    return [
        Tool(
            name="alerts",
            description="Get conversations needing attention: needs_reply, needs_read, important, overdue, blocked. Returns top priority conversations requiring action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 50, max 200)", "default": 50},
                    "min_priority": {"type": "number", "description": "Minimum priority score filter", "default": None},
                },
            },
        ),
        Tool(
            name="conversations",
            description="List conversations (Matrix, email, etc.) with priority scoring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "source": {"type": "string", "description": "Filter by source (e.g. 'matrix', 'email')", "default": None},
                    "needs_reply": {"type": "boolean", "description": "Filter to conversations needing reply", "default": None},
                },
            },
        ),
        Tool(
            name="conversation",
            description="Get a single conversation by ID with full details.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "UUID of the conversation"},
                },
                "required": ["conversation_id"],
            },
        ),
        Tool(
            name="messages",
            description="List message events from conversations, ordered by most recent first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "Filter by conversation UUID"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                    "source": {"type": "string", "description": "Filter by source"},
                },
            },
        ),
        Tool(
            name="commitments",
            description="Track commitments made to others — promises, agreements, todos assigned by others.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: open, in_progress, blocked, done, cancelled"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                },
            },
        ),
        Tool(
            name="reminders",
            description="Reminders for time-sensitive items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: scheduled, queued, sent, snoozed, cancelled, completed"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                },
            },
        ),
        Tool(
            name="tasks",
            description="Planned actions / tasks — items you've committed to doing yourself.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: proposed, scheduled, ready, done, cancelled"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                },
            },
        ),
        Tool(
            name="calendar_events",
            description="Calendar events from external calendars (Google Calendar, etc.) synced into LifeRadar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date (ISO 8601)"},
                    "to_date": {"type": "string", "description": "End date (ISO 8601)"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                },
            },
        ),
        Tool(
            name="memories",
            description="Memory records — facts, preferences, relationships, skills about you.",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Filter by kind: fact, preference, relationship, skill"},
                    "subject_type": {"type": "string", "description": "Subject type filter"},
                    "active": {"type": "boolean", "description": "Only active records (default true)", "default": True},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                },
            },
        ),
        Tool(
            name="probe_status",
            description="Status of runtime probes (Matrix, email, calendar) — are data sources connected and healthy?",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="probe_candidates",
            description="Messaging candidates — contacts/conversations that are candidates for automated triage.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search",
            description="Full-text search across conversations, messages, and memories using keyword matching.",
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query (required, min 1 char)"},
                    "limit": {"type": "integer", "description": "Max results (default 20, max 100)", "default": 20},
                },
                "required": ["q"],
            },
        ),
        Tool(
            name="health",
            description="Health check — verify the LifeRadar API and database are reachable.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle a tool call from an MCP client."""
    params = arguments.copy()

    match name:
        case "health":
            result = await call_api("health")
        case "alerts":
            result = await call_api("alerts", params)
        case "conversations":
            result = await call_api("conversations", params)
        case "conversation":
            cid = params.pop("conversation_id")
            result = await call_api(f"conversations/{cid}", params)
        case "messages":
            result = await call_api("messages", params)
        case "commitments":
            result = await call_api("commitments", params)
        case "reminders":
            result = await call_api("reminders", params)
        case "tasks":
            result = await call_api("tasks", params)
        case "calendar_events":
            result = await call_api("calendar/events", params)
        case "memories":
            result = await call_api("memories", params)
        case "probe_status":
            result = await call_api("probe-status")
        case "probe_candidates":
            result = await call_api("probe-status/candidates")
        case "search":
            result = await call_api("search", params)
        case _:
            result = [{"error": f"Unknown tool: {name}"}]

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    """Run the MCP server over stdio transport (HTTP health server runs in background)."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
