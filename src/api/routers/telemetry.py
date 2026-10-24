from __future__ import annotations

import asyncio
import queue

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..admin_auth import get_required_admin_pin, validate_admin_token
from ...core import config as core_config
from ...services import telemetry

router = APIRouter()


@router.get("/api/telemetry/stream")
async def telemetry_stream(
    request: Request, admin_token: str | None = None, telemetry_token: str | None = None
):
    pin_required = get_required_admin_pin()
    if pin_required:
        token = (admin_token or request.query_params.get("admin_token") or "").strip()
        if not validate_admin_token(token):
            # Allow a stable, server-managed telemetry token so the UI can subscribe
            # without persisting admin session tokens in localStorage.
            tt = (
                (telemetry_token or request.query_params.get("telemetry_token") or "").strip()
                or (request.headers.get("X-Arcanea-Telemetry-Token") or "").strip()
            )
            if not tt:
                raise HTTPException(status_code=403, detail="forbidden")
            stored = core_config._read_setting("telemetry_token")  # type: ignore[attr-defined]
            if not stored or str(stored).strip() != tt:
                raise HTTPException(status_code=403, detail="forbidden")

    q = telemetry.subscribe()

    async def event_stream():
        try:
            snap = telemetry.snapshot()
            yield telemetry.format_sse("snapshot", snap)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.to_thread(q.get, True, 0.75)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                except Exception:
                    yield ": ping\n\n"
                    continue
                try:
                    etype = ev.get("type") or "message"
                except Exception:
                    etype = "message"
                yield telemetry.format_sse(str(etype), ev)
        finally:
            telemetry.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
