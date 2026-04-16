"""
LifeRadar API Server — FastAPI
"""
import os
import logging
import json
import secrets
import asyncpg
import httpx
from decimal import Decimal
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Query, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, HTMLResponse
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

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
    ConnectorStatus,
    ConnectorLoginStartRequest,
    ConnectorLoginStepRequest,
    ConnectorLoginAttempt,
    ConnectorLogoutResponse,
    MemoryRecord,
    RuntimeProbe,
    MessagingCandidate,
    Alert,
    HealthResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Shutdown: close pool
    await close_pool()


app = FastAPI(
    title="LifeRadar API",
    description="Personal intelligence and communications triage API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MCP_URL = os.environ.get("LIFE_RADAR_MCP_URL", "http://liferadar-mcp:8090")
MATRIX_BRIDGE_URL = os.environ.get(
    "LIFE_RADAR_MATRIX_BRIDGE_URL", "http://life-radar-matrix-bridge:8010"
)
CHAT_GATEWAY_URL = os.environ.get(
    "LIFE_RADAR_CHAT_GATEWAY_URL", "http://life-radar-chat-gateway:8020"
)
BEEPER_ENABLED = os.environ.get("LIFE_RADAR_BEEPER_ENABLED", "false").lower() == "true"

logger = logging.getLogger(__name__)


def _normalize_db_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_normalize_db_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_db_value(item) for key, item in value.items()}
    return value


def _record_to_model(model_cls, record):
    payload = {}
    for key, value in dict(record).items():
        normalized = _normalize_db_value(value)
        field = model_cls.model_fields.get(key)
        annotation = getattr(field, "annotation", None) if field is not None else None
        if isinstance(normalized, str) and annotation in (dict, list):
            try:
                normalized = json.loads(normalized)
            except json.JSONDecodeError:
                pass
        if normalized is None and field is not None:
            if field.default_factory is not None:
                normalized = field.default_factory()
            elif field.default is not PydanticUndefined:
                normalized = field.default
        payload[key] = normalized
    return model_cls(**payload)


def _records_to_models(model_cls, records):
    items = []
    for record in records:
        try:
            items.append(_record_to_model(model_cls, record))
        except ValidationError:
            logger.exception("Skipping invalid %s record", model_cls.__name__)
    return items


def _expected_api_key() -> str:
    return os.environ.get("LIFE_RADAR_API_KEY", "").strip()


def _provided_api_key(request: Request, allow_query_param: bool = False) -> str:
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    provided = request.headers.get("x-api-key", "").strip()
    if provided:
        return provided
    if allow_query_param:
        return request.query_params.get("api_key", "").strip()
    return ""


def require_api_key(request: Request, allow_query_param: bool = False) -> None:
    expected = _expected_api_key()
    if not expected:
        return
    provided = _provided_api_key(request, allow_query_param=allow_query_param)
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
        )


async def load_conversation_for_send(conversation_id: UUID) -> asyncpg.Record:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source, external_id FROM life_radar.conversations WHERE id = $1",
            conversation_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return row


async def run_matrix_send(room_id: str, content_text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{MATRIX_BRIDGE_URL.rstrip('/')}/send",
                json={"room_id": room_id, "content_text": content_text},
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail="Matrix bridge is unavailable") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Matrix send timed out") from exc

    if response.status_code >= 400:
        detail = response.text[:400]
        raise HTTPException(status_code=502, detail=detail or "matrix send failed")

    payload = response.json()
    event_id = payload.get("event_id")
    if not event_id:
        raise HTTPException(status_code=502, detail="Matrix bridge did not return an event_id")
    return str(event_id)


async def call_chat_gateway(method: str, path: str, payload: Optional[dict] = None) -> dict | list:
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.request(
                method=method,
                url=f"{CHAT_GATEWAY_URL.rstrip('/')}{path}",
                json=payload,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail="Chat gateway is unavailable") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Chat gateway timed out") from exc

    if response.status_code >= 400:
        detail = response.text[:400]
        raise HTTPException(status_code=response.status_code, detail=detail or "Chat gateway error")

    if not response.content:
        return {}
    return response.json()


async def run_direct_connector_send(provider: str, external_id: str, content_text: str, conversation_id: UUID) -> str:
    payload = await call_chat_gateway(
        "POST",
        "/internal/send",
        {
            "provider": provider,
            "external_id": external_id,
            "content_text": content_text,
            "conversation_id": str(conversation_id),
        },
    )
    message_id = payload.get("message_id")
    if not message_id:
        raise HTTPException(status_code=502, detail=f"{provider} gateway did not return a message_id")
    return str(message_id)


def _connector_auth_page(provider: str, api_key: str) -> str:
    safe_provider = provider.lower()
    submit_mode = "poll" if safe_provider == "whatsapp" else "submit"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LifeRadar {provider.title()} Login</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f1ea; --panel:#fffaf2; --text:#1f1b16; --accent:#0f766e; --muted:#6b6257; --border:#ddd3c3; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top, #fff8ed, var(--bg)); color:var(--text); }}
    main {{ max-width:720px; margin:48px auto; background:var(--panel); border:1px solid var(--border); border-radius:24px; padding:28px; box-shadow:0 16px 40px rgba(31,27,22,.08); }}
    h1 {{ margin-top:0; }}
    .muted {{ color:var(--muted); }}
    .hidden {{ display:none; }}
    input {{ width:100%; padding:12px 14px; margin:8px 0 12px; border-radius:12px; border:1px solid var(--border); font-size:16px; }}
    button {{ background:var(--accent); color:white; border:none; border-radius:999px; padding:12px 18px; cursor:pointer; font-size:15px; }}
    pre {{ white-space:pre-wrap; background:#f8f5ef; padding:12px; border-radius:14px; border:1px solid var(--border); }}
    #qr svg {{ width:100%; max-width:320px; height:auto; }}
  </style>
</head>
<body>
<main>
  <h1>{provider.title()} Login</h1>
  <p class="muted">This page drives the connector auth flow through the internal chat gateway.</p>
  <button id="start">Start Login</button>
  <div id="attempt" class="hidden">
    <p id="prompt"></p>
    <div id="telegram-fields" class="hidden">
      <input id="phone_number" placeholder="Phone number" />
      <input id="code" placeholder="Verification code" />
      <input id="password" placeholder="Password" type="password" />
      <button id="submit">Continue</button>
    </div>
    <div id="qr" class="hidden"></div>
    <pre id="status"></pre>
  </div>
</main>
<script>
const provider = {json.dumps(safe_provider)};
const apiKey = {json.dumps(api_key)};
let attemptId = null;
async function api(path, options={{}}) {{
  const headers = Object.assign({{"Content-Type":"application/json"}}, options.headers || {{}});
  if (apiKey) headers["X-API-Key"] = apiKey;
  const response = await fetch(path, Object.assign({{}}, options, {{ headers }}));
  const text = await response.text();
  const data = text ? JSON.parse(text) : {{}};
  if (!response.ok) throw new Error(data.detail || data.error || text || "Request failed");
  return data;
}}
function renderAttempt(data) {{
  document.getElementById("attempt").classList.remove("hidden");
  document.getElementById("prompt").textContent = data.prompt || data.state;
  document.getElementById("status").textContent = JSON.stringify(data, null, 2);
  const fieldsBox = document.getElementById("telegram-fields");
  const qrBox = document.getElementById("qr");
  fieldsBox.classList.toggle("hidden", provider === "whatsapp");
  qrBox.classList.toggle("hidden", !data.qr_svg);
  qrBox.innerHTML = data.qr_svg || "";
}}
async function poll() {{
  if (!attemptId) return;
  const data = await api(`/connectors/${{provider}}/login/${{attemptId}}`);
  renderAttempt(data);
  if (!["completed","failed","error"].includes(data.state)) {{
    setTimeout(poll, 3000);
  }}
}}
document.getElementById("start").onclick = async () => {{
  const data = await api(`/connectors/${{provider}}/login`, {{ method:"POST", body: JSON.stringify({{force:false}}) }});
  attemptId = data.attempt_id;
  renderAttempt(data);
  if ({json.dumps(submit_mode)} === "poll") poll();
}};
document.getElementById("submit").onclick = async () => {{
  if (!attemptId) return;
  const body = {{
    phone_number: document.getElementById("phone_number").value || undefined,
    code: document.getElementById("code").value || undefined,
    password: document.getElementById("password").value || undefined
  }};
  const data = await api(`/connectors/${{provider}}/login/${{attemptId}}/submit`, {{ method:"POST", body: JSON.stringify(body) }});
  renderAttempt(data);
  if (!["completed","failed","error"].includes(data.state)) poll();
}};
</script>
</body>
</html>"""


# --- MCP proxy (Streamable HTTP) ---
async def _proxy_to_mcp_impl(request: Request, path: str = ""):
    require_api_key(request)
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


@app.api_route("/mcp", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_mcp_root(request: Request):
    """Proxy MCP root requests to the MCP server container."""
    return await _proxy_to_mcp_impl(request)


@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_mcp_path(request: Request, path: str):
    """Proxy MCP path requests to the MCP server container."""
    return await _proxy_to_mcp_impl(request, path)


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


@app.get("/connectors", response_model=list[ConnectorStatus])
async def list_connectors(request: Request):
    require_api_key(request)
    payload = await call_chat_gateway("GET", "/internal/connectors")
    return payload


@app.post("/connectors/{provider}/login", response_model=ConnectorLoginAttempt)
async def start_connector_login(
    provider: str,
    body: ConnectorLoginStartRequest,
    request: Request,
):
    require_api_key(request)
    payload = await call_chat_gateway(
        "POST",
        f"/internal/connectors/{provider}/login",
        body.model_dump(),
    )
    return payload


@app.get("/connectors/{provider}/login/{attempt_id}", response_model=ConnectorLoginAttempt)
async def connector_login_status(provider: str, attempt_id: str, request: Request):
    require_api_key(request)
    payload = await call_chat_gateway(
        "GET",
        f"/internal/connectors/{provider}/login/{attempt_id}",
    )
    return payload


@app.post("/connectors/{provider}/login/{attempt_id}/submit", response_model=ConnectorLoginAttempt)
async def submit_connector_login(
    provider: str,
    attempt_id: str,
    body: ConnectorLoginStepRequest,
    request: Request,
):
    require_api_key(request)
    payload = await call_chat_gateway(
        "POST",
        f"/internal/connectors/{provider}/login/{attempt_id}/submit",
        body.model_dump(exclude_none=True),
    )
    return payload


@app.post("/connectors/{provider}/logout", response_model=ConnectorLogoutResponse)
async def connector_logout(provider: str, request: Request):
    require_api_key(request)
    payload = await call_chat_gateway(
        "POST",
        f"/internal/connectors/{provider}/logout",
        {},
    )
    return payload


@app.get("/auth/telegram", response_class=HTMLResponse)
async def telegram_auth_page(request: Request):
    require_api_key(request, allow_query_param=True)
    return HTMLResponse(_connector_auth_page("telegram", _provided_api_key(request, True)))


@app.get("/auth/whatsapp", response_class=HTMLResponse)
async def whatsapp_auth_page(request: Request):
    require_api_key(request, allow_query_param=True)
    return HTMLResponse(_connector_auth_page("whatsapp", _provided_api_key(request, True)))


@app.get("/openapi.json")
async def openapi_schema(request: Request):
    require_api_key(request)
    return JSONResponse(
        get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
    )


@app.get("/docs")
async def swagger_ui(request: Request):
    require_api_key(request)
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Swagger UI",
    )


# --- /alerts ---
@app.get("/alerts", response_model=list[Alert])
async def get_alerts(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    min_priority: Optional[float] = None,
):
    """
    Get conversations needing attention, surfaced as alerts.
    Includes: needs_reply, needs_read, important, overdue, blocked.
    """
    require_api_key(request)
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
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = None,
    needs_reply: Optional[bool] = None,
    state: Optional[str] = None,
):
    require_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["COALESCE(state, 'active') = $1 OR ($1 IS NULL AND COALESCE(state, 'active') != 'archived')"]
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
            SELECT
                id,
                source,
                external_id,
                account_id,
                title,
                COALESCE(participants, '[]'::jsonb) AS participants,
                COALESCE(state, 'active') AS state,
                COALESCE(needs_read, FALSE) AS needs_read,
                COALESCE(needs_reply, FALSE) AS needs_reply,
                COALESCE(important_now, FALSE) AS important_now,
                COALESCE(waiting_on_other, FALSE) AS waiting_on_other,
                COALESCE(follow_up_later, FALSE) AS follow_up_later,
                COALESCE(ready_to_act, FALSE) AS ready_to_act,
                COALESCE(blocked_needs_context, FALSE) AS blocked_needs_context,
                last_event_at,
                last_triaged_at,
                priority_score::double precision AS priority_score,
                urgency_score::double precision AS urgency_score,
                social_weight::double precision AS social_weight,
                reward_value::double precision AS reward_value,
                energy_fit::double precision AS energy_fit,
                effort_estimate_minutes,
                due_at,
                COALESCE(metadata, '{{}}'::jsonb) AS metadata,
                created_at,
                updated_at
            FROM life_radar.conversations
            WHERE {where}
            ORDER BY priority_score DESC NULLS LAST, last_event_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])
        rows = await conn.fetch(query, *params)
        return _records_to_models(Conversation, rows)


@app.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: UUID, request: Request):
    require_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM life_radar.conversations WHERE id = $1", conversation_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return _record_to_model(Conversation, row)


# --- /messages ---
@app.get("/messages", response_model=list[MessageEvent])
async def get_messages(
    request: Request,
    conversation_id: Optional[UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = None,
):
    require_api_key(request)
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

        where = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT
                id,
                conversation_id,
                source,
                external_id,
                sender_id,
                sender_label,
                occurred_at,
                content_text,
                COALESCE(content_json, '{{}}'::jsonb) AS content_json,
                COALESCE(is_inbound, TRUE) AS is_inbound,
                reply_needed,
                needs_read,
                needs_reply,
                importance_score::double precision AS importance_score,
                triage_summary,
                COALESCE(provenance, '{{}}'::jsonb) AS provenance,
                created_at,
                updated_at
            FROM life_radar.message_events
            WHERE {where}
            ORDER BY occurred_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])
        rows = await conn.fetch(query, *params)
        return _records_to_models(MessageEvent, rows)


# --- /commitments ---
@app.get("/commitments", response_model=list[Commitment])
async def get_commitments(
    request: Request,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    require_api_key(request)
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
        return _records_to_models(Commitment, rows)


# --- /reminders ---
@app.get("/reminders", response_model=list[Reminder])
async def get_reminders(
    request: Request,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    require_api_key(request)
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
        return _records_to_models(Reminder, rows)


# --- /tasks (alias for planned_actions) ---
@app.get("/tasks", response_model=list[PlannedAction])
async def get_tasks(
    request: Request,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    require_api_key(request)
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
                limit,
            )
        return _records_to_models(PlannedAction, rows)


@app.post("/tasks", response_model=PlannedAction)
async def create_task(task: TaskCreate, request: Request):
    """Create a new task (planned action)."""
    require_api_key(request)
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
        return _record_to_model(PlannedAction, row)


# --- /calendar/events ---
@app.get("/calendar/events", response_model=list[CalendarEvent])
async def get_calendar_events(
    request: Request,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    days: Optional[int] = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Calendar events from planned_actions with calendar_external_id set.
    """
    require_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["calendar_external_id IS NOT NULL"]
        params = []
        idx = 1
        effective_from = from_date
        effective_to = to_date

        if days is not None and effective_to is None:
            if effective_from is None:
                effective_from = datetime.now(timezone.utc)
            effective_to = effective_from + timedelta(days=days)

        if effective_from:
            # Include events that are already in progress at the window start.
            conditions.append(f"COALESCE(scheduled_end, scheduled_start) >= ${idx}")
            params.append(effective_from)
            idx += 1

        if effective_to:
            # Include events that start before the window end, even if they span beyond it.
            conditions.append(f"scheduled_start <= ${idx}")
            params.append(effective_to)
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
        return _records_to_models(CalendarEvent, rows)


@app.post("/calendar/events", response_model=PlannedAction)
async def upsert_calendar_event(event: CalendarEventUpsert, request: Request):
    """
    Upsert a calendar event into planned_actions.
    If calendar_external_id is provided, updates existing event with that external_id.
    If not provided, inserts a new event.
    """
    require_api_key(request)
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
        return _record_to_model(PlannedAction, row)


@app.post("/messages/send", response_model=MessageSendResponse)
async def send_message(request: MessageSendRequest, http_request: Request):
    """
    Send a message via an active connector (user-approved only).
    """
    require_api_key(http_request)
    conversation = await load_conversation_for_send(request.conversation_id)

    if conversation["source"] == "matrix":
        if not BEEPER_ENABLED:
            raise HTTPException(
                status_code=501,
                detail="Sending messages for source 'matrix' is disabled while Beeper integration is off",
            )
        message_id = await run_matrix_send(conversation["external_id"], request.content_text)
        return MessageSendResponse(status="sent", message_id=message_id)

    if conversation["source"] in {"telegram", "whatsapp"}:
        message_id = await run_direct_connector_send(
            conversation["source"],
            conversation["external_id"],
            request.content_text,
            request.conversation_id,
        )
        return MessageSendResponse(status="sent", message_id=message_id)

    raise HTTPException(
        status_code=501,
        detail=f"Sending messages for source '{conversation['source']}' is not implemented",
    )


# --- /memories ---
@app.get("/memories", response_model=list[MemoryRecord])
async def get_memories(
    request: Request,
    kind: Optional[str] = None,
    subject_type: Optional[str] = None,
    active: Optional[bool] = True,
    limit: int = Query(50, ge=1, le=200),
):
    require_api_key(request)
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
        return _records_to_models(MemoryRecord, rows)


# --- /probe-status ---
@app.get("/probe-status", response_model=list[RuntimeProbe])
async def get_probe_status(request: Request):
    require_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                   id,
                   candidate_id,
                   candidate_type,
                   COALESCE(status, 'ok') AS status,
                   observed_at,
                   latency_ms,
                   freshness_seconds,
                   total_events,
                   decrypt_failures,
                   encrypted_non_text,
                   running_processes,
                   COALESCE(metadata, '{}'::jsonb) AS metadata,
                   notes
               FROM life_radar.runtime_probes
               ORDER BY observed_at DESC
               LIMIT 20"""
        )
        return _records_to_models(RuntimeProbe, rows)


@app.get("/probe-status/candidates", response_model=list[MessagingCandidate])
async def get_probe_candidates(request: Request):
    require_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                   candidate_id,
                   candidate_type,
                   COALESCE(last_status, 'ok') AS last_status,
                   last_probe_at,
                   latest_freshness_seconds,
                   latest_total_events,
                   latest_decrypt_failures,
                   latest_encrypted_non_text,
                   latest_running_processes,
                   latest_notes,
                   COALESCE(metadata, '{}'::jsonb) AS metadata,
                   updated_at
               FROM life_radar.messaging_candidates
               ORDER BY last_probe_at DESC"""
        )
        return _records_to_models(MessagingCandidate, rows)


# --- /search ---
class SearchResult(BaseModel):
    type: str
    id: str
    subject: Optional[str] = None
    body: Optional[str] = None
    score: Optional[float] = None


@app.get("/search", response_model=list[SearchResult])
async def search(
    request: Request,
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
    require_api_key(request)
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
               LIMIT $2""",
            likq, limit,
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
               LIMIT $2""",
            likq, limit,
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
