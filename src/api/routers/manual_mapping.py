from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...db import models
from ...services import manual_mapping
from ..deps import get_db, admin_required

router = APIRouter()


def _require_key():
    try:
        if not manual_mapping._get_tmdb_api_key():
            raise HTTPException(status_code=400, detail="tmdb_api_key_missing")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="tmdb_api_key_missing")


@router.get("/api/manual-mapping/{item_id}")
def get_manual_mapping(item_id: int, db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    mm = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == item_id).first()
    rows = (
        db.query(models.ManualOverride)
        .filter(models.ManualOverride.media_item_id == item_id)
        .order_by(models.ManualOverride.updated_at.desc())
        .all()
    )
    overrides = []
    for r in rows:
        overrides.append(
            {
                "language": r.language,
                "title": r.title,
                "overview": r.overview,
                "genres": r.genres,
                "episode_overrides": r.episode_overrides,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        )
    return {
        "ok": True,
        "mapping": {
            "tmdb_id": getattr(mm, "tmdb_id", None),
            "media_type": getattr(mm, "media_type", None),
            "season_number": getattr(mm, "season_number", None),
            "poster_url": getattr(mm, "poster_url", None),
            "backdrop_url": getattr(mm, "backdrop_url", None),
        }
        if mm
        else None,
        "overrides": overrides,
    }


@router.get("/api/manual-mapping/{item_id}/local-seasons")
def get_manual_local_seasons(item_id: int, db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    """Return local seasons (media_item_ids) for the logical series that `item_id` belongs to.

    This is used by the Manual Mapping UI to bulk-apply a TMDB match across multiple seasons
    without fetching full episode lists (which can be large).
    """
    si = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == item_id).first()
    if not si:
        return {"ok": True, "series": None, "seasons": []}

    season = db.query(models.Season).filter(models.Season.id == si.season_id).first()
    if not season:
        return {"ok": True, "series": None, "seasons": []}

    series = None
    try:
        if getattr(season, "series_id", None) is not None:
            series = db.query(models.Series).filter(models.Series.id == season.series_id).first()
    except Exception:
        series = None

    rows: list[tuple[Any, Any, Any]] = []
    try:
        if series and getattr(series, "id", None) is not None:
            rows = (
                db.query(models.Season, models.SeasonItem, models.MediaItem)
                .join(models.SeasonItem, models.SeasonItem.season_id == models.Season.id)
                .join(models.MediaItem, models.MediaItem.id == models.SeasonItem.media_item_id)
                .filter(models.Season.series_id == series.id)
                .order_by(models.Season.season_number.asc(), models.Season.id.asc())
                .all()
            )
        else:
            mi = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
            rows = [(season, si, mi)]
    except Exception:
        mi = db.query(models.MediaItem).filter(models.MediaItem.id == item_id).first()
        rows = [(season, si, mi)]

    seasons_out = []
    for srow, sitem, mi in rows:
        mid = getattr(sitem, "media_item_id", None)
        if mid is None:
            continue
        try:
            mid_i = int(mid)
        except Exception:
            continue
        seasons_out.append(
            {
                "media_item_id": mid_i,
                "season_id": getattr(srow, "id", None),
                "season_number": getattr(srow, "season_number", None),
                "title_en": getattr(srow, "title_en", None),
                "title_localized": getattr(srow, "title_localized", None),
                "canonical_path": getattr(mi, "canonical_path", None) if mi else None,
            }
        )

    return {
        "ok": True,
        "series": {"id": series.id, "title": series.title} if series else None,
        "seasons": seasons_out,
    }


@router.get("/api/manual-mapping/tmdb/search")
def manual_tmdb_search(query: str, language: str = "es-MX", _: bool = Depends(admin_required)):
    _require_key()
    if not str(query or "").strip():
        return {"ok": True, "results": []}
    return manual_mapping.tmdb_search_multi(str(query), language=language)


@router.get("/api/manual-mapping/jikan/search")
def manual_jikan_search(query: str, _: bool = Depends(admin_required)):
    if not str(query or "").strip():
        return {"ok": True, "results": []}
    return manual_mapping.jikan_search(str(query))


@router.get("/api/manual-mapping/kitsu/search")
def manual_kitsu_search(query: str, _: bool = Depends(admin_required)):
    if not str(query or "").strip():
        return {"ok": True, "results": []}
    return manual_mapping.kitsu_search(str(query))


@router.get("/api/manual-mapping/jikan/{mal_id}/details")
def manual_jikan_details(mal_id: str, _: bool = Depends(admin_required)):
    return manual_mapping.jikan_details(mal_id)


@router.get("/api/manual-mapping/kitsu/{kitsu_id}/details")
def manual_kitsu_details(kitsu_id: str, _: bool = Depends(admin_required)):
    return manual_mapping.kitsu_details(kitsu_id)


@router.get("/api/manual-mapping/tmdb/{tmdb_id}/details")
def manual_tmdb_details(tmdb_id: str, media_type: str = "tv", language: str = "es-MX", _: bool = Depends(admin_required)):
    _require_key()
    media_type = str(media_type or "tv").strip().lower()
    if media_type not in ("tv", "movie"):
        raise HTTPException(status_code=400, detail="invalid_media_type")
    return manual_mapping.tmdb_details(tmdb_id, media_type=media_type, language=language)


@router.get("/api/manual-mapping/tmdb/{tmdb_id}/seasons")
def manual_tmdb_seasons(tmdb_id: str, language: str = "es-MX", _: bool = Depends(admin_required)):
    _require_key()
    return manual_mapping.tmdb_seasons(tmdb_id, language=language)


@router.get("/api/manual-mapping/tmdb/{tmdb_id}/season/{season_number}")
def manual_tmdb_season(
    tmdb_id: str,
    season_number: int,
    language: str = "es-MX",
    _: bool = Depends(admin_required),
):
    _require_key()
    return manual_mapping.tmdb_season_details(tmdb_id, season_number=season_number, language=language)


@router.post("/api/manual-mapping/{item_id}/apply")
def apply_manual_mapping(item_id: int, payload: dict[str, Any], db: Session = Depends(get_db), _: bool = Depends(admin_required)):
    try:
        provider_raw = payload.get("provider")
        if provider_raw is None or not str(provider_raw).strip():
            provider = "tmdb"
        else:
            provider = str(provider_raw).strip().lower()
        if provider not in ("tmdb", "jikan", "kitsu"):
            raise HTTPException(status_code=400, detail="invalid_provider")
        mal_id = str(payload.get("mal_id") or "").strip() or None
        kitsu_id = str(payload.get("kitsu_id") or "").strip() or None
        tmdb_id = str(payload.get("tmdb_id") or "").strip() or None
        media_type = str(payload.get("media_type") or "").strip() or None
        season_number = payload.get("season_number")
        try:
            season_number = int(season_number) if season_number is not None else None
        except Exception:
            season_number = None
        language = str(payload.get("language") or "es-MX").strip()
        title = payload.get("title") or None
        overview = payload.get("overview") or None
        genres = payload.get("genres") if isinstance(payload.get("genres"), list) else None
        poster_url = payload.get("poster_url") or None
        backdrop_url = payload.get("backdrop_url") or None
        season_title = payload.get("season_title") or None
        episode_overrides = payload.get("episode_overrides") if isinstance(payload.get("episode_overrides"), list) else None
        download_assets = bool(payload.get("download_assets")) if payload.get("download_assets") is not None else True
        translation_only = bool(payload.get("translation_only")) if payload.get("translation_only") is not None else False
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_payload")

    if provider == "jikan":
        if not mal_id:
            raise HTTPException(status_code=400, detail="mal_id_missing")
        return manual_mapping.apply_manual_mapping_jikan(
            db=db,
            media_item_id=int(item_id),
            mal_id=mal_id,
            fetch_episodes=bool(payload.get("fetch_episodes", False))
        )

    if provider == "kitsu":
        if not kitsu_id:
            raise HTTPException(status_code=400, detail="kitsu_id_missing")
        return manual_mapping.apply_manual_mapping_kitsu(
            db=db,
            media_item_id=int(item_id),
            kitsu_id=kitsu_id,
            season_number=season_number,
            language=language,
            title=title,
            overview=overview,
            genres=genres,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
            season_title=season_title,
            download_assets=download_assets,
        )

    return manual_mapping.apply_manual_mapping(
        db=db,
        media_item_id=int(item_id),
        tmdb_id=tmdb_id,
        media_type=media_type,
        season_number=season_number,
        language=language,
        title=title,
        overview=overview,
        genres=genres,
        poster_url=poster_url,
        backdrop_url=backdrop_url,
        season_title=season_title,
        episode_overrides=episode_overrides,
        download_assets=download_assets,
        translation_only=translation_only,
    )
