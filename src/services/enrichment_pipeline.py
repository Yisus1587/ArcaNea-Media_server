from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from ..core.config import get
from ..db import SessionLocal
from ..db.models import FileRecord, MediaItem
from . import enrichment
from .enrich_state import clear_state, set_state

logger = logging.getLogger(__name__)

try:
    _FETCH_WORKERS = max(1, int(str(os.environ.get("ARCANEA_ENRICH_FETCH_WORKERS") or "4")))
except Exception:
    _FETCH_WORKERS = 4


def _provider_override_list(providers_override: Iterable[str] | None) -> list[str]:
    try:
        return [p.strip().lower() for p in (providers_override or []) if isinstance(p, str) and p.strip()]
    except Exception:
        return []


def _is_anime_context(mi: MediaItem, media_type: str | None, root_type: str | None) -> bool:
    try:
        if str(root_type or "").strip().lower() == "anime":
            return True
    except Exception:
        pass
    try:
        if str(getattr(mi, "library_type", "") or "").strip().lower() == "anime":
            return True
    except Exception:
        pass
    try:
        if bool(getattr(mi, "mal_id", None)):
            return True
    except Exception:
        pass
    try:
        return str(media_type or "").strip().lower() == "anime"
    except Exception:
        return False


def _fetch_provider_result(
    *,
    session,
    mi: MediaItem,
    provider: str,
    candidate_titles: list[str],
    media_type: str,
    tmdb_media_type: str,
    is_anime_context: bool,
) -> tuple[dict | None, bool]:
    """Return (normalized_payload, provider_error). This function must not write DB."""
    provider_error = False
    res = None
    if provider == "jikan" and enrichment.jikan is not None:
        if not is_anime_context:
            return None, False
        try:
            if getattr(mi, "mal_id", None) and hasattr(enrichment.jikan, "fetch_by_id"):
                res = enrichment.jikan.fetch_by_id(str(getattr(mi, "mal_id")))
        except Exception:
            provider_error = True
            res = None
        if not res:
            for cand in candidate_titles[:2]:
                try:
                    r = enrichment.jikan.search(cand) if hasattr(enrichment.jikan, "search") else None
                except Exception:
                    provider_error = True
                    break
                if isinstance(r, list) and r:
                    res = r[0]
                    break
                if r and not isinstance(r, list):
                    res = r
                    break
    elif provider == "kitsu" and enrichment.kitsu is not None:
        if not is_anime_context:
            return None, False
        try:
            min_conf = float(os.environ.get("ARCANEA_KITSU_MIN_CONF", "0.92") or 0.92)
        except Exception:
            min_conf = 0.92
        try:
            prov0 = str(getattr(mi, "provider", "") or "").strip().lower()
            pid0 = str(getattr(mi, "provider_id", "") or "").strip()
        except Exception:
            prov0 = ""
            pid0 = ""
        if prov0 == "kitsu" and pid0 and hasattr(enrichment.kitsu, "fetch_by_id"):
            try:
                d = enrichment.kitsu.fetch_by_id(pid0)
                if d and enrichment._kitsu_match_allowed_for_item(d, mi):
                    res = d
            except Exception:
                provider_error = True
                res = None
        if not res and getattr(mi, "mal_id", None) and hasattr(enrichment.kitsu, "fetch_by_mal_id"):
            try:
                # Exact sidecar/provider identity beats title numerology.
                res = enrichment.kitsu.fetch_by_mal_id(str(getattr(mi, "mal_id")))
            except Exception:
                provider_error = True
                res = None
        if not res:
            for cand in candidate_titles:
                try:
                    if hasattr(enrichment.kitsu, "best_match"):
                        r = enrichment.kitsu.best_match(cand, min_conf=min_conf)
                    else:
                        rows = enrichment.kitsu.search(cand) if hasattr(enrichment.kitsu, "search") else None
                        r = rows[0] if isinstance(rows, list) and rows else None
                except Exception:
                    provider_error = True
                    r = None
                if r and enrichment._kitsu_match_allowed_for_item(r, mi):
                    res = r
                    break
    elif provider == "tmdb" and enrichment.tmdb is not None:
        try:
            for cand in candidate_titles:
                try:
                    r = enrichment._tmdb_search_with_optional_season(cand, tmdb_media_type)
                except Exception:
                    provider_error = True
                    r = None
                if isinstance(r, list) and r:
                    res = r[0]
                    break
                if r and not isinstance(r, list):
                    if not enrichment._tmdb_match_allowed_for_item(r, mi, cand):
                        continue
                    res = r
                    break
        except Exception:
            provider_error = True

    if not res:
        return None, provider_error
    try:
        normalized = enrichment._normalize_result(provider, res)
    except Exception:
        logger.exception("Failed normalizing provider=%s media_item=%s", provider, getattr(mi, "id", None))
        return None, True
    return (normalized if normalized else None), provider_error


def download_metadata_payload(
    media_item_id: int,
    *,
    providers_override: Iterable[str] | None = None,
    force_media_type: str | None = None,
) -> dict:
    """Fetch and normalize provider metadata without persisting it."""
    session = SessionLocal()
    try:
        mi = session.query(MediaItem).filter(MediaItem.id == int(media_item_id)).first()
        if not mi:
            return {"ok": False, "status": "missing", "media_item_id": int(media_item_id)}
        try:
            set_state(
                {
                    "running": True,
                    "current_id": getattr(mi, "id", None),
                    "current_title": getattr(mi, "title", None),
                    "current_step": "download_metadata",
                }
            )
        except Exception:
            pass

        if force_media_type:
            media_type = str(force_media_type)
        else:
            inferred = enrichment._decide_media_type(mi, session) or "movie"
            cur_mt = str(getattr(mi, "media_type", "") or "").strip().lower()
            media_type = "series" if inferred == "series" and cur_mt not in ("series", "tv", "show") else (getattr(mi, "media_type", None) or inferred)

        candidate_titles = enrichment._candidate_titles_for_item(mi)
        root_type = enrichment._root_type_for_media_item(session, mi)
        providers_override_norm = _provider_override_list(providers_override)
        strict_override = bool(providers_override_norm)
        cfg_movies = get("metadata_movies_provider") or "tmdb"
        cfg_anime = get("metadata_anime_provider") or "kitsu"
        if providers_override_norm:
            providers_order = providers_override_norm
        else:
            providers_order = enrichment._provider_order_for_item(
                media_type,
                getattr(mi, "mal_id", None),
                cfg_movies,
                cfg_anime,
                getattr(mi, "media_root", None),
                root_type,
            )
        root_allows_tmdb = enrichment._tmdb_allowed_for_root_type(root_type)
        manual_tmdb_override = bool(strict_override and providers_order == ["tmdb"])
        tmdb_allowed = bool((root_allows_tmdb or manual_tmdb_override) and ((not strict_override) or ("tmdb" in providers_order)))
        anime_context = _is_anime_context(mi, media_type, root_type)
        if strict_override and any(p in ("jikan", "kitsu") for p in providers_order):
            anime_context = True
        has_mal_identity = bool(str(getattr(mi, "mal_id", "") or "").strip())
        force_tmdb_identity = bool(manual_tmdb_override)
        prefer_mal_identity = bool(has_mal_identity and not force_tmdb_identity)

        try:
            tmdb_ok = enrichment._tmdb_configured()
        except Exception:
            tmdb_ok = False
        if "tmdb" in providers_order and (not tmdb_allowed or not tmdb_ok):
            providers_order = [p for p in providers_order if p != "tmdb"]
        if not providers_order:
            return {"ok": True, "status": "no_match", "media_item_id": int(media_item_id), "media_type": media_type}

        if (not prefer_mal_identity) and tmdb_allowed and enrichment.tmdb is not None and hasattr(enrichment.tmdb, "tmdb_fetch_season"):
            try:
                inherited = enrichment._inherit_tmdb_identity_from_parent_if_season(mi, session)
            except Exception:
                inherited = None
            if inherited:
                parent_tmdb_id, season_number = inherited
                try:
                    lang = str(get("metadata_language") or "").strip() or "en-US"
                except Exception:
                    lang = "en-US"
                try:
                    normalized = enrichment.tmdb.tmdb_fetch_season(str(parent_tmdb_id), season_number=int(season_number), language=lang)
                except Exception:
                    normalized = None
                if isinstance(normalized, dict) and normalized:
                    normalized.setdefault("provider", "tmdb")
                    normalized.setdefault("tmdb_id", str(parent_tmdb_id))
                    normalized.setdefault("provider_id", str(parent_tmdb_id))
                    normalized.setdefault("season_number", int(season_number))
                    enrichment._attach_tmdb_i18n(normalized, "series")
                    enrichment._attach_episodes("tmdb", normalized, "series", mi)
                    return {
                        "ok": True,
                        "status": "matched",
                        "media_item_id": int(media_item_id),
                        "media_type": "series",
                        "normalized": normalized,
                    }

        if (not prefer_mal_identity) and tmdb_allowed and tmdb_ok and enrichment.tmdb is not None:
            try:
                mt0 = str(getattr(mi, "media_type", None) or media_type or "").strip().lower()
                tmdb_id0 = str(getattr(mi, "tmdb_id", None) or "").strip() or None
            except Exception:
                mt0 = str(media_type or "").strip().lower()
                tmdb_id0 = None
            explicit_tmdb_ids: list[str] = []
            try:
                for cand in candidate_titles:
                    m = re.search(r"themoviedb\.org/(?:movie|tv)/(\d+)", str(cand or ""), flags=re.IGNORECASE)
                    if m and str(m.group(1)) not in explicit_tmdb_ids:
                        explicit_tmdb_ids.append(str(m.group(1)))
            except Exception:
                explicit_tmdb_ids = []
            prefer_explicit_tmdb_identity = bool(explicit_tmdb_ids and (not tmdb_id0 or str(tmdb_id0) != explicit_tmdb_ids[0]))
            if tmdb_id0 and not prefer_explicit_tmdb_identity:
                try:
                    lang0 = str(get("metadata_language") or "").strip() or "en-US"
                except Exception:
                    lang0 = "en-US"
                normalized = None
                if mt0 in ("series", "tv", "show") and hasattr(enrichment.tmdb, "tmdb_fetch_season"):
                    try:
                        _series0, season0 = enrichment.get_series_and_season_for_item(mi, session)
                        sn0 = getattr(season0, "season_number", None) if season0 is not None else None
                        sn_int = int(sn0) if sn0 is not None else None
                    except Exception:
                        sn_int = None
                    if sn_int is not None:
                        try:
                            normalized = enrichment.tmdb.tmdb_fetch_season(str(tmdb_id0), season_number=int(sn_int), language=lang0)
                        except Exception:
                            normalized = None
                        if isinstance(normalized, dict) and normalized:
                            normalized.setdefault("season_number", int(sn_int))
                if not isinstance(normalized, dict) or not normalized:
                    if hasattr(enrichment.tmdb, "tmdb_fetch_by_id"):
                        try:
                            media_pref0 = "tv" if mt0 in ("series", "tv", "show", "anime") else "movie"
                            normalized = enrichment.tmdb.tmdb_fetch_by_id(str(tmdb_id0), media_type=media_pref0, language=lang0)
                        except Exception:
                            normalized = None
                if isinstance(normalized, dict) and normalized and enrichment._tmdb_match_allowed_for_item(normalized, mi):
                    normalized.setdefault("provider", "tmdb")
                    normalized.setdefault("tmdb_id", str(tmdb_id0))
                    normalized.setdefault("provider_id", str(tmdb_id0))
                    enrichment._attach_tmdb_i18n(normalized, str(media_type))
                    enrichment._attach_episodes("tmdb", normalized, str(media_type), mi)
                    return {
                        "ok": True,
                        "status": "matched",
                        "media_item_id": int(media_item_id),
                        "media_type": media_type,
                        "normalized": normalized,
                    }

        provider_error_any = False
        normalized: dict | None = None
        tmdb_media_type = enrichment._tmdb_search_media_type_for_item(mi, media_type) or media_type
        for provider in providers_order:
            try:
                set_state({"current_step": f"download:{provider}"})
            except Exception:
                pass
            norm, provider_error = _fetch_provider_result(
                session=session,
                mi=mi,
                provider=provider,
                candidate_titles=candidate_titles,
                media_type=str(media_type),
                tmdb_media_type=str(tmdb_media_type),
                is_anime_context=anime_context,
            )
            provider_error_any = provider_error_any or provider_error
            if norm:
                normalized = norm
                break

        if tmdb_allowed and normalized and str(normalized.get("provider") or "").strip().lower() == "jikan":
            try:
                existing_tid = getattr(mi, "tmdb_id", None)
                if existing_tid:
                    normalized["tmdb_id"] = str(existing_tid)
                elif tmdb_ok and enrichment.tmdb is not None:
                    anchors: list[str] = []
                    for key in ("title_english", "title"):
                        v = normalized.get(key)
                        if isinstance(v, str) and v.strip() and v.strip() not in anchors:
                            anchors.append(v.strip())
                    syns = normalized.get("title_synonyms") or []
                    if isinstance(syns, list):
                        for s in syns:
                            if isinstance(s, str) and s.strip() and s.strip() not in anchors:
                                anchors.append(s.strip())
                    mt = str(normalized.get("media_type") or media_type or "").strip().lower()
                    tmdb_type = "tv" if mt in ("series", "tv", "show", "anime") else "movie"
                    for cand in anchors[:4]:
                        try:
                            r = enrichment._tmdb_search_with_optional_season(cand, tmdb_type)
                            if isinstance(r, list) and r:
                                r = r[0]
                            norm_t = enrichment._normalize_result("tmdb", r) if isinstance(r, dict) and r else None
                        except Exception:
                            norm_t = None
                        if norm_t:
                            tid = norm_t.get("tmdb_id") or norm_t.get("provider_id")
                            if tid:
                                normalized["tmdb_id"] = tid
                            normalized["tmdb_match"] = norm_t
                            break
            except Exception:
                pass

        if not normalized and tmdb_allowed and tmdb_ok and ("tmdb" not in providers_order) and ("jikan" in providers_order):
            try:
                is_series = str(media_type or "").strip().lower() in ("series", "tv", "anime")
                if not is_series:
                    is_series = session.query(FileRecord).filter(FileRecord.media_item_id == int(media_item_id)).count() > 1
                if is_series:
                    cand0 = str(candidate_titles[0] if candidate_titles else getattr(mi, "title", "") or "").strip()
                    if cand0:
                        r = enrichment._tmdb_search_with_optional_season(cand0, "tv")
                        if isinstance(r, list) and r:
                            r = r[0]
                        if isinstance(r, dict) and r:
                            normalized = enrichment._normalize_result("tmdb", r)
            except Exception:
                pass

        if not normalized:
            return {
                "ok": not provider_error_any,
                "status": "provider_error" if provider_error_any else "no_match",
                "media_item_id": int(media_item_id),
                "media_type": media_type,
            }

        try:
            enrichment._attach_tmdb_i18n(normalized, str(media_type))
            enrichment._attach_episodes(str(normalized.get("provider") or ""), normalized, str(media_type), mi)
            normalized = enrichment._supplement_jikan_with_kitsu(mi, normalized, str(media_type))
            if str(normalized.get("provider") or "").strip().lower() == "jikan" and str(getattr(mi, "mal_id", "") or "").strip() == str(normalized.get("provider_id") or normalized.get("mal_id") or "").strip() and not normalized.get("_episodes_from") and isinstance(normalized.get("episodes"), list) and normalized.get("episodes"):
                normalized["_episodes_from"] = "jikan_id"
        except Exception:
            pass
        return {
            "ok": True,
            "status": "matched",
            "media_item_id": int(media_item_id),
            "media_type": media_type,
            "normalized": normalized,
        }
    except Exception as exc:
        logger.exception("metadata download failed for media_item=%s", media_item_id)
        return {"ok": False, "status": "error", "media_item_id": int(media_item_id), "detail": str(exc)}
    finally:
        session.close()


def persist_metadata_payload(payload: dict) -> bool:
    """Validate and persist a downloaded metadata payload in a short DB session."""
    media_item_id = payload.get("media_item_id")
    if media_item_id is None:
        return False
    session = SessionLocal()
    try:
        mi = session.query(MediaItem).filter(MediaItem.id == int(media_item_id)).first()
        if not mi:
            return False
        media_type = payload.get("media_type")
        if isinstance(media_type, str) and media_type.strip():
            try:
                mi.media_type = media_type.strip()
            except Exception:
                pass
        status = str(payload.get("status") or "").strip().lower()
        if status == "no_match":
            try:
                mi.title = enrichment._fallback_title_for_item(mi)
            except Exception:
                pass
            mi.status = "NO_MATCH"
            try:
                mi.is_identified = False
            except Exception:
                pass
            session.add(mi)
            session.commit()
            return True
        if status != "matched":
            return False
        normalized = payload.get("normalized")
        if not isinstance(normalized, dict) or not normalized:
            return False
        try:
            # Movie roots can deliberately use TMDB TV identities for one-file
            # film-like releases (for example Halo web films) without turning
            # the local item into a series.
            if (
                str(getattr(mi, "library_type", "") or "").strip().lower() == "movie"
                and isinstance(media_type, str)
                and media_type.strip().lower() in ("movie", "film")
                and str(normalized.get("media_type") or "").strip().lower() in ("tv", "series", "show")
            ):
                normalized = dict(normalized)
                normalized["media_type"] = "movie"
        except Exception:
            pass
        try:
            set_state({"running": True, "current_id": int(media_item_id), "current_title": getattr(mi, "title", None), "current_step": "persist_metadata"})
        except Exception:
            pass
        enrichment._persist_metadata(session, mi, normalized)
        try:
            enrichment._map_episode_files_for_item(mi, session)
        except Exception:
            pass
        try:
            if enrichment._apply_jikan_episode_titles(mi, normalized, session):
                session.commit()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
        return True
    except Exception:
        logger.exception("metadata persist failed for media_item=%s", media_item_id)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()
        try:
            clear_state()
        except Exception:
            pass


def enrich_one_decoupled(
    media_item_id: int,
    *,
    providers_override: Iterable[str] | None = None,
    force_media_type: str | None = None,
) -> bool:
    payload = download_metadata_payload(
        int(media_item_id),
        providers_override=providers_override,
        force_media_type=force_media_type,
    )
    return persist_metadata_payload(payload)


def enrich_items_decoupled(
    media_item_ids: Iterable[int],
    *,
    providers_override: Iterable[str] | None = None,
    force_media_type: str | None = None,
    max_workers: int | None = None,
) -> dict:
    ids = [int(mid) for mid in media_item_ids if mid is not None]
    if not ids:
        return {"ok": True, "processed": 0, "downloaded": 0, "persisted": 0, "failed": 0}
    workers = max(1, int(max_workers or _FETCH_WORKERS))
    downloaded = 0
    persisted = 0
    failed = 0
    started = time.perf_counter()
    payloads: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich-fetch") as executor:
        futures = {
            executor.submit(
                download_metadata_payload,
                mid,
                providers_override=providers_override,
                force_media_type=force_media_type,
            ): mid
            for mid in ids
        }
        for future in as_completed(futures):
            mid = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                logger.exception("metadata download future failed for media_item=%s", mid)
                payload = {"ok": False, "status": "error", "media_item_id": mid, "detail": str(exc)}
            payloads.append(payload)
            if payload.get("ok") or payload.get("status") in ("matched", "no_match"):
                downloaded += 1
            else:
                failed += 1

    # SQLite writes stay serialized. This avoids lock churn while network fetches remain parallel.
    for payload in sorted(payloads, key=lambda p: int(p.get("media_item_id") or 0)):
        if persist_metadata_payload(payload):
            persisted += 1
        elif payload.get("status") != "provider_error":
            failed += 1

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": failed == 0,
        "processed": len(ids),
        "downloaded": downloaded,
        "persisted": persisted,
        "failed": failed,
        "elapsed_ms": elapsed_ms,
        "workers": workers,
    }
