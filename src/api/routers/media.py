from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Response
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from ...db import SessionLocal, models
from ...core.config import get as get_config
from ...core.translations import preferred_lang_chain_from_config, resolve_translation_fields
from ...services import enrichment_runner
from ...services import recommendations
from ...services import search_suggest
from ...services.enrich_state import get_state as get_enrich_state
from ...services.enrichment_runner import enrich_one_serialized
from ...utils.utils import map_tmdb_genres
from ..deps import get_db, admin_required

router = APIRouter()


class ProgressPayload(BaseModel):
    position: float | None = None
    duration: float | None = None
    path: str | None = None


@router.get("/api/library/overview")
def library_overview(group_by: str = "library_type", sample: int = 6, db: Session = Depends(get_db)):
    """Return an organized overview of the library grouped by `library_type`, `media_root` or `series`.

    Each group contains a total count and a small sample of recent items.
    """
    gb = (str(group_by or "")).strip().lower()
    if gb not in ("library_type", "media_root", "series"):
        gb = "library_type"

    groups = []
    try:
        if gb == "library_type":
            rows = db.query(models.MediaItem.library_type, func.count(models.MediaItem.id)).group_by(models.MediaItem.library_type).all()
            for lib_type, cnt in rows:
                try:
                    items = (
                        db.query(models.MediaItem)
                        .filter(models.MediaItem.library_type == lib_type)
                        .order_by(models.MediaItem.id.desc())
                        .limit(int(sample))
                        .all()
                    )
                except Exception:
                    items = []
                sample_out = []
                for it in items:
                    try:
                        sample_out.append(
                            {
                                "id": it.id,
                                "title": it.title or getattr(it, "base_title", None),
                                "media_type": getattr(it, "media_type", None),
                                "poster_url": _poster_url_for_item(it),
                                "is_identified": getattr(it, "is_identified", None),
                            }
                        )
                    except Exception:
                        continue
                groups.append({"key": lib_type or "unknown", "label": lib_type or "Unknown", "count": int(cnt or 0), "sample": sample_out})

        elif gb == "media_root":
            rows = db.query(models.MediaItem.media_root, func.count(models.MediaItem.id)).group_by(models.MediaItem.media_root).all()
            for root, cnt in rows:
                try:
                    items = (
                        db.query(models.MediaItem)
                        .filter(models.MediaItem.media_root == root)
                        .order_by(models.MediaItem.id.desc())
                        .limit(int(sample))
                        .all()
                    )
                except Exception:
                    items = []
                sample_out = []
                for it in items:
                    try:
                        sample_out.append(
                            {
                                "id": it.id,
                                "title": it.title or getattr(it, "base_title", None),
                                "media_type": getattr(it, "media_type", None),
                                "poster_url": _poster_url_for_item(it),
                                "is_identified": getattr(it, "is_identified", None),
                            }
                        )
                    except Exception:
                        continue
                groups.append({"key": root or "unknown", "label": root or "Unknown", "count": int(cnt or 0), "sample": sample_out})

        elif gb == "series":
            rows = db.query(models.Series).order_by(models.Series.title.asc()).all()
            for s in rows:
                try:
                    cnt = int(
                        db.query(func.count(models.SeasonItem.id))
                        .join(models.Season, models.SeasonItem.season_id == models.Season.id)
                        .filter(models.Season.series_id == getattr(s, "id", None))
                        .scalar()
                        or 0
                    )
                except Exception:
                    cnt = 0
                try:
                    items = (
                        db.query(models.MediaItem)
                        .join(models.SeasonItem, models.MediaItem.id == models.SeasonItem.media_item_id)
                        .join(models.Season, models.SeasonItem.season_id == models.Season.id)
                        .filter(models.Season.series_id == getattr(s, "id", None))
                        .order_by(models.MediaItem.id.desc())
                        .limit(int(sample))
                        .all()
                    )
                except Exception:
                    items = []
                sample_out = []
                for it in items:
                    try:
                        sample_out.append(
                            {
                                "id": it.id,
                                "title": it.title or getattr(it, "base_title", None),
                                "media_type": getattr(it, "media_type", None),
                                "poster_url": _poster_url_for_item(it),
                                "is_identified": getattr(it, "is_identified", None),
                            }
                        )
                    except Exception:
                        continue
                groups.append({"key": getattr(s, "id", None), "label": getattr(s, "title", None) or getattr(s, "title_en", None) or "Series", "count": cnt, "sample": sample_out})
    except Exception:
        groups = []

    return {"groups": groups, "group_by": gb}


def _as_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _pick_primary_metadata(metadata_out: list[dict[str, Any]] | None):
    if not metadata_out:
        return None
    try:
        for m in metadata_out:
            if m.get("provider"):
                return m
        return metadata_out[-1]
    except Exception as e:
        logging.getLogger(__name__).debug(f"Error picking primary metadata: {e}")
        return None


def _safe_iso(dt: Any) -> str | None:
    try:
        if isinstance(dt, datetime.datetime):
            return dt.isoformat()
    except Exception:
        pass
    return None


def _safe_ts(dt: Any) -> int | None:
    try:
        if isinstance(dt, datetime.datetime):
            return int(dt.timestamp())
        if isinstance(dt, (int, float)) and dt > 0:
            return int(dt)
    except Exception:
        pass
    return None


def _normalize_genres_list(raw_genres: Any) -> list[str] | None:
    """Return human-readable genres list; map numeric TMDB ids when possible."""
    tokens: list[str] = []
    try:
        if isinstance(raw_genres, str):
            tokens = [x.strip() for x in raw_genres.split(",") if x and x.strip()]
        elif isinstance(raw_genres, list):
            for x in raw_genres:
                s = str(x).strip()
                if s:
                    tokens.append(s)
    except Exception:
        tokens = []
    if not tokens:
        return None

    try:
        mapped = map_tmdb_genres(", ".join(tokens))
        if isinstance(mapped, list) and mapped:
            tokens = [str(x).strip() for x in mapped if str(x).strip()]
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    for g in tokens:
        key = g.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(g.strip())
    return out or None


def _pick_best_poster_url_from_images(imgs: Any) -> str | None:
    if not imgs:
        return None
    if isinstance(imgs, str):
        s = imgs.strip()
        return s or None
    if not isinstance(imgs, dict):
        return None

    def _get_str(v: Any) -> str | None:
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    # Jikan-style images: {"jpg": {...}, "webp": {...}}
    for fmt in ("jpg", "webp"):
        b = imgs.get(fmt)
        if isinstance(b, dict):
            for k in ("large_image_url", "image_url", "small_image_url"):
                u = _get_str(b.get(k))
                if u:
                    return u

    # TMDB raw images payloads may include a posters list.
    try:
        posters = imgs.get("posters")
        if isinstance(posters, list) and posters:
            p0 = posters[0]
            if isinstance(p0, dict):
                u = _get_str(p0.get("file_path"))
                if u:
                    return u
    except Exception as e:
        logging.getLogger(__name__).debug(f"Error picking poster from TMDB payload: {e}")
        pass

    # Last resort keys
    for k in ("poster", "poster_url", "url"):
        u = _get_str(imgs.get(k))
        if u:
            return u
    return None


def _pick_best_backdrop_url_from_images(imgs: Any) -> str | None:
    if not imgs:
        return None
    if isinstance(imgs, str):
        s = imgs.strip()
        return s or None
    if not isinstance(imgs, dict):
        return None

    def _get_str(v: Any) -> str | None:
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    # Normalized-style: {"backdrop": {"large_image_url": .., "image_url": .., "small_image_url": ..}}
    b = imgs.get("backdrop")
    if isinstance(b, dict):
        for k in ("large_image_url", "image_url", "small_image_url"):
            u = _get_str(b.get(k))
            if u:
                return u

    # TMDB raw images payloads may include a backdrops list.
    try:
        backs = imgs.get("backdrops")
        if isinstance(backs, list) and backs:
            b0 = backs[0]
            if isinstance(b0, dict):
                u = _get_str(b0.get("file_path"))
                if u:
                    return u
    except Exception:
        pass

    # Last resort keys
    for k in ("backdrop", "backdrop_url", "url"):
        u = _get_str(imgs.get(k))
        if u:
            return u
    return None


def _poster_url_for_item(it: Any) -> str:
    try:
        pp = getattr(it, "poster_path", None)
        if isinstance(pp, str) and pp.strip():
            s = pp.strip()
            if s.startswith("/media-assets/") or s.startswith("http://") or s.startswith("https://"):
                return s
    except Exception:
        pass
    return f"/api/media/{getattr(it, 'id', '')}/poster"


def _backdrop_url_for_item(it: Any) -> str | None:
    try:
        bp = getattr(it, "backdrop_path", None)
        if isinstance(bp, str) and bp.strip():
            s = bp.strip()
            if s.startswith("/media-assets/") or s.startswith("http://") or s.startswith("https://"):
                return s
    except Exception:
        pass
    return None


def _append_cache_bust(url: str | None, ts: Any) -> str | None:
    if not url:
        return url
    v = _safe_ts(ts)
    if not v:
        return url
    try:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}v={v}"
    except Exception:
        return url


def _status_from_metadata_blob(data: Any) -> str | None:
    try:
        obj = json.loads(str(data)) if isinstance(data, str) else data
    except Exception:
        obj = data
    try:
        if isinstance(obj, dict):
            v = obj.get("status")
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:
        pass
    return None


def _is_airing_from_status(provider: str | None, media_type: str | None, status_raw: str | None) -> bool:
    mt = str(media_type or "").strip().lower()
    if mt in ("movie", "film"):
        return False
    s_raw = str(status_raw or "").strip()
    if not s_raw:
        return False
    s = s_raw.lower()
    p = str(provider or "").strip().lower()
    if p == "jikan":
        return s in ("currently airing", "airing")
    if p == "kitsu":
        return s in ("current", "ongoing", "airing")
    if p == "tmdb":
        return s in ("returning series", "in production", "continuing")
    return s in {
        "currently airing",
        "airing",
        "current",
        "ongoing",
        "returning series",
        "in production",
        "continuing",
    }


@router.get("/api/media")
def list_media(
    skip: int = 0,
    limit: int = 100,
    search: str | None = None,
    types: str | None = None,
    status: str | None = None,
    is_kids_safe: bool | None = None,
    lite: bool = True,
    db: Session = Depends(get_db),
):
    q = db.query(models.MediaItem)
    try:
        if search and str(search).strip():
            s = f"%{str(search).strip()}%"
            q = q.filter(
                (models.MediaItem.title.ilike(s))
                | (models.MediaItem.base_title.ilike(s))
                | (models.MediaItem.canonical_path.ilike(s))
            )
    except Exception:
        pass

    try:
        if types and str(types).strip():
            raw = [t.strip().lower() for t in str(types).split(",") if t and t.strip()]
            raw = [t for t in raw if t]
            if raw:
                cond = None
                for t in raw:
                    c = (models.MediaItem.media_type.ilike(t)) | (models.MediaItem.library_type.ilike(t))
                    cond = c if cond is None else (cond | c)
                if cond is not None:
                    q = q.filter(cond)
    except Exception:
        pass

    try:
        if status and str(status).strip():
            raw = [s.strip().upper() for s in str(status).split(",") if s and s.strip()]
            raw = [s for s in raw if s]
            if raw:
                cond = None
                for s in raw:
                    if s in ("NULL", "NONE", "UNSET"):
                        c = (models.MediaItem.status.is_(None))
                    else:
                        c = (models.MediaItem.status == s)
                    cond = c if cond is None else (cond | c)
                if cond is not None:
                    q = q.filter(cond)
    except Exception as e:
        logging.getLogger(__name__).debug(f"Error filtering status: {e}")
        pass

    try:
        if is_kids_safe:
            allowed = {"G", "PG", "TV-Y", "TV-G", "TV-Y7"}
            q = q.filter(models.MediaItem.content_rating.in_(list(allowed)))
            blocked = (
                models.MediaItem.genres.ilike("%hentai%")
                | models.MediaItem.genres.ilike("%erotica%")
                | models.MediaItem.genres.ilike("%erotic%")
            )
            q = q.filter((models.MediaItem.genres.is_(None)) | (~blocked))
    except Exception as e:
        logging.getLogger(__name__).debug(f"Error filtering kids_safe: {e}")
        pass

    try:
        total = int(q.count() or 0)
    except Exception:
        total = 0

    try:
        q = q.order_by(models.MediaItem.id.desc())
    except Exception:
        pass

    items = q.offset(skip).limit(limit).all()

    ids: list[int] = []
    try:
        ids = []
        for it in items:
            it_id = _as_int(getattr(it, "id", None))
            if it_id is None:
                continue
            ids.append(it_id)
    except Exception:
        ids = []

    # Precompute season counts per series (exclude Season 0 specials).
    media_to_series: dict[int, int] = {}
    media_to_season: dict[int, int] = {}
    series_counts: dict[int, int] = {}
    try:
        if ids:
            rows_ms = (
                db.query(models.SeasonItem.media_item_id, models.Season.series_id, models.Season.season_number)
                .join(models.Season, models.Season.id == models.SeasonItem.season_id)
                .filter(models.SeasonItem.media_item_id.in_(ids))
                .all()
            )
            series_ids = set()
            for mid, sid, sn in rows_ms:
                try:
                    if mid is not None and sid is not None:
                        media_to_series[int(mid)] = int(sid)
                        if sn is not None:
                            media_to_season[int(mid)] = int(sn)
                        series_ids.add(int(sid))
                except Exception:
                    continue
            if series_ids:
                rows_sc = (
                    db.query(models.Season.series_id, func.count(models.Season.id))
                    .filter(models.Season.series_id.in_(list(series_ids)))
                    .filter(models.Season.season_number.is_not(None))
                    .filter(models.Season.season_number > 0)
                    .group_by(models.Season.series_id)
                    .all()
                )
                for sid, cnt in rows_sc:
                    try:
                        series_counts[int(sid)] = int(cnt)
                    except Exception:
                        continue
    except Exception:
        media_to_series = {}
        series_counts = {}

    # Resolve localized UI fields via media_translations with deterministic fallback chain.
    preferred_langs = preferred_lang_chain_from_config(get_config)
    translations_by_item: dict[int, dict[str, str | None]] = {}
    try:
        if ids:
            rows_tr = db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(ids)).all()
            grouped: dict[int, list[models.MediaTranslation]] = {}
            for r in rows_tr:
                try:
                    grouped.setdefault(int(getattr(r, "path_id", 0)), []).append(r)
                except Exception:
                    continue
            for mid in ids:
                trs = grouped.get(mid) or []
                translations_by_item[mid] = resolve_translation_fields(trs, preferred_langs=preferred_langs, allow_jikan=False)
    except Exception:
        translations_by_item = {}
    manual_by_item: dict[int, dict[str, str | None]] = {}
    try:
        if ids:
            rows_mm = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id.in_(ids)).all()
            for r in rows_mm:
                try:
                    manual_by_item[int(getattr(r, "media_item_id", 0))] = {
                        "poster": getattr(r, "poster_url", None),
                        "backdrop": getattr(r, "backdrop_url", None),
                    }
                except Exception:
                    continue
    except Exception:
        manual_by_item = {}

    files_by_item: dict[int, list[models.FileRecord]] = {}
    metadata_by_item: dict[int, list[models.MediaMetadata]] = {}
    latest_metadata_by_item: dict[int, models.MediaMetadata] = {}
    relations_by_from: dict[int, list[models.MediaRelation]] = {}
    related_by_id: dict[int, models.MediaItem] = {}
    if not lite:
        try:
            if ids:
                rows_fr = db.query(models.FileRecord).filter(models.FileRecord.media_item_id.in_(ids)).all()
                for r in rows_fr:
                    try:
                        mid = int(getattr(r, "media_item_id", 0))
                    except Exception:
                        continue
                    files_by_item.setdefault(mid, []).append(r)
        except Exception:
            files_by_item = {}
        try:
            if ids:
                rows_md = (
                    db.query(models.MediaMetadata)
                    .filter(models.MediaMetadata.media_item_id.in_(ids))
                    .order_by(
                        models.MediaMetadata.media_item_id,
                        models.MediaMetadata.version.desc(),
                        models.MediaMetadata.created_at.desc(),
                        models.MediaMetadata.id.desc(),
                    )
                    .all()
                )
                for r in rows_md:
                    try:
                        mid = int(getattr(r, "media_item_id", 0))
                    except Exception:
                        continue
                    metadata_by_item.setdefault(mid, []).append(r)
                    if mid not in latest_metadata_by_item:
                        latest_metadata_by_item[mid] = r
        except Exception:
            metadata_by_item = {}
        try:
            if ids:
                rows_rel = db.query(models.MediaRelation).filter(models.MediaRelation.from_item_id.in_(ids)).all()
                rel_targets: set[int] = set()
                for r in rows_rel:
                    try:
                        frm = int(getattr(r, "from_item_id", 0))
                    except Exception:
                        continue
                    relations_by_from.setdefault(frm, []).append(r)
                    try:
                        tid = getattr(r, "to_item_id", None)
                        if tid is not None:
                            rel_targets.add(int(tid))
                    except Exception:
                        pass
                if rel_targets:
                    rows_t = db.query(models.MediaItem).filter(models.MediaItem.id.in_(list(rel_targets))).all()
                    for t in rows_t:
                        try:
                            related_by_id[int(getattr(t, "id", 0))] = t
                        except Exception:
                            continue
        except Exception:
            relations_by_from = {}
            related_by_id = {}
    else:
        try:
            if ids:
                rows_md = (
                    db.query(models.MediaMetadata)
                    .filter(models.MediaMetadata.media_item_id.in_(ids))
                    .order_by(
                        models.MediaMetadata.media_item_id,
                        models.MediaMetadata.version.desc(),
                        models.MediaMetadata.created_at.desc(),
                        models.MediaMetadata.id.desc(),
                    )
                    .all()
                )
                for r in rows_md:
                    try:
                        mid = int(getattr(r, "media_item_id", 0))
                    except Exception:
                        continue
                    if mid not in latest_metadata_by_item:
                        latest_metadata_by_item[mid] = r
        except Exception:
            latest_metadata_by_item = {}
    result = []
    season_series_poster_by_item: dict[int, str] = {}
    try:
        item_ids = [int(getattr(it, "id", 0)) for it in items if getattr(it, "id", None) is not None]
    except Exception:
        item_ids = []
    if item_ids:
        try:
            season_rows = (
                db.query(models.SeasonItem, models.Season, models.Series)
                .join(models.Season, models.Season.id == models.SeasonItem.season_id)
                .join(models.Series, models.Series.id == models.Season.series_id)
                .filter(models.SeasonItem.media_item_id.in_(item_ids))
                .all()
            )
        except Exception:
            season_rows = []
        series_poster_by_series_id: dict[int, str] = {}
        for si, season_row, series_row in season_rows:
            try:
                sid = int(getattr(series_row, "id", 0))
            except Exception:
                continue
            if sid and sid not in series_poster_by_series_id:
                try:
                    mp = getattr(series_row, "main_poster", None)
                except Exception:
                    mp = None
                if isinstance(mp, str) and mp.strip():
                    series_poster_by_series_id[sid] = mp.strip()
        missing_series_ids = [int(getattr(season_row, "series_id", 0)) for _, season_row, _ in season_rows if int(getattr(season_row, "series_id", 0) or 0) and int(getattr(season_row, "series_id", 0)) not in series_poster_by_series_id]
        if missing_series_ids:
            try:
                rows = (
                    db.query(models.Season, models.SeasonItem, models.MediaItem)
                    .join(models.SeasonItem, models.SeasonItem.season_id == models.Season.id)
                    .join(models.MediaItem, models.MediaItem.id == models.SeasonItem.media_item_id)
                    .filter(models.Season.series_id.in_(list(set(missing_series_ids))))
                    .all()
                )
                for season_row, _, mi_row in rows:
                    try:
                        sid = int(getattr(season_row, "series_id", 0))
                    except Exception:
                        continue
                    if not sid or sid in series_poster_by_series_id:
                        continue
                    try:
                        pp = getattr(mi_row, "poster_path", None)
                    except Exception:
                        pp = None
                    if isinstance(pp, str) and pp.strip():
                        series_poster_by_series_id[sid] = pp.strip()
            except Exception:
                pass
        for si, season_row, series_row in season_rows:
            try:
                item_id = int(getattr(si, "media_item_id", 0))
                sid = int(getattr(season_row, "series_id", 0))
            except Exception:
                continue
            if not item_id or not sid:
                continue
            poster = series_poster_by_series_id.get(sid)
            if isinstance(poster, str) and poster.strip():
                season_series_poster_by_item[item_id] = poster.strip()
    for it in items:
        item_id = int(getattr(it, "id", 0)) if getattr(it, "id", None) is not None else 0
        if not item_id:
            continue
            
        tr = translations_by_item.get(item_id)
        files_out = []
        if not lite:
            files = files_by_item.get(item_id, [])
            for f in files:
                path = str(f.path) if f.path is not None else ""
                filename = path.split("\\")[-1].split("/")[-1] if path else None
                fmt = None
                if filename and "." in filename:
                    fmt = filename.rsplit(".", 1)[1].lower()
                files_out.append(
                    {"filename": filename, "path": path, "size": f.size, "mtime": f.mtime, "format": fmt, "index": getattr(f, "file_index", None)}
                )
            try:
                files_out.sort(
                    key=lambda x: (
                        x.get("index") is None,
                        x.get("index") if x.get("index") is not None else 0,
                        x.get("filename") or "",
                    )
                )
            except Exception:
                pass

        metadata = metadata_by_item.get(item_id, [])
        metadata_out = []
        poster_url = None
        backdrop_url = None
        try:
            pp = getattr(it, "poster_path", None)
            if isinstance(pp, str) and pp.strip():
                if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                    poster_url = pp
        except Exception as e:
            logging.getLogger(__name__).debug(f"Error checking poster_path: {e}")
            poster_url = None
        try:
            if not poster_url:
                mm = manual_by_item.get(item_id)
                mp = mm.get("poster") if isinstance(mm, dict) else None
                if isinstance(mp, str) and mp.strip():
                    poster_url = mp
        except Exception as e:
            logging.getLogger(__name__).debug(f"Error checking manual poster: {e}")
            pass
        try:
            bp = getattr(it, "backdrop_path", None)
            if isinstance(bp, str) and bp.strip():
                if bp.startswith("/media-assets/") or bp.startswith("http://") or bp.startswith("https://"):
                    backdrop_url = bp
        except Exception as e:
            logging.getLogger(__name__).debug(f"Error checking backdrop_path: {e}")
            backdrop_url = None
        try:
            if not backdrop_url:
                mm = manual_by_item.get(item_id)
                mb = mm.get("backdrop") if isinstance(mm, dict) else None
                if isinstance(mb, str) and mb.strip():
                    backdrop_url = mb
        except Exception as e:
            logging.getLogger(__name__).debug(f"Error checking manual backdrop: {e}")
            pass
        if not lite:
            for m in metadata:
                try:
                    # Ensure we are loading from a string for json.loads
                    raw_data = str(m.data) if m.data is not None else None
                    data = json.loads(raw_data) if raw_data is not None else None
                except Exception as e:
                    logging.getLogger(__name__).debug(f"Error parsing metadata JSON: {e}")
                    data = m.data
                metadata_out.append(
                    {"provider": m.provider, "provider_id": m.provider_id, "data": data, "created_at": m.created_at.isoformat() if m.created_at is not None else None}
                )
                if not poster_url and data is not None:
                    try:
                        imgs = data.get("images") if isinstance(data, dict) else None
                    except Exception:
                        imgs = None
                    if imgs:
                        try:
                            u = _pick_best_poster_url_from_images(imgs)
                            if u:
                                poster_url = u
                        except Exception:
                            pass
                        try:
                            if isinstance(imgs, dict):
                                posters = imgs.get("posters")
                                if isinstance(posters, list) and len(posters) > 0:
                                    p = posters[0].get("file_path")
                                    if p:
                                        poster_url = p
                        except Exception:
                            pass
                        try:
                            if isinstance(imgs, str) and imgs:
                                poster_url = imgs
                        except Exception:
                            pass
                if not backdrop_url and data is not None:
                    try:
                        imgs = data.get("images") if isinstance(data, dict) else None
                    except Exception:
                        imgs = None
                    if imgs:
                        try:
                            u2 = _pick_best_backdrop_url_from_images(imgs)
                            if u2:
                                backdrop_url = u2
                        except Exception:
                            pass

        if not poster_url:
            poster_url = _poster_url_for_item(it)
        if not backdrop_url:
            backdrop_url = _backdrop_url_for_item(it)

        season_series_poster = season_series_poster_by_item.get(item_id)
        if isinstance(season_series_poster, str) and season_series_poster.strip():
            poster_url = season_series_poster.strip()

        primary_metadata = _pick_primary_metadata(metadata_out) if not lite else None
        related_out = []
        if not lite:
            try:
                rels = relations_by_from.get(item_id, [])
                for r in rels:
                    target_id = getattr(r, "to_item_id", None)
                    if target_id is None:
                        continue
                    tgt = related_by_id.get(int(target_id))
                    if not tgt:
                        continue
                    related_out.append(
                        {
                            "id": tgt.id,
                            "title": tgt.title or tgt.base_title,
                            "relation": r.relation_type,
                            "mal_id": getattr(tgt, "mal_id", None),
                            "poster_url": _poster_url_for_item(tgt),
                        }
                    )
            except Exception as e:
                logging.getLogger(__name__).debug(f"Error resolving relations: {e}")
                related_out = []
        cast_list = None
        try:
            c = getattr(it, "cast", None)
            if isinstance(c, str) and c.strip():
                cast_list = [x.strip() for x in c.split(",") if x.strip()]
            elif isinstance(c, list):
                cast_list = c
        except Exception:
            cast_list = None
        genres_list = _normalize_genres_list(getattr(it, "genres", None))
        airing_status = None
        is_airing = False
        try:
            md = latest_metadata_by_item.get(item_id)
            provider_for_airing = (
                str(getattr(md, "provider", None) or getattr(it, "provider", None) or "").strip().lower()
            )
            airing_status = _status_from_metadata_blob(getattr(md, "data", None)) if md is not None else None
            is_airing = _is_airing_from_status(provider_for_airing, getattr(it, "media_type", None), airing_status)
        except Exception:
            airing_status = None
            is_airing = False

        result.append(
            {
                "id": it.id,
                "media_id": getattr(it, "media_id", None),
                "canonical_path": getattr(it, "canonical_path", None),
                "canonical_path_hash": getattr(it, "canonical_path_hash", None),
                "base_title": getattr(it, "base_title", None) or it.title,
                "title": it.title,
                "title_en": getattr(it, "title_en", None),
                "title_localized": (tr.get("title") if isinstance(tr, dict) else None) or getattr(it, "title_localized", None),
                "synopsis_localized": (tr.get("overview") if isinstance(tr, dict) else None) or getattr(it, "synopsis_localized", None),
                "media_type": it.media_type,
                "status": getattr(it, "status", None),
                "media_root": getattr(it, "media_root", None),
                "provider": it.provider,
                "provider_id": it.provider_id,
                "mal_id": getattr(it, "mal_id", None),
                "tmdb_id": getattr(it, "tmdb_id", None),
                "poster_path": (season_series_poster.strip() if isinstance(season_series_poster, str) and season_series_poster.strip() else getattr(it, "poster_path", None)),
                "backdrop_path": (manual_by_item.get(item_id, {}).get("backdrop") if isinstance(manual_by_item.get(item_id, {}), dict) else None)
                or getattr(it, "backdrop_path", None),
                "series_id": media_to_series.get(item_id),
                "season_number": media_to_season.get(item_id),
                "season_count": series_counts.get(media_to_series.get(item_id) or 0),
                "has_multiple_seasons": bool(series_counts.get(media_to_series.get(item_id) or 0, 0) > 1),
                "rating": getattr(it, "rating", None),
                "content_rating": getattr(it, "content_rating", None),
                "genres": genres_list,
                "is_animated": getattr(it, "is_animated", None),
                "origin": getattr(it, "origin", None),
                "release_year": getattr(it, "release_year", None),
                "runtime": getattr(it, "runtime", None),
                "cast": cast_list,
                "is_airing": bool(is_airing),
                "airing_status": airing_status,
                "created_at": _safe_iso(getattr(it, "created_at", None)),
                "is_identified": getattr(it, "is_identified", None),
                "files": files_out,
                "metadata": metadata_out,
                "primary_metadata": primary_metadata,
                "poster_url": poster_url,
                "backdrop_url": backdrop_url,
                "related": related_out,
            }
        )
    return {"total": total, "items": result}


@router.get("/api/media/search-suggest")
def search_suggest_media(
    query: str,
    limit: int = 20,
    types: str | None = None,
    db: Session = Depends(get_db),
):
    raw_types: list[str] | None = None
    try:
        if types and str(types).strip():
            raw = [t.strip().lower() for t in str(types).split(",") if t and t.strip()]
            raw_types = raw if raw else None
    except Exception:
        raw_types = None

    ids = search_suggest.suggest_media(db=db, query=str(query or ""), limit=limit, types=raw_types)
    if not ids:
        return {"items": [], "total": 0}

    items = db.query(models.MediaItem).filter(models.MediaItem.id.in_(ids)).all()
    preferred_langs = preferred_lang_chain_from_config(get_config)
    translations_by_item: dict[int, dict[str, str | None]] = {}
    try:
        rows_tr = db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(ids)).all()
        grouped: dict[int, list[models.MediaTranslation]] = {}
        for r in rows_tr:
            try:
                grouped.setdefault(int(getattr(r, "path_id", 0)), []).append(r)
            except Exception:
                continue
        for mid in ids:
            trs = grouped.get(mid) or []
            translations_by_item[mid] = resolve_translation_fields(trs, preferred_langs=preferred_langs, allow_jikan=False)
    except Exception:
        translations_by_item = {}

    by_id: dict[int, Any] = {}
    for it in items:
        it_id = _as_int(getattr(it, "id", None))
        if it_id is None:
            continue
        by_id[it_id] = it
    result = []
    for mid in ids:
        mid_id = _as_int(mid)
        if mid_id is None:
            continue
        it = by_id.get(mid_id)
        if not it:
            continue
        it_id = _as_int(getattr(it, "id", None))
        tr = translations_by_item.get(it_id) if it_id is not None else None
        poster_url = None
        try:
            pp = getattr(it, "poster_path", None)
            if isinstance(pp, str) and pp.strip():
                if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                    poster_url = pp
        except Exception:
            poster_url = None
        if not poster_url:
            poster_url = _poster_url_for_item(it)
        backdrop_url = None
        try:
            bp = getattr(it, "backdrop_path", None)
            if isinstance(bp, str) and bp.strip():
                if bp.startswith("/media-assets/") or bp.startswith("http://") or bp.startswith("https://"):
                    backdrop_url = bp
        except Exception:
            backdrop_url = None
        if not backdrop_url:
            backdrop_url = _backdrop_url_for_item(it)
        result.append(
            {
                "id": it.id,
                "title": it.title,
                "base_title": getattr(it, "base_title", None),
                "title_en": getattr(it, "title_en", None),
                "title_localized": (tr.get("title") if isinstance(tr, dict) else None) or getattr(it, "title_localized", None),
                "synopsis_localized": (tr.get("overview") if isinstance(tr, dict) else None) or getattr(it, "synopsis_localized", None),
                "media_type": it.media_type,
                "library_type": getattr(it, "library_type", None),
                "poster_path": getattr(it, "poster_path", None),
                "poster_url": poster_url,
                "backdrop_path": getattr(it, "backdrop_path", None),
                "backdrop_url": backdrop_url,
                "release_year": getattr(it, "release_year", None),
                "rating": getattr(it, "rating", None),
                "content_rating": getattr(it, "content_rating", None),
                "genres": getattr(it, "genres", None),
                "created_at": _safe_iso(getattr(it, "created_at", None)),
                "mal_id": getattr(it, "mal_id", None),
                "is_identified": getattr(it, "is_identified", None),
            }
        )
    return {"items": result, "total": len(result)}


@router.get("/api/media/recommendations")
def get_recommendations(
    limit: int = 20,
    profile_id: str | None = None,
    profile_header: str | None = Header(None, alias="X-Arcanea-Profile-Id"),
    db: Session = Depends(get_db),
):
    pid = profile_id or profile_header
    items = recommendations.get_recommendations(db, profile_id=pid, limit=limit)
    preferred_langs = preferred_lang_chain_from_config(get_config)
    translations_by_item: dict[int, dict[str, str | None]] = {}
    metadata_by_item: dict[int, models.MediaMetadata] = {}
    media_to_series: dict[int, int] = {}
    series_counts: dict[int, int] = {}
    try:
        ids = [int(getattr(it, "id", 0)) for it in items if getattr(it, "id", None) is not None]
        if ids:
            # Resolve season counts for recommendations
            rows_ms = (
                db.query(models.SeasonItem.media_item_id, models.Season.series_id, models.Season.season_number)
                .join(models.Season, models.Season.id == models.SeasonItem.season_id)
                .filter(models.SeasonItem.media_item_id.in_(ids))
                .all()
            )
            series_ids = set()
            for mid, sid, sn in rows_ms:
                if mid is not None and sid is not None:
                    media_to_series[int(mid)] = int(sid)
                    series_ids.add(int(sid))
            
            if series_ids:
                rows_sc = (
                    db.query(models.Season.series_id, func.count(models.Season.id))
                    .filter(models.Season.series_id.in_(list(series_ids)))
                    .filter(models.Season.season_number.is_not(None))
                    .filter(models.Season.season_number > 0)
                    .group_by(models.Season.series_id)
                    .all()
                )
                for sid, cnt in rows_sc:
                    series_counts[int(sid)] = int(cnt)

            rows_tr = db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(ids)).all()
            grouped: dict[int, list[models.MediaTranslation]] = {}
            for r in rows_tr:
                try:
                    grouped.setdefault(int(getattr(r, "path_id", 0)), []).append(r)
                except Exception:
                    continue
            for mid in ids:
                trs = grouped.get(mid) or []
                translations_by_item[mid] = resolve_translation_fields(trs, preferred_langs=preferred_langs, allow_jikan=False)
            try:
                rows_md = (
                    db.query(models.MediaMetadata)
                    .filter(models.MediaMetadata.media_item_id.in_(ids))
                    .order_by(models.MediaMetadata.version.desc(), models.MediaMetadata.created_at.desc(), models.MediaMetadata.id.desc())
                    .all()
                )
                for r in rows_md:
                    mid = getattr(r, "media_item_id", None)
                    if mid is None:
                        continue
                    if int(mid) not in metadata_by_item:
                        metadata_by_item[int(mid)] = r
            except Exception:
                metadata_by_item = {}
    except Exception:
        translations_by_item = {}

    out = []
    for it in items:
        item_id = _as_int(getattr(it, "id", None))
        if item_id is None:
            continue
            
        tr = translations_by_item.get(item_id)
        poster_url = None
        backdrop_url = None
        try:
            pp = getattr(it, "poster_path", None)
            if isinstance(pp, str) and pp.strip():
                if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                    poster_url = pp
        except Exception:
            poster_url = None
        try:
            bp = getattr(it, "backdrop_path", None)
            if isinstance(bp, str) and bp.strip():
                if bp.startswith("/media-assets/") or bp.startswith("http://") or bp.startswith("https://"):
                    backdrop_url = bp
        except Exception:
            backdrop_url = None
        if (not poster_url) or (not backdrop_url):
            try:
                r = metadata_by_item.get(item_id)
                data = None
                if r and getattr(r, "data", None):
                    try:
                        raw_r_data = str(r.data) if r.data is not None else None
                        data = json.loads(raw_r_data) if raw_r_data is not None else r.data
                    except Exception:
                        data = r.data
                imgs = data.get("images") if isinstance(data, dict) else None
                if imgs:
                    if not poster_url:
                        poster_url = _pick_best_poster_url_from_images(imgs)
                    if not backdrop_url:
                        backdrop_url = _pick_best_backdrop_url_from_images(imgs)
            except Exception:
                pass
        if not poster_url:
            poster_url = _poster_url_for_item(it)
        if not backdrop_url:
            backdrop_url = _backdrop_url_for_item(it)
            
        series_id = media_to_series.get(item_id)
        scount = series_counts.get(series_id, 0) if series_id is not None else 0
        
        out.append(
            {
                "id": it.id,
                "title": it.title,
                "title_localized": (tr.get("title") if isinstance(tr, dict) else None) or getattr(it, "title_localized", None),
                "synopsis_localized": (tr.get("overview") if isinstance(tr, dict) else None) or getattr(it, "synopsis_localized", None),
                "media_type": it.media_type,
                "poster_url": poster_url,
                "backdrop_path": getattr(it, "backdrop_path", None),
                "backdrop_url": backdrop_url,
                "rating": getattr(it, "rating", None),
                "content_rating": getattr(it, "content_rating", None),
                "genres": getattr(it, "genres", None),
                "created_at": _safe_iso(getattr(it, "created_at", None)),
                "is_identified": getattr(it, "is_identified", None),
                "season_count": scount,
                "has_multiple_seasons": scount >= 2,
            }
        )
    return {"ok": True, "items": out}


@router.get("/api/media/in-progress")
def get_in_progress(
    limit: int = 12,
    profile_id: str | None = None,
    profile_header: str | None = Header(None, alias="X-Arcanea-Profile-Id"),
    db: Session = Depends(get_db),
):
    pid = profile_id or profile_header
    rows = recommendations.get_in_progress(db, profile_id=pid, limit=limit)
    if not rows:
        return {"ok": True, "items": []}

    items = [it for it, _ph, _ratio in rows if getattr(it, "id", None) is not None]
    ids: list[int] = []
    try:
        ids = [int(getattr(it, "id", 0)) for it in items if getattr(it, "id", None) is not None]
    except Exception:
        ids = []

    preferred_langs = preferred_lang_chain_from_config(get_config)
    translations_by_item: dict[int, dict[str, str | None]] = {}
    metadata_by_item: dict[int, models.MediaMetadata] = {}
    media_to_series: dict[int, int] = {}
    series_counts: dict[int, int] = {}

    try:
        if ids:
            rows_ms = (
                db.query(models.SeasonItem.media_item_id, models.Season.series_id, models.Season.season_number)
                .join(models.Season, models.Season.id == models.SeasonItem.season_id)
                .filter(models.SeasonItem.media_item_id.in_(ids))
                .all()
            )
            series_ids = set()
            for mid, sid, sn in rows_ms:
                if mid is not None and sid is not None:
                    media_to_series[int(mid)] = int(sid)
                    series_ids.add(int(sid))

            if series_ids:
                rows_sc = (
                    db.query(models.Season.series_id, func.count(models.Season.id))
                    .filter(models.Season.series_id.in_(list(series_ids)))
                    .filter(models.Season.season_number.is_not(None))
                    .filter(models.Season.season_number > 0)
                    .group_by(models.Season.series_id)
                    .all()
                )
                for sid, cnt in rows_sc:
                    series_counts[int(sid)] = int(cnt)

            rows_tr = db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(ids)).all()
            grouped: dict[int, list[models.MediaTranslation]] = {}
            for r in rows_tr:
                try:
                    grouped.setdefault(int(getattr(r, "path_id", 0)), []).append(r)
                except Exception:
                    continue
            for mid in ids:
                trs = grouped.get(mid) or []
                translations_by_item[mid] = resolve_translation_fields(
                    trs, preferred_langs=preferred_langs, allow_jikan=False
                )
            try:
                rows_md = (
                    db.query(models.MediaMetadata)
                    .filter(models.MediaMetadata.media_item_id.in_(ids))
                    .order_by(
                        models.MediaMetadata.version.desc(),
                        models.MediaMetadata.created_at.desc(),
                        models.MediaMetadata.id.desc(),
                    )
                    .all()
                )
                for r in rows_md:
                    mid = getattr(r, "media_item_id", None)
                    if mid is None:
                        continue
                    if int(mid) not in metadata_by_item:
                        metadata_by_item[int(mid)] = r
            except Exception:
                metadata_by_item = {}
    except Exception:
        translations_by_item = {}

    out = []
    for it, ph, ratio in rows:
        item_id = _as_int(getattr(it, "id", None))
        if item_id is None:
            continue

        tr = translations_by_item.get(item_id)
        poster_url = None
        backdrop_url = None
        try:
            pp = getattr(it, "poster_path", None)
            if isinstance(pp, str) and pp.strip():
                if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                    poster_url = pp
        except Exception:
            poster_url = None
        try:
            bp = getattr(it, "backdrop_path", None)
            if isinstance(bp, str) and bp.strip():
                if bp.startswith("/media-assets/") or bp.startswith("http://") or bp.startswith("https://"):
                    backdrop_url = bp
        except Exception:
            backdrop_url = None
        if (not poster_url) or (not backdrop_url):
            try:
                r = metadata_by_item.get(item_id)
                data = None
                if r and getattr(r, "data", None):
                    try:
                        raw_r_data = str(r.data) if r.data is not None else None
                        data = json.loads(raw_r_data) if raw_r_data is not None else r.data
                    except Exception:
                        data = r.data
                imgs = data.get("images") if isinstance(data, dict) else None
                if imgs:
                    if not poster_url:
                        poster_url = _pick_best_poster_url_from_images(imgs)
                    if not backdrop_url:
                        backdrop_url = _pick_best_backdrop_url_from_images(imgs)
            except Exception:
                pass
        if not poster_url:
            poster_url = _poster_url_for_item(it)
        if not backdrop_url:
            backdrop_url = _backdrop_url_for_item(it)

        series_id = media_to_series.get(item_id)
        scount = series_counts.get(series_id, 0) if series_id is not None else 0
        progress = 0.0
        try:
            progress = max(0.0, min(100.0, float(ratio) * 100.0))
        except Exception:
            progress = 0.0
        resume_snapshot = recommendations.build_progress_snapshot(
            position=getattr(ph, "last_position", None),
            duration=getattr(ph, "last_duration", None),
            path=getattr(ph, "last_path", None),
            watched=bool(getattr(ph, "watched", False)),
        )

        out.append(
            {
                "id": it.id,
                "title": it.title,
                "title_localized": (tr.get("title") if isinstance(tr, dict) else None)
                or getattr(it, "title_localized", None),
                "synopsis_localized": (tr.get("overview") if isinstance(tr, dict) else None)
                or getattr(it, "synopsis_localized", None),
                "media_type": it.media_type,
                "poster_url": poster_url,
                "backdrop_path": getattr(it, "backdrop_path", None),
                "backdrop_url": backdrop_url,
                "rating": getattr(it, "rating", None),
                "content_rating": getattr(it, "content_rating", None),
                "genres": getattr(it, "genres", None),
                "created_at": _safe_iso(getattr(it, "created_at", None)),
                "is_identified": getattr(it, "is_identified", None),
                "season_count": scount,
                "has_multiple_seasons": scount >= 2,
                "watch_progress": progress,
                "resume_position": getattr(ph, "last_position", None),
                "resume_duration": getattr(ph, "last_duration", None),
                "resume_path": getattr(ph, "last_path", None),
                "resume_position_minutes": resume_snapshot.get("position_minutes"),
                "resume_duration_minutes": resume_snapshot.get("duration_minutes"),
                "resume_remaining_minutes": resume_snapshot.get("remaining_minutes"),
                "resume_season_number": resume_snapshot.get("season_number"),
                "resume_episode_number": resume_snapshot.get("episode_number"),
            }
        )
    return {"ok": True, "items": out}


@router.post("/api/media/{item_id}/play")
def record_play(
    item_id: int,
    profile_id: str | None = None,
    profile_header: str | None = Header(None, alias="X-Arcanea-Profile-Id"),
    db: Session = Depends(get_db),
):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    pid = profile_id or profile_header
    return recommendations.record_play(db, media_item_id=item_id, profile_id=pid)


@router.post("/api/media/{item_id}/progress")
def record_progress(
    item_id: int,
    payload: ProgressPayload,
    profile_id: str | None = None,
    profile_header: str | None = Header(None, alias="X-Arcanea-Profile-Id"),
    db: Session = Depends(get_db),
):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    pid = profile_id or profile_header
    return recommendations.record_progress(
        db,
        media_item_id=item_id,
        profile_id=pid,
        position=payload.position,
        duration=payload.duration,
        path=payload.path,
    )


@router.get("/api/media/{item_id}/progress")
def get_progress(
    item_id: int,
    profile_id: str | None = None,
    profile_header: str | None = Header(None, alias="X-Arcanea-Profile-Id"),
    db: Session = Depends(get_db),
):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    pid = profile_id or profile_header
    return recommendations.get_progress(db, media_item_id=item_id, profile_id=pid)


@router.get("/api/media/{item_id}")
def get_media(item_id: int, db: Session = Depends(get_db)):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")

    preferred_langs = preferred_lang_chain_from_config(get_config)
    tr = None
    try:
        rows_tr = db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id == int(item_id)).all()
        tr = resolve_translation_fields(rows_tr or [], preferred_langs=preferred_langs, allow_jikan=False)
    except Exception:
        tr = None
    files = db.query(models.FileRecord).filter(models.FileRecord.media_item_id == it.id).all()
    files_out = []
    for f in files:
        path = str(f.path) if f.path is not None else ""
        filename = path.split("\\")[-1].split("/")[-1] if path else None
        fmt = None
        if filename and "." in filename:
            fmt = filename.rsplit(".", 1)[1].lower()
        files_out.append({"filename": filename, "path": path, "size": f.size, "mtime": f.mtime, "format": fmt, "index": getattr(f, "file_index", None)})
    try:
        files_out.sort(
            key=lambda x: (
                x.get("index") is None,
                x.get("index") if x.get("index") is not None else 0,
                x.get("filename") or "",
            )
        )
    except Exception:
        pass

    metadata = (
        db.query(models.MediaMetadata)
        .filter(models.MediaMetadata.media_item_id == it.id)
        .order_by(models.MediaMetadata.version.desc(), models.MediaMetadata.created_at.desc(), models.MediaMetadata.id.desc())
        .all()
    )
    metadata_out = []
    poster_url = None
    backdrop_url = None
    try:
        pp = getattr(it, "poster_path", None)
        if isinstance(pp, str) and pp.strip():
            if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                poster_url = pp
    except Exception:
        poster_url = None
    manual_backdrop = None
    manual_updated_at = None
    try:
        if not poster_url:
            mm = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == it.id).first()
            mp = getattr(mm, "poster_url", None) if mm else None
            if isinstance(mp, str) and mp.strip():
                poster_url = mp
            manual_backdrop = getattr(mm, "backdrop_url", None) if mm else None
            manual_updated_at = getattr(mm, "updated_at", None) if mm else None
    except Exception:
        manual_backdrop = None
        manual_updated_at = None
    try:
        bp = getattr(it, "backdrop_path", None)
        if isinstance(bp, str) and bp.strip():
            if bp.startswith("/media-assets/") or bp.startswith("http://") or bp.startswith("https://"):
                backdrop_url = bp
    except Exception:
        backdrop_url = None
    try:
        if not backdrop_url and isinstance(manual_backdrop, str) and manual_backdrop.strip():
            backdrop_url = manual_backdrop.strip()
    except Exception:
        pass
    for m in metadata:
        try:
            raw_data = str(m.data) if m.data is not None else None
            data = json.loads(raw_data) if raw_data is not None else None
        except Exception as e:
            logging.getLogger(__name__).debug(f"Error parsing metadata JSON: {e}")
            data = m.data
        metadata_out.append({"provider": m.provider, "provider_id": m.provider_id, "data": data, "created_at": m.created_at.isoformat() if m.created_at is not None else None})
        if not poster_url and data is not None:
            try:
                imgs = data.get("images") if isinstance(data, dict) else None
            except Exception:
                imgs = None
            if imgs:
                try:
                    u = _pick_best_poster_url_from_images(imgs)
                    if u:
                        poster_url = u
                except Exception:
                    pass
                try:
                    if isinstance(imgs, dict):
                        posters = imgs.get("posters")
                        if isinstance(posters, list) and len(posters) > 0:
                            p = posters[0].get("file_path")
                            if p:
                                poster_url = p
                except Exception:
                    pass
                try:
                    if isinstance(imgs, str) and imgs:
                        poster_url = imgs
                except Exception:
                    pass
        if not backdrop_url and data is not None:
            try:
                imgs = data.get("images") if isinstance(data, dict) else None
            except Exception:
                imgs = None
            if imgs:
                try:
                    u2 = _pick_best_backdrop_url_from_images(imgs)
                    if u2:
                        backdrop_url = u2
                except Exception:
                    pass

    related_out = []
    try:
        rels = (
            db.query(models.MediaRelation)
            .filter(models.MediaRelation.from_item_id == it.id)
            .all()
        )
        for r in rels:
            if getattr(r, "to_item_id", None) is None:
                continue
            tgt = db.query(models.MediaItem).filter(models.MediaItem.id == r.to_item_id).first()
            if not tgt:
                continue
            related_out.append(
                {
                    "id": tgt.id,
                    "title": tgt.title or tgt.base_title,
                    "relation": r.relation_type,
                    "mal_id": getattr(tgt, "mal_id", None),
                    "poster_url": _poster_url_for_item(tgt),
                }
            )
    except Exception:
        related_out = []

    if not poster_url:
        poster_url = _poster_url_for_item(it)
    # Cache-bust manual assets using manual mapping timestamp (best-effort).
    poster_url = _append_cache_bust(poster_url, manual_updated_at)
    backdrop_url = _append_cache_bust(backdrop_url, manual_updated_at)

    primary_metadata = _pick_primary_metadata(metadata_out)
    cast_list = None
    try:
        c = getattr(it, "cast", None)
        if isinstance(c, str) and c.strip():
            cast_list = [x.strip() for x in c.split(",") if x.strip()]
        elif isinstance(c, list):
            cast_list = c
    except Exception:
        cast_list = None
    genres_list = _normalize_genres_list(getattr(it, "genres", None))
    series_id = None
    season_count = 0
    try:
        sm = (
            db.query(models.SeasonItem, models.Season)
            .join(models.Season, models.Season.id == models.SeasonItem.season_id)
            .filter(models.SeasonItem.media_item_id == it.id)
            .first()
        )
        if sm:
            _si, _season = sm
            series_id = getattr(_season, "series_id", None)
        if series_id is not None:
            sc = (
                db.query(func.count(models.Season.id))
                .filter(models.Season.series_id == int(series_id))
                .filter(models.Season.season_number.is_not(None))
                .filter(models.Season.season_number > 0)
                .scalar()
            )
            season_count = int(sc or 0)
    except Exception as e:
        logging.getLogger(__name__).debug(f"Error getting season count: {e}")
        series_id = None
        season_count = 0
    return {
        "id": it.id,
        "media_id": getattr(it, "media_id", None),
        "canonical_path": getattr(it, "canonical_path", None),
        "canonical_path_hash": getattr(it, "canonical_path_hash", None),
        "base_title": getattr(it, "base_title", None) or it.title,
        "title": it.title,
        "title_en": getattr(it, "title_en", None),
        "title_localized": (tr.get("title") if isinstance(tr, dict) else None) or getattr(it, "title_localized", None),
        "synopsis_localized": (tr.get("overview") if isinstance(tr, dict) else None) or getattr(it, "synopsis_localized", None),
        "media_type": it.media_type,
        "status": getattr(it, "status", None),
        "media_root": getattr(it, "media_root", None),
        "provider": it.provider,
        "provider_id": it.provider_id,
        "mal_id": getattr(it, "mal_id", None),
        "tmdb_id": getattr(it, "tmdb_id", None),
        "poster_path": getattr(it, "poster_path", None),
        "backdrop_path": manual_backdrop or getattr(it, "backdrop_path", None),
        "backdrop_url": backdrop_url,
        "rating": getattr(it, "rating", None),
        "content_rating": getattr(it, "content_rating", None),
        "genres": genres_list,
        "is_animated": getattr(it, "is_animated", None),
        "origin": getattr(it, "origin", None),
                "release_year": getattr(it, "release_year", None),
                "runtime": getattr(it, "runtime", None),
                "cast": cast_list,
                "created_at": _safe_iso(getattr(it, "created_at", None)),
                "is_identified": getattr(it, "is_identified", None),
        "season_count": season_count,
        "has_multiple_seasons": season_count >= 2,
        "files": files_out,
        "metadata": metadata_out,
        "primary_metadata": primary_metadata,
        "poster_url": poster_url,
        "related": related_out,
    }


@router.post("/api/media/{item_id}/enrich")
def enrich_media_item(item_id: int, _: bool = Depends(admin_required)):
    try:
        db = SessionLocal()
        try:
            it = db.query(models.MediaItem).filter(models.MediaItem.id == int(item_id)).first()
        finally:
            db.close()
        if not it:
            raise HTTPException(status_code=404, detail="Not found")

        ok = bool(enrich_one_serialized(int(item_id)))
        if not ok:
            db2 = SessionLocal()
            try:
                it2 = db2.query(models.MediaItem).filter(models.MediaItem.id == int(item_id)).first()
                st = getattr(it2, "status", None) if it2 else None
            finally:
                db2.close()
            if st == "NO_MATCH":
                return {"ok": False, "detail": "No match (Jikan/TMDB) para este título."}
            return {"ok": False, "detail": "No se pudo consultar el proveedor (Jikan/TMDB). Intenta de nuevo en unos minutos."}
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger(__name__).exception("Failed forcing enrichment for media item %s", item_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/media/{item_id}/omit")
def omit_media_item(item_id: int, db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        setattr(it, "status", "OMITTED")
        db.add(it)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="failed updating status")
    return {"ok": True, "id": item_id, "status": "OMITTED"}


@router.get("/api/media/{item_id}/poster")
def media_poster(item_id: int, db: Session = Depends(get_db)):
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        pp = getattr(it, "poster_path", None)
        if isinstance(pp, str) and pp.strip():
            if pp.startswith("/media-assets/") or pp.startswith("http://") or pp.startswith("https://"):
                return RedirectResponse(pp)
        if pp and os.path.isfile(str(pp)):
            return FileResponse(str(pp))
    except Exception:
        pass
    metadata = db.query(models.MediaMetadata).filter(models.MediaMetadata.media_item_id == it.id).all()
    for m in metadata:
        try:
            raw_data = str(m.data) if m.data is not None else None
            data = json.loads(raw_data) if raw_data is not None else None
        except Exception:
            data = m.data
        if data is None:
            continue
        imgs = None
        try:
            imgs = data.get("images") if isinstance(data, dict) else None
        except Exception:
            imgs = None
        if imgs:
            try:
                u = _pick_best_poster_url_from_images(imgs)
                if u:
                    return RedirectResponse(u)
            except Exception:
                pass
            try:
                if isinstance(imgs, dict):
                    posters = imgs.get("posters")
                    if isinstance(posters, list) and len(posters) > 0:
                        p = posters[0].get("file_path")
                        if p:
                            return RedirectResponse(p)
            except Exception:
                pass
            try:
                if isinstance(imgs, str) and imgs:
                    return RedirectResponse(imgs)
            except Exception:
                pass
    try:
        cp = getattr(it, "canonical_path", None)
        if cp and os.path.isdir(cp):
            candidates = ["poster.jpg", "poster.png", "folder.jpg", "folder.png", "cover.jpg", "cover.png"]
            for c in candidates:
                p = os.path.join(cp, c)
                if os.path.exists(p) and os.path.isfile(p):
                    ct, _ = mimetypes.guess_type(p)
                    ct = ct or "image/jpeg"
                    return StreamingResponse(open(p, "rb"), media_type=ct)
            for entry in os.listdir(cp):
                ln = entry.lower()
                if ln in ["poster.jpg", "poster.png", "folder.jpg", "folder.png", "cover.jpg", "cover.png", "1.jpg", "1.webp", "1.png"]:
                    p = os.path.join(cp, entry)
                    if os.path.exists(p) and os.path.isfile(p):
                        ct, _ = mimetypes.guess_type(p)
                        ct = ct or "image/jpeg"
                        return StreamingResponse(open(p, "rb"), media_type=ct)
    except Exception:
        pass

    # Fallback: never 404 for posters.
    # The frontend uses this endpoint as a stable poster URL, so returning 404 produces "broken" gray cards.
    # Use a lightweight SVG placeholder instead.
    try:
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600" viewBox="0 0 400 600">
<defs>
  <linearGradient id="bg" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0%" stop-color="#0B0F1A"/>
    <stop offset="55%" stop-color="#171B2C"/>
    <stop offset="100%" stop-color="#2B1B3D"/>
  </linearGradient>
  <linearGradient id="glow" x1="0" x2="1" y1="0" y2="1">
    <stop offset="0%" stop-color="#5B6CFF" stop-opacity="0.25"/>
    <stop offset="100%" stop-color="#D946EF" stop-opacity="0.10"/>
  </linearGradient>
</defs>
<rect width="400" height="600" fill="url(#bg)"/>
<rect x="0" y="0" width="400" height="600" fill="url(#glow)"/>
<rect x="20" y="24" width="360" height="552" rx="28" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.10)"/>
<text x="200" y="315" text-anchor="middle" fill="#E6E9FF" font-family="Segoe UI, Arial, sans-serif" font-size="38" font-weight="700" letter-spacing="1">ArcaNea</text>
<text x="200" y="350" text-anchor="middle" fill="#A9B3D9" font-family="Segoe UI, Arial, sans-serif" font-size="14" letter-spacing="2">NO POSTER</text>
</svg>"""
        return Response(content=svg.encode("utf-8"), media_type="image/svg+xml")
    except Exception:
        raise HTTPException(status_code=404, detail="No poster available")


@router.get("/api/media/{item_id}/episode-seasons")
def get_media_episode_seasons(
    item_id: int,
    background_tasks: BackgroundTasks,
    include_related: bool = True,
    db: Session = Depends(get_db),
):
    """Return JOINed seasons/episodes for a media item.

    The frontend should use this instead of reading episode lists from `media_metadata.data` blobs.
    """
    it = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Not found")

    si = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == item_id).first()
    if not si:
        return {"ok": True, "series": None, "seasons": [], "related": []}

    season = db.query(models.Season).filter(models.Season.id == si.season_id).first()
    if not season:
        return {"ok": True, "series": None, "seasons": [], "related": []}

    series = None
    try:
        if getattr(season, "series_id", None) is not None:
            series = db.query(models.Series).filter(models.Series.id == season.series_id).first()
    except Exception:
        series = None

    series_backdrop = None
    try:
        series_backdrop = getattr(it, "backdrop_path", None)
    except Exception:
        series_backdrop = None

    seasons_out: list[dict[str, Any]] = []
    preferred_langs = preferred_lang_chain_from_config(get_config)
    manual_episode_overrides: list[dict[str, Any]] | None = None
    try:
        rows_mo = (
            db.query(models.ManualOverride)
            .filter(models.ManualOverride.media_item_id == item_id)
            .order_by(models.ManualOverride.updated_at.desc())
            .all()
        )
        if rows_mo:
            chosen = None
            for lang in preferred_langs:
                for r in rows_mo:
                    if str(getattr(r, "language", "")).lower() == str(lang).lower():
                        chosen = r
                        break
                if chosen:
                    break
            if not chosen:
                chosen = rows_mo[0]
            raw = getattr(chosen, "episode_overrides", None)
            try:
                manual_episode_overrides = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                manual_episode_overrides = None
            if not isinstance(manual_episode_overrides, list):
                manual_episode_overrides = None
    except Exception:
        manual_episode_overrides = None

    def build_season_payload(season_row: Any, media_item_id: int | None) -> dict[str, Any]:
        eps = (
            db.query(models.Episode, models.FileRecord)
            .join(models.EpisodeFile, models.EpisodeFile.episode_id == models.Episode.id)
            .join(models.FileRecord, models.FileRecord.id == models.EpisodeFile.file_record_id)
            .filter(models.Episode.season_id == season_row.id)
            .order_by(models.Episode.episode_number.asc(), models.Episode.id.asc())
            .all()
        )
        episodes_out = []
        for ep, fr in eps:
            path = getattr(fr, "path", None)
            filename = None
            try:
                filename = path.split("\\")[-1].split("/")[-1] if path else None
            except Exception:
                filename = None
            try:
                title_best = getattr(ep, "title_localized", None) or getattr(ep, "title_en", None) or getattr(ep, "title", None)
            except Exception:
                title_best = getattr(ep, "title", None)
            episodes_out.append(
                {
                    "id": ep.id,
                    "episode_number": getattr(ep, "episode_number", None),
                    "title": title_best,
                    "title_en": getattr(ep, "title_en", None),
                    "title_localized": getattr(ep, "title_localized", None),
                    "synopsis_localized": getattr(ep, "synopsis_localized", None),
                    "still_path": (
                        (getattr(ep, "still_path", None) or None)
                        if (getattr(ep, "still_path", None) or "")
                        else (series_backdrop if isinstance(series_backdrop, str) and series_backdrop.strip() else None)
                    ),
                    "file": {
                        "id": fr.id,
                        "path": path,
                        "filename": filename,
                        "file_index": getattr(fr, "file_index", None),
                        "size": getattr(fr, "size", None),
                        "mtime": getattr(fr, "mtime", None),
                    },
                }
            )
        if manual_episode_overrides:
            def _norm_title(v: str | None) -> str:
                try:
                    return "".join(ch.lower() for ch in str(v or "").strip() if ch.isalnum())
                except Exception:
                    return ""

            ep_by_num = {}
            ep_by_title = {}
            for ep_row in episodes_out:
                n = ep_row.get("episode_number")
                try:
                    if n is not None:
                        ep_by_num[int(n)] = ep_row
                except Exception:
                    pass
                t = ep_row.get("title_localized") or ep_row.get("title_en") or ep_row.get("title")
                nt = _norm_title(t)
                if nt:
                    ep_by_title.setdefault(nt, []).append(ep_row)

            remapped = []
            used_ids: set[int] = set()
            for idx, ov in enumerate(manual_episode_overrides):
                if not isinstance(ov, dict):
                    continue
                original_num = ov.get("original_episode_number")
                target = None
                if original_num is not None:
                    try:
                        target = ep_by_num.get(int(original_num))
                    except Exception:
                        target = None
                if not target:
                    t = _norm_title(ov.get("title"))
                    if t and t in ep_by_title:
                        for cand in ep_by_title[t]:
                            try:
                                if int(cand.get("id")) in used_ids:
                                    continue
                            except Exception:
                                pass
                            target = cand
                            break
                if not target:
                    val_ep = ov.get("episode_number")
                    if val_ep is not None:
                        try:
                            target = ep_by_num.get(int(val_ep))
                        except Exception:
                            target = None
                if not target:
                    continue
                try:
                    target_id = target.get("id")
                    if target_id is not None:
                        used_ids.add(int(target_id))
                except Exception:
                    pass
                new_num = ov.get("episode_number")
                try:
                    new_num = int(new_num) if new_num is not None else idx + 1
                except Exception:
                    new_num = idx + 1
                ep_out = dict(target)
                ep_out["episode_number"] = new_num
                if ov.get("title"):
                    ep_out["title"] = ov.get("title")
                    ep_out["title_localized"] = ov.get("title")
                if ov.get("overview"):
                    ep_out["synopsis_localized"] = ov.get("overview")
                try:
                    if isinstance(ep_out.get("file"), dict):
                        ep_out["file"] = dict(ep_out["file"])
                        ep_out["file"]["file_index"] = new_num
                except Exception:
                    pass
                remapped.append(ep_out)
            if remapped:
                episodes_out = remapped
        try:
            season_title_best = getattr(season_row, "title_localized", None) or getattr(season_row, "title_en", None) or None
        except Exception:
            season_title_best = None
        return {
            "season_id": season_row.id,
            "season_number": getattr(season_row, "season_number", None),
            "title": season_title_best,
            "title_en": getattr(season_row, "title_en", None),
            "title_localized": getattr(season_row, "title_localized", None),
            "synopsis_localized": getattr(season_row, "synopsis_localized", None),
            "poster_path": getattr(season_row, "poster_path", None),
            "backdrop_path": getattr(season_row, "backdrop_path", None),
            "tmdb_season_number": getattr(season_row, "tmdb_season_number", None),
            "tmdb_episode_start": getattr(season_row, "tmdb_episode_start", None),
            "tmdb_episode_end": getattr(season_row, "tmdb_episode_end", None),
            "air_date_start": getattr(season_row, "air_date_start", None),
            "air_date_end": getattr(season_row, "air_date_end", None),
            "media_item_id": media_item_id,
            "episodes": episodes_out,
        }

    def _is_generic_episode_title(v: str | None) -> bool:
        if v is None:
            return True
        s = str(v).strip()
        if not s:
            return True
        if re.fullmatch(r"\\d{1,4}", s):
            return True
        if s.lower().startswith("episode ") or s.lower().startswith("episodio "):
            return True
        return False

    # Primary: seasons that belong to the same logical series (when available).
    try:
        if series and getattr(series, "id", None) is not None:
            srows = (
                db.query(models.Season)
                .filter(models.Season.series_id == series.id)
                .order_by(models.Season.season_number.asc(), models.Season.id.asc())
                .all()
            )
        else:
            srows = [season]
    except Exception:
        srows = [season]

    for srow in srows:
        mid: int | None = None
        try:
            sm = db.query(models.SeasonItem).filter(models.SeasonItem.season_id == srow.id).first()
            if sm is not None:
                mid_val = getattr(sm, "media_item_id", None)
                if mid_val is not None:
                    mid = int(mid_val)
        except Exception:
            mid = None
        seasons_out.append(build_season_payload(srow, mid))

    # Lazy episode title backfill: if we detect generic titles, schedule a background backfill for this item.
    # This keeps UI "self-healing" for cases where scan rebuilt normalized tables after enrichment.
    backfill_scheduled = False
    try:
        if background_tasks is not None and seasons_out:
            # If any episode in the primary season has a generic title, request a backfill.
            primary = seasons_out[0]
            eps0 = primary.get("episodes") if isinstance(primary, dict) else None
            needs = False
            if isinstance(eps0, list) and eps0:
                for e in eps0:
                    tloc = e.get("title_localized") if isinstance(e, dict) else None
                    ten = e.get("title_en") if isinstance(e, dict) else None
                    tbest = e.get("title") if isinstance(e, dict) else None
                    if (not (str(tloc or "").strip())) and (_is_generic_episode_title(str(ten or tbest or ""))):
                        needs = True
                        break
            if needs:
                try:
                    st = get_enrich_state()
                    if not st.get("running"):
                        background_tasks.add_task(enrichment_runner.backfill_episodes_for_media_item_serialized, int(item_id))
                        backfill_scheduled = True
                except Exception:
                    # If we can't read state, still attempt in background.
                    background_tasks.add_task(enrichment_runner.backfill_episodes_for_media_item_serialized, int(item_id))
                    backfill_scheduled = True
    except Exception:
        backfill_scheduled = False

    related_out: list[dict[str, Any]] = []
    if include_related:
        try:
            rels = (
                db.query(models.MediaRelation)
                .filter(models.MediaRelation.from_item_id == item_id)
                .all()
            )
            seen_season_ids = {s.get("season_id") for s in seasons_out if s.get("season_id")}
            for r in rels:
                if not getattr(r, "to_item_id", None):
                    continue
                t_si = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == r.to_item_id).first()
                if not t_si:
                    continue
                t_season = db.query(models.Season).filter(models.Season.id == t_si.season_id).first()
                if not t_season:
                    continue
                if t_season.id in seen_season_ids:
                    continue
                tgt = db.query(models.MediaItem).filter(models.MediaItem.id == r.to_item_id).first()
                related_out.append(
                    {
                        "relation": getattr(r, "relation_type", None),
                        "media_item_id": getattr(r, "to_item_id", None),
                        "title": (getattr(tgt, "title", None) or getattr(tgt, "base_title", None)) if tgt else None,
                        "season_id": t_season.id,
                        "season_number": getattr(t_season, "season_number", None),
                    }
                )
        except Exception:
            related_out = []

    return {
        "ok": True,
        "series": {"id": series.id, "title": series.title} if series else None,
        "seasons": seasons_out,
        "related": related_out,
        "backfill_scheduled": bool(backfill_scheduled),
    }


@router.get("/api/library/overview")
def library_overview(sample: int = 6, db: Session = Depends(get_db)):
    """Return an organized overview of the library grouped by `library_type`.

    Each group contains a total count and a small sample of recent items.
    """
    try:
        rows = db.query(models.MediaItem.library_type, func.count(models.MediaItem.id)).group_by(models.MediaItem.library_type).all()
    except Exception:
        rows = []
    groups = []
    try:
        for lib_type, cnt in rows:
            try:
                items = (
                    db.query(models.MediaItem)
                    .filter(models.MediaItem.library_type == lib_type)
                    .order_by(models.MediaItem.id.desc())
                    .limit(int(sample))
                    .all()
                )
            except Exception:
                items = []
            sample_out = []
            for it in items:
                try:
                    sample_out.append(
                        {
                            "id": it.id,
                            "title": it.title or getattr(it, "base_title", None),
                            "media_type": getattr(it, "media_type", None),
                            "poster_url": _poster_url_for_item(it),
                            "is_identified": getattr(it, "is_identified", None),
                        }
                    )
                except Exception:
                    continue
            groups.append({"library_type": lib_type, "count": int(cnt or 0), "sample": sample_out})
    except Exception:
        groups = []
    return {"groups": groups}
