"""
LifeRadar API Server — FastAPI
"""
import os
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from starlette.requests import Request as StarletteRequest
from pydantic import BaseModel

from api.db import get_pool, close_pool
from api.models import (
    Conversation,
    MessageEvent,
    Commitment,
    Reminder,
    PlannedAction,
    CalendarEvent,
    TaskCreate,
    CalendarEventUpsert,
    MessageSendRequest,
    MessageSendResponse,
    MemoryRecord,
    RuntimeProbe,
    MessagingCandidate,
    Alert,
    HealthResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create connection pool
    db_host = os.environ.get("LIFE_RADAR_DB_HOST", "NOT SET")
    db_port = os.environ.get("LIFE_RADAR_DB_PORT", "NOT SET")
    db_name = os.environ.get("LIFE_RADAR_DB_NAME", "NOT SET")
    print(f"[STARTUP] DB config: host={db_host}, port={db_port}, name={db_name}")
    await get_pool()
    print("[STARTUP] Pool initialization attempted")
    yield
    # Shutdown: close pool
    await close_pool()


app = FastAPI(
    title="LifeRadar API",
    description="Personal intelligence and communications triage API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MCP_URL = os.environ.get("LIFE_RADAR_MCP_URL", "http://liferadar-mcp:8090")


# --- MCP proxy (Streamable HTTP) ---
@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.api_route("/mcp", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_mcp(request: Request, path: str = ""):
    """Proxy MCP requests to the MCP server container."""
    target_url = f"{MCP_URL}/{path}" if path else f"{MCP_URL}/"
    headers = dict(request.headers)
    headers.pop("host", None)
    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )
    except httpx.ConnectError:
        return JSONResponse(status_code=503, content={"error": "MCP server unavailable"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- /health ---
@app.get("/health", response_model=HealthResponse)
async def health():
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return HealthResponse(status="ok", database="connected")
    except Exception as e:
        return HealthResponse(status="degraded", database=f"error: {e}")


# --- /alerts ---
@app.get("/alerts", response_model=list[Alert])
async def get_alerts(
    limit: int = Query(50, ge=1, le=200),
    min_priority: Optional[float] = None,
):
    """
    Get conversations needing attention, surfaced as alerts.
    Includes: needs_reply, needs_read, important, overdue, blocked.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        query = """
            SELECT
                c.id as conversation_id,
                COALESCE(c.title, c.external_id) as title,
                c.source,
                c.priority_score,
                c.urgency_score,
                c.due_at,
                c.needs_reply,
                c.needs_read,
                c.important_now,
                c.blocked_needs_context,
                c.waiting_on_other
            FROM life_radar.conversations c
            WHERE c.state = 'active'
              AND (
                  c.needs_reply = TRUE
                  OR c.needs_read = TRUE
                  OR c.important_now = TRUE
                  OR c.blocked_needs_context = TRUE
                  OR (c.due_at IS NOT NULL AND c.due_at < NOW())
              )
            ORDER BY c.priority_score DESC NULLS LAST, c.last_event_at DESC
            LIMIT $1
        """
        rows = await conn.fetch(query, limit)
        alerts = []
        for r in rows:
            if r["needs_reply"]:
                alert_type = "needs_reply"
            elif r["blocked_needs_context"]:
                alert_type = "blocked"
            elif r["needs_read"]:
                alert_type = "needs_read"
            elif r["important_now"]:
                alert_type = "important"
            elif r["due_at"] and r["due_at"] < datetime.now(timezone.utc):
                alert_type = "overdue"
            else:
                alert_type = "needs_read"

            alerts.append(
                Alert(
                    conversation_id=r["conversation_id"],
                    title=r["title"] or r["source"],
                    alert_type=alert_type,
                    priority_score=float(r["priority_score"] or 0),
                    urgency_score=float(r["urgency_score"]) if r["urgency_score"] else None,
                    due_at=r["due_at"],
                    source=r["source"],
                )
            )
        return alerts


# --- /conversations ---
@app.get("/conversations", response_model=list[Conversation])
async def get_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = None,
    needs_reply: Optional[bool] = None,
    state: Optional[str] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["state = $1 OR ($1 IS NULL AND state != 'archived')"]
        params = [state]
        idx = 2

        if source:
            conditions.append(f"source = ${idx}")
            params.append(source)
            idx += 1

        if needs_reply is not None:
            conditions.append(f"needs_reply = ${idx}")
            params.append(needs_reply)
            idx += 1

        where = " AND ".join(conditions)
        query = f"""
            SELECT * FROM life_radar.conversations
            WHERE {where}
            ORDER BY priority_score DESC NULLS LAST, last_event_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])
        rows = await conn.fetch(query, *params)
        return [Conversation(**dict(r)) for r in rows]


@app.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM life_radar.conversations WHERE id = $1", conversation_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Conversation(**dict(row))


# --- /messages ---
@app.get("/messages", response_model=list[MessageEvent])
async def get_messages(
    conversation_id: Optional[UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1

        if conversation_id:
            conditions.append(f"conversation_id = ${idx}")
            params.append(conversation_id)
            idx += 1

        if source:
            conditions.append(f"source = ${idx}")
            params.append(source)
            idx += 1

        where = f" AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT * FROM life_radar.message_events
            WHERE {where}
            ORDER BY occurred_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])
        rows = await conn.fetch(query, *params)
        return [MessageEvent(**dict(r)) for r in rows]


# --- /commitments ---
@app.get("/commitments", response_model=list[Commitment])
async def get_commitments(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.commitments
                   WHERE status = $1
                   ORDER BY due_at ASC NULLS LAST
                   LIMIT $2""",
                status, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.commitments
                   ORDER BY due_at ASC NULLS LAST
                   LIMIT $1""",
                limit,
            )
        return [Commitment(**dict(r)) for r in rows]


# --- /reminders ---
@app.get("/reminders", response_model=list[Reminder])
async def get_reminders(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.reminders
                   WHERE status = $1
                   ORDER BY remind_at ASC
                   LIMIT $2""",
                status, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.reminders
                   ORDER BY remind_at ASC
                   LIMIT $1""",
                None, limit,
            )
        return [Reminder(**dict(r)) for r in rows]


# --- /tasks (alias for planned_actions) ---
@app.get("/tasks", response_model=list[PlannedAction])
async def get_tasks(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.planned_actions
                   WHERE status = $1
                   ORDER BY scheduled_start ASC NULLS LAST
                   LIMIT $2""",
                status, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM life_radar.planned_actions
                   ORDER BY scheduled_start ASC NULLS LAST
                   LIMIT $1""",
                None, limit,
            )
        return [PlannedAction(**dict(r)) for r in rows]


@app.post("/tasks", response_model=PlannedAction)
async def create_task(task: TaskCreate):
    """Create a new task (planned action)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO life_radar.planned_actions
               (source_entity_type, title, summary, status, scheduled_start, scheduled_end, effort_estimate_minutes)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING *""",
            task.source_entity_type,
            task.title,
            task.summary,
            task.status,
            task.scheduled_start,
            task.scheduled_end,
            task.effort_estimate_minutes,
        )
        return PlannedAction(**dict(row))


# --- /calendar/events ---
@app.get("/calendar/events", response_model=list[CalendarEvent])
async def get_calendar_events(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
):
    """
    Calendar events from planned_actions with calendar_external_id set.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["calendar_external_id IS NOT NULL"]
        params = []
        idx = 1

        if from_date:
            conditions.append(f"scheduled_start >= ${idx}")
            params.append(from_date)
            idx += 1

        if to_date:
            conditions.append(f"scheduled_end <= ${idx}")
            params.append(to_date)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)
        query = f"""
            SELECT * FROM life_radar.planned_actions
            WHERE {where}
            ORDER BY scheduled_start ASC
            LIMIT ${idx}
        """
        rows = await conn.fetch(query, *params)
        return [CalendarEvent(**dict(r)) for r in rows]


@app.post("/calendar/events", response_model=PlannedAction)
async def upsert_calendar_event(event: CalendarEventUpsert):
    """
    Upsert a calendar event into planned_actions.
    If calendar_external_id is provided, updates existing event with that external_id.
    If not provided, inserts a new event.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if event.calendar_external_id:
            # Upsert: INSERT ... ON CONFLICT DO UPDATE
            row = await conn.fetchrow(
                """INSERT INTO life_radar.planned_actions
                   (title, summary, scheduled_start, scheduled_end, calendar_external_id,
                    calendar_provider, source_entity_type, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (calendar_external_id) DO UPDATE SET
                     title = EXCLUDED.title,
                     summary = EXCLUDED.summary,
                     scheduled_start = EXCLUDED.scheduled_start,
                     scheduled_end = EXCLUDED.scheduled_end,
                     calendar_provider = EXCLUDED.calendar_provider
                   RETURNING *""",
                event.title,
                event.summary,
                event.scheduled_start,
                event.scheduled_end,
                event.calendar_external_id,
                event.calendar_provider,
                "calendar",
                "scheduled",
            )
        else:
            # Plain INSERT for new events without external_id
            row = await conn.fetchrow(
                """INSERT INTO life_radar.planned_actions
                   (title, summary, scheduled_start, scheduled_end, calendar_provider,
                    source_entity_type, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   RETURNING *""",
                event.title,
                event.summary,
                event.scheduled_start,
                event.scheduled_end,
                event.calendar_provider,
                "calendar",
                "scheduled",
            )
        return PlannedAction(**dict(row))


@app.post("/messages/send", response_model=MessageSendResponse)
async def send_message(request: MessageSendRequest):
    """
    Send a message via Matrix/Outlook (user-approved only).
    
    This is a stub implementation that logs the send intent and returns a queued status.
    Full Matrix sending requires the Rust E2EE binary with access token, which the worker provides.
    To send via Matrix, the conversation must have a Matrix room_id stored in external_id.
    """
    import sys
    import uuid

    message_id = str(uuid.uuid4())

    # Log the send intent
    print(
        f"[messages/send] queued: conversation_id={request.conversation_id} "
        f"message_id={message_id} content_text={request.content_text[:50]!r}...",
        file=sys.stderr,
    )

    # TODO: Full implementation requires:
    # 1. Look up conversation to get Matrix room_id from external_id
    # 2. Use Matrix REST API with access token to send the message
    # 3. The Rust E2EE binary at /usr/local/bin/life-radar-matrix-rust-probe handles E2EE
    # For now, we log and return queued status

    return MessageSendResponse(status="queued", message_id=message_id)


# --- /memories ---
@app.get("/memories", response_model=list[MemoryRecord])
async def get_memories(
    kind: Optional[str] = None,
    subject_type: Optional[str] = None,
    active: Optional[bool] = True,
    limit: int = Query(50, ge=1, le=200),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1

        if kind:
            conditions.append(f"kind = ${idx}")
            params.append(kind)
            idx += 1

        if subject_type:
            conditions.append(f"subject_type = ${idx}")
            params.append(subject_type)
            idx += 1

        if active is not None:
            conditions.append(f"active = ${idx}")
            params.append(active)
            idx += 1

        where = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT * FROM life_radar.memory_records
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT ${idx}
        """
        params.append(limit)
        rows = await conn.fetch(query, *params)
        return [MemoryRecord(**dict(r)) for r in rows]


# --- /probe-status ---
@app.get("/probe-status", response_model=list[RuntimeProbe])
async def get_probe_status():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM life_radar.runtime_probes
               ORDER BY observed_at DESC
               LIMIT 20"""
        )
        return [RuntimeProbe(**dict(r)) for r in rows]


@app.get("/probe-status/candidates", response_model=list[MessagingCandidate])
async def get_probe_candidates():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM life_radar.messaging_candidates ORDER BY last_probe_at DESC"
        )
        return [MessagingCandidate(**dict(r)) for r in rows]


# --- /search ---
class SearchResult(BaseModel):
    type: str
    id: str
    subject: Optional[str] = None
    body: Optional[str] = None
    score: Optional[float] = None


@app.get("/search", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    use_vector: bool = Query(False, description="Use pgvector semantic search if embeddings exist"),
):
    """
    Search across conversations, messages, and memories.

    Two modes:
    - use_vector=false (default): Fast ILIKE text search across tables
    - use_vector=true: pgvector cosine similarity search (requires embeddings to be pre-generated)

    Embeddings are stored in life_radar.embeddings table. Use the worker to generate them.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        likq = f"%{q}%"

        # Check if embeddings exist for vector search
        has_embeddings = await conn.fetchval(
            "SELECT COUNT(*) > 0 FROM life_radar.embeddings WHERE embedding_model = 'text-embedding-3-small'"
        )

        results = []

        # Vector semantic search (if enabled and embeddings exist)
        if use_vector and has_embeddings:
            # TODO: Generate embedding for query text using OpenAI API
            # For now, document that this requires embeddings to be pre-generated
            # This branch will be implemented when embedding generation is wired up
            pass

        # Text search (fast ILIKE - primary for now)
        # Conversations
        convs = await conn.fetch(
            """SELECT id, 'conversation' as type, title as subject, NULL as body, priority_score
               FROM life_radar.conversations
               WHERE title ILIKE $1 OR external_id ILIKE $1
               LIMIT $2""",
            likq, limit,
        )
        for c in convs:
            results.append(SearchResult(
                type="conversation",
                id=str(c["id"]),
                subject=c["subject"],
                score=float(c["priority_score"] or 0),
            ))

        # Messages
        msgs = await conn.fetch(
            """SELECT id, 'message' as type, sender_label as subject, content_text as body, NULL as score
               FROM life_radar.message_events
               WHERE content_text ILIKE $1
               ORDER BY occurred_at DESC
               LIMIT $3""",
            likq, None, limit,
        )
        for m in msgs:
            results.append(SearchResult(
                type="message",
                id=str(m["id"]),
                subject=m["subject"],
                body=m["body"],
            ))

        # Memories
        mems = await conn.fetch(
            """SELECT id, 'memory' as type, title as subject, summary as body, confidence
               FROM life_radar.memory_records
               WHERE title ILIKE $1 OR summary ILIKE $1 OR detail ILIKE $1
               LIMIT $3""",
            likq, None, limit,
        )
        for m in mems:
            results.append(SearchResult(
                type="memory",
                id=str(m["id"]),
                subject=m["subject"],
                body=m["body"],
                score=float(m["confidence"] or 0),
            ))

        return results[:limit]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
