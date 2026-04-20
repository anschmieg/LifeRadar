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
from pathlib import Path
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
    MatrixLoginRequest,
    MatrixLoginResponse,
    MatrixSessionStatus,
    MatrixDeviceSummary,
    MatrixDeviceVerificationAttempt,
    MatrixDeviceVerificationDecisionRequest,
    MatrixDeviceVerificationStartRequest,
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

MCP_URL = os.environ.get("LIFERADAR_MCP_URL", "http://liferadar-mcp:8090")
MATRIX_BRIDGE_URL = os.environ.get(
    "LIFERADAR_MATRIX_BRIDGE_URL", "http://liferadar-matrix-bridge:8010"
)
CHAT_GATEWAY_URL = os.environ.get(
    "LIFERADAR_CHAT_GATEWAY_URL", "http://liferadar-chat-gateway:8020"
)
MATRIX_HOMESERVER_URL = os.environ.get(
    "LIFERADAR_MATRIX_HOMESERVER_URL", "https://matrix.beeper.com"
).rstrip("/")
MATRIX_SESSION_PATH = Path(
    os.environ.get("MATRIX_RUST_SESSION_PATH", "/app/identity/matrix-session.json")
)
MATRIX_STORE_PATH = Path(
    os.environ.get("MATRIX_RUST_STORE", "/app/identity/matrix-rust-sdk-store")
)

logger = logging.getLogger(__name__)


def is_matrix_enabled() -> bool:
    return os.environ.get("LIFERADAR_MATRIX_ENABLED", "true").lower() != "false"


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
    return os.environ.get("LIFERADAR_API_KEY", "").strip()


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


def _detect_matrix_identifier(
    raw_identifier: str,
    phone_country: Optional[str],
    identifier_kind: Optional[str] = None,
) -> dict:
    identifier = raw_identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required")

    normalized_kind = (identifier_kind or "").strip().lower()
    if normalized_kind:
        if normalized_kind == "email":
            if "@" not in identifier:
                raise HTTPException(status_code=400, detail="Enter a valid email address")
            return {
                "type": "m.id.thirdparty",
                "medium": "email",
                "address": identifier.lower(),
            }
        if normalized_kind == "username":
            return {"type": "m.id.user", "user": identifier}
        if normalized_kind in {"matrix_id", "matrix"}:
            return {"type": "m.id.user", "user": identifier}
        raise HTTPException(status_code=400, detail="Unsupported Matrix identifier type")

    if identifier.startswith("@") or ":" in identifier or ("@" not in identifier and identifier.replace("_", "").replace("-", "").isalnum()):
        return {"type": "m.id.user", "user": identifier}

    if "@" in identifier:
        return {
            "type": "m.id.thirdparty",
            "medium": "email",
            "address": identifier.lower(),
        }

    digits = "".join(ch for ch in identifier if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return {
            "type": "m.id.thirdparty",
            "medium": "msisdn",
            "address": digits[1:],
        }

    if digits and phone_country:
        return {
            "type": "m.id.phone",
            "country": phone_country.strip().upper(),
            "phone": digits,
        }

    return {"type": "m.id.user", "user": identifier}


def _read_matrix_session_file() -> Optional[dict]:
    if not MATRIX_SESSION_PATH.is_file():
        return None
    try:
        return json.loads(MATRIX_SESSION_PATH.read_text())
    except Exception:
        logger.exception("Failed to read Matrix session file")
        return None


def _write_matrix_session_file(payload: dict) -> None:
    MATRIX_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = MATRIX_SESSION_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2))
    temp_path.replace(MATRIX_SESSION_PATH)


def _reset_matrix_local_identity_state() -> None:
    if MATRIX_SESSION_PATH.exists():
        MATRIX_SESSION_PATH.unlink()
    if MATRIX_STORE_PATH.exists():
        if MATRIX_STORE_PATH.is_dir():
            import shutil
            shutil.rmtree(MATRIX_STORE_PATH)
        else:
            MATRIX_STORE_PATH.unlink()


async def _matrix_whoami(access_token: str, homeserver: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{homeserver.rstrip('/')}/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("error", "")
        except Exception:
            detail = response.text[:400]
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or "Matrix session is invalid",
        )
    return response.json()


async def ensure_valid_matrix_session() -> dict:
    session = _read_matrix_session_file()
    if not session:
        raise HTTPException(status_code=401, detail="No local Matrix session. Sign in again.")
    try:
        await _matrix_whoami(session["access_token"], session["homeserver"])
    except HTTPException as exc:
        if exc.status_code == 401:
            _reset_matrix_local_identity_state()
            raise HTTPException(
                status_code=401,
                detail="Matrix session expired or was revoked. Sign in again.",
            ) from exc
        raise
    return session


async def perform_matrix_password_login(body: MatrixLoginRequest) -> MatrixLoginResponse:
    password = body.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")

    identifier = _detect_matrix_identifier(
        body.identifier,
        None,
        body.identifier_kind,
    )
    request_body = {
        "type": "m.login.password",
        "identifier": identifier,
        "password": password,
        "initial_device_display_name": body.initial_device_display_name or "LifeRadar Matrix",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{MATRIX_HOMESERVER_URL}/_matrix/client/v3/login",
                json=request_body,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail="Matrix homeserver is unavailable") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Matrix login timed out") from exc

    payload = {}
    if response.content:
        try:
            payload = response.json()
        except Exception:
            payload = {}

    if response.status_code >= 400:
        detail = payload.get("error") or response.text[:400] or "Matrix login failed"
        raise HTTPException(status_code=response.status_code, detail=detail)

    access_token = payload.get("access_token")
    user_id = payload.get("user_id")
    device_id = payload.get("device_id")
    if not access_token or not user_id or not device_id:
        raise HTTPException(status_code=502, detail="Matrix homeserver did not return a complete session")

    _reset_matrix_local_identity_state()
    _write_matrix_session_file(
        {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token"),
            "user_id": user_id,
            "device_id": device_id,
            "homeserver": MATRIX_HOMESERVER_URL,
            "expires_at": None,
            "expires_in": payload.get("expires_in_ms"),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    return MatrixLoginResponse(
        status="logged_in",
        user_id=user_id,
        device_id=device_id,
        homeserver=MATRIX_HOMESERVER_URL,
        verification_required=True,
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


async def call_matrix_bridge(method: str, path: str, payload: Optional[dict] = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.request(
                method=method,
                url=f"{MATRIX_BRIDGE_URL.rstrip('/')}{path}",
                json=payload,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail="Matrix bridge is unavailable") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Matrix bridge timed out") from exc

    if response.status_code >= 400:
        detail = response.text[:400]
        raise HTTPException(status_code=response.status_code, detail=detail or "Matrix bridge error")

    if not response.content:
        return {}
    return response.json()


async def _matrix_list_devices(session: dict) -> list[MatrixDeviceSummary]:
    homeserver = str(session.get("homeserver") or MATRIX_HOMESERVER_URL).rstrip("/")
    access_token = session["access_token"]
    user_id = session["user_id"]
    current_device_id = session.get("device_id")

    async with httpx.AsyncClient(timeout=30.0) as client:
        devices_response = await client.get(
            f"{homeserver}/_matrix/client/v3/devices",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if devices_response.status_code >= 400:
            detail = ""
            try:
                detail = devices_response.json().get("error", "")
            except Exception:
                detail = devices_response.text[:400]
            raise HTTPException(
                status_code=devices_response.status_code,
                detail=detail or "Could not load Matrix devices",
            )
        devices_payload = devices_response.json()
        devices = devices_payload.get("devices") or []

        query_payload = {
            "timeout": 10000,
            "device_keys": {
                user_id: [
                    str(item.get("device_id"))
                    for item in devices
                    if item.get("device_id")
                ]
            },
        }
        keys_response = await client.post(
            f"{homeserver}/_matrix/client/v3/keys/query",
            headers={"Authorization": f"Bearer {access_token}"},
            json=query_payload,
        )
        device_keys = {}
        master_key = None
        self_signing_key = None
        if keys_response.status_code < 400:
            keys_payload = keys_response.json()
            device_keys = (
                keys_payload.get("device_keys", {})
                .get(user_id, {})
            )
            master_key = (
                keys_payload.get("master_keys", {})
                .get(user_id)
            )
            self_signing_key = (
                keys_payload.get("self_signing_keys", {})
                .get(user_id)
            )

    verified_device_ids: set[str] = set()
    if master_key and self_signing_key:
        for device_id, key_payload in device_keys.items():
            signatures = key_payload.get("signatures", {}).get(user_id, {})
            self_signing_values = set((self_signing_key.get("keys") or {}).values())
            if self_signing_values and any(sig in self_signing_values for sig in signatures.values()):
                verified_device_ids.add(device_id)

    items: list[MatrixDeviceSummary] = []
    for item in devices:
        device_id = str(item.get("device_id") or "")
        keys_payload = device_keys.get(device_id, {}) if isinstance(device_keys, dict) else {}
        algorithms = keys_payload.get("algorithms") or []
        supports_encryption = (
            "m.olm.v1.curve25519-aes-sha2" in algorithms
            and "m.megolm.v1.aes-sha2" in algorithms
        )
        items.append(
            MatrixDeviceSummary(
                device_id=device_id,
                display_name=item.get("display_name"),
                last_seen_ip=item.get("last_seen_ip"),
                last_seen_ts=(
                    datetime.fromtimestamp(item["last_seen_ts"] / 1000, tz=timezone.utc)
                    if item.get("last_seen_ts")
                    else None
                ),
                is_current=device_id == current_device_id,
                is_verified=device_id in verified_device_ids if device_id else None,
                supports_encryption=supports_encryption,
            )
        )

    def sort_key(device: MatrixDeviceSummary) -> tuple[int, int, int, str]:
        return (
            1 if device.is_current else 0,
            0 if device.is_verified else 1,
            0 if device.supports_encryption else 1,
            (device.display_name or device.device_id).lower(),
        )

    return sorted(items, key=sort_key)


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
    submit_mode = "poll" if safe_provider in {"whatsapp", "telegram"} else "submit"
    title = provider.title()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LifeRadar {title} Login</title>
  <style>
    :root {{
      color-scheme: light;
      --bg:#f4f1ea;
      --panel:#fffaf2;
      --text:#1f1b16;
      --accent:#0f766e;
      --accent-2:#134e4a;
      --muted:#6b6257;
      --border:#ddd3c3;
      --success:#166534;
      --error:#b91c1c;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:
      radial-gradient(circle at top, #fff8ed 0%, #f8f2e6 42%, var(--bg) 100%); color:var(--text); }}
    main {{ max-width:760px; margin:48px auto; background:var(--panel); border:1px solid var(--border); border-radius:28px; padding:32px; box-shadow:0 16px 40px rgba(31,27,22,.08); }}
    h1 {{ margin:0 0 8px; font-size:clamp(2rem,4vw,3rem); }}
    h2 {{ margin:0 0 8px; font-size:1.2rem; }}
    .muted {{ color:var(--muted); }}
    .hidden {{ display:none; }}
    .hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:28px; }}
    .badge {{ display:inline-flex; align-items:center; gap:8px; font-size:.8rem; letter-spacing:.08em; text-transform:uppercase; color:var(--accent-2); background:#e6f4f1; border:1px solid #cce8e3; border-radius:999px; padding:6px 10px; }}
    .card {{ border:1px solid var(--border); border-radius:20px; background:#fffdf8; padding:20px; }}
    .stack {{ display:grid; gap:16px; }}
    .steps {{ display:flex; gap:10px; flex-wrap:wrap; margin:12px 0 0; padding:0; list-style:none; }}
    .steps li {{ border:1px solid var(--border); border-radius:999px; padding:8px 12px; font-size:.9rem; color:var(--muted); background:#faf6ee; }}
    .steps li.active {{ color:var(--accent-2); border-color:#9fd5cb; background:#ecf8f5; }}
    .steps li.done {{ color:var(--success); border-color:#a7d8b4; background:#eefbf1; }}
    label {{ display:block; font-weight:600; margin:0 0 6px; }}
    input {{ width:100%; padding:14px 16px; margin:0; border-radius:14px; border:1px solid var(--border); font-size:16px; background:white; }}
    input:focus {{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(15,118,110,.12); }}
    button {{ background:var(--accent); color:white; border:none; border-radius:999px; padding:13px 18px; cursor:pointer; font-size:15px; font-weight:600; }}
    button.secondary {{ background:#efe8dc; color:var(--text); }}
    button:disabled {{ opacity:.55; cursor:wait; }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; }}
    .status {{ border-radius:16px; padding:14px 16px; font-size:.95rem; }}
    .status.info {{ background:#f5f8fa; border:1px solid #d6e3ea; }}
    .status.success {{ background:#eefbf1; border:1px solid #b9e5c4; color:var(--success); }}
    .status.error {{ background:#fff0f0; border:1px solid #f3c6c6; color:var(--error); }}
    .qr-wrap {{ display:grid; justify-items:center; gap:16px; padding:24px; background:#fff; border:1px dashed #cfc3b3; border-radius:20px; }}
    #qr svg {{ width:100%; max-width:320px; height:auto; }}
    .hint {{ font-size:.92rem; color:var(--muted); }}
    .field {{ display:grid; gap:6px; }}
    .field.hidden {{ display:none; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    @media (max-width: 720px) {{
      main {{ margin:20px; padding:22px; }}
      .hero {{ flex-direction:column; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <div class="badge">LifeRadar Connector</div>
      <h1>{title} Login</h1>
      <p class="muted">
        {"Link your account by scanning a QR code, just like the normal web client flow." if safe_provider == "whatsapp" else "Link your Telegram account using QR by default, with a phone-and-code fallback if you prefer."}
      </p>
      <ul class="steps" id="steps">
        <li id="step-start" class="active">Start</li>
        <li id="step-verify">Verify</li>
        <li id="step-done">Connected</li>
      </ul>
    </div>
    <div class="card">
      <h2>{title}</h2>
      <div class="hint" id="summary">
        {"Open the pair screen and scan the QR code with your phone." if safe_provider == "whatsapp" else "Start with QR. If you want, you can switch to a phone-and-code login instead."}
      </div>
    </div>
  </section>

  <section class="stack">
    <div class="actions">
      <button id="start">Start Login</button>
      {"<button id='useCode' class='secondary'>Use Phone + Code Instead</button>" if safe_provider == "telegram" else ""}
      <button id="refresh" class="secondary hidden">Refresh Status</button>
    </div>

    <div id="attempt" class="hidden stack">
      <div id="statusBox" class="status info">Waiting to start.</div>

      <div id="telegram-fields" class="card hidden stack">
        <div class="field hidden" id="field-phone">
          <label for="phone_number">Phone number</label>
          <input id="phone_number" placeholder="+49 170 1234567" autocomplete="tel" />
        </div>
        <div class="field hidden" id="field-code">
          <label for="code">Verification code</label>
          <input id="code" placeholder="Telegram login code" inputmode="numeric" />
        </div>
        <div class="actions">
          <button id="submit">Continue</button>
        </div>
      </div>

      <div id="qr-panel" class="card hidden stack">
        <div class="qr-wrap">
          <div id="qr"></div>
          <div class="hint" id="qrHint">{'On your phone: <span class="mono">WhatsApp → Settings → Linked Devices → Link a Device</span>' if safe_provider == 'whatsapp' else 'On your phone: <span class="mono">Telegram → Settings → Devices → Link Desktop Device</span>'}</div>
        </div>
      </div>

      <div id="doneBox" class="card hidden">
        <h2>Connected</h2>
        <div class="hint">LifeRadar can now keep this connector session alive and backfill chat history.</div>
      </div>
    </div>
  </section>
</main>
<script>
const provider = {json.dumps(safe_provider)};
const apiKey = {json.dumps(api_key)};
let attemptId = null;
let pollTimer = null;
let telegramMode = "qr";

async function api(path, options={{}}) {{
  const headers = Object.assign({{"Content-Type":"application/json"}}, options.headers || {{}});
  if (apiKey) headers["X-API-Key"] = apiKey;
  const response = await fetch(path, Object.assign({{}}, options, {{ headers }}));
  const text = await response.text();
  const data = text ? JSON.parse(text) : {{}};
  if (!response.ok) throw new Error(data.detail || data.error || text || "Request failed");
  return data;
}}

function setStatus(kind, text) {{
  const box = document.getElementById("statusBox");
  box.className = `status ${{kind}}`;
  box.textContent = text;
}}

function setStep(state) {{
  const states = {{
    start: ["step-start"],
    awaiting_phone: ["step-start", "step-verify"],
    awaiting_code: ["step-start", "step-verify"],
    awaiting_password: ["step-start", "step-verify"],
    initializing: ["step-start"],
    awaiting_qr_scan: ["step-start", "step-verify"],
    completed: ["step-start", "step-verify", "step-done"],
  }};
  const active = new Set(states[state] || ["step-start"]);
  for (const id of ["step-start","step-verify","step-done"]) {{
    const el = document.getElementById(id);
    el.classList.remove("active","done");
    if (active.has(id)) el.classList.add(id === "step-done" && state === "completed" ? "done" : "active");
    if (state === "completed") el.classList.add("done");
  }}
}}

function toggleField(id, visible) {{
  document.getElementById(id).classList.toggle("hidden", !visible);
}}

function renderAttempt(data) {{
  document.getElementById("attempt").classList.remove("hidden");
  document.getElementById("refresh").classList.remove("hidden");
  document.getElementById("summary").textContent = data.prompt || (data.state === "completed" ? "Account linked successfully." : "Follow the current login step.");
  document.getElementById("doneBox").classList.toggle("hidden", data.state !== "completed");
  setStep(data.state);

  const isWhatsapp = provider === "whatsapp";
  const isTelegramQr = provider === "telegram" && (data.metadata?.mode || telegramMode) === "qr";
  document.getElementById("qr-panel").classList.toggle("hidden", !(isWhatsapp || isTelegramQr) || data.state === "completed");
  document.getElementById("telegram-fields").classList.toggle("hidden", provider !== "telegram" || isTelegramQr || data.state === "completed");
  document.getElementById("qr").innerHTML = data.qr_svg || "";

  toggleField("field-phone", data.state === "awaiting_phone");
  toggleField("field-code", data.state === "awaiting_code");

  if (data.state === "completed") {{
    setStatus("success", "Connected successfully. LifeRadar is now syncing this account.");
    return;
  }}
  if (data.error) {{
    setStatus("error", data.error);
    return;
  }}
  if (data.state === "awaiting_qr_scan") {{
    setStatus("info", "Scan the QR code with your phone to finish linking.");
    return;
  }}
  if (data.state === "awaiting_phone") {{
    setStatus("info", "Enter the phone number for the account you want to connect.");
    return;
  }}
  if (data.state === "awaiting_code") {{
    setStatus("info", "Enter the verification code that was sent to Telegram.");
    return;
  }}
  setStatus("info", data.prompt || "Waiting for the next step.");
}}

async function poll() {{
  if (!attemptId) return;
  try {{
    const data = await api(`/connectors/${{provider}}/login/${{attemptId}}`);
    renderAttempt(data);
    if (!["completed","failed","error"].includes(data.state)) {{
      pollTimer = setTimeout(poll, 2500);
    }}
  }} catch (error) {{
    setStatus("error", error.message || "Could not refresh login status.");
  }}
}}

function clearInputs() {{
  for (const id of ["phone_number","code"]) {{
    const el = document.getElementById(id);
    if (el) el.value = "";
  }}
}}

document.getElementById("start").onclick = async () => {{
  clearInputs();
  setStatus("info", "Starting login…");
  const button = document.getElementById("start");
  button.disabled = true;
  try {{
    const data = await api(`/connectors/${{provider}}/login`, {{ method:"POST", body: JSON.stringify({{force:false, mode: provider === "telegram" ? telegramMode : undefined}}) }});
    attemptId = data.attempt_id;
    renderAttempt(data);
    if ({json.dumps(submit_mode)} === "poll" || data.state === "initializing" || data.state === "awaiting_qr_scan") poll();
  }} catch (error) {{
    setStatus("error", error.message || "Could not start login.");
  }} finally {{
    button.disabled = false;
  }}
}};
document.getElementById("submit").onclick = async () => {{
  if (!attemptId) return;
  const button = document.getElementById("submit");
  button.disabled = true;
  const body = {{
    mode: provider === "telegram" ? telegramMode : undefined,
    phone_number: document.getElementById("phone_number").value || undefined,
    code: document.getElementById("code").value || undefined
  }};
  try {{
    const data = await api(`/connectors/${{provider}}/login/${{attemptId}}/submit`, {{ method:"POST", body: JSON.stringify(body) }});
    renderAttempt(data);
    if (!["completed","failed","error"].includes(data.state)) poll();
  }} catch (error) {{
    setStatus("error", error.message || "Login step failed.");
  }} finally {{
    button.disabled = false;
  }}
}};
document.getElementById("refresh").onclick = async () => {{
  if (pollTimer) clearTimeout(pollTimer);
  await poll();
}};
const useCode = document.getElementById("useCode");
if (useCode) {{
  useCode.onclick = () => {{
    telegramMode = "code";
    document.getElementById("attempt").classList.remove("hidden");
    document.getElementById("telegram-fields").classList.remove("hidden");
    document.getElementById("qr-panel").classList.add("hidden");
    toggleField("field-phone", true);
    toggleField("field-code", false);
    setStatus("info", "Phone-and-code login selected. If Telegram requires 2FA, use QR login instead.");
    document.getElementById("summary").textContent = "Enter your phone number and then the Telegram confirmation code.";
  }};
}}
</script>
</body>
</html>"""


def _matrix_device_verification_page(api_key: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LifeRadar Matrix Device Verification</title>
  <style>
    :root {{
      color-scheme: light;
      --bg:#f4f1ea;
      --panel:#fffaf2;
      --text:#1f1b16;
      --accent:#0f766e;
      --accent-2:#134e4a;
      --muted:#6b6257;
      --border:#ddd3c3;
      --success:#166534;
      --error:#b91c1c;
      --warn:#92400e;
      --soft:#f7f2e8;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:
      radial-gradient(circle at top, #fff8ed 0%, #f8f2e6 42%, var(--bg) 100%); color:var(--text); }}
    main {{ max-width:900px; margin:48px auto; background:var(--panel); border:1px solid var(--border); border-radius:28px; padding:32px; box-shadow:0 16px 40px rgba(31,27,22,.08); }}
    h1 {{ margin:0 0 8px; font-size:clamp(2rem,4vw,3rem); }}
    h2 {{ margin:0 0 8px; font-size:1.2rem; }}
    .muted {{ color:var(--muted); }}
    .hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:28px; }}
    .badge {{ display:inline-flex; align-items:center; gap:8px; font-size:.8rem; letter-spacing:.08em; text-transform:uppercase; color:var(--accent-2); background:#e6f4f1; border:1px solid #cce8e3; border-radius:999px; padding:6px 10px; }}
    .card {{ border:1px solid var(--border); border-radius:20px; background:#fffdf8; padding:20px; }}
    .stack {{ display:grid; gap:16px; }}
    .status {{ border-radius:16px; padding:14px 16px; font-size:.95rem; }}
    .status.info {{ background:#f5f8fa; border:1px solid #d6e3ea; }}
    .status.success {{ background:#eefbf1; border:1px solid #b9e5c4; color:var(--success); }}
    .status.error {{ background:#fff0f0; border:1px solid #f3c6c6; color:var(--error); }}
    .status.warn {{ background:#fff7ed; border:1px solid #f5d0a6; color:var(--warn); }}
    .grid {{ display:grid; gap:16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    label {{ display:block; font-weight:600; margin:0 0 6px; }}
    input {{ width:100%; padding:14px 16px; border-radius:14px; border:1px solid var(--border); font-size:16px; background:white; }}
    input:focus {{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(15,118,110,.12); }}
    button {{ background:var(--accent); color:white; border:none; border-radius:999px; padding:13px 18px; cursor:pointer; font-size:15px; font-weight:600; }}
    button.secondary {{ background:#efe8dc; color:var(--text); }}
    button.danger {{ background:#8b1e1e; }}
    button:disabled {{ opacity:.55; cursor:wait; }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; }}
    .hidden {{ display:none; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .segmented {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .segmented input {{ position:absolute; opacity:0; pointer-events:none; width:1px; height:1px; }}
    .choice {{ display:inline-flex; align-items:center; gap:8px; padding:12px 14px; border:1px solid var(--border); border-radius:999px; background:#faf6ee; cursor:pointer; font-weight:600; }}
    .choice.active {{ border-color:#9fd5cb; background:#ecf8f5; color:var(--accent-2); }}
    .device-list {{ display:grid; gap:12px; }}
    .device-option {{ display:grid; gap:8px; border:1px solid var(--border); border-radius:18px; background:white; padding:16px; cursor:pointer; text-align:left; color:var(--text); font:inherit; }}
    .device-option.active {{ border-color:#9fd5cb; box-shadow:0 0 0 3px rgba(15,118,110,.08); background:#f4fbf9; }}
    .device-top {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
    .device-meta {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .device-name {{ font-size:1rem; font-weight:700; line-height:1.35; }}
    .device-id {{ margin-top:4px; }}
    .pill {{ display:inline-flex; align-items:center; gap:6px; border:1px solid var(--border); border-radius:999px; padding:6px 10px; font-size:.82rem; background:var(--soft); color:var(--muted); }}
    .pill.good {{ background:#eefbf1; border-color:#b9e5c4; color:var(--success); }}
    .pill.warn {{ background:#fff7ed; border-color:#f5d0a6; color:var(--warn); }}
    .pill.info {{ background:#ecf8f5; border-color:#9fd5cb; color:var(--accent-2); }}
    .emoji-grid {{ display:grid; gap:10px; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }}
    .emoji {{ border:1px solid var(--border); border-radius:18px; background:white; padding:14px 10px; text-align:center; }}
    .emoji .symbol {{ font-size:2rem; line-height:1; margin-bottom:6px; }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; background:#fbf7f0; border:1px solid var(--border); border-radius:16px; padding:14px; max-height:220px; overflow:auto; }}
    @media (max-width: 720px) {{
      main {{ margin:20px; padding:22px; }}
      .hero {{ flex-direction:column; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <div class="badge">LifeRadar Matrix</div>
      <h1>Device Verification</h1>
      <p class="muted">Sign in in the format you actually use, let LifeRadar create a fresh Matrix device, then choose the existing trusted client that should verify it.</p>
    </div>
    <div class="card">
      <h2>Expected flow</h2>
      <div class="muted">1. Choose username, email, or full Matrix ID. 2. Sign in. 3. Pick a trusted device from the list. 4. Accept there. 5. Compare emojis and confirm here.</div>
    </div>
  </section>

  <section class="stack">
    <div class="card stack" id="loginPanel">
      <div class="field">
        <label>How do you sign in?</label>
        <div class="segmented" id="identifierKinds">
          <label class="choice active" for="kind-username"><input id="kind-username" type="radio" name="identifier_kind" value="username" checked />Username</label>
          <label class="choice" for="kind-email"><input id="kind-email" type="radio" name="identifier_kind" value="email" />Email</label>
          <label class="choice" for="kind-matrix"><input id="kind-matrix" type="radio" name="identifier_kind" value="matrix_id" />Matrix ID</label>
        </div>
      </div>
      <div class="field">
        <label for="identifier">Identifier</label>
        <input id="identifier" placeholder="anschmieg" />
        <div class="muted" id="identifierHelp">Use the Beeper username you normally type into a Matrix-style login form.</div>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" type="password" placeholder="Matrix / Beeper password" />
      </div>
      <div class="actions">
        <button id="login">Sign In</button>
      </div>
    </div>

    <div class="card stack">
      <div class="grid">
        <div>
          <label for="current_device_id">New LifeRadar device id</label>
          <input id="current_device_id" class="mono" placeholder="DEVICEID" disabled />
        </div>
        <div>
          <label for="user_id">Matrix user id</label>
          <input id="user_id" class="mono" placeholder="@user:server" disabled />
        </div>
      </div>
      <div class="muted" id="postLoginHint">Sign in first. After that, LifeRadar will show the trusted devices it can verify against.</div>
    </div>

    <div id="statusBox" class="status info">Sign in to create a fresh LifeRadar Matrix device. Then choose a trusted client and LifeRadar will start verification automatically.</div>

    <div class="card stack hidden" id="devicePickerPanel">
      <div>
        <h2>Choose a trusted device</h2>
        <div class="muted">Pick the client that should receive the verification request. Element is usually the most reliable choice.</div>
      </div>
      <div id="deviceList" class="device-list"></div>
      <div class="actions">
        <button id="retryDevices" class="secondary">Refresh Device List</button>
        <button id="refresh" class="secondary hidden">Refresh Verification Status</button>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Attempt</h2>
        <div class="muted">Attempt id</div>
        <div id="attemptId" class="mono">not started</div>
        <div class="muted" style="margin-top:12px;">Flow id</div>
        <div id="flowId" class="mono">not started</div>
      </div>
      <div class="card">
        <h2>Status</h2>
        <div class="muted">Current state</div>
        <div id="stateLabel" class="mono">idle</div>
        <div class="muted" style="margin-top:12px;">Verifier device</div>
        <div id="targetLabel" class="mono">—</div>
      </div>
    </div>

    <div id="emojiPanel" class="card hidden stack">
      <div>
        <h2>Compare These Emojis</h2>
        <div class="muted">Match these with the verification sheet shown in the trusted client you selected.</div>
      </div>
      <div id="emojiGrid" class="emoji-grid"></div>
      <div class="muted">Decimals: <span id="decimals" class="mono">—</span></div>
      <div class="actions">
        <button id="confirm">Yes, they match</button>
        <button id="reject" class="danger">No, they do not match</button>
        <button id="cancel" class="secondary">Cancel</button>
      </div>
    </div>

    <div class="card">
      <h2>Bridge Log</h2>
      <pre id="logs">No bridge activity yet.</pre>
    </div>
  </section>
</main>
<script>
const apiKey = {json.dumps(api_key)};
let attemptId = null;
let pollTimer = null;
let currentSession = null;
let selectedVerifierDeviceId = null;
let selectedVerifierDeviceLabel = "";

const identifierHelp = {{
  username: "Use the Beeper username you normally type into a Matrix-style login form.",
  email: "Use the email address attached to your Matrix or Beeper login.",
  matrix_id: "Use your full Matrix ID, for example @user:beeper.com.",
}};

async function api(path, options={{}}) {{
  const headers = Object.assign({{"Content-Type":"application/json"}}, options.headers || {{}});
  if (apiKey) headers["X-API-Key"] = apiKey;
  const response = await fetch(path, Object.assign({{}}, options, {{ headers }}));
  const text = await response.text();
  const data = text ? JSON.parse(text) : {{}};
  if (!response.ok) throw new Error(data.detail || data.error || text || "Request failed");
  return data;
}}

function selectedIdentifierKind() {{
  const chosen = document.querySelector('input[name="identifier_kind"]:checked');
  return chosen ? chosen.value : "username";
}}

function updateIdentifierUi() {{
  const kind = selectedIdentifierKind();
  for (const label of document.querySelectorAll('#identifierKinds .choice')) {{
    const input = label.querySelector('input');
    label.classList.toggle('active', !!input && input.checked);
  }}
  const input = document.getElementById("identifier");
  if (kind === "email") {{
    input.placeholder = "you@example.com";
  }} else if (kind === "matrix_id") {{
    input.placeholder = "@user:beeper.com";
  }} else {{
    input.placeholder = "anschmieg";
  }}
  document.getElementById("identifierHelp").textContent = identifierHelp[kind] || "";
}}

function setStatus(kind, text) {{
  const box = document.getElementById("statusBox");
  box.className = `status ${{kind}}`;
  box.textContent = text;
}}

function formatIsoMinuteLocal(value) {{
  if (!value) return null;
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return null;
  const year = String(when.getFullYear());
  const month = String(when.getMonth() + 1).padStart(2, "0");
  const day = String(when.getDate()).padStart(2, "0");
  const hours = String(when.getHours()).padStart(2, "0");
  const minutes = String(when.getMinutes()).padStart(2, "0");
  return `${{year}}-${{month}}-${{day}} ${{hours}}:${{minutes}}`;
}}

function describeLastSeen(device) {{
  if (!device.last_seen_ts) return "Last seen unknown";
  const formatted = formatIsoMinuteLocal(device.last_seen_ts);
  return formatted ? `Last seen ${{formatted}}` : "Last seen unknown";
}}

function renderAttempt(data) {{
  attemptId = data.attempt_id;
  document.getElementById("attemptId").textContent = data.attempt_id || "—";
  document.getElementById("flowId").textContent = data.flow_id || "waiting";
  document.getElementById("stateLabel").textContent = data.status || "unknown";
  document.getElementById("targetLabel").textContent = data.target_device_id || "—";
  document.getElementById("refresh").classList.remove("hidden");
  document.getElementById("logs").textContent = (data.logs && data.logs.length ? data.logs.join("\\n") : "No bridge activity yet.");

  const emojiPanel = document.getElementById("emojiPanel");
  const emojiGrid = document.getElementById("emojiGrid");
  const shouldShowEmoji = data.status === "waiting_for_confirm" && data.emojis && data.emojis.length;
  emojiPanel.classList.toggle("hidden", !shouldShowEmoji);
  emojiGrid.innerHTML = "";
  if (shouldShowEmoji) {{
    for (const item of data.emojis) {{
      const node = document.createElement("div");
      node.className = "emoji";
      node.innerHTML = `<div class="symbol">${{item.symbol}}</div><div>${{item.description}}</div>`;
      emojiGrid.appendChild(node);
    }}
  }}
  document.getElementById("decimals").textContent = (data.decimals || []).length ? data.decimals.join(" · ") : "—";

  if (data.error) {{
    setStatus("error", data.error);
  }} else if (data.status === "done") {{
    setStatus("success", data.detail || "Verification completed.");
  }} else if (data.status === "cancelled") {{
    setStatus("warn", data.detail || data.error || "Verification was cancelled.");
  }} else if (data.status === "waiting_for_accept") {{
    const target = selectedVerifierDeviceLabel || data.target_device_id || "your selected device";
    setStatus("info", `Verification started. Please accept on ${{target}} to proceed.`);
  }} else if (data.status === "waiting_for_emoji") {{
    const target = selectedVerifierDeviceLabel || data.target_device_id || "your selected device";
    setStatus("info", `Waiting for ${{target}} to show the emoji comparison.`);
  }} else if (data.status === "waiting_for_confirm") {{
    setStatus("warn", "Compare the emojis with your trusted client, then confirm or reject them here.");
  }} else if (data.status === "confirming") {{
    setStatus("info", "Waiting for Matrix to finish confirming the verification.");
  }} else {{
    setStatus("info", data.detail || "Verification is in progress.");
  }}

  if (pollTimer) clearTimeout(pollTimer);
  if (!data.done) {{
    pollTimer = setTimeout(refreshAttempt, 2000);
  }}
}}

function renderSession(session) {{
  currentSession = session;
  const hasSession = !!(session && session.has_session);
  document.getElementById("loginPanel").classList.toggle("hidden", hasSession);
  document.getElementById("devicePickerPanel").classList.toggle("hidden", !hasSession);
  document.getElementById("current_device_id").value = hasSession ? (session.device_id || "") : "";
  document.getElementById("user_id").value = hasSession ? (session.user_id || "") : "";
  document.getElementById("postLoginHint").textContent = hasSession
    ? "Choose which existing trusted client should receive the verification request."
    : "Sign in first. After that, LifeRadar will show the trusted devices it can verify against.";
}}

function renderDeviceList(devices) {{
  const list = document.getElementById("deviceList");
  list.innerHTML = "";
  const verifierCandidates = devices.filter((device) => !device.is_current);
  if (!verifierCandidates.length) {{
    list.innerHTML = '<div class="muted">No other devices are available yet. Sign in to Element or Beeper on another client first.</div>';
    return;
  }}

  if (!selectedVerifierDeviceId || !verifierCandidates.some((device) => device.device_id === selectedVerifierDeviceId)) {{
    selectedVerifierDeviceId = verifierCandidates[0].device_id;
    selectedVerifierDeviceLabel = verifierCandidates[0].display_name || verifierCandidates[0].device_id;
  }}

  for (const device of verifierCandidates) {{
    const label = device.display_name || `Matrix device ${{device.device_id}}`;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "device-option" + (device.device_id === selectedVerifierDeviceId ? " active" : "");
    const verificationPill = device.is_verified
      ? '<span class="pill good">Verified</span>'
      : '<span class="pill warn">Not verified yet</span>';
    const cryptoPill = device.supports_encryption
      ? '<span class="pill info">Encryption ready</span>'
      : '<span class="pill warn">No encryption keys</span>';
    card.innerHTML = `
      <div class="device-top">
        <div>
          <div class="device-name">${{label}}</div>
          <div class="mono device-id">${{device.device_id}}</div>
        </div>
        <div class="device-meta">${{verificationPill}}${{cryptoPill}}</div>
      </div>
      <div class="muted">${{describeLastSeen(device)}}</div>
    `;
    card.onclick = async () => {{
      selectedVerifierDeviceId = device.device_id;
      selectedVerifierDeviceLabel = label;
      renderDeviceList(devices);
      await startVerification();
    }};
    list.appendChild(card);
  }}
}}

async function loadSession() {{
  try {{
    const session = await api("/matrix/session");
    renderSession(session);
    if (session.has_session) {{
      await loadDevices();
    }}
  }} catch (error) {{
    setStatus("error", error.message || "Failed to load Matrix session status.");
  }}
}}

async function loadDevices() {{
  if (!(currentSession && currentSession.has_session)) return;
  try {{
    const devices = await api("/matrix/devices");
    renderDeviceList(devices);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to load available Matrix devices.");
  }}
}}

async function refreshAttempt() {{
  if (!attemptId) return;
  try {{
    const data = await api(`/matrix/verification/${{attemptId}}`);
    renderAttempt(data);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to refresh verification status.");
  }}
}}

async function startVerification() {{
  if (!selectedVerifierDeviceId) {{
    setStatus("error", "Choose a trusted device first.");
    return;
  }}
  setStatus("info", `Starting verification with ${{selectedVerifierDeviceLabel || selectedVerifierDeviceId}}…`);
  try {{
    const data = await api("/matrix/verification/start", {{
      method: "POST",
      body: JSON.stringify({{ target_device_id: selectedVerifierDeviceId }})
    }});
    renderAttempt(data);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to start verification.");
  }}
}}

document.getElementById("login").onclick = async () => {{
  const identifier = document.getElementById("identifier").value.trim();
  const password = document.getElementById("password").value;
  if (!identifier || !password) {{
    setStatus("error", "Enter your identifier and password first.");
    return;
  }}
  setStatus("info", "Signing in and creating a fresh LifeRadar Matrix device…");
  try {{
    const session = await api("/matrix/login", {{
      method: "POST",
      body: JSON.stringify({{
        identifier,
        password,
        identifier_kind: selectedIdentifierKind()
      }})
    }});
    renderSession({{
      has_session: true,
      user_id: session.user_id,
      device_id: session.device_id,
      homeserver: session.homeserver
    }});
    selectedVerifierDeviceId = null;
    selectedVerifierDeviceLabel = "";
    setStatus("warn", `Signed in as ${{session.user_id}}. Now choose which existing trusted device should verify LifeRadar device ${{session.device_id}}.`);
    document.getElementById("password").value = "";
    await loadDevices();
  }} catch (error) {{
    setStatus("error", error.message || "Matrix login failed.");
  }}
}};

document.getElementById("retryDevices").onclick = loadDevices;
document.getElementById("refresh").onclick = refreshAttempt;
document.getElementById("confirm").onclick = async () => {{
  if (!attemptId) return;
  try {{
    const data = await api(`/matrix/verification/${{attemptId}}/confirm`, {{
      method: "POST",
      body: JSON.stringify({{ decision: "yes" }})
    }});
    renderAttempt(data);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to confirm verification.");
  }}
}};
document.getElementById("reject").onclick = async () => {{
  if (!attemptId) return;
  try {{
    const data = await api(`/matrix/verification/${{attemptId}}/confirm`, {{
      method: "POST",
      body: JSON.stringify({{ decision: "no" }})
    }});
    renderAttempt(data);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to reject verification.");
  }}
}};
document.getElementById("cancel").onclick = async () => {{
  if (!attemptId) return;
  try {{
    const data = await api(`/matrix/verification/${{attemptId}}/confirm`, {{
      method: "POST",
      body: JSON.stringify({{ decision: "cancel" }})
    }});
    renderAttempt(data);
  }} catch (error) {{
    setStatus("error", error.message || "Failed to cancel verification.");
  }}
}};
for (const radio of document.querySelectorAll('input[name="identifier_kind"]')) {{
  radio.addEventListener("change", updateIdentifierUi);
}}
updateIdentifierUi();
loadSession();
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


@app.get("/auth/matrix-device", response_class=HTMLResponse)
async def matrix_device_verification_page(request: Request):
    require_api_key(request, allow_query_param=True)
    return HTMLResponse(_matrix_device_verification_page(_provided_api_key(request, True)))


@app.get("/matrix/session", response_model=MatrixSessionStatus)
async def matrix_session_status(request: Request):
    require_api_key(request)
    session = _read_matrix_session_file()
    if not session:
        return MatrixSessionStatus(has_session=False)
    try:
        await _matrix_whoami(session["access_token"], session["homeserver"])
    except HTTPException as exc:
        if exc.status_code == 401:
            _reset_matrix_local_identity_state()
            return MatrixSessionStatus(has_session=False)
        raise
    return MatrixSessionStatus(
        has_session=True,
        user_id=session.get("user_id"),
        device_id=session.get("device_id"),
        homeserver=session.get("homeserver"),
    )


@app.get("/matrix/devices", response_model=list[MatrixDeviceSummary])
async def matrix_devices(request: Request):
    require_api_key(request)
    session = await ensure_valid_matrix_session()
    return await _matrix_list_devices(session)


@app.post("/matrix/login", response_model=MatrixLoginResponse)
async def matrix_login(
    body: MatrixLoginRequest,
    request: Request,
):
    require_api_key(request)
    return await perform_matrix_password_login(body)


@app.post("/matrix/verification/start", response_model=MatrixDeviceVerificationAttempt)
async def start_matrix_device_verification(
    body: MatrixDeviceVerificationStartRequest,
    request: Request,
):
    require_api_key(request)
    await ensure_valid_matrix_session()
    payload = await call_matrix_bridge(
        "POST",
        "/verification/start",
        body.model_dump(),
    )
    return payload


@app.get("/matrix/verification/{attempt_id}", response_model=MatrixDeviceVerificationAttempt)
async def matrix_device_verification_status(attempt_id: str, request: Request):
    require_api_key(request)
    payload = await call_matrix_bridge("GET", f"/verification/{attempt_id}")
    return payload


@app.post("/matrix/verification/{attempt_id}/confirm", response_model=MatrixDeviceVerificationAttempt)
async def matrix_device_verification_confirm(
    attempt_id: str,
    body: MatrixDeviceVerificationDecisionRequest,
    request: Request,
):
    require_api_key(request)
    payload = await call_matrix_bridge(
        "POST",
        f"/verification/{attempt_id}/confirm",
        body.model_dump(),
    )
    return payload


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
    if not request.user_approved:
        raise HTTPException(
            status_code=403,
            detail=(
                "Explicit user approval is required before sending a message. "
                "Prompt the user for confirmation, then retry with user_approved=true "
                "and an approval_note describing that approval."
            ),
        )
    conversation = await load_conversation_for_send(request.conversation_id)

    if conversation["source"] == "matrix":
        if not is_matrix_enabled():
            raise HTTPException(
                status_code=501,
                detail="Sending messages for source 'matrix' is disabled (LIFERADAR_MATRIX_ENABLED=false)",
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
