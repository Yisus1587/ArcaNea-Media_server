from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, BackgroundTasks, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func

from ...db import SessionLocal, models
from ...services.enrich_state import get_state as get_enrich_state
from ...services.scan_state import get_state as get_scan_state, set_state as set_scan_state
from ...tools.apply_db_migrations import main as apply_db_migrations_main
from ...services import media_pipeline as scan_service
from ..services import status as status_service
from ..deps import admin_required
from ..admin_auth import validate_admin_token
import os

router = APIRouter()


def _compute_root_counts(db) -> dict:
    counts: dict[str, dict[str, int]] = {}
    try:
        rows = (
            db.query(
                models.MediaItem.media_root,
                models.MediaItem.library_type,
                models.MediaItem.media_type,
                func.count(models.MediaItem.id),
            )
            .group_by(models.MediaItem.media_root, models.MediaItem.library_type, models.MediaItem.media_type)
            .all()
        )
        for root, library_type, media_type, total in rows:
            root_key = (str(root or "")).strip()
            if not root_key:
                continue
            entry = counts.setdefault(
                root_key,
                {
                    "total": 0,
                    "anime": 0,
                    "series": 0,
                    "movie": 0,
                    "specials": 0,
                    "ova": 0,
                    "ona": 0,
                    "other": 0,
                },
            )
            try:
                cnt = int(total or 0)
            except Exception:
                cnt = 0
            entry["total"] += cnt
            lt = str(library_type or "").strip().lower()
            mt = str(media_type or "").strip().lower()
            if lt == "anime":
                entry["anime"] += cnt
            elif lt == "movie" or mt == "movie":
                entry["movie"] += cnt
            elif lt in ("series", "tv", "show") or mt == "series":
                entry["series"] += cnt
            else:
                entry["other"] += cnt
    except Exception:
        return {}

    try:
        specials_rows = (
            db.query(models.MediaItem.media_root, func.count(models.MediaItem.id))
            .join(models.SeasonItem, models.SeasonItem.media_item_id == models.MediaItem.id)
            .join(models.Season, models.Season.id == models.SeasonItem.season_id)
            .filter(models.Season.season_number == 0)
            .group_by(models.MediaItem.media_root)
            .all()
        )
        for root, total in specials_rows:
            root_key = (str(root or "")).strip()
            if not root_key:
                continue
            counts.setdefault(root_key, {"total": 0, "anime": 0, "series": 0, "movie": 0, "specials": 0, "ova": 0, "ona": 0, "other": 0})
            counts[root_key]["specials"] = int(total or 0)
    except Exception:
        pass

    for label in ("ova", "ona"):
        try:
            rows = (
                db.query(models.MediaItem.media_root, func.count(models.MediaItem.id))
                .join(models.SeasonItem, models.SeasonItem.media_item_id == models.MediaItem.id)
                .join(models.Season, models.Season.id == models.SeasonItem.season_id)
                .filter(models.Season.season_number == 0)
                .filter(func.lower(models.MediaItem.title).like(f"%{label}%"))
                .group_by(models.MediaItem.media_root)
                .all()
            )
            for root, total in rows:
                root_key = (str(root or "")).strip()
                if not root_key:
                    continue
                counts.setdefault(root_key, {"total": 0, "anime": 0, "series": 0, "movie": 0, "specials": 0, "ova": 0, "ona": 0, "other": 0})
                counts[root_key][label] = int(total or 0)
        except Exception:
            continue

    return counts


@router.post("/api/scan")
def trigger_scan(background_tasks: BackgroundTasks, _: bool = Depends(admin_required)):
    try:
        try:
            apply_db_migrations_main()
        except Exception as e:
            logging.getLogger(__name__).exception("DB migration failed before scan")
            return JSONResponse({"status": "migration_failed", "detail": str(e)}, status_code=500)
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error while attempting migrations")

    try:
        set_scan_state({"status": "queued"})
    except Exception:
        pass
    return scan_service.enqueue_scan_all_roots()


@router.post("/api/scan/path")
def scan_path(payload: dict, _: bool = Depends(admin_required)):
    try:
        path = str(payload.get("path") or "").strip()
    except Exception:
        path = ""
    if not path:
        return JSONResponse({"status": "bad_request", "detail": "path_required"}, status_code=400)
    res = scan_service.enqueue_scan_path(path, pipeline=True)
    return res


@router.post("/api/scan/normalize")
def normalize_now(_: bool = Depends(admin_required)):
    try:
        # La normalización ahora es parte del flujo de enriquecimiento principal.
        started = bool(scan_service.start_enrichment_job())
        return {"status": "ok", "started": started}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.get("/api/scan/status")
def scan_status(_: bool = Depends(admin_required)):
    try:
        s: dict = dict(get_scan_state())
    except Exception:
        s = {"status": "idle"}
    try:
        enrich = get_enrich_state()
        s["enrichment"] = dict(enrich) if isinstance(enrich, dict) else enrich
    except Exception:
        s["enrichment"] = {"running": False}
    try:
        db = SessionLocal()
        try:
            s["root_counts"] = _compute_root_counts(db)
        finally:
            db.close()
    except Exception:
        s["root_counts"] = {}
    return s


@router.get("/api/scan/items")
def scan_items(limit: int = 200, offset: int = 0, root: str | None = None, _: bool = Depends(admin_required)):
    """Return a paged list of detected items (title + path) for UI validation."""
    try:
        limit = int(limit)
        offset = int(offset)
    except Exception:
        limit = 200
        offset = 0
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    db = SessionLocal()
    try:
        q = db.query(models.MediaItem)
        if root:
            try:
                r = str(root).strip()
            except Exception:
                r = ""
            if r:
                base = r.rstrip("\\/")
                q = q.filter(models.MediaItem.canonical_path.like(f"{base}{os.sep}%"))
        total = q.count()
        rows = (
            q.order_by(models.MediaItem.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        items = []
        for mi in rows:
            items.append(
                {
                    "id": getattr(mi, "id", None),
                    "title": getattr(mi, "title", None) or getattr(mi, "base_title", None),
                    "path": getattr(mi, "canonical_path", None),
                    "media_root": getattr(mi, "media_root", None),
                    "media_type": getattr(mi, "media_type", None),
                }
            )
        return {"total": int(total), "items": items}
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.websocket("/api/ws/status")
async def ws_status(ws: WebSocket):
    """Push scan/enrichment status snapshots in real-time."""
    pin_required = (os.environ.get("ARCANEA_ADMIN_PIN") or "").strip()
    if pin_required:
        token = (ws.headers.get("x-arcanea-admin-token") or ws.query_params.get("admin_token") or "").strip()
        if not validate_admin_token(token):
            await ws.close(code=1008)
            return
    await ws.accept()
    db = SessionLocal()
    last_payload = None
    last_counts_at = 0.0
    counts_cache: dict = {}
    try:
        while True:
            now = time.time()
            if (now - last_counts_at) >= 2.0:
                try:
                    counts_cache = status_service.compute_enrich_counts(db)
                except Exception:
                    counts_cache = {}
                last_counts_at = now
            snap: dict = dict(status_service.status_snapshot(None))
            snap["enrich_counts"] = counts_cache
            payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
            if payload != last_payload:
                await ws.send_text(payload)
                last_payload = payload
            await asyncio.sleep(0.75)
    except WebSocketDisconnect:
        return
    except Exception:
        logging.getLogger(__name__).exception("ws_status crashed")
    finally:
        try:
            db.close()
        except Exception:
            pass
