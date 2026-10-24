from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..core.config import DATA_DIR, get as get_config
from ..db import SessionLocal, models

logger = logging.getLogger(__name__)


def _cache_dir() -> Path:
    try:
        p = Path(str(get_config("cache_dir") or "")).resolve()
        if p:
            return p
    except Exception:
        pass
    return (DATA_DIR / "cache").resolve()


def search_media_items(db: Session, q: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    q = str(q or "").strip()
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    query = db.query(models.MediaItem)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                models.MediaItem.title.ilike(like),
                models.MediaItem.base_title.ilike(like),
                models.MediaItem.canonical_path.ilike(like),
                models.MediaItem.tmdb_id == q,
            )
        )
    total = query.count()
    rows = (
        query.order_by(models.MediaItem.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    ids = [int(r.id) for r in rows]
    first_paths: Dict[int, str] = {}
    if ids:
        files = (
            db.query(models.FileRecord)
            .filter(models.FileRecord.media_item_id.in_(ids))
            .order_by(models.FileRecord.id.asc())
            .all()
        )
        for f in files:
            mid = int(getattr(f, "media_item_id", 0) or 0)
            if mid and mid not in first_paths:
                first_paths[mid] = getattr(f, "path", "") or ""

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r.id),
                "title": getattr(r, "title", None),
                "base_title": getattr(r, "base_title", None),
                "canonical_path": getattr(r, "canonical_path", None),
                "canonical_path_hash": getattr(r, "canonical_path_hash", None),
                "tmdb_id": getattr(r, "tmdb_id", None),
                "media_type": getattr(r, "media_type", None),
                "status": getattr(r, "status", None),
                "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
                "path": first_paths.get(int(r.id)) or getattr(r, "canonical_path", None),
            }
        )
    return {"ok": True, "total": total, "items": items}


def _purge_season_tree(db: Session, season_id: int) -> None:
    try:
        eps = db.query(models.Episode).filter(models.Episode.season_id == season_id).all()
    except Exception:
        eps = []
    ep_ids = [int(e.id) for e in eps if getattr(e, "id", None) is not None]
    if ep_ids:
        try:
            db.query(models.EpisodeFile).filter(models.EpisodeFile.episode_id.in_(ep_ids)).delete(
                synchronize_session=False
            )
        except Exception:
            pass
        try:
            db.query(models.Episode).filter(models.Episode.id.in_(ep_ids)).delete(synchronize_session=False)
        except Exception:
            pass
    try:
        db.query(models.Season).filter(models.Season.id == season_id).delete(synchronize_session=False)
    except Exception:
        pass


def _cleanup_series_if_orphan(db: Session, series_id: int) -> None:
    try:
        remaining = db.query(models.Season.id).filter(models.Season.series_id == series_id).first()
    except Exception:
        remaining = None
    if remaining:
        return
    try:
        db.query(models.Series).filter(models.Series.id == series_id).delete(synchronize_session=False)
    except Exception:
        pass


def deep_purge_media_item(db: Session, item_id: int) -> Dict[str, Any]:
    mi = db.query(models.MediaItem).filter(models.MediaItem.id == int(item_id)).first()
    if not mi:
        return {"ok": False, "detail": "not_found"}

    canonical_path = getattr(mi, "canonical_path", None)

    # Resolve season/series linkage if any
    try:
        si = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == mi.id).first()
    except Exception:
        si = None
    season_id = int(si.season_id) if si and getattr(si, "season_id", None) else None
    series_id = None
    if season_id:
        try:
            season = db.query(models.Season).filter(models.Season.id == season_id).first()
        except Exception:
            season = None
        if season and getattr(season, "series_id", None):
            series_id = int(season.series_id)

    try:
        db.query(models.MediaRelation).filter(
            (models.MediaRelation.from_item_id == mi.id) | (models.MediaRelation.to_item_id == mi.id)
        ).delete(synchronize_session=False)
    except Exception:
        pass

    try:
        db.query(models.FileRecord).filter(models.FileRecord.media_item_id == mi.id).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        db.query(models.MediaMetadata).filter(models.MediaMetadata.media_item_id == mi.id).delete(
            synchronize_session=False
        )
    except Exception:
        pass
    try:
        db.query(models.MediaImage).filter(models.MediaImage.media_item_id == mi.id).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id == mi.id).delete(
            synchronize_session=False
        )
    except Exception:
        pass
    try:
        db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == mi.id).delete(
            synchronize_session=False
        )
    except Exception:
        pass
    try:
        db.query(models.ManualOverride).filter(models.ManualOverride.media_item_id == mi.id).delete(
            synchronize_session=False
        )
    except Exception:
        pass
    try:
        db.query(models.UserList).filter(models.UserList.media_item_id == mi.id).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        db.query(models.PlayHistory).filter(models.PlayHistory.media_item_id == mi.id).delete(
            synchronize_session=False
        )
    except Exception:
        pass
    try:
        db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == mi.id).delete(synchronize_session=False)
    except Exception:
        pass

    if season_id:
        _purge_season_tree(db, season_id)
    if series_id:
        _cleanup_series_if_orphan(db, series_id)

    try:
        db.query(models.MediaItem).filter(models.MediaItem.id == mi.id).delete(synchronize_session=False)
    except Exception:
        pass

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "detail": str(e)}

    # Remove cached assets
    try:
        assets_dir = (DATA_DIR / "assets" / str(item_id)).resolve()
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir, ignore_errors=True)
    except Exception:
        pass
    try:
        cache_dir = _cache_dir()
        for suffix in (str(item_id), f"item_{item_id}", f"media_{item_id}"):
            candidate = cache_dir / suffix
            if candidate.exists() and candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
    except Exception:
        pass

    # Remove from scan queue if present
    try:
        if canonical_path:
            from ..api.services import scan as scan_service  # local import to avoid cycles
            scan_service.clear_pending_path(str(canonical_path))
    except Exception:
        pass

    return {"ok": True, "id": int(item_id)}


def find_conflicts(db: Session, kind: str = "all") -> Dict[str, Any]:
    kind = (kind or "all").strip().lower()
    out: Dict[str, Any] = {"path_hash": [], "tmdb_id": []}

    def _collect(field_name: str):
        field = getattr(models.MediaItem, field_name)
        q = (
            db.query(field.label("key"), func.count(models.MediaItem.id).label("cnt"))
            .filter(field != None)  # noqa: E711
            .filter(field != "")
            .group_by(field)
            .having(func.count(models.MediaItem.id) > 1)
        )
        groups = []
        for key, cnt in q.all():
            try:
                items = (
                    db.query(models.MediaItem)
                    .filter(field == key)
                    .order_by(models.MediaItem.id.asc())
                    .all()
                )
            except Exception:
                items = []
            rows = []
            for it in items:
                rows.append(
                    {
                        "id": int(it.id),
                        "title": getattr(it, "title", None),
                        "base_title": getattr(it, "base_title", None),
                        "canonical_path": getattr(it, "canonical_path", None),
                        "canonical_path_hash": getattr(it, "canonical_path_hash", None),
                        "tmdb_id": getattr(it, "tmdb_id", None),
                        "media_type": getattr(it, "media_type", None),
                        "status": getattr(it, "status", None),
                        "created_at": it.created_at.isoformat() if getattr(it, "created_at", None) else None,
                    }
                )
            groups.append({"key": key, "count": int(cnt), "items": rows, "kind": field_name})
        return groups

    if kind in ("all", "path_hash", "canonical_path_hash"):
        out["path_hash"] = _collect("canonical_path_hash")
    if kind in ("all", "tmdb_id"):
        out["tmdb_id"] = _collect("tmdb_id")

    return {"ok": True, "conflicts": out}


def merge_media_items(db: Session, keep_id: int, drop_id: int) -> Dict[str, Any]:
    if int(keep_id) == int(drop_id):
        return {"ok": False, "detail": "same_id"}
    keep = db.query(models.MediaItem).filter(models.MediaItem.id == int(keep_id)).first()
    drop = db.query(models.MediaItem).filter(models.MediaItem.id == int(drop_id)).first()
    if not keep or not drop:
        return {"ok": False, "detail": "not_found"}

    # Prefer keeping existing identifiers; fill gaps only.
    try:
        if not getattr(keep, "tmdb_id", None) and getattr(drop, "tmdb_id", None):
            keep.tmdb_id = drop.tmdb_id
        if not getattr(keep, "title", None) and getattr(drop, "title", None):
            keep.title = drop.title
        if not getattr(keep, "media_type", None) and getattr(drop, "media_type", None):
            keep.media_type = drop.media_type
        db.add(keep)
    except Exception:
        pass

    def _update_fk(model, field):
        try:
            db.query(model).filter(field == drop_id).update({field: keep_id}, synchronize_session=False)
        except Exception:
            pass

    _update_fk(models.FileRecord, models.FileRecord.media_item_id)
    _update_fk(models.MediaMetadata, models.MediaMetadata.media_item_id)
    _update_fk(models.MediaImage, models.MediaImage.media_item_id)
    _update_fk(models.MediaTranslation, models.MediaTranslation.path_id)
    _update_fk(models.ManualOverride, models.ManualOverride.media_item_id)
    _update_fk(models.UserList, models.UserList.media_item_id)
    _update_fk(models.PlayHistory, models.PlayHistory.media_item_id)
    _update_fk(models.MediaRelation, models.MediaRelation.from_item_id)
    _update_fk(models.MediaRelation, models.MediaRelation.to_item_id)

    # ManualMapping is unique per item: keep existing mapping if present.
    try:
        mm_keep = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == keep_id).first()
        mm_drop = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == drop_id).first()
        if mm_drop:
            if mm_keep:
                db.delete(mm_drop)
            else:
                mm_drop.media_item_id = keep_id
                db.add(mm_drop)
    except Exception:
        pass

    # SeasonItem special handling
    try:
        si_keep = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == keep_id).first()
        si_drop = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == drop_id).first()
    except Exception:
        si_keep = None
        si_drop = None

    if si_drop:
        if si_keep:
            # Remove season tree for drop only if it is exclusive
            try:
                season_id = int(si_drop.season_id)
            except Exception:
                season_id = None
            series_id = None
            if season_id:
                try:
                    season = db.query(models.Season).filter(models.Season.id == season_id).first()
                    if season and getattr(season, "series_id", None):
                        series_id = int(season.series_id)
                except Exception:
                    series_id = None
            try:
                db.delete(si_drop)
            except Exception:
                pass
            if season_id:
                _purge_season_tree(db, season_id)
                if series_id:
                    _cleanup_series_if_orphan(db, int(series_id))
        else:
            try:
                si_drop.media_item_id = keep_id
                db.add(si_drop)
            except Exception:
                pass

    try:
        db.query(models.MediaItem).filter(models.MediaItem.id == drop_id).delete(synchronize_session=False)
    except Exception:
        pass

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "detail": str(e)}

    return {"ok": True, "keep_id": int(keep_id), "drop_id": int(drop_id)}


def full_system_reset(confirm: str) -> Dict[str, Any]:
    if str(confirm or "").strip().upper() != "RESET":
        return {"ok": False, "detail": "confirmation_required"}

    db = SessionLocal()
    try:
        # Delete in a safe order
        for model in (
            models.EpisodeFile,
            models.Episode,
            models.SeasonItem,
            models.Season,
            models.Series,
            models.MediaRelation,
            models.MediaTranslation,
            models.MediaImage,
            models.MediaMetadata,
            models.FileRecord,
            models.ManualMapping,
            models.ManualOverride,
            models.UserList,
            models.PlayHistory,
            models.MediaItem,
            models.MediaRoot,
            models.Setting,
        ):
            try:
                db.query(model).delete(synchronize_session=False)
            except Exception:
                pass
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "detail": str(e)}
    finally:
        db.close()

    try:
        cache_dir = _cache_dir()
        if cache_dir.exists() and cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        assets_dir = (DATA_DIR / "assets").resolve()
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir, ignore_errors=True)
            assets_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        from ..api.services import scan as scan_service  # local import to avoid cycles
        scan_service.clear_scan_queue()
    except Exception:
        pass

    return {"ok": True}


def clear_scan_queue() -> Dict[str, Any]:
    from ..api.services import scan as scan_service
    return scan_service.clear_scan_queue()
