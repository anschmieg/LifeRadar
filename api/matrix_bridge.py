"""
Internal Matrix send bridge hosted in the worker image.
"""
import json
import os
import subprocess

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


@app.get("/health")
async def health():
    return {"status": "ok"}


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
