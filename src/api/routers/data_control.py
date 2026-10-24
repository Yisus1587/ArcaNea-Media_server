from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..deps import admin_required, get_db
from ...services import data_control
from ...services import enrichment_runner

router = APIRouter()


@router.get("/api/admin/entities/search")
def search_entities(q: str = "", limit: int = 50, offset: int = 0, db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    return data_control.search_media_items(db, q=q, limit=limit, offset=offset)


@router.post("/api/admin/entities/{item_id}/deep-purge")
def deep_purge(item_id: int, db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    return data_control.deep_purge_media_item(db, item_id)


@router.post("/api/admin/entities/{item_id}/rematch")
def manual_rematch(item_id: int, payload: dict[str, Any], _: bool = Depends(admin_required)):
    try:
        tmdb_id = str(payload.get("tmdb_id") or "").strip()
    except Exception:
        tmdb_id = ""
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="tmdb_id_required")
    media_type = payload.get("media_type")
    season_number = payload.get("season_number")
    language = payload.get("language")
    download_assets = bool(payload.get("download_assets")) if payload.get("download_assets") is not None else False
    return enrichment_runner.enrich_from_tmdb_id_serialized(
        media_item_id=int(item_id),
        tmdb_id=tmdb_id,
        media_type=media_type,
        season_number=season_number,
        language=language,
        download_assets=download_assets,
    )


@router.get("/api/admin/conflicts")
def conflicts(kind: str = "all", db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    return data_control.find_conflicts(db, kind=kind)


@router.post("/api/admin/conflicts/merge")
def merge_conflicts(payload: dict[str, Any], db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    keep_id = payload.get("keep_id")
    drop_id = payload.get("drop_id")
    if keep_id is None or drop_id is None:
        raise HTTPException(status_code=400, detail="keep_id_drop_id_required")
    return data_control.merge_media_items(db, int(keep_id), int(drop_id))


@router.post("/api/admin/system/reset")
def system_reset(payload: dict[str, Any], _: bool = Depends(admin_required)):
    confirm = str(payload.get("confirm") or "")
    return data_control.full_system_reset(confirm)


@router.post("/api/admin/queue/clear")
def clear_queue(_: bool = Depends(admin_required)):
    return data_control.clear_scan_queue()

