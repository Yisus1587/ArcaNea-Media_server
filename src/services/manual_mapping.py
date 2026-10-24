from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, cast

import requests

from ..core.config import get as get_config
from ..core.config import DATA_DIR
from ..db import models, SessionLocal

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/"

try:
    from ..providers import provider_jikan as jikan
except Exception:
    try:
        from src.providers import provider_jikan as jikan  # type: ignore
    except Exception:
        jikan = None
try:
    from ..providers import provider_kitsu as kitsu
except Exception:
    try:
        from src.providers import provider_kitsu as kitsu  # type: ignore
    except Exception:
        kitsu = None


def _is_anime_context_for_manual_mapping(db, mi: Any) -> bool:
    """Allow Jikan/Kitsu only for anime-like items (safety guard).

    Prevents accidentally applying anime providers to movie/series libraries.
    """
    if not mi:
        return False
    try:
        if str(getattr(mi, "library_type", "") or "").strip().lower() == "anime":
            return True
    except Exception:
        pass
    try:
        root_path = getattr(mi, "media_root", None)
        if root_path:
            mr = db.query(models.MediaRoot).filter(models.MediaRoot.path == str(root_path)).first()
            if mr and str(getattr(mr, "type", "") or "").strip().lower() == "anime":
                return True
    except Exception:
        pass
    try:
        if getattr(mi, "mal_id", None):
            return True
    except Exception:
        pass
    return False


def _guess_ext(url: str, content_type: str | None = None) -> str:
    try:
        _, ext = os.path.splitext(url)
        if ext and len(ext) <= 5:
            return ext
    except Exception:
        pass
    if content_type:
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
    return ".jpg"


def _download_to(url: str, target_path: Path) -> Path | None:
    try:
        resp = requests.get(url, stream=True, timeout=25)
        resp.raise_for_status()
        ct = resp.headers.get("content-type")
        ext = _guess_ext(url, ct)
        target_path = target_path.with_suffix(ext)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
        return target_path
    except Exception as e:
        logger.warning("Failed downloading asset %s: %s", url, e)
        return None


def _is_remote_asset_url(value: str | None) -> bool:
    try:
        v = str(value or "").strip().lower()
        return v.startswith("http://") or v.startswith("https://")
    except Exception:
        return False


def _is_current_manual_asset_url(media_item_id: int, value: str | None) -> bool:
    try:
        v = str(value or "").strip()
        return bool(v and v.startswith(f"/media-assets/{int(media_item_id)}/"))
    except Exception:
        return False


def _cleanup_manual_asset_dir(
    media_item_id: int,
    *,
    remove_poster: bool = True,
    remove_backdrop: bool = True,
) -> Path:
    base_dir = Path(DATA_DIR) / "assets" / str(media_item_id)
    if base_dir.exists():
        try:
            for item in base_dir.iterdir():
                if not item.is_file():
                    continue
                stem = str(item.stem or "").strip().lower()
                should_remove = (
                    (remove_poster and stem.startswith("poster"))
                    or (remove_backdrop and stem.startswith("backdrop"))
                )
                if should_remove:
                    item.unlink()
        except Exception as e:
            logger.warning("Failed cleaning up existing assets in %s: %s", base_dir, e)
    return base_dir


def _clear_manual_provider_rows(db, media_item_id: int) -> None:
    try:
        db.query(models.MediaMetadata).filter(models.MediaMetadata.media_item_id == int(media_item_id)).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        db.query(models.MediaImage).filter(models.MediaImage.media_item_id == int(media_item_id)).delete(synchronize_session=False)
    except Exception:
        pass


def _reset_media_item_manual_state(
    mi: Any,
    *,
    clear_artwork: bool = False,
    clear_descriptive: bool = False,
) -> None:
    # Provider identity and provider-derived fields must not survive a new manual mapping.
    for attr in (
        "provider",
        "provider_id",
        "tmdb_id",
        "mal_id",
        "jikan_type",
        "jikan_anchor_mal_id",
        "title_en",
        "release_year",
        "runtime",
        "cast",
        "rating",
        "content_rating",
        "origin",
        "is_animated",
    ):
        try:
            setattr(mi, attr, None)
        except Exception:
            pass
    if clear_artwork:
        for attr in ("poster_path", "backdrop_path"):
            try:
                setattr(mi, attr, None)
            except Exception:
                pass
    if clear_descriptive:
        for attr in ("genres",):
            try:
                setattr(mi, attr, None)
            except Exception:
                pass


def _normalize_episodes_for_item(media_item_id: int) -> bool:
    """Best-effort: rebuild normalized Episode/EpisodeFile rows for a media item.

    Uses existing FileRecord rows; no external API calls.
    """
    try:
        from ..scanner.scanner import Scanner
    except Exception:
        return False

    session = SessionLocal()
    try:
        mi = session.query(models.MediaItem).filter(models.MediaItem.id == int(media_item_id)).first()
        if not mi:
            return False
        files = session.query(models.FileRecord).filter(models.FileRecord.media_item_id == int(media_item_id)).all()
        media_files = [str(getattr(f, "path", "") or "") for f in files if getattr(f, "path", None)]
        if not media_files:
            return False
        folder = str(getattr(mi, "canonical_path", "") or "").strip()
        if not folder:
            try:
                folder = str(Path(media_files[0]).parent)
            except Exception:
                folder = ""
        if not folder:
            return False
    finally:
        try:
            session.close()
        except Exception:
            pass

    try:
        scanner = Scanner(discovery_only=False)
        scanner._upsert_folder(
            folder,
            media_files,
            skip_grouping=True,
            canonical_path_override=folder,
            discovery_only_override=False,
        )
        return True
    except Exception as e:
        logger.exception("Manual mapping normalization failed for media_item=%s: %s", media_item_id, e)
        return False


def _get_tmdb_api_key() -> str:
    try:
        return str(get_config("tmdb_api_key") or "").strip()
    except Exception:
        return ""


def _tmdb_get(path: str, *, language: str | None = None, params: dict[str, Any] | None = None) -> dict:
    key = _get_tmdb_api_key()
    if not key:
        raise RuntimeError("tmdb_api_key_missing")
    url = f"{TMDB_BASE}/{path.lstrip('/')}"
    q = {"api_key": key}
    if language:
        q["language"] = language
    if params:
        q.update(params)
    res = requests.get(url, params=q, timeout=20)
    res.raise_for_status()
    return res.json() if res.content else {}


def _merge_tmdb_images(*image_payloads: dict) -> tuple[list[str], list[str]]:
    posters: list[str] = []
    backdrops: list[str] = []
    seen_posters: set[str] = set()
    seen_backdrops: set[str] = set()
    for images in image_payloads:
        for p in images.get("posters") or []:
            fp = p.get("file_path")
            if not fp or fp in seen_posters:
                continue
            seen_posters.add(fp)
            posters.append(f"{TMDB_IMAGE_BASE}w780{fp}")
        for b in images.get("backdrops") or []:
            fp = b.get("file_path")
            if not fp or fp in seen_backdrops:
                continue
            seen_backdrops.add(fp)
            backdrops.append(f"{TMDB_IMAGE_BASE}original{fp}")
    return posters, backdrops


def _tmdb_images(media_type: str, tmdb_id: str, *, language: str | None) -> tuple[list[str], list[str]]:
    # TMDB images endpoint supports include_image_language to broaden results.
    lang = (language or "").strip()
    langs = []
    if lang:
        langs.append(lang)
    # Always add common sources for richer choices.
    for fallback in ("en", "en-US", "ja", "ja-JP", "null"):
        if fallback not in langs:
            langs.append(fallback)
    include_lang = ",".join(langs)
    region = None
    if "-" in lang:
        region = lang.split("-", 1)[1].upper()
    images = _tmdb_get(
        f"{media_type}/{tmdb_id}/images",
        params={"include_image_language": include_lang, **({"image_region": region} if region else {})},
    )
    return _merge_tmdb_images(images)


def tmdb_search_multi(query: str, *, language: str) -> dict:
    data = _tmdb_get("search/multi", language=language, params={"query": query, "include_adult": "false"})
    out = []
    for r in data.get("results") or []:
        if not r or r.get("media_type") not in ("tv", "movie"):
            continue
        out.append(
            {
                "tmdb_id": r.get("id"),
                "media_type": r.get("media_type"),
                "title": r.get("title") or r.get("name") or "",
                "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
                "poster": f"{TMDB_IMAGE_BASE}w342{r['poster_path']}" if r.get("poster_path") else None,
            }
        )
    return {"ok": True, "results": out}


def tmdb_details(tmdb_id: str, *, media_type: str, language: str) -> dict:
    d = _tmdb_get(f"{media_type}/{tmdb_id}", language=language)
    posters, backdrops = _tmdb_images(media_type, tmdb_id, language=language)
    return {
        "ok": True,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": d.get("title") or d.get("name") or "",
        "overview": d.get("overview") or "",
        "genres": [g.get("name") for g in (d.get("genres") or []) if g and g.get("name")],
        "backdrop": f"{TMDB_IMAGE_BASE}original{d['backdrop_path']}" if d.get("backdrop_path") else None,
        "poster": f"{TMDB_IMAGE_BASE}w780{d['poster_path']}" if d.get("poster_path") else None,
        "posters": posters,
        "backdrops": backdrops,
        "number_of_seasons": d.get("number_of_seasons"),
    }


def tmdb_seasons(tmdb_id: str, *, language: str) -> dict:
    d = _tmdb_get(f"tv/{tmdb_id}", language=language)
    seasons = []
    for s in d.get("seasons") or []:
        if s is None:
            continue
        sn = s.get("season_number")
        if sn is None:
            continue
        seasons.append({"season_number": sn, "name": s.get("name")})
    return {"ok": True, "tmdb_id": tmdb_id, "seasons": seasons}


def tmdb_season_details(tmdb_id: str, *, season_number: int, language: str) -> dict:
    season = _tmdb_get(f"tv/{tmdb_id}/season/{season_number}", language=language)
    lang = (language or "").strip()
    langs = []
    if lang:
        langs.append(lang)
    for fallback in ("en", "en-US", "ja", "ja-JP", "null"):
        if fallback not in langs:
            langs.append(fallback)
    include_lang = ",".join(langs)
    region = None
    if "-" in lang:
        region = lang.split("-", 1)[1].upper()
    images = _tmdb_get(
        f"tv/{tmdb_id}/season/{season_number}/images",
        params={"include_image_language": include_lang, **({"image_region": region} if region else {})},
    )
    episodes = []
    for ep in season.get("episodes") or []:
        episodes.append(
            {
                "episode_number": ep.get("episode_number"),
                "title": ep.get("name") or "",
                "overview": ep.get("overview") or "",
            }
        )
    posters, backdrops = _merge_tmdb_images(images)
    return {
        "ok": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "title": season.get("name") or "",
        "overview": season.get("overview") or "",
        "episodes": episodes,
        "posters": posters,
        "backdrops": backdrops,
    }


def _jikan_media_type(obj: dict | None) -> str:
    try:
        t = str((obj or {}).get("type") or "").strip().lower()
    except Exception:
        t = ""
    if "movie" in t:
        return "movie"
    return "tv"


def _jikan_poster(obj: dict | None) -> str | None:
    try:
        images = (obj or {}).get("images") or {}
        jpg = images.get("jpg") or {}
        return jpg.get("large_image_url") or jpg.get("image_url")
    except Exception:
        return None


def _normalize_jikan_candidate(obj: dict) -> dict:
    poster = _jikan_poster(obj)
    title = obj.get("title_english") or obj.get("title") or obj.get("title_japanese") or ""
    year = obj.get("year") or ""
    return {
        "mal_id": obj.get("mal_id"),
        "media_type": _jikan_media_type(obj),
        "title": title,
        "year": str(year) if year is not None else "",
        "poster": poster,
    }


def _kitsu_pick_title(attrs: dict | None) -> str:
    a = attrs or {}
    try:
        ct = a.get("canonicalTitle")
        if isinstance(ct, str) and ct.strip():
            return ct.strip()
    except Exception:
        pass
    titles = a.get("titles") if isinstance(a.get("titles"), dict) else {}
    for k in ("en", "en_us", "en_jp", "es", "es_mx", "es_es", "ja_jp"):
        try:
            v = titles.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            continue
    try:
        v = titles.get("en") or titles.get("en_jp")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return ""


def _kitsu_poster(attrs: dict | None) -> str | None:
    a = attrs or {}
    try:
        pi = a.get("posterImage")
        if isinstance(pi, dict):
            return pi.get("original") or pi.get("large") or pi.get("medium") or pi.get("small")
    except Exception:
        pass
    return None


def _kitsu_cover(attrs: dict | None) -> str | None:
    a = attrs or {}
    try:
        ci = a.get("coverImage")
        if isinstance(ci, dict):
            return ci.get("original") or ci.get("large") or ci.get("medium") or ci.get("small")
    except Exception:
        pass
    return None


def _normalize_kitsu_candidate(obj: dict) -> dict:
    try:
        attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
    except Exception:
        attrs = {}
    title = _kitsu_pick_title(attrs)
    poster = _kitsu_poster(attrs)
    year = ""
    try:
        sd = attrs.get("startDate")
        if isinstance(sd, str) and len(sd) >= 4:
            year = sd[:4]
    except Exception:
        year = ""
    return {
        "kitsu_id": obj.get("id"),
        "media_type": "tv",
        "title": title,
        "year": year,
        "poster": poster,
    }


def kitsu_search(query: str) -> dict:
    if not kitsu:
        raise RuntimeError("kitsu_unavailable")
    q = str(query or "").strip()
    if not q:
        return {"ok": True, "results": []}
    try:
        rows = kitsu.search(q) if hasattr(kitsu, "search") else []
        out = [_normalize_kitsu_candidate(r) for r in rows if isinstance(r, dict)]
        return {"ok": True, "results": out}
    except Exception as e:
        logger.exception("Kitsu manual search failed for %s: %s", q, e)
        raise


def kitsu_details(kitsu_id: str) -> dict:
    if not kitsu:
        raise RuntimeError("kitsu_unavailable")
    kid = str(kitsu_id or "").strip()
    if not kid:
        return {"ok": False, "detail": "kitsu_id_missing"}
    try:
        res = kitsu.fetch_by_id(kid) if hasattr(kitsu, "fetch_by_id") else None
    except Exception as e:
        logger.exception("Kitsu manual details failed for kitsu_id=%s: %s", kid, e)
        raise
    if not isinstance(res, dict):
        return {"ok": False, "detail": "not_found"}
    try:
        data = res.get("data") if isinstance(res.get("data"), dict) else None
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "detail": "not_found"}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    title = _kitsu_pick_title(attrs)
    overview = ""
    try:
        syn = attrs.get("synopsis")
        overview = kitsu.clean_synopsis_text(syn) if isinstance(syn, str) and syn.strip() else ""
    except Exception:
        overview = ""
    poster = _kitsu_poster(attrs)
    cover = _kitsu_cover(attrs)
    posters = [poster] if poster else []
    backdrops = [cover] if cover else []
    year = ""
    try:
        sd = attrs.get("startDate")
        if isinstance(sd, str) and len(sd) >= 4:
            year = sd[:4]
    except Exception:
        year = ""
    return {
        "ok": True,
        "kitsu_id": kid,
        "media_type": "tv",
        "title": title,
        "overview": overview,
        "genres": [],
        "poster": poster,
        "posters": posters,
        "backdrops": backdrops,
        "year": year,
    }


def jikan_search(query: str) -> dict:
    if not jikan:
        raise RuntimeError("jikan_unavailable")
    q = str(query or "").strip()
    if not q:
        return {"ok": True, "results": []}
    try:
        results = []
        if hasattr(jikan, "buscar_anime_candidates"):
            results = jikan.buscar_anime_candidates(q) or []
        elif hasattr(jikan, "search"):
            one = jikan.search(q)
            results = [one] if one else []
        out = [_normalize_jikan_candidate(r) for r in results if isinstance(r, dict)]
        return {"ok": True, "results": out}
    except Exception as e:
        logger.exception("Jikan manual search failed for %s: %s", q, e)
        raise


def jikan_details(mal_id: str) -> dict:
    if not jikan:
        raise RuntimeError("jikan_unavailable")
    mid = str(mal_id or "").strip()
    if not mid:
        return {"ok": False, "detail": "invalid_mal_id"}
    res = None
    try:
        if hasattr(jikan, "fetch_by_id"):
            res = jikan.fetch_by_id(mid)
    except Exception as e:
        logger.exception("Jikan details failed for %s: %s", mid, e)
        raise
    if not res:
        return {"ok": False, "detail": "not_found"}
    poster = _jikan_poster(res)
    genres = []
    for g in (res.get("genres") or []):
        try:
            name = g.get("name") if isinstance(g, dict) else str(g)
            if name:
                genres.append(name)
        except Exception:
            continue
    title = res.get("title_english") or res.get("title") or res.get("title_japanese") or ""
    overview = res.get("synopsis") or ""
    return {
        "ok": True,
        "mal_id": mid,
        "media_type": _jikan_media_type(res),
        "title": title,
        "overview": overview,
        "genres": genres,
        "poster": poster,
        "posters": [poster] if poster else [],
        "backdrops": [],
        "year": str(res.get("year") or "") if res.get("year") is not None else "",
    }


def apply_manual_mapping_jikan(
    *,
    db,
    media_item_id: int,
    mal_id: str,
    fetch_episodes: bool = False,
) -> dict:
    mi = db.query(models.MediaItem).filter(models.MediaItem.id == media_item_id).first()
    if not mi:
        return {"ok": False, "detail": "media_item_not_found"}
    try:
        if not _is_anime_context_for_manual_mapping(db, mi):
            logger.warning(
                "Manual Jikan mapping on non-anime context media_item=%s (explicit override)",
                media_item_id,
            )
    except Exception:
        pass
    try:
        mi_any = cast(Any, mi)
        _clear_manual_provider_rows(db, int(media_item_id))
        _reset_media_item_manual_state(mi_any, clear_artwork=True, clear_descriptive=True)
        mi_any.provider = "jikan"
        mi_any.mal_id = str(mal_id).strip()
        try:
            if mi_any.status in ("NO_MATCH", "ERROR", "MANUAL"):
                mi_any.status = None
        except Exception:
            pass
        db.add(mi_any)
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Manual Jikan mapping commit failed for media_item=%s: %s", media_item_id, e)
        return {"ok": False, "detail": "db_commit_failed", "error": str(e)}
    try:
        from . import enrichment_runner
        # Use config override if fetch_episodes is True (best-effort).
        # Note: packaged builds might temporarily lag behind and miss this helper.
        set_temp_override = None
        if fetch_episodes:
            try:
                from ..core.config import set_temp_override as _set_temp_override  # type: ignore
                set_temp_override = _set_temp_override
            except Exception:
                set_temp_override = None
            if set_temp_override:
                set_temp_override("jikan_fetch_episodes", True)
                # Fetch per-episode synopsis as well (best-effort). Can be slow.
                set_temp_override("jikan_fetch_episode_details", True)
        
        ok = bool(enrichment_runner.enrich_one_serialized(int(media_item_id), providers_override=["jikan"]))
        
        if fetch_episodes and set_temp_override:
            set_temp_override("jikan_fetch_episodes", None)  # Clear override
            set_temp_override("jikan_fetch_episode_details", None)  # Clear override
            
        return {"ok": ok, "provider": "jikan", "detail": "enrich_failed" if not ok else None}
    except Exception as e:
        logger.exception("Manual Jikan enrichment failed for media_item=%s: %s", media_item_id, e)
        return {"ok": False, "detail": "enrich_failed", "error": str(e)}


def apply_manual_mapping_kitsu(
    *,
    db,
    media_item_id: int,
    kitsu_id: str,
    season_number: int | None = None,
    language: str = "es-MX",
    title: str | None = None,
    overview: str | None = None,
    genres: list[str] | None = None,
    poster_url: str | None = None,
    backdrop_url: str | None = None,
    season_title: str | None = None,
    download_assets: bool | None = None,
) -> dict:
    mi = db.query(models.MediaItem).filter(models.MediaItem.id == media_item_id).first()
    if not mi:
        return {"ok": False, "detail": "media_item_not_found"}
    try:
        if not _is_anime_context_for_manual_mapping(db, mi):
            logger.warning(
                "Manual Kitsu mapping on non-anime context media_item=%s (explicit override)",
                media_item_id,
            )
    except Exception:
        pass
    try:
        raw = kitsu.fetch_by_id(str(kitsu_id).strip()) if kitsu and hasattr(kitsu, "fetch_by_id") else None
        if not isinstance(raw, dict):
            return {"ok": False, "detail": "not_found"}
    except Exception as e:
        logger.exception("Manual Kitsu fetch failed for media_item=%s kitsu_id=%s: %s", media_item_id, kitsu_id, e)
        return {"ok": False, "detail": "fetch_failed", "error": str(e)}

    try:
        from . import enrichment as enrichment_service
    except Exception as e:
        logger.exception("Manual Kitsu enrichment service unavailable for media_item=%s: %s", media_item_id, e)
        return {"ok": False, "detail": "service_unavailable", "error": str(e)}

    try:
        normalized = enrichment_service._normalize_kitsu(raw) if hasattr(enrichment_service, "_normalize_kitsu") else {}
        if not isinstance(normalized, dict) or not normalized:
            return {"ok": False, "detail": "normalize_failed"}

        title_eff = str(title or normalized.get("title") or "").strip() or None
        overview_eff = str(overview or normalized.get("synopsis") or normalized.get("overview") or "").strip() or None
        genres_eff = genres if isinstance(genres, list) else (normalized.get("genres") if isinstance(normalized.get("genres"), list) else [])
        images = dict(normalized.get("images") or {})
        poster_eff = str(poster_url or images.get("poster") or "").strip() or None
        backdrop_eff = str(backdrop_url or images.get("backdrop") or "").strip() or None
        if poster_eff:
            images["poster"] = poster_eff
        if backdrop_eff:
            images["backdrop"] = backdrop_eff
        normalized["images"] = images
        normalized["provider"] = "kitsu"
        normalized["provider_id"] = str(kitsu_id).strip()
        normalized["kitsu_id"] = str(kitsu_id).strip()
        normalized["media_type"] = str(normalized.get("media_type") or "anime").strip().lower() or "anime"
        if title_eff:
            normalized["title"] = title_eff
            normalized["series_title"] = title_eff
        if overview_eff:
            normalized["synopsis"] = overview_eff
        if genres_eff:
            normalized["genres"] = genres_eff

        mi_any = cast(Any, mi)
        _clear_manual_provider_rows(db, int(media_item_id))
        _reset_media_item_manual_state(mi_any, clear_artwork=True, clear_descriptive=True)
        mi_any.provider = "kitsu"
        mi_any.provider_id = str(kitsu_id).strip()
        try:
            if mi_any.status in ("NO_MATCH", "ERROR", "MANUAL"):
                mi_any.status = None
        except Exception:
            pass
        db.add(mi_any)
        try:
            enrichment_service._attach_episodes("kitsu", normalized, normalized.get("media_type"), mi)
        except Exception:
            pass
        enrichment_service._persist_metadata(db, mi, normalized)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Manual Kitsu mapping persist failed for media_item=%s: %s", media_item_id, e)
        return {"ok": False, "detail": "db_commit_failed", "error": str(e)}

    try:
        out = apply_manual_mapping(
            db=db,
            media_item_id=int(media_item_id),
            tmdb_id=None,
            media_type=normalized.get("media_type"),
            season_number=season_number,
            language=language,
            title=title_eff,
            overview=overview_eff,
            genres=genres_eff,
            poster_url=poster_eff,
            backdrop_url=backdrop_eff,
            season_title=season_title,
            episode_overrides=[],
            download_assets=download_assets,
            translation_only=False,
            reset_provider_state=False,
        )
        if not out.get("ok"):
            return out
        mi_any = cast(Any, mi)
        mi_any.provider = "kitsu"
        mi_any.provider_id = str(kitsu_id).strip()
        mi_any.status = "ENRICHED"
        try:
            mi_any.is_identified = True
        except Exception:
            pass
        db.add(mi_any)
        db.commit()
        return {"ok": True, "provider": "kitsu", "item": out.get("item"), "normalized": out.get("normalized", False)}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Manual Kitsu finalize failed for media_item=%s: %s", media_item_id, e)
        return {"ok": False, "detail": "manual_finalize_failed", "error": str(e)}


def apply_manual_mapping(
    *,
    db,
    media_item_id: int,
    tmdb_id: str | None,
    media_type: str | None,
    season_number: int | None,
    language: str,
    title: str | None,
    overview: str | None,
    genres: list[str] | None,
    poster_url: str | None,
    backdrop_url: str | None,
    season_title: str | None,
    episode_overrides: list[dict[str, Any]] | None,
    download_assets: bool | None = None,
    translation_only: bool | None = None,
    reset_provider_state: bool | None = True,
) -> dict:
    now = datetime.datetime.utcnow()
    translation_only = bool(translation_only)
    incoming_poster_url = poster_url
    incoming_backdrop_url = backdrop_url
    mi = db.query(models.MediaItem).filter(models.MediaItem.id == media_item_id).first()
    if not mi:
        return {"ok": False, "detail": "media_item_not_found"}

    mm = db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id == media_item_id).first()
    if not mm:
        mm = models.ManualMapping(media_item_id=media_item_id)
    # Detect identity changes early so we can clear stale season/episode metadata (best-effort)
    # and force a refresh for the correct TMDB match.
    prev_tmdb_id = None
    prev_media_type = None
    prev_season_number = None
    try:
        prev_tmdb_id = getattr(mm, "tmdb_id", None) or getattr(mi, "tmdb_id", None)
    except Exception:
        prev_tmdb_id = None
    try:
        prev_media_type = getattr(mm, "media_type", None) or getattr(mi, "media_type", None)
    except Exception:
        prev_media_type = None
    try:
        prev_season_number = getattr(mm, "season_number", None)
    except Exception:
        prev_season_number = None
    try:
        if poster_url is None:
            poster_url = getattr(mm, "poster_url", None)
        if backdrop_url is None:
            backdrop_url = getattr(mm, "backdrop_url", None)
    except Exception:
        pass
    mm_any = cast(Any, mm)
    mm_any.tmdb_id = tmdb_id
    mm_any.media_type = media_type
    mm_any.season_number = season_number
    mm_any.poster_url = poster_url
    mm_any.backdrop_url = backdrop_url
    mm_any.updated_at = now
    db.add(mm_any)

    replace_manual_assets = (not translation_only) and (
        incoming_poster_url is not None or incoming_backdrop_url is not None
    )
    replace_poster_asset = bool(incoming_poster_url is not None and not _is_current_manual_asset_url(int(media_item_id), incoming_poster_url))
    replace_backdrop_asset = bool(incoming_backdrop_url is not None and not _is_current_manual_asset_url(int(media_item_id), incoming_backdrop_url))
    if (not translation_only) and bool(reset_provider_state):
        try:
            _clear_manual_provider_rows(db, int(media_item_id))
        except Exception:
            pass
    if replace_manual_assets:
        try:
            _cleanup_manual_asset_dir(
                int(media_item_id),
                remove_poster=replace_poster_asset,
                remove_backdrop=replace_backdrop_asset,
            )
        except Exception:
            pass

    mo = (
        db.query(models.ManualOverride)
        .filter(models.ManualOverride.media_item_id == media_item_id)
        .filter(models.ManualOverride.language == language)
        .first()
    )
    if not mo:
        mo = models.ManualOverride(media_item_id=media_item_id, language=language, source="manual")
    mo_any = cast(Any, mo)
    mo_any.title = title
    mo_any.overview = overview
    mo_any.genres = json.dumps(genres or [], ensure_ascii=False)
    mo_any.episode_overrides = json.dumps(episode_overrides or [], ensure_ascii=False)
    mo_any.updated_at = now
    db.add(mo_any)

    try:
        tr = (
            db.query(models.MediaTranslation)
            .filter(models.MediaTranslation.path_id == media_item_id)
            .filter(models.MediaTranslation.language == language)
            .filter(models.MediaTranslation.source == "manual")
            .first()
        )
        if not tr:
            tr = models.MediaTranslation(path_id=media_item_id, language=language, source="manual")
        tr_any = cast(Any, tr)
        tr_any.title = title
        tr_any.overview = overview
        tr_any.updated_at = now
        db.add(tr_any)
    except Exception:
        pass

    if not translation_only:
        try:
            mi_any = cast(Any, mi)
            if bool(reset_provider_state):
                _reset_media_item_manual_state(
                    mi_any,
                    clear_artwork=replace_manual_assets or bool(tmdb_id),
                    clear_descriptive=bool(tmdb_id or genres),
                )
            if tmdb_id:
                # Movies should be unique by TMDB id; TV series/seasons can share the same TMDB series id.
                mt_l = (media_type or "").strip().lower()
                allow_shared_tmdb_id = (mt_l in ("tv", "series", "show")) or (season_number is not None)
                if not allow_shared_tmdb_id:
                    try:
                        conflict = (
                            db.query(models.MediaItem)
                            .filter(models.MediaItem.tmdb_id == tmdb_id, models.MediaItem.id != media_item_id)
                            .first()
                        )
                        if conflict:
                            logger.info("Conflict found for tmdb_id %s on item %s. Freeing it up.", tmdb_id, conflict.id)
                            conflict.tmdb_id = None
                            db.add(conflict)
                            # No commit here yet; keep it inside the same transaction.
                    except Exception as e:
                        logger.warning("Error checking for tmdb_id conflicts: %s", e)

                mi_any.tmdb_id = tmdb_id
            if media_type:
                mi_any.media_type = media_type
            if title:
                mi_any.title_localized = title
            if overview:
                mi_any.synopsis_localized = overview
            if poster_url:
                mi_any.poster_path = poster_url
            if backdrop_url:
                mi_any.backdrop_path = backdrop_url
            if genres:
                mi_any.genres = ",".join(genres)
            try:
                mi_any.is_identified = True
            except Exception:
                pass
            try:
                mi_any.status = "MANUAL"
            except Exception:
                pass
            db.add(mi_any)
        except Exception:
            pass

    season_id = None
    identity_changed = False
    try:
        if tmdb_id and str(tmdb_id).strip() and str(prev_tmdb_id or "").strip():
            identity_changed = str(tmdb_id).strip() != str(prev_tmdb_id).strip()
        elif tmdb_id and str(tmdb_id).strip() and not str(prev_tmdb_id or "").strip():
            identity_changed = True
    except Exception:
        identity_changed = False
    try:
        if media_type and str(media_type).strip() and str(prev_media_type or "").strip():
            identity_changed = identity_changed or (str(media_type).strip() != str(prev_media_type).strip())
    except Exception:
        pass
    try:
        if season_number is not None and prev_season_number is not None:
            identity_changed = identity_changed or (int(season_number) != int(prev_season_number))
    except Exception:
        pass

    if season_number is not None and not translation_only:
        try:
            si = db.query(models.SeasonItem).filter(models.SeasonItem.media_item_id == media_item_id).first()
            if si:
                season_id = si.season_id
            if season_id:
                s = db.query(models.Season).filter(models.Season.id == season_id).first()
                if s:
                    s_any = cast(Any, s)
                    s_any.season_number = season_number
                    if season_title:
                        if language.lower().startswith("en") and not s_any.title_en:
                            s_any.title_en = season_title
                        s_any.title_localized = season_title
                    if poster_url:
                        s_any.poster_path = poster_url
                    if backdrop_url:
                        s_any.backdrop_path = backdrop_url
                    db.add(s_any)
        except Exception:
            season_id = None

    # If identity changed (wrong TMDB match before), clear stale season/episode derived fields so a backfill
    # can repopulate correct posters/synopsis/episode titles. Avoid clearing when explicit episode_overrides
    # are provided (user is intentionally overriding).
    if season_id and identity_changed and not translation_only:
        try:
            s = db.query(models.Season).filter(models.Season.id == season_id).first()
            if s:
                s_any = cast(Any, s)
                try:
                    s_any.synopsis_localized = None
                except Exception:
                    pass
                try:
                    s_any.poster_path = None
                except Exception:
                    pass
                try:
                    s_any.backdrop_path = None
                except Exception:
                    pass
                db.add(s_any)
        except Exception:
            pass

        if not episode_overrides:
            try:
                eps_only = db.query(models.Episode).filter(models.Episode.season_id == season_id).all()
                for ep in eps_only:
                    ep_any = cast(Any, ep)
                    try:
                        ep_any.title_localized = None
                    except Exception:
                        pass
                    try:
                        ep_any.synopsis_localized = None
                    except Exception:
                        pass
                    # Force TMDB backfill to refresh EN titles too (it only updates when titles are generic).
                    try:
                        ep_any.title_en = None
                    except Exception:
                        pass
                    try:
                        ep_any.still_path = None
                    except Exception:
                        pass
                    db.add(ep_any)
            except Exception:
                pass

    if season_id and episode_overrides and not translation_only:
        try:
            overrides_list = [e for e in episode_overrides if isinstance(e, dict)]

            eps = (
                db.query(models.Episode, models.FileRecord)
                .join(models.EpisodeFile, models.EpisodeFile.episode_id == models.Episode.id)
                .join(models.FileRecord, models.FileRecord.id == models.EpisodeFile.file_record_id)
                .filter(models.Episode.season_id == season_id)
                .order_by(models.Episode.episode_number.asc(), models.Episode.id.asc())
                .all()
            )

            def _norm_title(v: str | None) -> str:
                try:
                    return "".join(ch.lower() for ch in str(v or "").strip() if ch.isalnum())
                except Exception:
                    return ""

            ep_by_num: dict[int, tuple[Any, Any]] = {}
            ep_by_title: dict[str, list[tuple[Any, Any]]] = {}
            for ep, fr in eps:
                try:
                    n = getattr(ep, "episode_number", None)
                    if n is not None:
                        ep_by_num[int(n)] = (ep, fr)
                except Exception:
                    pass
                try:
                    title_best = getattr(ep, "title_localized", None) or getattr(ep, "title_en", None) or getattr(ep, "title", None)
                except Exception:
                    title_best = getattr(ep, "title", None)
                nt = _norm_title(title_best)
                if nt:
                    ep_by_title.setdefault(nt, []).append((ep, fr))

            used_ids: set[int] = set()
            updated_any = False
            for idx, ov in enumerate(overrides_list):
                original_num = ov.get("original_episode_number")
                target: tuple[Any, Any] | None = None
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
                                if int(getattr(cand[0], "id", 0)) in used_ids:
                                    continue
                            except Exception:
                                pass
                            target = cand
                            break
                if not target:
                    try:
                        target = ep_by_num.get(int(ov.get("episode_number")))
                    except Exception:
                        target = None
                if not target:
                    continue

                ep, fr = target
                try:
                    used_ids.add(int(getattr(ep, "id", 0)))
                except Exception:
                    pass

                try:
                    new_num = int(ov.get("episode_number")) if ov.get("episode_number") is not None else idx + 1
                except Exception:
                    new_num = idx + 1
                t = ov.get("title")
                ov_text = ov.get("overview")
                ep_any = cast(Any, ep)
                if t:
                    ep_any.title_localized = t
                if ov_text:
                    ep_any.synopsis_localized = ov_text
                if new_num:
                    ep_any.episode_number = new_num
                db.add(ep_any)
                try:
                    fr_any = cast(Any, fr)
                    if new_num:
                        fr_any.file_index = new_num
                        db.add(fr_any)
                except Exception:
                    pass
                updated_any = True

            if updated_any:
                for ep, fr in eps:
                    try:
                        if int(getattr(ep, "id", 0)) in used_ids:
                            continue
                    except Exception:
                        pass
                    try:
                        old_num = getattr(ep, "episode_number", None)
                        new_hidden = 10000 + (int(old_num) if old_num is not None else 0)
                        ep_any = cast(Any, ep)
                        ep_any.episode_number = new_hidden
                        db.add(ep_any)
                        fr_any = cast(Any, fr)
                        fr_any.file_index = new_hidden
                        db.add(fr_any)
                    except Exception:
                        pass

            if not updated_any:
                by_num = {}
                for e in overrides_list:
                    try:
                        n_raw = e.get("episode_number")
                        if n_raw is None:
                            continue
                        n = int(n_raw)
                    except Exception:
                        continue
                    by_num[n] = e
                eps_only = db.query(models.Episode).filter(models.Episode.season_id == season_id).all()
                for ep in eps_only:
                    n = getattr(ep, "episode_number", None)
                    if n is None:
                        continue
                    data = by_num.get(int(n))
                    if not data:
                        continue
                    t = data.get("title")
                    ov_text = data.get("overview")
                    ep_any = cast(Any, ep)
                    if t:
                        ep_any.title_localized = t
                    if ov_text:
                        ep_any.synopsis_localized = ov_text
                    db.add(ep_any)
        except Exception:
            pass

    # Commit the mapping/override/episode changes first (fast, DB-only).
    # Downloading assets can be slow; doing it *inside* the same transaction increases the chance
    # of sqlite lock contention and leads to user-visible `db_commit_failed`.
    try:
        # Even with busy_timeout/WAL, concurrent writers (scan/enrichment) can briefly lock SQLite.
        # Retry small commits a few times to avoid user-visible failures from transient locks.
        last_err: str | None = None
        for attempt in range(6):
            try:
                db.commit()
                last_err = None
                break
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                try:
                    last_err = str(e)
                except Exception:
                    last_err = "unknown"
                msg_l = (last_err or "").lower()
                if ("locked" in msg_l) or ("busy" in msg_l):
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise
        if last_err:
            raise RuntimeError(last_err)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            msg = str(e)
        except Exception:
            msg = "unknown"
        logger.exception("Manual mapping commit failed for media_item=%s: %s", media_item_id, msg)
        return {"ok": False, "detail": "db_commit_failed", "error": msg}

    normalized = False
    if (not translation_only) and (media_type in ("tv", "series") or season_number is not None):
        try:
            normalized = _normalize_episodes_for_item(media_item_id)
        except Exception:
            normalized = False

    # Manual mapping owns explicit artwork choices. Always replace local poster/backdrop
    # files for selected remote images so stale cached assets cannot survive a correction.
    if replace_manual_assets and not translation_only:
        try:
            base_dir = _cleanup_manual_asset_dir(
                int(media_item_id),
                remove_poster=replace_poster_asset,
                remove_backdrop=replace_backdrop_asset,
            )
            web_base = f"/media-assets/{media_item_id}"
            local_poster = _download_to(incoming_poster_url, base_dir / "poster.jpg") if _is_remote_asset_url(incoming_poster_url) else None
            local_backdrop = _download_to(incoming_backdrop_url, base_dir / "backdrop.jpg") if _is_remote_asset_url(incoming_backdrop_url) else None

            desired_poster = f"{web_base}/{local_poster.name}" if local_poster else (incoming_poster_url if incoming_poster_url and not replace_poster_asset else None)
            desired_backdrop = f"{web_base}/{local_backdrop.name}" if local_backdrop else (incoming_backdrop_url if incoming_backdrop_url and not replace_backdrop_asset else None)
            any_updated = bool(desired_poster or desired_backdrop)
            if any_updated:
                # Very small commit, but sqlite can still be busy/locked. Retry a few times.
                for attempt in range(3):
                    try:
                        if desired_poster:
                            try:
                                mm_any.poster_url = desired_poster
                            except Exception:
                                pass
                            try:
                                mi_any = cast(Any, mi)
                                mi_any.poster_path = desired_poster
                                db.add(mi_any)
                            except Exception:
                                pass
                            try:
                                if season_id:
                                    s = db.query(models.Season).filter(models.Season.id == season_id).first()
                                    if s:
                                        cast(Any, s).poster_path = desired_poster
                                        db.add(s)
                            except Exception:
                                pass
                        if desired_backdrop:
                            try:
                                mm_any.backdrop_url = desired_backdrop
                            except Exception:
                                pass
                            try:
                                mi_any = cast(Any, mi)
                                mi_any.backdrop_path = desired_backdrop
                                db.add(mi_any)
                            except Exception:
                                pass
                            try:
                                if season_id:
                                    s = db.query(models.Season).filter(models.Season.id == season_id).first()
                                    if s:
                                        cast(Any, s).backdrop_path = desired_backdrop
                                        db.add(s)
                            except Exception:
                                pass
                        try:
                            db.add(mm_any)
                        except Exception:
                            pass
                        db.commit()
                        break
                    except Exception as e:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        msg = str(e)
                        if "locked" in msg.lower() or "busy" in msg.lower():
                            time.sleep(0.2 * (attempt + 1))
                            continue
                        raise
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.exception("Failed downloading/applying assets for media_item=%s: %s", media_item_id, e)

    # Best-effort: for TMDB TV seasons, immediately backfill season synopsis + episodes titles/overviews/stills.
    # Manual mapping marks items as MANUAL and the background enrichment runner skips those, so without this
    # the season can remain stale/wrong until the user forces a re-enrich.
    if season_id and tmdb_id and (not translation_only) and (media_type in ("tv", "series") or season_number is not None):
        try:
            from . import enrichment as enrichment_service
            enrichment_service.backfill_tmdb_episode_titles_for_media_item(int(media_item_id), manage_state=False)
        except Exception:
            pass

    try:
        poster_out = None
        backdrop_out = None
        try:
            poster_out = getattr(mi, "poster_path", None)
            backdrop_out = getattr(mi, "backdrop_path", None)
        except Exception:
            poster_out = None
            backdrop_out = None
        if not poster_out:
            try:
                poster_out = getattr(mm, "poster_url", None)
            except Exception:
                poster_out = None
        if not backdrop_out:
            try:
                backdrop_out = getattr(mm, "backdrop_url", None)
            except Exception:
                backdrop_out = None
        return {
            "ok": True,
            "item": {
                "id": media_item_id,
                "poster_path": poster_out,
                "backdrop_path": backdrop_out,
            },
            "normalized": bool(normalized),
        }
    except Exception:
        return {"ok": True, "normalized": bool(normalized)}
