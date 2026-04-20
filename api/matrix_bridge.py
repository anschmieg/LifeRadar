"""
Internal Matrix send bridge hosted in the worker image.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import subprocess
import threading
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


MATRIX_SEND_BINARY = os.environ.get(
    "LIFERADAR_MATRIX_SEND_BINARY", "/usr/local/bin/liferadar-matrix"
)

app = FastAPI(
    title="LifeRadar Matrix Bridge",
    description="Internal bridge for Matrix send operations",
    version="1.0.0",
)


class MatrixSendRequest(BaseModel):
    room_id: str
    content_text: str


class MatrixSendResponse(BaseModel):
    status: str
    event_id: str


class MatrixVerificationStartRequest(BaseModel):
    target_device_id: str


class MatrixVerificationDecisionRequest(BaseModel):
    decision: str


@dataclass
class VerificationAttempt:
    attempt_id: str
    target_device_id: str
    status: str = "starting"
    detail: str = "Starting Matrix device verification…"
    flow_id: str | None = None
    emojis: list[dict[str, str]] = field(default_factory=list)
    decimals: list[int] = field(default_factory=list)
    error: str | None = None
    done: bool = False
    latest_event: str | None = None
    started_at: str = field(default_factory=lambda: _iso_now())
    updated_at: str = field(default_factory=lambda: _iso_now())
    logs: list[str] = field(default_factory=list)
    process: subprocess.Popen[str] | None = None


_attempt_lock = threading.Lock()
_verification_attempts: dict[str, VerificationAttempt] = {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(attempt: VerificationAttempt, message: str) -> None:
    if not message:
        return
    attempt.logs.append(message)
    if len(attempt.logs) > 80:
        attempt.logs = attempt.logs[-80:]
    attempt.updated_at = _iso_now()


def _attempt_snapshot(attempt: VerificationAttempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "target_device_id": attempt.target_device_id,
        "status": attempt.status,
        "detail": attempt.detail,
        "flow_id": attempt.flow_id,
        "emojis": attempt.emojis,
        "decimals": attempt.decimals,
        "error": attempt.error,
        "done": attempt.done,
        "latest_event": attempt.latest_event,
        "started_at": attempt.started_at,
        "updated_at": attempt.updated_at,
        "logs": attempt.logs[-20:],
    }


def _apply_verification_event(attempt: VerificationAttempt, payload: dict[str, Any]) -> None:
    event = str(payload.get("event") or "")
    status = str(payload.get("status") or attempt.status)
    detail = (
        payload.get("detail")
        or payload.get("reason")
        or attempt.detail
    )
    attempt.latest_event = event or attempt.latest_event
    attempt.updated_at = _iso_now()

    if "flow_id" in payload:
        attempt.flow_id = payload.get("flow_id") or attempt.flow_id
    if "emojis" in payload:
        attempt.emojis = payload.get("emojis") or []
    if "decimals" in payload:
        attempt.decimals = payload.get("decimals") or []
    if detail:
        attempt.detail = str(detail)

    if event in {"request_created", "request_pending", "request_received"}:
        attempt.status = "waiting_for_accept"
    elif event in {"request_ready", "sas_started", "sas_created", "sas_accepted", "secret_recovery", "room_key_import"}:
        attempt.status = "waiting_for_emoji"
    elif event == "emoji_ready":
        attempt.status = "waiting_for_confirm"
        attempt.detail = "Compare the emojis in your trusted Matrix client, then confirm or reject them here."
    elif event in {"verification_confirmed", "sas_confirmed"}:
        attempt.status = "confirming"
    elif event == "verification_complete":
        attempt.status = "done"
        attempt.done = True
        attempt.error = None
        attempt.detail = str(payload.get("status") or "Verification completed.")
    elif event in {"verification_cancelled", "verification_cancelled_locally", "request_cancelled_locally"}:
        attempt.status = "cancelled"
        attempt.done = True
        attempt.error = str(payload.get("reason") or payload.get("detail") or "Verification cancelled")
    else:
        attempt.status = status


def _monitor_verification_process(attempt_id: str) -> None:
    with _attempt_lock:
        attempt = _verification_attempts.get(attempt_id)
        process = attempt.process if attempt else None

    if attempt is None or process is None:
        return

    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        with _attempt_lock:
            current = _verification_attempts.get(attempt_id)
            if current is None:
                continue
            try:
                payload = json.loads(line)
                _apply_verification_event(current, payload)
            except json.JSONDecodeError:
                _append_log(current, f"stdout: {line}")

    return_code = process.wait()
    stderr_text = ""
    if process.stderr is not None:
        stderr_text = process.stderr.read().strip()

    with _attempt_lock:
        current = _verification_attempts.get(attempt_id)
        if current is None:
            return
        if stderr_text:
            for line in stderr_text.splitlines():
                _append_log(current, f"stderr: {line}")
        if return_code != 0 and not current.done:
            current.status = "failed"
            current.done = True
            current.error = (stderr_text or "Matrix verification failed").strip()[:600]
            current.detail = current.error
        elif return_code == 0 and not current.done:
            current.done = True
            current.status = "done"
            current.detail = current.detail or "Verification completed."
        current.updated_at = _iso_now()


def _start_verification_process(target_device_id: str) -> VerificationAttempt:
    attempt = VerificationAttempt(
        attempt_id=uuid.uuid4().hex,
        target_device_id=target_device_id,
    )
    env = os.environ.copy()
    env["LIFERADAR_MATRIX_RUST_MODE"] = "verify_device_interactive"
    env["LIFERADAR_VERIFY_TARGET_DEVICE_ID"] = target_device_id

    try:
        process = subprocess.Popen(
            [MATRIX_SEND_BINARY],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Matrix verification binary is unavailable") from exc

    attempt.process = process
    with _attempt_lock:
        _verification_attempts[attempt.attempt_id] = attempt

    threading.Thread(
        target=_monitor_verification_process,
        args=(attempt.attempt_id,),
        daemon=True,
    ).start()
    return attempt


def _get_attempt(attempt_id: str) -> VerificationAttempt:
    with _attempt_lock:
        attempt = _verification_attempts.get(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="Verification attempt not found")
        return attempt


def _send_verification_decision(attempt: VerificationAttempt, decision: str) -> None:
    if attempt.done:
        raise HTTPException(status_code=409, detail="Verification attempt is already finished")
    process = attempt.process
    if process is None or process.poll() is not None or process.stdin is None:
        raise HTTPException(status_code=409, detail="Verification process is not running")

    normalized = decision.strip().lower()
    if normalized not in {"yes", "confirm", "no", "reject", "cancel"}:
        raise HTTPException(status_code=400, detail="Decision must be yes, no, reject, confirm, or cancel")

    process.stdin.write(f"{normalized}\n")
    process.stdin.flush()
    attempt.updated_at = _iso_now()
    _append_log(attempt, f"decision:{normalized}")


@app.get("/health")
async def health():
    with _attempt_lock:
        active_attempts = sum(1 for attempt in _verification_attempts.values() if not attempt.done)
    return {"status": "ok", "active_verifications": active_attempts}


@app.post("/send", response_model=MatrixSendResponse)
async def send_message(request: MatrixSendRequest):
    env = os.environ.copy()
    env["LIFERADAR_MATRIX_RUST_MODE"] = "send_message"
    env["LIFERADAR_SEND_ROOM_ID"] = request.room_id
    env["LIFERADAR_SEND_TEXT"] = request.content_text

    try:
        proc = subprocess.run(
            [MATRIX_SEND_BINARY],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Matrix send binary is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Matrix send timed out") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "matrix send failed").strip()
        raise HTTPException(status_code=502, detail=stderr[:400])

    try:
        payload = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Matrix send returned invalid output") from exc

    event_id = payload.get("event_id")
    if not event_id:
        raise HTTPException(status_code=502, detail="Matrix send did not return an event_id")

    return MatrixSendResponse(status="sent", event_id=str(event_id))


@app.post("/verification/start")
async def start_verification(request: MatrixVerificationStartRequest):
    attempt = _start_verification_process(request.target_device_id.strip())
    return _attempt_snapshot(attempt)


@app.get("/verification/{attempt_id}")
async def verification_status(attempt_id: str):
    attempt = _get_attempt(attempt_id)
    with _attempt_lock:
        return _attempt_snapshot(attempt)


@app.post("/verification/{attempt_id}/confirm")
async def verification_confirm(attempt_id: str, request: MatrixVerificationDecisionRequest):
    with _attempt_lock:
        attempt = _verification_attempts.get(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="Verification attempt not found")
        _send_verification_decision(attempt, request.decision)
        return _attempt_snapshot(attempt)
