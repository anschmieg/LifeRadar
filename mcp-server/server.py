"""
LifeRadar MCP Server — exposes LifeRadar API tools via MCP protocol.
Uses SSE (Server-Sent Events) transport for streaming HTTP connections.
"""
import os
import json
import httpx
from typing import Any

# MCP server implementation using mcp SDK
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

# ASGI framework
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, Response
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
import hypercorn.config
from hypercorn.asyncio import serve

# LifeRadar API base URL — use host.docker.internal to reach API container
LIFE_RADAR_API_URL = os.environ.get(
    "LIFE_RADAR_API_URL",
    "http://host.docker.internal:8000"
)

APP_NAME = "liferadar-mcp"
VERSION = "1.0.0"

server = Server(APP_NAME)


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


async def call_api_post(path: str, body: dict | None = None) -> list[dict]:
    """Make a POST request to the LifeRadar API and return parsed JSON."""
    url = f"{LIFE_RADAR_API_URL.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json=body or {})
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
            description="Calendar events from external calendars (Google Calendar, etc.) synced into LifeRadar. Supports GET to list events and POST to create/update events.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string", "description": "Start date (ISO 8601) - GET only"},
                    "to_date": {"type": "string", "description": "End date (ISO 8601) - GET only"},
                    "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                    "title": {"type": "string", "description": "Event title - POST only"},
                    "summary": {"type": "string", "description": "Event description/summary - POST only"},
                    "scheduled_start": {"type": "string", "description": "Start datetime (ISO 8601) - POST only"},
                    "scheduled_end": {"type": "string", "description": "End datetime (ISO 8601) - POST only"},
                    "calendar_external_id": {"type": "string", "description": "External calendar ID for upsert - POST only"},
                    "calendar_provider": {"type": "string", "description": "Provider: google, outlook - POST only"},
                },
            },
        ),
        Tool(
            name="send-message",
            description="Send a message in a Matrix/Outlook conversation. Requires user approval. Returns a message_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "UUID of the conversation"},
                    "content_text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["conversation_id", "content_text"],
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
            if "title" in params or "calendar_external_id" in params:
                result = await call_api_post("calendar/events", params)
            else:
                result = await call_api("calendar/events", params)
        case "send-message":
            result = await call_api_post("messages/send", params)
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


# ── SSE Transport Setup ───────────────────────────────────────────────────────

# Create SSE transport with message endpoint
sse_transport = SseServerTransport("/messages/")


async def handle_sse(request):
    """Handle SSE connection request."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0], streams[1], server.create_initialization_options()
        )
    return Response()


async def handle_messages(request):
    """Handle incoming JSON-RPC messages via POST."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)
    return Response(status_code=202)


async def health(request):
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "service": APP_NAME, "version": VERSION})


# Create Starlette app with routes
app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=handle_messages, methods=["POST"]),
        Route("/health", endpoint=health),
    ],
)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    
    # Configure Hypercorn
    config = hypercorn.config.Config()
    config.bind = ["0.0.0.0:8090"]
    
    print(f"[liferadar] Starting MCP SSE server on :8090")
    print(f"[liferadar] SSE endpoint: /sse")
    print(f"[liferadar] Messages endpoint: /messages/")
    print(f"[liferadar] Health endpoint: /health")
    
    asyncio.run(serve(app, config))
