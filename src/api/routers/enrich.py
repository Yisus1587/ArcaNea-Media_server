from __future__ import annotations

import logging

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...db import SessionLocal, models
from ...services.enrich_state import get_state as get_enrich_state
from ...services.scan_state import get_state as get_scan_state
from ...services.enrichment_runner import (
    request_stop as request_enrich_stop,
    set_paused as set_enrich_paused,
    start_enrichment_job,
)
from ...tools.apply_db_migrations import main as apply_db_migrations_main
from ..deps import get_db, admin_required
from ..services.status import compute_enrich_counts
from ...services import enrichment as enrichment_service

router = APIRouter()


@router.get("/api/enrich/status")
def enrich_status(db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    """Return enrichment runner and DB ingestion counts."""
    try:
        counts = compute_enrich_counts(db)
        try:
            enrich = get_enrich_state()
        except Exception:
            enrich = {"running": False}

        return {
            "running": bool(enrich.get("running", False)),
            "current_id": enrich.get("current_id"),
            "current_title": enrich.get("current_title"),
            "current_step": enrich.get("current_step"),
            "last_updated": enrich.get("last_updated"),
            "total": counts.get("total", 0),
            "pending": counts.get("pending", 0),
            "pending_total": counts.get("pending_total", 0),
            "enriched": counts.get("enriched", 0),
            "no_match": counts.get("no_match", 0),
            "error": counts.get("error", 0),
        }
    except Exception:
        logging.getLogger(__name__).exception("Failed computing enrich status")
        return {"running": False}


@router.post("/api/enrich/start")
def enrich_start(_: bool = Depends(admin_required)):
    """Request the enrichment runner to start (non-blocking)."""
    try:
        try:
            st = get_scan_state()
            if (st.get("status") or "").lower() in ("scanning", "queued"):
                return {"started": False, "detail": "scan_in_progress"}
        except Exception:
            pass
        try:
            apply_db_migrations_main()
        except Exception:
            logging.getLogger(__name__).exception("DB migration failed before enrichment")
        started = start_enrichment_job()
        return {"started": bool(started)}
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error starting enrichment")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.post("/api/enrich/pause")
def enrich_pause(_: bool = Depends(admin_required)):
    try:
        set_enrich_paused(True)
        return {"paused": True}
    except Exception as e:
        return {"paused": False, "error": str(e)}


@router.post("/api/enrich/resume")
def enrich_resume(_: bool = Depends(admin_required)):
    try:
        set_enrich_paused(False)
        return {"paused": False}
    except Exception as e:
        return {"paused": True, "error": str(e)}


@router.post("/api/enrich/stop")
def enrich_stop(_: bool = Depends(admin_required)):
    """Request the enrichment runner to stop after the current item."""
    try:
        request_enrich_stop()
        return {"stop_requested": True}
    except Exception as e:
        return {"stop_requested": False, "error": str(e)}


@router.post("/api/enrich/reset-no-match")
def enrich_reset_no_match(anime_provider: str | None = None, start: bool = False, _: bool = Depends(admin_required)):
    """Reset items marked as NO_MATCH back to SCANNED so they can be retried."""
    db = SessionLocal()
    try:
        # Optional: set provider mode for anime retries ("jikan" | "kitsu" | "tmdb" | "auto").
        # Stored in settings.metadata.animeProvider so long-running servers pick it up without restart.
        try:
            if anime_provider is not None:
                ap = str(anime_provider or "").strip().lower()
                if ap in ("jikan", "kitsu", "tmdb", "auto"):
                    row = db.query(models.Setting).filter(models.Setting.key == "metadata").first()
                    md = {}
                    if row and getattr(row, "value", None):
                        try:
                            md = json.loads(getattr(row, "value") or "") or {}
                        except Exception:
                            md = {}
                    if not isinstance(md, dict):
                        md = {}
                    md2 = dict(md)
                    md2["animeProvider"] = ap
                    raw = json.dumps(md2, ensure_ascii=False)
                    if row:
                        row.value = raw
                        try:
                            row.updated_at = datetime.utcnow()
                        except Exception:
                            pass
                        db.add(row)
                    else:
                        db.add(models.Setting(key="metadata", value=raw))
                    db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        subq = (
            db.query(models.MediaMetadata.media_item_id)
            .filter(models.MediaMetadata.provider != None)  # noqa: E711
            .filter(models.MediaMetadata.provider != "")
        )
        q = (
            db.query(models.MediaItem)
            .filter(~models.MediaItem.id.in_(subq))
            .filter(models.MediaItem.status == "NO_MATCH")
        )
        items = q.all()
        for mi in items:
            mi.status = "SCANNED"
            db.add(mi)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        out = {"reset": len(items)}
        if start:
            try:
                started = start_enrichment_job()
                out["started"] = bool(started)
            except Exception:
                out["started"] = False
        return out
    finally:
        db.close()


@router.post("/api/enrich/reset-error")
def enrich_reset_error(anime_provider: str | None = None, start: bool = False, _: bool = Depends(admin_required)):
    """Reset items marked as ERROR back to SCANNED so they can be retried."""
    db = SessionLocal()
    try:
        # Same optional provider mode as reset-no-match.
        try:
            if anime_provider is not None:
                ap = str(anime_provider or "").strip().lower()
                if ap in ("jikan", "kitsu", "tmdb", "auto"):
                    row = db.query(models.Setting).filter(models.Setting.key == "metadata").first()
                    md = {}
                    if row and getattr(row, "value", None):
                        try:
                            md = json.loads(getattr(row, "value") or "") or {}
                        except Exception:
                            md = {}
                    if not isinstance(md, dict):
                        md = {}
                    md2 = dict(md)
                    md2["animeProvider"] = ap
                    raw = json.dumps(md2, ensure_ascii=False)
                    if row:
                        row.value = raw
                        try:
                            row.updated_at = datetime.utcnow()
                        except Exception:
                            pass
                        db.add(row)
                    else:
                        db.add(models.Setting(key="metadata", value=raw))
                    db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        subq = (
            db.query(models.MediaMetadata.media_item_id)
            .filter(models.MediaMetadata.provider != None)  # noqa: E711
            .filter(models.MediaMetadata.provider != "")
        )
        q = (
            db.query(models.MediaItem)
            .filter(~models.MediaItem.id.in_(subq))
            .filter(models.MediaItem.status == "ERROR")
        )
        items = q.all()
        for mi in items:
            mi.status = "SCANNED"
            db.add(mi)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        out = {"reset": len(items)}
        if start:
            try:
                started = start_enrichment_job()
                out["started"] = bool(started)
            except Exception:
                out["started"] = False
        return out
    finally:
        db.close()


@router.post("/api/enrich/backfill-episodes")
def enrich_backfill_episodes(limit_seasons: int | None = None, _: bool = Depends(admin_required)):
    """Backfill episode titles/synopsis from TMDB for already-enriched items.

    Useful after schema upgrades or when scans rebuilt normalized episode tables.
    """
    try:
        try:
            apply_db_migrations_main()
        except Exception:
            logging.getLogger(__name__).exception("DB migration failed before backfill-episodes")
        # Phase 0: ensure Jikan-identified anime series have a tmdb_id so the backfill can localize episodes.
        sync_res = enrichment_service.tmdb_sync_from_jikan_title_en(manage_state=True)
        res = enrichment_service.backfill_tmdb_episode_titles(limit_seasons=limit_seasons, manage_state=True)
        try:
            out = dict(res or {})
        except Exception:
            out = {"ok": bool(res)}
        out["tmdb_sync"] = sync_res
        return out
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error in backfill-episodes")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.post("/api/enrich/repair-series")
def enrich_repair_series(dry_run: bool = False, _: bool = Depends(admin_required)):
    """Repair normalized Series/Season grouping for TMDB TV shows split across multiple Series rows."""
    try:
        try:
            apply_db_migrations_main()
        except Exception:
            logging.getLogger(__name__).exception("DB migration failed before repair-series")
        return enrichment_service.repair_series_grouping_by_tmdb(dry_run=bool(dry_run))
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error in repair-series")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.post("/api/enrich/backfill-images")
def enrich_backfill_images(limit_items: int | None = None, _: bool = Depends(admin_required)):
    """Download missing posters/backdrops using existing cached metadata image URLs."""
    try:
        try:
            apply_db_migrations_main()
        except Exception:
            logging.getLogger(__name__).exception("DB migration failed before backfill-images")
        return enrichment_service.backfill_cached_images(limit_items=limit_items, manage_state=True)
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error in backfill-images")
        raise HTTPException(status_code=500, detail="Unexpected error")
