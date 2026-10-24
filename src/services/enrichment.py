"""Enrichment service: fetch and persist metadata from providers.

This module is the project's enrichment logic adapted from the provided
`enrichment.py` attachment. It locates pending `MediaItem` rows without
metadata, calls providers (Jikan/TMDB) to obtain metadata, normalizes the
responses and persists them into `media_metadata` and optional `media_image`.

Key public functions:
- `enrich_pending(limit=None) -> int` : process up to `limit` pending items.
- `enrich_one(media_item_id, providers_override=None) -> bool` : enrich a single item.
- `enrich_pending_detailed(limit=None, dry_run=False) -> dict` : detailed report.
"""
from typing import Optional, Iterable, Any
import json
import logging
import time
from datetime import datetime
import hashlib
import os
import re
import threading
from pathlib import Path, PurePath
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from . import telemetry
from .db_commit import commit_and_report
from ..db.models import (
    MediaItem,
    FileRecord,
    MediaMetadata,
    MediaImage,
    MediaRelation,
    MediaTranslation,
    ManualMapping,
    ManualOverride,
    UserList,
    PlayHistory,
    MediaRoot,
    Series,
    Season,
    Episode,
    SeasonItem,
    EpisodeFile,
)
from typing import Any as _Any, Optional as _Optional, cast as _cast
try:
    from ..services.classifier_enhanced import classify_metadata
except Exception:
    def classify_metadata(normalized: dict) -> _Optional[str]:
        return None
from .enrich_state import set_state, clear_state
from ..core.config import get, normalize_metadata_language
from ..core.translations import preferred_lang_chain_from_config
from ..utils.utils import map_tmdb_genres

# Providers: prefer package-relative imports, but fall back to absolute `src.*` when running in odd contexts.
try:
    from ..providers import provider_jikan as jikan
except Exception:
    try:
        from src.providers import provider_jikan as jikan  # type: ignore
    except Exception:
        jikan = None
try:
    # dynamic import; static analyzer may not know the symbol exists
    from ..providers import provider_tmdb as tmdb  # type: ignore
except Exception:
    try:
        from src.providers import provider_tmdb as tmdb  # type: ignore
    except Exception:
        tmdb = None
try:
    from ..providers import provider_kitsu as kitsu  # type: ignore
except Exception:
    try:
        from src.providers import provider_kitsu as kitsu  # type: ignore
    except Exception:
        kitsu = None

logger = logging.getLogger(__name__)



def get_series_and_season_for_item(mi_obj: MediaItem, session):
    try:
        si = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi_obj.id).first()
    except Exception:
        si = None
    if not si:
        return None, None
    try:
        season = session.query(Season).filter(Season.id == si.season_id).first()
    except Exception:
        season = None
    if not season:
        return None, None
    try:
        series = session.query(Series).filter(Series.id == season.series_id).first()
    except Exception:
        series = None
    return series, season


def sync_series_season_fields(mi_obj: MediaItem, session):
    series, season = get_series_and_season_for_item(mi_obj, session)
    if not series:
        return
    # Jikan anchoring: group by MAL id and force Season 0 for OVA/ONA/Special.
    try:
        anchor = getattr(mi_obj, "jikan_anchor_mal_id", None) or getattr(mi_obj, "mal_id", None)
    except Exception:
        anchor = None
    try:
        jtype = getattr(mi_obj, "jikan_type", None)
    except Exception:
        jtype = None
    try:
        is_anime_item = False
        try:
            is_anime_item = str(getattr(mi_obj, "library_type", "") or "").strip().lower() == "anime"
        except Exception:
            is_anime_item = False
        if anchor and not is_anime_item:
            existing = (
                session.query(Series)
                .filter(Series.mal_id == str(anchor))
                .order_by(Series.id.asc())
                .first()
            )
            if existing and getattr(existing, "id", None) != getattr(series, "id", None):
                try:
                    if season and getattr(season, "series_id", None) != getattr(existing, "id", None):
                        season.series_id = existing.id
                        session.add(season)
                except Exception:
                    pass
                series = existing
            try:
                series.mal_id = str(anchor)
            except Exception:
                pass
        if jtype and (not getattr(series, "jikan_type", None)):
            try:
                series.jikan_type = str(jtype)
            except Exception:
                pass
        if season and isinstance(jtype, str) and jtype.strip().upper() != "TV":
            try:
                season.season_number = 0
            except Exception:
                pass
    except Exception:
        pass
    # Keep `title` as best-display for back-compat; store anchor/localized in dedicated columns.
    # Important: avoid overwriting series titles with season-specific titles ("Show - Season 2").
    # We only promote titles from Season 1 (or when series fields are empty).
    season_num = None
    try:
        season_num = getattr(season, "season_number", None) if season else None
    except Exception:
        season_num = None

    # If enrichment resolves a later season after the scanner already created the bridge,
    # keep the normalized series/season tables in sync. This fixes folders like
    # "The Angel Next Door Spoils Me Rotten 2" that were initially attached as Season 1.
    try:
        local_hint = _season_hint_for_media_item(mi_obj)
    except Exception:
        local_hint = None
    try:
        local_hint_int = int(local_hint) if local_hint is not None else None
    except Exception:
        local_hint_int = None
    if season is not None and local_hint_int is not None and local_hint_int > 1:
        try:
            if int(getattr(season, "season_number", 0) or 0) != local_hint_int:
                season.season_number = local_hint_int
                session.add(season)
                season_num = local_hint_int
        except Exception:
            pass

        def _series_title_key(value: str | None) -> str:
            try:
                base, _sn = _extract_season_from_title(value)
                text = (base or value or "").strip().lower()
            except Exception:
                text = str(value or "").strip().lower()
            if not text:
                return ""
            text = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", text)
            text = re.sub(r"[^a-z0-9]+", " ", text)
            return re.sub(r"\s+", " ", text).strip()

        def _local_base_key() -> str:
            values: list[str] = []
            for attr in ("title_en", "title", "base_title", "canonical_path", "search_titles"):
                try:
                    raw = getattr(mi_obj, attr, None)
                except Exception:
                    raw = None
                if not raw:
                    continue
                if attr == "canonical_path":
                    try:
                        values.append(os.path.basename(str(raw)).strip())
                    except Exception:
                        pass
                    continue
                if attr == "search_titles":
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list):
                        values.extend([x.strip() for x in parsed if isinstance(x, str) and x.strip()])
                    continue
                if isinstance(raw, str) and raw.strip():
                    values.append(raw.strip())
            for value in values:
                try:
                    base, sn = _extract_season_from_title(value)
                except Exception:
                    base, sn = value, None
                if sn == local_hint_int:
                    key = _series_title_key(base)
                    if key:
                        return key
            for value in values:
                key = _series_title_key(value)
                if key:
                    return key
            return ""

        try:
            base_key = _local_base_key()
            if base_key and not is_anime_item:
                target_series = None
                for candidate in session.query(Series).order_by(Series.id.asc()).all():
                    if getattr(candidate, "id", None) == getattr(series, "id", None):
                        continue
                    for attr in ("title", "title_en", "title_localized"):
                        try:
                            if _series_title_key(getattr(candidate, attr, None)) == base_key:
                                target_series = candidate
                                break
                        except Exception:
                            continue
                    if target_series is not None:
                        break
                if target_series is not None and getattr(target_series, "id", None):
                    season.series_id = target_series.id
                    series = target_series
                    session.add(season)
        except Exception:
            pass

    allow_promote_series_title = bool(season_num in (None, 1))

    # Prevenir que tÃ­tulos que parezcan episodios o canales se promuevan al tÃ­tulo de la serie
    def _is_safe_series_title(title: str | None) -> bool:
        if not title:
            return False
        if _looks_like_episode_title(title):
            return False
        if _looks_like_generic_season_marker(title):
            return False
        return True

    # NOTE: SQLAlchemy models are not typed with `Mapped[]` here, so Pylance often sees
    # attributes as `Column[...]`. Use `getattr` + runtime checks to keep type-checkers quiet.
    try:
        mi_title_en = getattr(mi_obj, "title_en", None)
        series_title_en = getattr(series, "title_en", None)
        if isinstance(mi_title_en, str) and mi_title_en.strip():
            if allow_promote_series_title or not (isinstance(series_title_en, str) and series_title_en.strip()):
                if _is_safe_series_title(mi_title_en):
                    series.title_en = mi_title_en.strip()
    except Exception:
        pass
    try:
        mi_title_loc = getattr(mi_obj, "title_localized", None)
        series_title_loc = getattr(series, "title_localized", None)
        if isinstance(mi_title_loc, str) and mi_title_loc.strip():
            if allow_promote_series_title or not (isinstance(series_title_loc, str) and series_title_loc.strip()):
                if _is_safe_series_title(mi_title_loc):
                    series.title_localized = mi_title_loc.strip()
    except Exception:
        pass
    try:
        # `title` should always be non-empty. Only overwrite it for Season 1 (or if it's empty).
        series_title = getattr(series, "title", None)
        if allow_promote_series_title or not (isinstance(series_title, str) and series_title.strip()):
            candidate = (
                getattr(mi_obj, "title_localized", None)
                or getattr(mi_obj, "title_en", None)
                or getattr(mi_obj, "title", None)
                or series_title
            )
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if _is_safe_series_title(candidate):
                    series.title = candidate
    except Exception:
        pass
    try:
        mal_id_val = getattr(mi_obj, "mal_id", None)
        if (not is_anime_item) and mal_id_val is not None and str(mal_id_val).strip():
            series.mal_id = str(mal_id_val).strip()
    except Exception:
        pass
    try:
        tmdb_id_val = getattr(mi_obj, 'tmdb_id', None)
    except Exception:
        tmdb_id_val = None
    if tmdb_id_val and not is_anime_item:
        try:
            tmdb_id_str = str(tmdb_id_val)
        except Exception:
            tmdb_id_str = None
        if tmdb_id_str:
            try:
                existing = (
                    session.query(Series)
                    .filter(Series.tmdb_id == tmdb_id_str)
                    .order_by(Series.id.asc())
                    .first()
                )
            except Exception:
                existing = None
            if existing and getattr(existing, "id", None) != getattr(series, "id", None):
                try:
                    if season and getattr(season, "series_id", None) != getattr(existing, "id", None):
                        season.series_id = existing.id
                        session.add(season)
                except Exception:
                    pass
                series = existing
            try:
                series.tmdb_id = tmdb_id_str
            except Exception:
                pass
    try:
        session.add(series)
        if season:
            session.add(season)
    except Exception:
        pass


def _should_map_episode_files(mi_obj: MediaItem) -> bool:
    # For bulk metadata injection runs, we sometimes want to avoid touching episode tables.
    # EpisodeFile mappings are primarily produced by the scanner.
    try:
        v = get("enrichment_map_episode_files")
        if v is not None:
            s = str(v).strip().lower()
            if s in ("0", "false", "no", "off"):
                return False
    except Exception:
        pass
    try:
        mt = str(getattr(mi_obj, "media_type", "") or "").strip().lower()
    except Exception:
        mt = ""
    try:
        lib = str(getattr(mi_obj, "library_type", "") or "").strip().lower()
    except Exception:
        lib = ""
    if mt in ("movie", "film"):
        return False
    if mt in ("series", "tv", "show"):
        return True
    if lib in ("anime",):
        return True
    return False


def _map_episode_files_for_item(mi_obj: MediaItem, session) -> bool:
    """Ensure Episode/EpisodeFile mapping exists for a series/anime media item.

    Uses the scanner's folder upsert to rebuild episode rows from FileRecord entries
    without running a full normalization pipeline.
    """
    if not _should_map_episode_files(mi_obj):
        return False

    try:
        file_rows = (
            session.query(FileRecord.id, FileRecord.path)
            .filter(FileRecord.media_item_id == mi_obj.id)
            .all()
        )
    except Exception:
        file_rows = []
    if not file_rows:
        return False

    file_ids = [int(fr[0]) for fr in file_rows if fr and fr[0] is not None]
    file_paths = [str(fr[1]) for fr in file_rows if fr and fr[1]]
    if not file_paths:
        return False

    try:
        si = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi_obj.id).first()
    except Exception:
        si = None

    if file_ids:
        try:
            mapped = (
                session.query(EpisodeFile.file_record_id)
                .filter(EpisodeFile.file_record_id.in_(file_ids))
                .distinct()
                .count()
            )
        except Exception:
            mapped = 0
        if si is not None and mapped >= len(file_ids):
            return False

    try:
        folder = str(getattr(mi_obj, "canonical_path", "") or "").strip()
    except Exception:
        folder = ""
    if not folder:
        try:
            folder = str(Path(file_paths[0]).parent)
        except Exception:
            folder = ""
    if not folder:
        return False

    try:
        from ..scanner.scanner import Scanner
    except Exception:
        return False

    try:
        scanner = Scanner(discovery_only=False)
        media_type_override = None
        try:
            mt_hint = str(getattr(mi_obj, "media_type", "") or "").strip().lower()
        except Exception:
            mt_hint = ""
        try:
            lib_hint = str(getattr(mi_obj, "library_type", "") or "").strip().lower()
        except Exception:
            lib_hint = ""
        try:
            folder_name_hint = str(Path(folder).name)
        except Exception:
            folder_name_hint = str(folder or "")
        try:
            def _looks_like_series_folder(name: str, files: list[str]) -> bool:
                n = str(name or "")
                if re.search(r"(?i)\b(?:season|temporada)\s*0*\d{1,2}\b", n):
                    return True
                if re.search(r"(?i)\bS0*\d{1,2}\b", n):
                    return True
                for fp in files:
                    try:
                        base = Path(fp).name
                    except Exception:
                        base = str(fp or "")
                    if re.search(r"(?i)\bS\d{1,2}E\d{1,3}\b", base):
                        return True
                    if re.search(r"(?i)\b(?:episode|episodio|cap(?:itulo)?)\s*0*\d{1,3}\b", base):
                        return True
                return False
            if mt_hint in ("series", "tv", "show", "anime") or lib_hint in ("series", "tv", "anime"):
                media_type_override = "series"
            elif _looks_like_series_folder(folder_name_hint, file_paths):
                media_type_override = "series"
        except Exception:
            media_type_override = None
        scanner._upsert_folder(
            folder,
            list(dict.fromkeys(file_paths)),
            skip_grouping=True,
            media_type_override=media_type_override,
            canonical_path_override=folder,
            discovery_only_override=False,
        )
        return True
    except Exception:
        logger.exception("Failed episode mapping for media_item=%s", getattr(mi_obj, "id", None))
        return False

# Ensure sessions created via `SessionLocal()` use a safe commit wrapper that
# delegates to `commit_and_report` to avoid raising on DB commit failures.
try:
    _orig_session_factory = SessionLocal
    def _session_factory(*a, **kw):
        s = _orig_session_factory(*a, **kw)
        try:
            s_any = _cast(_Any, s)
            def _commit_override():
                try:
                    orig = getattr(s_any, "_orig_commit", None)
                    if orig is None:
                        return commit_and_report(s, None)
                    setattr(s_any, "commit", orig)
                    try:
                        return commit_and_report(s, None)
                    finally:
                        setattr(s_any, "commit", _commit_override)
                except Exception:
                    try:
                        s_any.rollback()
                    except Exception:
                        pass
                    return False
            setattr(s_any, "_orig_commit", getattr(s_any, "commit", None))
            setattr(s_any, "commit", _commit_override)
        except Exception:
            pass
        return s
    SessionLocal = _session_factory
except Exception:
    pass

# Throttle image downloads to reduce network spikes.
_IMG_CONCURRENCY = max(1, int(os.environ.get("ARCANEA_IMAGE_CONCURRENCY", "2") or 2))
_IMG_DELAY_SEC = max(0.0, float(os.environ.get("ARCANEA_IMAGE_DELAY_MS", "100") or 100) / 1000.0)
_img_sema = None
try:
    _img_sema = threading.BoundedSemaphore(_IMG_CONCURRENCY)
except Exception:
    _img_sema = None
_image_session = requests.Session()
_image_session.mount('http://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
_image_session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
_image_download_blocked = False

# Best-effort TMDB series cache to improve season-folder resolution.
# Keyed by normalized base title -> tmdb_id (TV).
_TMDB_SERIES_CACHE: dict[str, str] = {}
_TMDB_SERIES_CACHE_LOCK = threading.Lock()




def _season_hint_from_title(title: str | None) -> int | None:
    """Extract season-like hint from title suffixes (S2, Season 2, 2nd, II, etc.)."""
    if not isinstance(title, str):
        return None
    s = title.strip()
    if not s:
        return None
    patterns = [
        r"(?i)\bseason\s*(\d{1,2})\b",
        r"(?i)\btemporada\s*(\d{1,2})\b",
        r"(?i)\bs(?:eason)?\s*0*(\d{1,2})\b",
        r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+season\b",
        r"(?i)\bpart\s*(\d{1,2})\b",
        r"(?i)\bcour\s*(\d{1,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            try:
                v = int(m.group(1))
                if 1 <= v <= 99:
                    return v
            except Exception:
                pass
    m = re.search(r"(?i)\b([ivx]{1,6})\b$", s)
    if m:
        roman = m.group(1).upper()
        table = {
            'I':1,'II':2,'III':3,'IV':4,'V':5,'VI':6,'VII':7,'VIII':8,'IX':9,'X':10
        }
        return table.get(roman)
    # Anime folders often use a plain numeric suffix for later seasons:
    # "The Angel Next Door Spoils Me Rotten 2". Treat only 2..20 as a
    # season hint and ignore year-like suffixes.
    try:
        m = re.search(r"(?<!\d)([2-9]|1\d|20)\s*$", s)
        if m and re.search(r"[A-Za-z]", s):
            n = int(m.group(1))
            prefix = s[: m.start()].strip(" ._-")
            if len(prefix) >= 3 and not re.search(r"(?:19|20)\d{2}\s*$", s):
                return n
    except Exception:
        pass
    return None


def _season_hint_for_media_item(media_item: MediaItem) -> int | None:
    """Prefer stable local naming fields over provider-mutated titles."""
    sources: list[str] = []
    for attr in ("base_title", "canonical_path", "search_titles", "title", "title_en"):
        try:
            value = getattr(media_item, attr, None)
        except Exception:
            value = None
        if not value:
            continue
        if attr == "canonical_path":
            try:
                base = os.path.basename(str(value)).strip()
                if base:
                    sources.append(base)
            except Exception:
                pass
            continue
        if attr == "search_titles":
            parsed = None
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.strip():
                        sources.append(item.strip())
            continue
        if isinstance(value, str) and value.strip():
            sources.append(value.strip())

    first_hint: int | None = None
    for source in sources:
        hint = _season_hint_from_title(source)
        if hint is None:
            continue
        if first_hint is None:
            first_hint = hint
        if hint > 1:
            return hint
    return first_hint


def _kitsu_match_allowed_for_item(candidate: dict | None, media_item: MediaItem) -> bool:
    """Guard Kitsu match by title numerology to avoid cross-season wrong matches.

    If local title includes explicit season hint (>1), candidate title must include same hint.
    """
    try:
        local_hint = _season_hint_for_media_item(media_item)
    except Exception:
        local_hint = None
    if not local_hint or local_hint <= 1:
        return True
    ctitle = None
    try:
        cand_obj = candidate
        if isinstance(cand_obj, dict) and isinstance(cand_obj.get("data"), dict):
            cand_obj = cand_obj.get("data")
        attrs = (cand_obj or {}).get('attributes') if isinstance(cand_obj, dict) else None
        if isinstance(attrs, dict):
            ctitle = attrs.get('canonicalTitle') or attrs.get('enTitle')
            if not ctitle and isinstance(attrs.get('titles'), dict):
                ctitle = attrs['titles'].get('en') or attrs['titles'].get('en_jp') or attrs['titles'].get('en_us')
    except Exception:
        ctitle = None
    cand_hint = _season_hint_from_title(ctitle)
    if cand_hint is None:
        return False
    return cand_hint == local_hint
def _normalize_provider_name(p: str | None, default: str) -> str:
    try:
        pn = (p or '').strip().lower()
    except Exception:
        pn = ''
    return pn or default


def _fallback_title_for_item(mi: MediaItem) -> str:
    try:
        if getattr(mi, "title", None):
            return str(getattr(mi, "title") or "")
    except Exception:
        pass
    try:
        bt = getattr(mi, "base_title", None)
        if isinstance(bt, str) and bt.strip():
            return bt.strip()
    except Exception:
        pass
    try:
        cp = getattr(mi, "canonical_path", None)
        if cp:
            p = Path(str(cp))
            name = p.stem or p.name
            if name:
                return str(name)
    except Exception:
        pass
    return "Sin Nombre"

def _looks_like_episode_title(v: str | None) -> bool:
    if v is None:
        return False
    try:
        s = str(v).strip()
    except Exception:
        return False
    if not s:
        return False
    # Rechazar nÃºmeros puros o nÃºmeros con guiones
    if re.fullmatch(r"\d{1,4}(?:-\d{1,4})?", s):
        return True
    if re.search(r"(?i)\bS\d{1,2}E\d{1,3}(?:-\d{1,3})?\b", s):
        return True
    if re.search(r"(?i)\b(?:episode|episodio|cap(?:itulo)?|ch(?:apter)?)\s*\d{1,3}(?:-\d{1,3})?\b", s):
        return True
    if re.search(r"(?i)\bE\s*\d{1,3}\b", s):
        return True
    # Rechazar tÃ­tulos que parezcan nombres de canales comunes
    if re.search(r"(?i)\b(?:canal|tv|network|channel)\b", s) and len(s.split()) <= 3:
         return True
    return False


def _looks_like_generic_season_marker(v: str | None) -> bool:
    """Return True for strings that are basically just season/part markers (no series title).

    Examples: "Temporada 2", "Season 3", "Temp 1", "S02", "2da Temporada", "Cour 2".

    We treat these as unsafe TMDB/Jikan search anchors because they can produce catastrophic
    matches ("1" -> Pride, "2" -> Clerks, etc.). The caller should prefer parent series titles.
    """
    if v is None:
        return False
    try:
        s0 = str(v).strip()
    except Exception:
        return False
    if not s0:
        return False
    s = s0.strip()
    # Fast-path exact patterns.
    try:
        if re.fullmatch(r"(?i)(?:s|season|temporada|temp(?:\.|orada)?|part|cour)\s*0*\d{1,2}", s):
            return True
        if re.fullmatch(r"(?i)0*\d{1,2}(?:st|nd|rd|th)\s*(?:season|temporada|part|cour)", s):
            return True
        if re.fullmatch(r"(?i)0*\d{1,2}\s*(?:a|Âª|da|ra|ta|ba|va|ma|na)\s*temporada", s):
            return True
        if re.fullmatch(r"(?i)(?:season|temporada|part|cour)\s*(i|ii|iii|iv|v|vi|vii|viii|ix|x)", s):
            return True
        if re.fullmatch(r"(?i)(i|ii|iii|iv|v|vi|vii|viii|ix|x)\s*(?:season|temporada|part|cour)", s):
            return True
    except Exception:
        pass

    # General check: remove season words + ordinals + numbers. If nothing meaningful remains, it's generic.
    try:
        t = s.lower()
        t = t.replace("_", " ").replace("-", " ")
        t = re.sub(r"(?i)\b(?:s|season|temporada|temp(?:\.|orada)?|part|cour)\b", " ", t)
        t = re.sub(r"(?i)\b(?:st|nd|rd|th|a|Âª|da|ra|ta|ba|va|ma|na)\b", " ", t)
        t = re.sub(r"\b0*\d{1,2}\b", " ", t)
        t = re.sub(r"(?i)\b(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        return not bool(t)
    except Exception:
        return False


def _looks_like_generic_search_anchor(v: str | None) -> bool:
    try:
        s = str(v or "").strip().lower()
    except Exception:
        return True
    if not s:
        return True
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s in {
        "series",
        "serie",
        "tv",
        "show",
        "shows",
        "season",
        "seasons",
        "temporada",
        "temporadas",
    }

def _candidate_titles_for_item(mi: MediaItem) -> list[str]:
    """Build candidate titles in priority order.
    1) search_titles (if present)
    2) canonical_path basename
    3) title (if present)
    """
    candidates: list[str] = []
    # If the canonical folder looks like a season subfolder (e.g. ".../Halo/Temporada 2"),
    # prefer using "<parent> <basename>" so TMDB can resolve the correct series+season.
    try:
        cp0 = getattr(mi, 'canonical_path', None)
        if cp0:
            cp = str(cp0)
            base = os.path.basename(cp).strip()
            parent = os.path.basename(os.path.dirname(cp)).strip()
            try:
                lib0 = str(getattr(mi, "library_type", "") or "").strip().lower()
            except Exception:
                lib0 = ""
            if base and parent:
                # basename-only season folders (e.g. "Temporada 2", "Season 2", "S2", "2da Temporada")
                if re.search(r"(?i)^(?:temporada|season|part|cour|temp(?:\.|orada)?|s)\s*0*\d{1,2}$|^\s*0*\d{1,2}\s*(?:st|nd|rd|th|a|Âª|da|ra|ta|ba|va|ma|na)?\s*(?:temporada|season|part|cour)\s*$", base):
                    combined = f"{parent} {base}".strip()
                    if combined and combined not in candidates:
                        candidates.append(combined)
                    if parent and parent not in candidates:
                        candidates.append(parent)
                # Series-special folders may include episode/special names in the basename.
                # Keep parent title as low-priority fallback for TV matching.
                elif lib0 in ("series", "tv", "show"):
                    if parent and parent not in candidates:
                        candidates.append(parent)
    except Exception:
        pass
    try:
        st = getattr(mi, 'search_titles', None)
        parsed = None
        if st:
            if isinstance(st, str):
                try:
                    parsed = json.loads(st)
                except Exception:
                    parsed = None
            else:
                parsed = st
        if isinstance(parsed, list):
            for t in parsed:
                if isinstance(t, str) and t:
                    if _looks_like_episode_title(t):
                        continue
                    if _looks_like_generic_season_marker(t):
                        continue
                    if t not in candidates:
                        candidates.append(t)
    except Exception:
        pass
    try:
        cp = getattr(mi, 'canonical_path', None)
        if cp:
            base = os.path.basename(str(cp))
            if base and not _looks_like_generic_season_marker(base) and base not in candidates:
                candidates.append(base)
    except Exception:
        pass
    try:
        base_title = getattr(mi, 'base_title', None)
        if isinstance(base_title, str) and base_title and not _looks_like_generic_season_marker(base_title) and base_title not in candidates:
            candidates.append(base_title)
    except Exception:
        pass
    try:
        title = getattr(mi, 'title', None)
        if isinstance(title, str) and title and not _looks_like_generic_season_marker(title) and title not in candidates:
            candidates.append(title)
    except Exception:
        pass

    # Expand additional normalized variants from basename/title to rescue noisy filenames.
    # Keep these as lower-priority candidates so explicit search_titles remain dominant.
    try:
        from ..utils.utils import normalize_folder_name_for_search
    except Exception:
        normalize_folder_name_for_search = None  # type: ignore[assignment]
    try:
        if normalize_folder_name_for_search is not None:
            raw_sources: list[str] = []
            try:
                cp = getattr(mi, "canonical_path", None)
                if cp:
                    b = os.path.basename(str(cp)).strip()
                    if b:
                        raw_sources.append(b)
            except Exception:
                pass
            try:
                t0 = getattr(mi, "title", None)
                if isinstance(t0, str) and t0.strip():
                    raw_sources.append(t0.strip())
            except Exception:
                pass
            try:
                bt0 = getattr(mi, "base_title", None)
                if isinstance(bt0, str) and bt0.strip():
                    raw_sources.append(bt0.strip())
            except Exception:
                pass
            for raw in raw_sources:
                try:
                    variants = normalize_folder_name_for_search(str(raw)) or []
                except Exception:
                    variants = []
                for v in variants:
                    try:
                        vv = str(v).strip()
                    except Exception:
                        continue
                    if not vv:
                        continue
                    if _looks_like_episode_title(vv) or _looks_like_generic_season_marker(vv):
                        continue
                    if vv not in candidates:
                        candidates.append(vv)
    except Exception:
        pass

    # Season-aware ordering:
    # For series/anime folders like "Tonikaku Kawaii 2nd Season", we must NOT move the seasonless base title
    # ahead of the season-aware variants; enrichment currently uses only the first candidate for Jikan searches.
    season_hint_found = False
    try:
        media_type = str(getattr(mi, "media_type", "") or "").strip().lower()
    except Exception:
        media_type = ""
    try:
        lib_type = str(getattr(mi, "library_type", "") or "").strip().lower()
    except Exception:
        lib_type = ""
    prefer_season_hint = bool(media_type == "series" or lib_type in ("anime", "series", "tv", "show"))
    if prefer_season_hint:
        try:
            seen = {c.strip().lower() for c in candidates if isinstance(c, str)}
            for raw in list(candidates)[:10]:
                if _looks_like_generic_season_marker(raw):
                    continue
                base, season = _extract_season_from_title(raw)
                if not base or season is None:
                    continue
                # Avoid generating variants from generic bases like "Temp 1" -> "1" (PRIDE) or "2" (Clerks).
                if _looks_like_generic_season_marker(base):
                    continue
                try:
                    if re.fullmatch(r"\d{1,4}", str(base).strip()):
                        continue
                except Exception:
                    pass
                try:
                    if not re.search(r"[A-Za-z]", str(base)):
                        continue
                except Exception:
                    pass
                season_hint_found = True
                # Jikan season matching is most reliable with a plain numeric suffix ("<title> 2").
                v_num = f"{base} {int(season)}".strip()
                v_season = f"{base} Season {int(season)}".strip()
                # Insert in reverse to preserve intended priority order at the front.
                for v in (v_season, v_num):
                    if not v:
                        continue
                    k = v.lower()
                    if k in seen:
                        continue
                    if _looks_like_generic_season_marker(v) or _looks_like_episode_title(v):
                        continue
                    candidates.insert(0, v)
                    seen.add(k)
        except Exception:
            season_hint_found = False

    # Extra hardening: ensure we also have a "clean" base title to improve matches for specials/noise.
    # When a season number is implied, keep the clean base as a fallback (append), do not demote season hints.
    try:
        def _clean_title(s: str) -> str:
            t = str(s or "").strip()
            if not t:
                return ""
            t = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", t)
            t = re.sub(r"(?i)\b(?:part|cour|season|temporada|temp(?:\.|orada)?)\s*0*\d{1,2}\b", " ", t)
            t = re.sub(r"(?i)\b0*(\d{1,2})(st|nd|rd|th)\s*(?:season|temporada|part|cour)\b", " ", t)
            t = re.sub(r"(?i)\b0*(\d{1,2})\s*(?:a|Ã‚Âª|da|ra|ta)\s*(?:temporada)\b", " ", t)
            t = re.sub(r"(?i)\b(?:season|temporada|part|cour)\s*(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", " ", t)
            t = re.sub(r"(?i)\b(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\s*(?:season|temporada|part|cour)\b", " ", t)
            t = re.sub(r"(?i)\b0*(\d{1,2})(st|nd|rd|th)\b", " ", t)
            t = re.sub(r"(?i)\b(?:season|temporada|part|cour)\b\s*$", " ", t)
            t = re.sub(r"(?i)\b(?:especial(?:es)?|specials?|ova(?:s)?)\b", " ", t)
            t = re.sub(r"[\-_]+", " ", t)
            t = re.sub(r"\s{2,}", " ", t).strip()
            return t

        for raw in list(candidates)[:3]:
            cleaned = _clean_title(raw)
            if cleaned and cleaned not in candidates:
                if _looks_like_episode_title(cleaned) or _looks_like_generic_season_marker(cleaned):
                    continue
                if season_hint_found and prefer_season_hint:
                    candidates.append(cleaned)
                else:
                    candidates.insert(0, cleaned)
    except Exception:
        pass

    # Final filter: drop unsafe generic/episode-like candidates; keep order.
    try:
        try:
            from ..utils.utils import normalize_folder_name_for_search as _norm_title_variants
        except Exception:
            _norm_title_variants = None  # type: ignore[assignment]
        seen_final: set[str] = set()
        direct_final: list[tuple[int, str]] = []
        out_final: list[str] = []
        for c in candidates:
            if not isinstance(c, str):
                continue
            expanded = [c]
            if "themoviedb.org/" not in str(c).lower() and _norm_title_variants is not None:
                try:
                    for v in _norm_title_variants(c) or []:
                        if isinstance(v, str) and v.strip():
                            expanded.append(v)
                except Exception:
                    pass
            for raw_cc in expanded:
                cc = str(raw_cc or "").strip()
                if not cc:
                    continue
                cc = re.sub(r"\.[a-zA-Z0-9]{1,4}$", "", cc).strip()
                cc = re.sub(r"\s{2,}", " ", cc).strip()
                if not cc:
                    continue
                numeric_movie_title = bool(media_type in ("movie", "film") and re.fullmatch(r"(?:19|20)\d{2}", cc))
                is_tmdb_url = "themoviedb.org/" in cc.lower()
                if (not is_tmdb_url) and (not numeric_movie_title) and (_looks_like_episode_title(cc) or _looks_like_generic_season_marker(cc)):
                    continue
                if (not is_tmdb_url) and (not numeric_movie_title) and _looks_like_generic_search_anchor(cc):
                    continue
                k = cc.lower()
                if k in seen_final:
                    continue
                seen_final.add(k)
                if "themoviedb.org/" in k:
                    try:
                        direct_priority = 0 if re.search(r"\b(?:19|20)\d{2}\b", str(c or "")) else 1
                    except Exception:
                        direct_priority = 1
                    direct_final.append((direct_priority, cc))
                else:
                    out_final.append(cc)
        candidates = [cc for _priority, cc in sorted(direct_final, key=lambda item: item[0])] + out_final
    except Exception:
        pass

    return candidates


def _extract_season_from_title(title: str | None) -> tuple[str, int | None]:
    """Return (base_title, season_number) if the title looks like a season folder."""
    try:
        t0 = (title or "").strip()
    except Exception:
        return "", None
    if not t0:
        return "", None

    t = t0
    season = None
    try:
        m = re.search(r"\b(?:temporada|season|temp(?:\.|orada)?)\s*(\d{1,2})\b", t, re.IGNORECASE)
        if m:
            season = int(m.group(1))
            t = re.sub(r"\b(?:temporada|season|temp(?:\.|orada)?)\s*\d{1,2}\b", "", t, flags=re.IGNORECASE)
    except Exception:
        season = None
    if season is None:
        # e.g. "2nd Season", "3rd Season", "2da Temporada", "3ra Temporada", "4ta Temporada"
        try:
            m = re.search(r"(?i)\b0*(\d{1,2})\s*(?:st|nd|rd|th)\s*(?:season|temporada|part|cour)\b", t)
            if not m:
                m = re.search(r"(?i)\b0*(\d{1,2})\s*(?:a|Ã‚Âª|da|ra|ta)\s*(?:temporada)\b", t)
            if m:
                season = int(m.group(1))
                t = re.sub(m.re.pattern, "", t).strip()
        except Exception:
            season = None
    if season is None:
        try:
            m = re.search(r"\bs(\d{1,2})\b", t, re.IGNORECASE)
            if m:
                season = int(m.group(1))
                t = re.sub(r"\bs\d{1,2}\b", "", t, flags=re.IGNORECASE)
        except Exception:
            season = None
    if season is None:
        try:
            m = re.search(r"(?i)\b(?:part|cour)\s*0*(\d{1,2})\b", t)
            if m:
                season = int(m.group(1))
                t = re.sub(r"(?i)\b(?:part|cour)\s*0*\d{1,2}\b", "", t).strip()
        except Exception:
            season = None
    if season is None:
        # roman numerals: "Season II", "Temporada IV", or "II Season"
        try:
            roman_map = {
                "I": 1,
                "II": 2,
                "III": 3,
                "IV": 4,
                "V": 5,
                "VI": 6,
                "VII": 7,
                "VIII": 8,
                "IX": 9,
                "X": 10,
            }
            m = re.search(r"(?i)\b(?:season|temporada|part|cour)\s*(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", t)
            if not m:
                m = re.search(r"(?i)\b(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b\s*(?:season|temporada|part|cour)\b", t)
            if m:
                rn = m.group(1).upper()
                season = roman_map.get(rn)
                if season:
                    t = re.sub(m.re.pattern, "", t).strip()
        except Exception:
            season = None
    if season is None:
        # Later anime seasons commonly live in folders like "<title> 2".
        # Keep this conservative to avoid treating years or episode numbers as seasons.
        try:
            m = re.search(r"(?<!\d)([2-9]|1\d|20)\s*$", t)
            if m and re.search(r"[A-Za-z]", t) and not re.search(r"(?:19|20)\d{2}\s*$", t):
                prefix = t[: m.start()].strip(" ._-")
                if len(prefix) >= 3:
                    season = int(m.group(1))
                    t = prefix
        except Exception:
            season = None

    try:
        t = re.sub(r"[\-_]+", " ", t).strip()
        t = re.sub(r"\s{2,}", " ", t).strip()
    except Exception:
        pass

    return t or t0, season


def _inherit_tmdb_identity_from_parent_if_season(mi: MediaItem, session) -> tuple[str, int] | None:
    """If `mi` looks like a season subfolder, inherit TMDB identity from its parent folder.

    Rule: if parent MediaItem (canonical_path parent) already has tmdb_id and is tv/series, we must NOT
    perform a new TMDB search for the child. We directly fetch `/tv/{tmdb_id}/season/{season_number}`.
    """
    try:
        cp = getattr(mi, "canonical_path", None)
        if not cp:
            return None
        cp_s = str(cp)
    except Exception:
        return None

    try:
        folder_name = os.path.basename(cp_s).strip()
    except Exception:
        folder_name = ""
    if not folder_name:
        return None

    try:
        _base, season = _extract_season_from_title(folder_name)
    except Exception:
        season = None
    if not season or int(season) <= 0:
        return None

    parent_path = None
    try:
        parent_path = os.path.dirname(cp_s)
    except Exception:
        parent_path = None
    if not parent_path:
        return None

    parent = None
    try:
        parent = session.query(MediaItem).filter(MediaItem.canonical_path == str(Path(parent_path).resolve())).first()
    except Exception:
        parent = None
    if not parent:
        return None

    try:
        tmdb_id = getattr(parent, "tmdb_id", None)
        if not tmdb_id:
            return None
        pmt = (getattr(parent, "media_type", None) or "").strip().lower()
        if pmt not in ("tv", "series", "show"):
            return None
    except Exception:
        return None

    return (str(tmdb_id), int(season))


def _tmdb_search_with_optional_season(candidate: str, media_type: str | None) -> dict | None:
    """TMDB search that upgrades season-folder names into season metadata (episodes, synopsis)."""
    if tmdb is None:
        return None
    try:
        numeric_movie_title = bool(str(media_type or "").strip().lower() in ("movie", "film") and re.fullmatch(r"(?:19|20)\d{2}", str(candidate or "").strip()))
    except Exception:
        numeric_movie_title = False
    if (not numeric_movie_title) and _looks_like_generic_search_anchor(candidate):
        return None
    base, season = _extract_season_from_title(candidate)
    # If it looks like a season folder, force a tv search on the base name.
    if season is not None and season > 0 and hasattr(tmdb, 'search_by_type'):
        cache_key = (base or "").strip().lower()
        if cache_key:
            with _TMDB_SERIES_CACHE_LOCK:
                cached_id = _TMDB_SERIES_CACHE.get(cache_key)
            if cached_id and hasattr(tmdb, 'tmdb_fetch_season'):
                sd = tmdb.tmdb_fetch_season(str(cached_id), season_number=season)
                if sd:
                    return sd
        series_res = tmdb.search_by_type(base, media_preference='tv')
        if isinstance(series_res, list) and series_res:
            series_res = series_res[0]
        if isinstance(series_res, dict):
            try:
                if _tmdb_title_confidence(base or candidate, series_res) < float(os.environ.get("ARCANEA_TMDB_SEARCH_MIN_CONF", "0.52") or 0.52):
                    return None
            except Exception:
                return None
            try:
                tid_cache = series_res.get('tmdb_id') or series_res.get('provider_id')
                if tid_cache and cache_key:
                    with _TMDB_SERIES_CACHE_LOCK:
                        _TMDB_SERIES_CACHE[cache_key] = str(tid_cache)
            except Exception:
                pass
            # Fetch season-level metadata if possible
            tid = series_res.get('tmdb_id') or series_res.get('provider_id')
            if tid and hasattr(tmdb, 'tmdb_fetch_season'):
                sd = tmdb.tmdb_fetch_season(str(tid), season_number=season)
                if sd:
                    return sd
            return series_res
    # Default behavior
    if hasattr(tmdb, 'search_by_type'):
        mt = (media_type or "").strip().lower()
        if mt in ("series", "tv", "show", "anime"):
            media_pref = "tv"
        elif mt in ("movie", "film"):
            media_pref = "movie"
        else:
            media_pref = "movie"
        res = tmdb.search_by_type(candidate, media_preference=media_pref)
        if isinstance(res, dict) and media_pref == "tv":
            try:
                anchor, _sn = _extract_season_from_title(candidate)
                min_conf = float(os.environ.get("ARCANEA_TMDB_SEARCH_MIN_CONF", "0.52") or 0.52)
                if _tmdb_title_confidence(anchor or candidate, res) < min_conf:
                    res = None
            except Exception:
                res = None
        # Safe fallback: if movie lookup fails, try TV with a strict title-confidence gate.
        if (not res) and media_pref == "movie":
            try:
                r_tv = tmdb.search_by_type(candidate, media_preference="tv")
            except Exception:
                r_tv = None
            if isinstance(r_tv, list) and r_tv:
                r_tv = r_tv[0]
            if isinstance(r_tv, dict) and r_tv:
                try:
                    base_tv, _sn_tv = _extract_season_from_title(candidate)
                    conf_tv = _tmdb_title_confidence(base_tv or candidate, r_tv)
                except Exception:
                    conf_tv = 0.0
                if conf_tv >= 0.93:
                    res = r_tv
    else:
        res = tmdb.search(candidate) if hasattr(tmdb, 'search') else None
    # Cache successful TV ids by base title to help future season items.
    try:
        if isinstance(res, dict) and (res.get('media_type') or '').lower().startswith('tv'):
            tid_cache = res.get('tmdb_id') or res.get('provider_id')
            b2, _s2 = _extract_season_from_title(candidate)
            ck = (b2 or "").strip().lower()
            if tid_cache and ck:
                with _TMDB_SERIES_CACHE_LOCK:
                    _TMDB_SERIES_CACHE[ck] = str(tid_cache)
    except Exception:
        pass
    return res


def _tmdb_title_confidence(anchor_title: str, tmdb_payload: dict) -> float:
    """Return best-effort title similarity (0..1) between local folder title and a TMDB payload.

    Used to guard anime->TMDB fallbacks so we only accept near-exact matches.
    """
    try:
        import difflib
        import re
    except Exception:
        return 0.0

    def _n(s: str | None) -> str:
        if not s:
            return ""
        ss = str(s).strip().lower()
        ss = re.sub(r"[^a-z0-9]+", " ", ss)
        ss = re.sub(r"\s{2,}", " ", ss).strip()
        return ss

    a = _n(anchor_title)
    if not a:
        return 0.0
    best = 0.0
    for k in ("title", "name", "series_title", "original_title", "original_name", "title_en", "title_localized"):
        try:
            v = tmdb_payload.get(k)
        except Exception:
            v = None
        if not isinstance(v, str) or not v.strip():
            continue
        try:
            r = difflib.SequenceMatcher(None, a, _n(v)).ratio()
        except Exception:
            r = 0.0
        if r > best:
            best = r
    return float(best)


def _tmdb_anchor_for_item(mi: Any, candidate: str | None = None) -> str:
    """Stable local title anchor for TMDB confidence checks."""
    sources: list[str] = []
    for attr in ("base_title", "canonical_path", "title_en", "title"):
        try:
            value = getattr(mi, attr, None)
        except Exception:
            value = None
        if not value:
            continue
        if attr == "canonical_path":
            try:
                base = os.path.basename(str(value)).strip()
            except Exception:
                base = ""
            if base and not _looks_like_generic_search_anchor(base):
                sources.append(base)
            continue
        if isinstance(value, str) and value.strip() and not _looks_like_generic_search_anchor(value):
            sources.append(value.strip())
    # Candidate is only a last-resort fallback. It may come from stale metadata or a
    # polluted search title, so it must not override the item's local identity.
    if isinstance(candidate, str) and candidate.strip() and not _looks_like_generic_search_anchor(candidate):
        sources.append(candidate.strip())
    for source in sources:
        try:
            base, _season = _extract_season_from_title(source)
            cleaned = base or source
        except Exception:
            cleaned = source
        cleaned = str(cleaned or "").strip()
        if cleaned:
            return cleaned
    return ""


def _tmdb_match_allowed_for_item(payload: dict | None, mi: Any, candidate: str | None = None) -> bool:
    """Reject stale/bad TMDB identities before they can poison series grouping."""
    if not isinstance(payload, dict) or not payload:
        return False
    try:
        mt = str(getattr(mi, "media_type", "") or "").strip().lower()
        lib = str(getattr(mi, "library_type", "") or "").strip().lower()
    except Exception:
        mt = ""
        lib = ""
    # Anime should not be corrected through fuzzy TMDB identities in the first place.
    if lib == "anime":
        return True
    try:
        cand_s = str(candidate or "").strip().lower()
        if "themoviedb.org/" in cand_s:
            return True
    except Exception:
        pass
    anchor = _tmdb_anchor_for_item(mi, candidate)
    if not anchor:
        return True
    try:
        conf = _tmdb_title_confidence(anchor, payload)
    except Exception:
        conf = 0.0
    try:
        threshold = float(os.environ.get("ARCANEA_TMDB_IDENTITY_MIN_CONF", "0.52") or 0.52)
    except Exception:
        threshold = 0.52
    # TV/series grouping is more dangerous than movie matching because one bad id
    # can collapse many folders into a single Series row.
    if mt in ("series", "tv", "show"):
        return conf >= threshold
    return conf >= min(0.45, threshold)


def _tmdb_search_media_type_for_item(mi: Any, media_type: str | None) -> str | None:
    """Heuristic override for TMDB search type.

    Some libraries (notably `library_type=anime`) may store movie items with `media_type=series`
    (e.g. movie folders with multiple qualities). When searching TMDB we should prefer the movie
    endpoint in those cases, otherwise we'll often get NO_MATCH.
    """
    if not media_type:
        return media_type
    try:
        mt = str(media_type).strip().lower()
    except Exception:
        return media_type

    try:
        lib_type = str(getattr(mi, "library_type", "") or "").strip().lower()
    except Exception:
        lib_type = ""

    # Library is explicitly series-like but media_type says movie.
    # This commonly happens for specials/OVAs/miniseries folders and should hit TMDB TV.
    if lib_type in ("series", "tv", "show") and mt in ("movie", "film"):
        return "tv"

    if lib_type != "anime":
        return media_type

    # If the item already claims it's a movie, keep it.
    if mt in ("movie", "film"):
        return "movie"

    # If it looks like a movie from the folder/title, search TMDB as a movie even if mt says "series".
    try:
        hints: list[str] = []
        for v in (
            getattr(mi, "title", None),
            getattr(mi, "base_title", None),
            getattr(mi, "canonical_path", None),
        ):
            if not v:
                continue
            s = str(v)
            if s and s not in hints:
                hints.append(s)
        try:
            cp = str(getattr(mi, "canonical_path", "") or "")
            if cp:
                base = os.path.basename(cp)
                if base and base not in hints:
                    hints.append(base)
        except Exception:
            pass

        movie_rx = re.compile(r"(?i)\b(movie|pel[iÃ­]cula|film|the\s+movie|gekijouban)\b")
        if any(movie_rx.search(h or "") for h in hints):
            return "movie"
    except Exception:
        pass

    return media_type


def _normalize_root_type(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        t = str(raw).strip().lower()
    except Exception:
        return None
    if not t or t in ("auto", "unknown", "unset", "none", "null", "infer", "inferred"):
        return None
    if t in ("tv", "show", "shows"):
        t = "series"
    if t not in ("anime", "series", "movie"):
        return None
    return t


def _root_type_for_media_item(session, mi: MediaItem) -> str | None:
    try:
        root_path = getattr(mi, "media_root", None)
        if not root_path:
            return None
        mr = session.query(MediaRoot).filter(MediaRoot.path == root_path).first()
        if not mr:
            return None
        return _normalize_root_type(getattr(mr, "type", None))
    except Exception:
        return None


def _tmdb_allowed_for_root_type(root_type: str | None) -> bool:
    rt = _normalize_root_type(root_type)
    return rt in ("movie", "series")


def _tmdb_allowed_for_media_item(session, mi: MediaItem, *, manual_override: bool = False) -> bool:
    if manual_override:
        return True
    try:
        root_type = _root_type_for_media_item(session, mi)
    except Exception:
        root_type = None
    return _tmdb_allowed_for_root_type(root_type)


def _provider_order_for_item(
    media_type: str | None,
    mal_id: Any,
    cfg_movies: Any,
    cfg_anime: Any,
    media_root: str | None = None,
    media_root_type: str | None = None,
) -> list[str]:
    mt = (media_type or '').strip().lower()
    rt = _normalize_root_type(media_root_type)
    try:
        cfg_movies_s = str(cfg_movies).strip() if cfg_movies is not None else None
    except Exception:
        cfg_movies_s = None
    if cfg_movies_s == "":
        cfg_movies_s = None
    try:
        cfg_anime_s = str(cfg_anime).strip() if cfg_anime is not None else None
    except Exception:
        cfg_anime_s = None
    if cfg_anime_s == "":
        cfg_anime_s = None

    # Root type (manual) wins over heuristics.
    if rt == 'anime':
        primary = _normalize_provider_name(cfg_anime_s, 'kitsu')
        if primary == "auto":
            # Auto mode: prefer Jikan when healthy; fall back to Kitsu.
            # TMDB is intentionally blocked for anime roots unless manual mapping explicitly requests it.
            try:
                if jikan is not None and hasattr(jikan, "healthcheck"):
                    hc = jikan.healthcheck()  # type: ignore[attr-defined]
                    ok = bool((hc or {}).get("ok", True))
                else:
                    ok = True
            except Exception:
                ok = True
            primary = "jikan" if ok else "kitsu"
        if primary == "tmdb":
            primary = "jikan" if mal_id else "kitsu"
        if primary not in ('jikan', 'kitsu'):
            primary = 'jikan'
        # Allow a safe secondary fallback for anime providers to reduce NO_MATCH churn.
        # This never falls back to TMDB automatically (too risky); only jikan<->kitsu.
        secondary = None
        try:
            if primary == "kitsu":
                secondary = "jikan"
            elif primary == "jikan":
                secondary = "kitsu"
        except Exception:
            secondary = None
        order = [primary]
        if mal_id and primary != "jikan":
            order = ["jikan", primary]
        if secondary and secondary not in order:
            order.append(secondary)
        return order
    if rt in ('series', 'movie'):
        return ['tmdb']

    # Heuristic: infer anime by root folder name if no manual type.
    try:
        if media_root:
            base = os.path.basename(str(media_root)).lower()
            if 'anime' in base or 'animes' in base:
                mt = 'anime'
    except Exception:
        pass

    if mal_id or mt == 'anime':
        primary = _normalize_provider_name(cfg_anime_s, 'kitsu')
        if primary == "auto":
            # Auto mode (anime): prefer Jikan when healthy; otherwise use Kitsu.
            # TMDB is only used when explicitly configured (or via separate fallback logic).
            try:
                if jikan is not None and hasattr(jikan, "healthcheck"):
                    hc = jikan.healthcheck()  # type: ignore[attr-defined]
                    ok = bool((hc or {}).get("ok", True))
                else:
                    ok = True
            except Exception:
                ok = True
            primary = "jikan" if ok else "kitsu"
        if primary == "tmdb":
            primary = "jikan" if mal_id else "kitsu"
        if primary not in ('jikan', 'kitsu'):
            primary = 'jikan'
        # Safe secondary fallback for anime.
        secondary = None
        try:
            if primary == "kitsu":
                secondary = "jikan"
            elif primary == "jikan":
                secondary = "kitsu"
        except Exception:
            secondary = None
        order = [primary]
        if mal_id and primary != "jikan":
            order = ["jikan", primary]
        if secondary and secondary not in order:
            order.append(secondary)
        return order

    primary = _normalize_provider_name(cfg_movies_s, 'tmdb')
    if primary not in ('tmdb', 'jikan'):
        primary = 'tmdb'
    order = [primary]
    if primary == 'jikan':
        order.append('tmdb')
    return order


def _decide_media_type(mi: MediaItem, session) -> str:
    try:
        count = session.query(FileRecord).filter(FileRecord.media_item_id == mi.id).count()
        if count > 1:
            return 'series'
    except Exception:
        pass
    return 'movie'


def _normalize_tmdb(res: dict) -> dict:
    if not res:
        return {}
    tmdb_id = res.get('tmdb_id') or res.get('provider_id') or res.get('id')
    genres = []
    try:
        if isinstance(res.get('genres'), list):
            for g in res.get('genres') or []:
                if isinstance(g, dict):
                    if g.get('name'):
                        genres.append(g.get('name'))
                elif isinstance(g, str):
                    genres.append(g)
        elif isinstance(res.get('genre_ids'), list):
            genres = res.get('genre_ids')
    except Exception:
        genres = res.get('genres') or []
    try:
        if isinstance(genres, list) and genres:
            mapped = map_tmdb_genres(", ".join(str(x).strip() for x in genres if str(x).strip()))
            if isinstance(mapped, list) and mapped:
                genres = mapped
    except Exception:
        pass
    return {
        'provider': 'tmdb',
        'provider_id': str(tmdb_id) if tmdb_id is not None else '',
        'tmdb_id': tmdb_id,
        'title': res.get('title') or res.get('series_title') or res.get('name') or '',
        'series_title': res.get('series_title') or res.get('name') or res.get('title') or '',
        'synopsis': res.get('synopsis') or res.get('overview') or '',
        'genres': genres,
        'images': res.get('images') or {},
        'media_type': (res.get('media_type') or res.get('media_type') or '').lower() or None,
        'season_number': res.get('season_number') if res.get('season_number') is not None else None,
        'total_seasons': res.get('total_seasons') if res.get('total_seasons') is not None else None,
        'release_date': res.get('release_date') or res.get('first_air_date') or None,
        'content_rating': res.get('content_rating') or res.get('certification') or None,
        'resolved_from_folder': True if (res.get('season_number') is not None or res.get('total_seasons') is not None) else False,
        'raw': res,
    }


def _normalize_kitsu(res: dict) -> dict:
    """Normalize a Kitsu JSON:API response into our internal metadata shape."""
    if not res:
        return {}
    data: Any = res
    try:
        if isinstance(res, dict) and "data" in res:
            data = res.get("data")
    except Exception:
        data = res
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {}

    try:
        kid = str(data.get("id") or data.get("kitsu_id") or data.get("provider_id") or "").strip()
    except Exception:
        kid = ""
    attrs_raw = data.get("attributes")
    attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}

    titles_raw = attrs.get("titles")
    titles: dict[str, Any] = titles_raw if isinstance(titles_raw, dict) else {}
    title = None
    try:
        ct = attrs.get("canonicalTitle")
        if isinstance(ct, str) and ct.strip():
            title = ct.strip()
    except Exception:
        title = None
    if not title:
        for k in ("en", "en_us", "en_jp", "es", "es_mx", "es_es", "ja_jp"):
            try:
                v = titles.get(k)
                if isinstance(v, str) and v.strip():
                    title = v.strip()
                    break
            except Exception:
                continue
    if not title:
        try:
            title = str(titles.get("en") or titles.get("en_jp") or "").strip() or None
        except Exception:
            title = None
    if not title:
        title = ""

    synopsis = ""
    try:
        syn = attrs.get("synopsis")
        if isinstance(syn, str) and syn.strip():
            synopsis = kitsu.clean_synopsis_text(syn) if kitsu is not None and hasattr(kitsu, "clean_synopsis_text") else syn.strip()
    except Exception:
        synopsis = ""

    episodes_count = None
    try:
        ec = attrs.get("episodeCount")
        episodes_count = int(ec) if ec is not None else None
    except Exception:
        episodes_count = None

    status = None
    try:
        st = attrs.get("status")
        status = str(st).strip() if st else None
    except Exception:
        status = None

    subtype = None
    try:
        sub = attrs.get("subtype")
        subtype = str(sub).strip() if sub else None
    except Exception:
        subtype = None

    images: dict[str, Any] = {}
    try:
        pi = attrs.get("posterImage")
        if isinstance(pi, dict):
            images["poster"] = pi.get("original") or pi.get("large") or pi.get("medium") or pi.get("small")
    except Exception:
        pass
    try:
        ci = attrs.get("coverImage")
        if isinstance(ci, dict):
            images["backdrop"] = ci.get("original") or ci.get("large") or ci.get("medium") or ci.get("small")
    except Exception:
        pass

    return {
        "provider": "kitsu",
        "provider_id": kid,
        "kitsu_id": kid,
        "title": title,
        "series_title": title,
        "synopsis": synopsis,
        "images": images,
        "episodes_count": episodes_count,
        "status": status,
        "anime_type": subtype,
        "media_type": "anime",
        "raw": res,
    }


def _normalize_jikan(res: dict) -> dict:
    if not res:
        return {}
    provider_id = None
    title = ''
    synopsis = ''
    genres = []
    images = {}
    url = None
    titles = []
    title_english = None
    title_japanese = None
    title_synonyms = []
    anime_type = None
    source = None
    episodes_count = None
    status = None
    airing = None
    aired = None
    duration = None
    rating = None
    background = None
    season = None
    year = None
    broadcast = None
    explicit_genres = []
    themes = []
    demographics = []
    relations = []
    theme_songs = None
    studios = []
    producers = []
    licensors = []
    score = None
    runtime = None
    try:
        provider_id = str(res.get('mal_id') or res.get('id') or '')
    except Exception:
        provider_id = ''
    try:
        title = res.get('title') or res.get('title_english') or res.get('title_japanese') or ''
    except Exception:
        title = ''
    try:
        synopsis = res.get('synopsis') or res.get('overview') or ''
    except Exception:
        synopsis = ''
    # Clean synopsis only for Jikan: strip trailing source/credits and bad attributions.
    try:
        from ..utils.utils import limpiar_traduccion  # local import to avoid import-order issues
        synopsis = limpiar_traduccion(synopsis, silent=True)
    except Exception:
        pass
    try:
        url = res.get('url') or None
    except Exception:
        url = None
    try:
        tts = res.get('titles') or []
        if isinstance(tts, list):
            titles = [x for x in tts if isinstance(x, dict) and x.get('title')]
    except Exception:
        titles = []
    try:
        te = res.get('title_english')
        title_english = str(te) if te else None
    except Exception:
        title_english = None
    try:
        tj = res.get('title_japanese')
        title_japanese = str(tj) if tj else None
    except Exception:
        title_japanese = None
    try:
        syns = res.get('title_synonyms') or []
        if isinstance(syns, list):
            title_synonyms = [str(x) for x in syns if x]
    except Exception:
        title_synonyms = []
    try:
        if res.get('type'):
            anime_type = str(res.get('type'))
    except Exception:
        anime_type = None
    try:
        if res.get('source'):
            source = str(res.get('source'))
    except Exception:
        source = None
    try:
        genres = [g.get('name') if isinstance(g, dict) else str(g) for g in (res.get('genres') or [])]
    except Exception:
        genres = []
    try:
        img = res.get('images')
        if isinstance(img, dict) and img:
            # Keep provider shape (jpg/webp with image_url/large_image_url) for the UI.
            images = img
        else:
            images = {}
    except Exception:
        images = {}
    try:
        s = res.get('score')
        if s is not None:
            score = float(s)
    except Exception:
        score = None
    try:
        if res.get('status'):
            status = str(res.get('status'))
    except Exception:
        status = None
    try:
        if res.get('episodes') is not None:
            ev = res.get('episodes')
            try:
                episodes_count = int(ev)  # type: ignore[arg-type]
            except Exception:
                episodes_count = None
    except Exception:
        episodes_count = None
    try:
        if res.get('airing') is not None:
            airing = bool(res.get('airing'))
    except Exception:
        airing = None
    try:
        a = res.get('aired')
        aired = a if isinstance(a, dict) else None
    except Exception:
        aired = None
    try:
        d = res.get('duration')
        duration = str(d) if d else None
    except Exception:
        duration = None
    try:
        r = res.get('rating')
        rating = str(r) if r else None
    except Exception:
        rating = None
    try:
        b = res.get('background')
        background = str(b) if b else None
    except Exception:
        background = None
    try:
        if res.get('season'):
            season = str(res.get('season'))
    except Exception:
        season = None
    try:
        if res.get('year') is not None:
            yv = res.get('year')
            try:
                year = int(yv)  # type: ignore[arg-type]
            except Exception:
                year = None
    except Exception:
        year = None
    try:
        br = res.get('broadcast')
        broadcast = br if isinstance(br, dict) else None
    except Exception:
        broadcast = None
    try:
        eg = res.get('explicit_genres') or []
        if isinstance(eg, list):
            explicit_genres = [x.get('name') if isinstance(x, dict) else str(x) for x in eg if x]
    except Exception:
        explicit_genres = []
    try:
        th = res.get('themes') or []
        if isinstance(th, list):
            themes = [x.get('name') if isinstance(x, dict) else str(x) for x in th if x]
    except Exception:
        themes = []
    try:
        demo = res.get('demographics') or []
        if isinstance(demo, list):
            demographics = [x.get('name') if isinstance(x, dict) else str(x) for x in demo if x]
    except Exception:
        demographics = []
    try:
        rels = res.get('relations') or []
        if isinstance(rels, list):
            for rel in rels:
                if not isinstance(rel, dict):
                    continue
                rel_name = rel.get('relation')
                entries = []
                ent = rel.get('entry') or []
                if isinstance(ent, list):
                    for e in ent:
                        if not isinstance(e, dict):
                            continue
                        entries.append({
                            'mal_id': e.get('mal_id'),
                            'type': e.get('type'),
                            'name': e.get('name'),
                            'url': e.get('url'),
                        })
                relations.append({'relation': rel_name, 'entry': entries})
    except Exception:
        relations = []
    try:
        thm = res.get('theme')
        if isinstance(thm, dict):
            ops = thm.get('openings') if isinstance(thm.get('openings'), list) else []
            ens = thm.get('endings') if isinstance(thm.get('endings'), list) else []
            # Avoid bloating the DB; keep a reasonable sample.
            theme_songs = {
                'openings': [str(x) for x in (ops or []) if x][:20],
                'endings': [str(x) for x in (ens or []) if x][:20],
            }
    except Exception:
        theme_songs = None
    try:
        st = res.get('studios') or []
        if isinstance(st, list):
            studios = [x.get('name') if isinstance(x, dict) else str(x) for x in st if x]
    except Exception:
        studios = []
    try:
        pr = res.get('producers') or []
        if isinstance(pr, list):
            producers = [x.get('name') if isinstance(x, dict) else str(x) for x in pr if x]
    except Exception:
        producers = []
    try:
        lc = res.get('licensors') or []
        if isinstance(lc, list):
            licensors = [x.get('name') if isinstance(x, dict) else str(x) for x in lc if x]
    except Exception:
        licensors = []
    try:
        # Jikan duration is often like "24 min per ep"
        dur = res.get('duration')
        if isinstance(dur, str):
            import re as _re
            m = _re.search(r'(\d+)\s*min', dur.lower())
            if m:
                runtime = int(m.group(1))
    except Exception:
        runtime = None
    # Map Jikan type to app media_type (movie vs series). Keep original in `anime_type`.
    norm_media_type = 'series'
    try:
        if isinstance(anime_type, str) and anime_type.strip().lower() == 'movie':
            norm_media_type = 'movie'
    except Exception:
        norm_media_type = 'series'
    return {
        'provider': 'jikan',
        'provider_id': provider_id,
        'mal_id': provider_id,
        'url': url,
        'titles': titles,
        'title': title,
        'title_english': title_english,
        'title_japanese': title_japanese,
        'title_synonyms': title_synonyms,
        'anime_type': anime_type,
        'source': source,
        'synopsis': synopsis,
        'background': background,
        'genres': genres,
        'explicit_genres': explicit_genres,
        'themes': themes,
        'demographics': demographics,
        'relations': relations,
        'theme': theme_songs,
        'aired': aired,
        'broadcast': broadcast,
        'duration': duration,
        'rating': rating,
        'content_rating': rating,
        'images': images,
        'score': score,
        'status': status,
        'episodes_count': episodes_count,
        'season': season,
        'year': year,
        'runtime': runtime,
        'studios': studios,
        'producers': producers,
        'licensors': licensors,
        'media_type': norm_media_type,
        'raw': res,
    }


def _normalize_result(provider_name: str, res: dict) -> dict:
    if not res:
        return {}
    if provider_name == 'tmdb' or (isinstance(res, dict) and res.get('provider') == 'tmdb'):
        return _normalize_tmdb(res)
    if provider_name == "kitsu":
        return _normalize_kitsu(res)
    return _normalize_jikan(res)


_GENERIC_EPISODE_TITLE_RX = re.compile(r"^(?:episode|ep)\s*#?\s*\d+\s*$", re.IGNORECASE)


def _is_generic_episode_title(title: Any) -> bool:
    try:
        t = str(title or "").strip()
    except Exception:
        t = ""
    if not t:
        return True
    return bool(_GENERIC_EPISODE_TITLE_RX.match(t))


def _episodes_need_jikan_fallback(episodes: list[dict]) -> bool:
    if not isinstance(episodes, list) or not episodes:
        return True
    total = 0
    good_titles = 0
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        total += 1
        title = ep.get("title") or ep.get("title_romanji") or ep.get("title_japanese")
        if not _is_generic_episode_title(title):
            good_titles += 1
    if total <= 0:
        return True
    # Trigger fallback when most titles are generic placeholders.
    return good_titles < max(2, int(total * 0.55))


def _kitsu_episode_issue_numbers(episodes: list[dict]) -> set[int]:
    """Return episode numbers that look low-quality and should be patched from Jikan."""
    issues: set[int] = set()
    if not isinstance(episodes, list) or not episodes:
        return issues

    title_counts: dict[str, int] = {}
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        t = str(ep.get("title") or "").strip().lower()
        if not t:
            continue
        title_counts[t] = int(title_counts.get(t, 0)) + 1

    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        try:
            ep_no_raw = ep.get("episode_number")
            ep_no = int(ep_no_raw) if ep_no_raw is not None else None
        except Exception:
            ep_no = None
        if ep_no is None or ep_no <= 0:
            continue

        title = str(ep.get("title") or "").strip()
        if _is_generic_episode_title(title):
            issues.add(ep_no)
            continue

        t_key = title.lower()
        # Kitsu sometimes returns repeated compact placeholders (e.g. "YuushaParty").
        if title_counts.get(t_key, 0) >= 2 and (" " not in title and ":" not in title and "-" not in title):
            issues.add(ep_no)
            continue

    return issues


def _build_jikan_episode_payload(raw_eps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for idx, ep in enumerate(raw_eps, start=1):
        if not isinstance(ep, dict):
            continue
        title = ep.get("title")
        aired = ep.get("aired")
        out.append(
            {
                "episode_number": idx,
                "title": str(title).strip() if title else f"Episode {idx}",
                "overview": None,
                "aired": aired,
                "filler": ep.get("filler"),
                "recap": ep.get("recap"),
                "url": ep.get("url") or ep.get("forum_url"),
                "id": ep.get("mal_id") or ep.get("id"),
            }
        )
    return out


def _merge_kitsu_jikan_episodes(kitsu_eps: list[dict], jikan_eps: list[dict], issue_numbers: set[int]) -> tuple[list[dict], int]:
    """Patch Kitsu episodes with Jikan titles for the detected problematic episodes."""
    if not isinstance(kitsu_eps, list) or not isinstance(jikan_eps, list):
        return kitsu_eps, 0

    jikan_by_no: dict[int, dict] = {}
    for ep in jikan_eps:
        if not isinstance(ep, dict):
            continue
        try:
            no_raw = ep.get("episode_number")
            no = int(no_raw) if no_raw is not None else None
        except Exception:
            no = None
        if no is None or no <= 0:
            continue
        jikan_by_no[no] = ep

    merged: list[dict] = []
    patched = 0
    patched_numbers: set[int] = set()
    for ep in kitsu_eps:
        if not isinstance(ep, dict):
            continue
        row = dict(ep)
        try:
            no_raw = row.get("episode_number")
            no = int(no_raw) if no_raw is not None else None
        except Exception:
            no = None

        if no is not None and no in jikan_by_no:
            jep = jikan_by_no[no]
            new_title = str(jep.get("title") or "").strip()
            cur_title = str(row.get("title") or "").strip()
            if no in issue_numbers and new_title and not _is_generic_episode_title(new_title):
                row["title"] = new_title
                if no not in patched_numbers:
                    patched_numbers.add(no)
                    patched += 1

            if not row.get("overview") and isinstance(jep.get("overview"), str) and str(jep.get("overview") or "").strip():
                row["overview"] = str(jep.get("overview") or "").strip()
            if not row.get("aired") and jep.get("aired"):
                row["aired"] = jep.get("aired")

            # If current title is empty/generic and Jikan has one, patch even without issue marker.
            if (not cur_title or _is_generic_episode_title(cur_title)) and new_title and not _is_generic_episode_title(new_title):
                row["title"] = new_title
                if no not in patched_numbers:
                    patched_numbers.add(no)
                    patched += 1

        merged.append(row)

    return merged, patched


def _episode_number_from_payload(ep: dict, fallback: int | None = None) -> int | None:
    if not isinstance(ep, dict):
        return fallback
    for key in ("episode_number", "number", "episode", "ep"):
        try:
            value = ep.get(key)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return fallback


def _is_low_quality_episode_title(value: Any) -> bool:
    try:
        text = str(value or "").strip()
    except Exception:
        return True
    if not text:
        return True
    low = text.lower()
    if re.fullmatch(r"\d{1,4}", low):
        return True
    if re.fullmatch(r"(?:e|ep|episode|episodio|cap(?:itulo)?)\.?\s*#?\s*\d{1,4}", low):
        return True
    if _is_generic_episode_title(text):
        return True
    return False


def _merge_episode_supplement(primary_eps: list[dict] | None, supplement_eps: list[dict] | None) -> tuple[list[dict], int]:
    if not isinstance(supplement_eps, list) or not supplement_eps:
        return (primary_eps if isinstance(primary_eps, list) else []), 0
    if not isinstance(primary_eps, list) or not primary_eps:
        out = []
        for idx, ep in enumerate(supplement_eps, start=1):
            if isinstance(ep, dict):
                row = dict(ep)
                row.setdefault("episode_number", _episode_number_from_payload(row, idx) or idx)
                out.append(row)
        return out, len(out)

    supp_by_no: dict[int, dict] = {}
    for idx, ep in enumerate(supplement_eps, start=1):
        if not isinstance(ep, dict):
            continue
        no = _episode_number_from_payload(ep, idx)
        if no is not None:
            supp_by_no[int(no)] = ep

    changed = 0
    out: list[dict] = []
    for idx, ep in enumerate(primary_eps, start=1):
        if not isinstance(ep, dict):
            continue
        row = dict(ep)
        no = _episode_number_from_payload(row, idx)
        supp = supp_by_no.get(int(no)) if no is not None else None
        if isinstance(supp, dict):
            title = supp.get("title") or supp.get("name") or supp.get("canonicalTitle")
            if isinstance(title, str) and title.strip() and _is_low_quality_episode_title(row.get("title")):
                row["title"] = title.strip()
                changed += 1
            overview = supp.get("overview") or supp.get("synopsis")
            if isinstance(overview, str) and overview.strip() and not str(row.get("overview") or row.get("synopsis") or "").strip():
                row["overview"] = overview.strip()
                changed += 1
            still = supp.get("still_path") or supp.get("thumbnail")
            if isinstance(still, str) and still.strip() and not str(row.get("still_path") or "").strip():
                row["still_path"] = still.strip()
                changed += 1
            if supp.get("aired") and not row.get("aired"):
                row["aired"] = supp.get("aired")
                changed += 1
            row.setdefault("_supplemented_from", "kitsu")
        out.append(row)
    return out, changed


def _supplement_jikan_with_kitsu(media_item: MediaItem, normalized: dict, media_type: str | None) -> dict:
    """Complement a Jikan payload with Kitsu images/episodes without changing identity."""
    try:
        if not isinstance(normalized, dict):
            return normalized
        if str(normalized.get("provider") or "").strip().lower() != "jikan":
            return normalized
        if kitsu is None:
            return normalized
        enabled = os.environ.get("ARCANEA_JIKAN_KITSU_SUPPLEMENT")
        if enabled is not None and str(enabled).strip().lower() in ("0", "false", "no", "off"):
            return normalized
    except Exception:
        return normalized

    anchors: list[str] = []
    for key in ("title_english", "title", "title_japanese", "series_title"):
        try:
            value = normalized.get(key)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip() and value.strip() not in anchors:
            anchors.append(value.strip())
    try:
        syns = normalized.get("title_synonyms") or []
        if isinstance(syns, list):
            for item in syns:
                if isinstance(item, str) and item.strip() and item.strip() not in anchors:
                    anchors.append(item.strip())
    except Exception:
        pass
    try:
        local_title = getattr(media_item, "base_title", None) or getattr(media_item, "title", None)
        if isinstance(local_title, str) and local_title.strip() and local_title.strip() not in anchors:
            anchors.append(local_title.strip())
    except Exception:
        pass
    if not anchors:
        return normalized

    try:
        min_conf = float(os.environ.get("ARCANEA_JIKAN_KITSU_SUPPLEMENT_MIN_CONF", "0.86") or 0.86)
    except Exception:
        min_conf = 0.86

    match = None
    try:
        mal_id = (
            normalized.get("mal_id")
            or normalized.get("provider_id")
            or getattr(media_item, "mal_id", None)
            or getattr(media_item, "jikan_anchor_mal_id", None)
        )
    except Exception:
        mal_id = None
    if mal_id and hasattr(kitsu, "fetch_by_mal_id"):
        try:
            candidate = kitsu.fetch_by_mal_id(str(mal_id))  # type: ignore[attr-defined]
        except Exception:
            candidate = None
        if candidate:
            # An explicit MAL->Kitsu mapping is stronger than title numerology.
            # This avoids false rejects for franchise numbers such as "5-toubun".
            match = candidate

    for anchor in anchors[:8]:
        if match:
            break
        try:
            if hasattr(kitsu, "best_match"):
                candidate = kitsu.best_match(anchor, min_conf=min_conf)  # type: ignore[attr-defined]
            else:
                rows = kitsu.search(anchor) if hasattr(kitsu, "search") else None  # type: ignore[attr-defined]
                candidate = rows[0] if isinstance(rows, list) and rows else None
        except Exception:
            candidate = None
        if candidate and _kitsu_match_allowed_for_item(candidate, media_item):
            match = candidate
            break
    if not match:
        return normalized

    try:
        kitsu_norm = _normalize_kitsu(match)
    except Exception:
        kitsu_norm = {}
    if not kitsu_norm:
        return normalized
    try:
        expected_count = normalized.get("episodes_count")
        expected_count = int(expected_count) if expected_count is not None else None
    except Exception:
        expected_count = None
    try:
        candidate_count = kitsu_norm.get("episodes_count")
        candidate_count = int(candidate_count) if candidate_count is not None else None
    except Exception:
        candidate_count = None
    if expected_count and candidate_count and expected_count != candidate_count:
        # Similar franchise titles can resolve to the wrong Kitsu season/special.
        # Do not supplement when episode cardinality disagrees with the exact MAL payload.
        try:
            logger.info(
                "Kitsu supplement rejected by episode count media_item=%s mal=%s kitsu=%s expected=%s candidate=%s",
                getattr(media_item, "id", None),
                normalized.get("provider_id") or normalized.get("mal_id"),
                kitsu_norm.get("provider_id") or kitsu_norm.get("kitsu_id"),
                expected_count,
                candidate_count,
            )
        except Exception:
            pass
        return normalized
    try:
        _attach_episodes("kitsu", kitsu_norm, media_type or normalized.get("media_type") or "anime", media_item)
    except Exception:
        pass

    out = dict(normalized)
    supplements = out.get("_supplements")
    if not isinstance(supplements, dict):
        supplements = {}
    supplements["kitsu"] = {
        "provider_id": kitsu_norm.get("provider_id") or kitsu_norm.get("kitsu_id"),
        "fields": [],
    }

    images = out.get("images")
    if not isinstance(images, dict):
        images = {}
    kitsu_images = kitsu_norm.get("images") if isinstance(kitsu_norm, dict) else None
    if isinstance(kitsu_images, dict):
        try:
            poster = _pick_best_image_url(kitsu_images, kind="poster")
            if poster and not _pick_best_image_url(images, kind="poster"):
                images["poster"] = poster
                supplements["kitsu"]["fields"].append("poster")
        except Exception:
            pass
        try:
            backdrop = _pick_best_image_url(kitsu_images, kind="backdrop")
            if backdrop and not _pick_best_image_url(images, kind="backdrop"):
                images["backdrop"] = backdrop
                supplements["kitsu"]["fields"].append("backdrop")
        except Exception:
            pass
    out["images"] = images

    primary_eps = out.get("episodes")
    supp_eps = kitsu_norm.get("episodes") if isinstance(kitsu_norm, dict) else None
    merged_eps, changed_eps = _merge_episode_supplement(
        primary_eps if isinstance(primary_eps, list) else None,
        supp_eps if isinstance(supp_eps, list) else None,
    )
    if changed_eps > 0:
        out["episodes"] = merged_eps
        out["_episodes_from"] = "kitsu_supplement"
        supplements["kitsu"]["fields"].append("episodes")

    syn = str(out.get("synopsis") or "").strip()
    ksyn = str(kitsu_norm.get("synopsis") or "").strip()
    if ksyn and (not syn or len(ksyn) > len(syn) * 1.4):
        out.setdefault("synopsis_jikan", syn)
        out["synopsis"] = ksyn
        supplements["kitsu"]["fields"].append("synopsis")

    out["_supplements"] = supplements
    try:
        raw = out.get("raw")
        if isinstance(raw, dict):
            raw["_kitsu_supplement"] = kitsu_norm
    except Exception:
        pass
    return out


def _kitsu_episode_fallback_to_jikan(media_item: MediaItem, normalized: dict, kitsu_episodes: list[dict]) -> list[dict] | None:
    try:
        if jikan is None or not hasattr(jikan, "buscar_anime_por_titulo_jikan") or not hasattr(jikan, "obtener_episodios"):
            return None
    except Exception:
        return None

    queries: list[str] = []
    for raw_q in (
        normalized.get("title"),
        normalized.get("series_title"),
        getattr(media_item, "title", None),
        getattr(media_item, "title_en", None),
        getattr(media_item, "base_title", None),
    ):
        q = str(raw_q or "").strip()
        if q and q.lower() not in (x.lower() for x in queries):
            queries.append(q)
    try:
        cp = str(getattr(media_item, "canonical_path", "") or "").strip()
        if cp:
            bn = os.path.basename(cp).strip()
            if bn and bn.lower() not in (x.lower() for x in queries):
                queries.append(bn)
    except Exception:
        pass

    expected_count = None
    try:
        ec = normalized.get("episodes_count")
        expected_count = int(ec) if ec is not None else None
    except Exception:
        expected_count = None

    local_season_hint = None
    try:
        sn = normalized.get("season_number")
        local_season_hint = int(sn) if sn is not None else None
    except Exception:
        local_season_hint = None
    if local_season_hint is None:
        try:
            local_season_hint = _season_hint_for_media_item(media_item)
        except Exception:
            local_season_hint = None

    def _norm_match_text(v: Any) -> str:
        try:
            s = str(v or "").strip().lower()
        except Exception:
            s = ""
        if not s:
            return ""
        s = re.sub(r"[^a-z0-9\s]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _candidate_similarity(cand: dict, all_queries: list[str]) -> tuple[float, str]:
        import difflib

        cand_titles: list[str] = []
        for key in ("title", "title_english", "title_japanese"):
            v = cand.get(key)
            if isinstance(v, str) and v.strip():
                cand_titles.append(v.strip())
        syns = cand.get("title_synonyms")
        if isinstance(syns, list):
            for s in syns:
                if isinstance(s, str) and s.strip():
                    cand_titles.append(s.strip())

        best = 0.0
        best_title = ""
        for qv in all_queries:
            qn = _norm_match_text(qv)
            if not qn:
                continue
            for cv in cand_titles:
                cn = _norm_match_text(cv)
                if not cn:
                    continue
                score = difflib.SequenceMatcher(None, qn, cn).ratio()
                if score > best:
                    best = float(score)
                    best_title = cv
        return best, best_title

    seen_mal_ids: set[int] = set()
    best_payload: list[dict] | None = None
    best_score = -1.0

    for q in queries:
        candidates: list[dict] = []
        try:
            cand = jikan.buscar_anime_por_titulo_jikan(str(q))
        except Exception:
            cand = None
        if isinstance(cand, dict):
            candidates.append(cand)

        try:
            if hasattr(jikan, "buscar_anime_candidates"):
                extra = jikan.buscar_anime_candidates(str(q), limit_per_variant=10, overall_limit=20)
                if isinstance(extra, list):
                    candidates.extend([x for x in extra if isinstance(x, dict)])
        except Exception:
            pass

        for cand_item in candidates:
            mal_id = cand_item.get("mal_id")
            try:
                mal_id_int = int(mal_id) if mal_id is not None else None
            except Exception:
                mal_id_int = None
            if mal_id_int is None or mal_id_int <= 0:
                continue
            if mal_id_int in seen_mal_ids:
                continue
            seen_mal_ids.add(mal_id_int)

            try:
                raw_j = jikan.obtener_episodios(str(mal_id_int))
            except Exception:
                raw_j = None
            if not isinstance(raw_j, list) or not raw_j:
                continue
            payload = _build_jikan_episode_payload(raw_j)
            if not payload:
                continue

            # Guardrail: avoid replacing with a clearly wrong episode count.
            if expected_count and expected_count > 0:
                if abs(len(payload) - int(expected_count)) > max(3, int(expected_count * 0.25)):
                    continue

            sim_score, sim_title = _candidate_similarity(cand_item, queries)
            # Keep Jikan fallback strict to avoid cross-title contamination.
            if sim_score < 0.72:
                continue

            cand_hint = _season_hint_from_title(sim_title or cand_item.get("title"))
            if local_season_hint and local_season_hint > 1:
                if cand_hint != local_season_hint:
                    continue

            good_titles = 0
            for ep in payload:
                if isinstance(ep, dict) and not _is_generic_episode_title(ep.get("title")):
                    good_titles += 1
            score = float(good_titles)
            score += float(sim_score) * 4.0
            if expected_count and expected_count > 0:
                score -= float(abs(len(payload) - int(expected_count))) * 0.45
            if cand_hint and cand_hint > 1 and not local_season_hint:
                score -= 1.5

            if score > best_score:
                best_score = score
                best_payload = payload

            try:
                logger.info(
                    "Kitsu episode fallback candidate from Jikan media_item=%s query=%s mal_id=%s (kitsu_eps=%s jikan_eps=%s sim=%.3f score=%.2f)",
                    getattr(media_item, "id", None),
                    q,
                    mal_id_int,
                    len(kitsu_episodes or []),
                    len(payload),
                    sim_score,
                    score,
                )
            except Exception:
                pass

    if best_payload:
        return best_payload
    return None


def _attach_episodes(provider_name: str, normalized: dict, media_type: str | None, media_item: MediaItem) -> None:
    """Attach an episode listing (best-effort) to the normalized payload.

    Stored inside MediaMetadata.data JSON; no DB schema change required.
    """
    try:
        if not isinstance(normalized, dict) or normalized.get('episodes') is not None:
            return

        mt = (media_type or normalized.get('media_type') or '').strip().lower()
        anime_type_l = str(normalized.get('anime_type') or '').strip().lower()
        is_anime_movie = (mt == 'anime' and anime_type_l == 'movie')
        is_series = (mt in ('series', 'tv', 'show')) or (mt == 'anime' and not is_anime_movie)
        # Be conservative for TMDB: avoid fetching episodes for movies.
        if provider_name == 'tmdb' and not is_series:
            return
        # For Jikan: only fetch episodes for series-like items (avoid movie/anime movies).
        if provider_name == 'jikan' and not is_series:
            return
        # For Kitsu: only fetch episodes for series-like items (avoid anime movies).
        if provider_name == "kitsu" and not is_series:
            return
        if provider_name == 'jikan':
            try:
                v = get("jikan_fetch_episodes")
            except Exception:
                v = None
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("0", "false", "no", "off"):
                    return
                if s in ("1", "true", "yes", "on"):
                    v = True
            if v is False:
                return

        if provider_name == 'jikan' and jikan is not None and hasattr(jikan, 'obtener_episodios'):
            mal_id = None
            try:
                mal_id = getattr(media_item, 'mal_id', None) or normalized.get('provider_id')
            except Exception:
                mal_id = normalized.get('provider_id')
            if not mal_id:
                return
            raw_eps = jikan.obtener_episodios(str(mal_id))
            if not raw_eps:
                return
            # Optional: fetch per-episode detail for synopsis (can be slow / rate-limited).
            fetch_details = False
            details_limit = 0
            try:
                vd = get("jikan_fetch_episode_details")
            except Exception:
                vd = None
            if isinstance(vd, str):
                s = vd.strip().lower()
                if s in ("1", "true", "yes", "on"):
                    fetch_details = True
                elif s in ("0", "false", "no", "off"):
                    fetch_details = False
            elif vd is True:
                fetch_details = True
            try:
                lim_raw = get("jikan_episode_details_limit")
            except Exception:
                lim_raw = None
            try:
                if lim_raw is None:
                    details_limit = 60
                elif isinstance(lim_raw, (int, float)):
                    details_limit = int(lim_raw)
                elif isinstance(lim_raw, str) and lim_raw.strip():
                    details_limit = int(lim_raw.strip())
                else:
                    details_limit = 60
            except Exception:
                details_limit = 60
            if details_limit < 0:
                details_limit = 0

            episodes = []
            for idx, ep in enumerate(raw_eps, start=1):
                if not isinstance(ep, dict):
                    continue
                overview = None
                if fetch_details and idx <= details_limit and hasattr(jikan, "obtener_episodio_detalle"):
                    try:
                        det = jikan.obtener_episodio_detalle(str(mal_id), int(idx))
                    except Exception:
                        det = None
                    if isinstance(det, dict):
                        try:
                            overview = det.get("synopsis") or det.get("overview") or None
                        except Exception:
                            overview = None
                episodes.append({
                    'episode_number': idx,
                    'title': ep.get('title') or ep.get('title_romanji') or ep.get('title_japanese') or '',
                    'overview': overview,
                    'aired': ep.get('aired'),
                    'filler': ep.get('filler'),
                    'recap': ep.get('recap'),
                    'url': ep.get('url') or ep.get('forum_url'),
                    'id': ep.get('mal_id') or ep.get('id'),
                })
            normalized['episodes'] = episodes
            return

        if provider_name == "kitsu" and kitsu is not None and hasattr(kitsu, "obtener_episodios"):
            kitsu_id = None
            try:
                kitsu_id = normalized.get("kitsu_id") or normalized.get("provider_id") or normalized.get("id")
            except Exception:
                kitsu_id = normalized.get("provider_id")
            if not kitsu_id:
                return
            try:
                raw_eps = kitsu.obtener_episodios(str(kitsu_id))
            except Exception:
                raw_eps = None
            if not isinstance(raw_eps, list) or not raw_eps:
                return

            episodes = []
            for ep in raw_eps:
                if not isinstance(ep, dict):
                    continue
                attrs_raw = ep.get("attributes")
                attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
                try:
                    num = attrs.get("number")
                    ep_no = int(num) if num is not None else None
                except Exception:
                    ep_no = None
                if ep_no is None:
                    continue
                title = ""
                try:
                    ct = attrs.get("canonicalTitle")
                    if isinstance(ct, str) and ct.strip():
                        title = ct.strip()
                except Exception:
                    title = ""
                overview = None
                try:
                    syn = attrs.get("synopsis")
                    overview = syn.strip() if isinstance(syn, str) and syn.strip() else None
                except Exception:
                    overview = None
                aired = None
                try:
                    ad = attrs.get("airdate")
                    aired = str(ad).strip() if ad else None
                except Exception:
                    aired = None
                still = None
                try:
                    th = attrs.get("thumbnail")
                    if isinstance(th, dict):
                        still = th.get("original") or th.get("large") or th.get("medium") or th.get("small")
                except Exception:
                    still = None
                episodes.append(
                    {
                        "episode_number": ep_no,
                        "title": title,
                        "overview": overview,
                        "aired": aired,
                        "still_path": still,
                        "id": ep.get("id"),
                    }
                )
            if episodes:
                normalized["episodes"] = episodes
                try:
                    needs_full_fallback = _episodes_need_jikan_fallback(episodes)
                except Exception:
                    needs_full_fallback = False
                try:
                    issue_numbers = _kitsu_episode_issue_numbers(episodes)
                except Exception:
                    issue_numbers = set()
                if needs_full_fallback or issue_numbers:
                    try:
                        jikan_eps = _kitsu_episode_fallback_to_jikan(media_item, normalized, episodes)
                    except Exception:
                        jikan_eps = None
                    if jikan_eps:
                        if needs_full_fallback:
                            normalized["episodes"] = jikan_eps
                            normalized["_episodes_from"] = "jikan_fallback"
                        else:
                            merged_eps, patched_count = _merge_kitsu_jikan_episodes(episodes, jikan_eps, issue_numbers)
                            if patched_count > 0:
                                normalized["episodes"] = merged_eps
                                normalized["_episodes_from"] = "jikan_fallback"
                                try:
                                    logger.info(
                                        "Kitsu episode patch from Jikan media_item=%s patched=%s issues=%s",
                                        getattr(media_item, "id", None),
                                        patched_count,
                                        sorted(issue_numbers),
                                    )
                                except Exception:
                                    pass
            return

        if provider_name == 'tmdb' and tmdb is not None and hasattr(tmdb, 'tmdb_get_episodes'):
            tmdb_id = normalized.get('tmdb_id') or normalized.get('provider_id')
            if not tmdb_id:
                return
            # Prefer i18n-based episodes list if available (allows per-season lists)
            try:
                raw = normalized.get('raw') or {}
                i18n = raw.get('i18n') if isinstance(raw, dict) else None
                primary = None
                if isinstance(raw, dict):
                    langs = raw.get('languages') or {}
                    if isinstance(langs, dict):
                        primary = langs.get('primary')
                primary_key = 'en' if str(primary or '').lower().startswith('en') else ('es' if str(primary or '').lower().startswith('es') else 'en')
                if isinstance(i18n, dict):
                    lang_obj = i18n.get(primary_key) or {}
                    seasons = lang_obj.get('seasons') if isinstance(lang_obj, dict) else None
                    if isinstance(seasons, dict):
                        season_number = normalized.get('season_number')
                        out = []
                        if season_number is not None:
                            s = seasons.get(str(season_number))
                            if isinstance(s, dict):
                                for ep in s.get('episodes') or []:
                                    if isinstance(ep, dict) and ep.get('title'):
                                        out.append({'title': ep.get('title')})
                        else:
                            # Flatten all seasons in order
                            def _sn_sort(x):
                                try:
                                    return int(x)
                                except Exception:
                                    return 0
                            for sn in sorted(seasons.keys(), key=_sn_sort):
                                s = seasons.get(sn)
                                if not isinstance(s, dict):
                                    continue
                                for ep in s.get('episodes') or []:
                                    if isinstance(ep, dict) and ep.get('title'):
                                        out.append({'title': ep.get('title')})
                        if out:
                            normalized['episodes'] = out
                            return
            except Exception:
                pass

            eps = tmdb.tmdb_get_episodes(str(tmdb_id))
            if eps:
                normalized['episodes'] = eps
            return
    except Exception:
        return


def _apply_jikan_episode_titles(mi_obj: MediaItem, normalized: dict, session) -> bool:
    """Persist provider episode titles into Episode rows (best-effort).

    Requires Episode rows to already exist (created from files).
    """
    try:
        if not isinstance(normalized, dict):
            return False
        try:
            prov = (normalized.get("provider") or "").strip().lower()
        except Exception:
            prov = ""
        # Allow providers that can supply episode lists; this is safe because we only
        # fill empty/generic fields and we require the SeasonItem bridge to exist.
        if prov not in ("jikan", "kitsu", "tmdb"):
            return False
        eps = normalized.get("episodes")
        if not isinstance(eps, list) or not eps:
            return False
    except Exception:
        return False

    try:
        si = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi_obj.id).first()
    except Exception:
        si = None
    if not si:
        return False

    try:
        season = session.query(Season).filter(Season.id == si.season_id).first()
    except Exception:
        season = None
    if not season:
        return False

    changed_season = False
    # Apply per-season synopsis/poster (Jikan has no "backdrop", so we fall back to poster).
    try:
        season_any = _cast(_Any, season)
        try:
            local_hint = _season_hint_for_media_item(mi_obj)
        except Exception:
            local_hint = None
        title_candidate = (
            normalized.get("title_english")
            or normalized.get("title_en")
            or normalized.get("title")
            or normalized.get("series_title")
            or None
        )
        if isinstance(title_candidate, str) and title_candidate.strip():
            cur_title_en = str(getattr(season, "title_en", "") or "").strip()
            prov_name = str(normalized.get("provider") or "").strip().lower()
            replace_title = False
            if not cur_title_en:
                replace_title = True
            elif prov_name in ("tmdb", "kitsu") and cur_title_en != title_candidate.strip():
                replace_title = True
            elif isinstance(local_hint, int) and local_hint > 1:
                replace_title = True
            elif re.search(r"(?i)\bken\d+\b", cur_title_en):
                replace_title = True
            if replace_title:
                season_any.title_en = title_candidate.strip()
                changed_season = True

        cur_syn = getattr(season, "synopsis_localized", None)
        syn = normalized.get("synopsis") or normalized.get("overview") or None
        if isinstance(syn, str) and syn.strip():
            syn_clean = syn.strip()
            try:
                if kitsu is not None and hasattr(kitsu, "clean_synopsis_text"):
                    syn_clean = kitsu.clean_synopsis_text(syn_clean)  # type: ignore[attr-defined]
            except Exception:
                syn_clean = syn.strip()
            cur_clean = str(cur_syn or "").strip()
            try:
                if cur_clean and kitsu is not None and hasattr(kitsu, "clean_synopsis_text"):
                    cur_clean = kitsu.clean_synopsis_text(cur_clean)  # type: ignore[attr-defined]
            except Exception:
                cur_clean = str(cur_syn or "").strip()
            replace_existing = bool(
                not cur_clean
                or cur_clean != str(cur_syn or "").strip()
                or (isinstance(local_hint, int) and local_hint > 1)
            )
            if syn_clean and replace_existing:
                season_any.synopsis_localized = syn_clean
                changed_season = True
            elif cur_clean and cur_clean != str(cur_syn or "").strip():
                season_any.synopsis_localized = cur_clean
                changed_season = True
        cur_poster = getattr(season, "poster_path", None)
        cur_backdrop = getattr(season, "backdrop_path", None)
        imgs = normalized.get("images") if isinstance(normalized, dict) else None
        poster = None
        try:
            poster = _pick_best_image_url(imgs, kind="poster") if imgs else None
        except Exception:
            poster = None
        if poster and not (isinstance(cur_poster, str) and cur_poster.strip()):
            season_any.poster_path = str(poster).strip()
            changed_season = True
        # Backdrop fallback: keep it populated for UI cards when episodes lack stills.
        if poster and not (isinstance(cur_backdrop, str) and cur_backdrop.strip()):
            season_any.backdrop_path = str(poster).strip()
            changed_season = True
        session.add(season)
    except Exception:
        pass

    try:
        rows = (
            session.query(Episode)
            .filter(Episode.season_id == season.id)
            .order_by(Episode.episode_number.asc().nullsfirst(), Episode.id.asc())
            .all()
        )
    except Exception:
        rows = []
    if not rows:
        return bool(changed_season)

    def _pick_title(ep: dict) -> str:
        for k in ("title", "title_romanji", "title_japanese", "name"):
            try:
                v = ep.get(k)
            except Exception:
                v = None
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _is_generic_episode_title(v: str | None) -> bool:
        s = (v or "").strip().lower()
        if not s:
            return True
        if re.fullmatch(r"(?:episode|episodio)\s*\d{1,4}", s):
            return True
        return False

    updated = bool(changed_season)
    force_title_overwrite = str(normalized.get("_episodes_from") or "").strip().lower() in (
        "jikan_fallback",
        "jikan-fallback",
        "jikan_id",
        "local_id",
    )
    for idx, row in enumerate(rows):
        try:
            ep_no = getattr(row, "episode_number", None)
        except Exception:
            ep_no = None
        pick_idx = None
        if isinstance(ep_no, int) and ep_no > 0 and ep_no <= len(eps):
            pick_idx = ep_no - 1
        elif idx < len(eps):
            pick_idx = idx
        if pick_idx is None:
            continue
        ep = eps[pick_idx]
        if not isinstance(ep, dict):
            continue
        title = _pick_title(ep)
        if not title:
            continue
        try:
            row_any = _cast(_Any, row)
            if force_title_overwrite or _is_generic_episode_title(getattr(row, "title", None)) or not getattr(row, "title", None):
                row_any.title = title
            if force_title_overwrite or _is_generic_episode_title(getattr(row, "title_localized", None)) or not getattr(row, "title_localized", None):
                row_any.title_localized = title
            if force_title_overwrite or _is_generic_episode_title(getattr(row, "title_en", None)) or not getattr(row, "title_en", None):
                row_any.title_en = title

            ov = ep.get("overview") or ep.get("synopsis") or None
            if isinstance(ov, str) and ov.strip():
                try:
                    cur_ov = getattr(row, "synopsis_localized", None)
                except Exception:
                    cur_ov = None
                if not (isinstance(cur_ov, str) and cur_ov.strip()):
                    row_any.synopsis_localized = ov.strip()

            # Episode still (thumbnail) if available from provider (e.g. TMDB/Kitsu).
            try:
                cur_still = getattr(row, "still_path", None)
            except Exception:
                cur_still = None
            if not (isinstance(cur_still, str) and cur_still.strip()):
                try:
                    still_url = ep.get("still_path")
                except Exception:
                    still_url = None
                if isinstance(still_url, str) and still_url.strip():
                    try:
                        _download_episode_still(session, row, still_url.strip())
                    except Exception:
                        pass

            session.add(row_any)
            updated = True
        except Exception:
            continue
    if updated:
        try:
            session.commit()
        except Exception:
            session.rollback()
            return False
    return updated


def _attach_tmdb_i18n(normalized: dict, media_type: str | None) -> None:
    """Attach TMDB i18n payload (ES/EN) into normalized['raw'] (best-effort).

    NOTE: The legacy normalization/i18n subsystem was moved out of the main backend
    flow. We keep this function as a no-op to avoid touching the many call sites.
    """
    return
    try:
        if not isinstance(normalized, dict):
            return
        if (normalized.get('provider') or '').strip().lower() != 'tmdb':
            return

        mt = (media_type or normalized.get('media_type') or '').strip().lower()
        is_series = mt in ('series', 'tv', 'show')
        if not is_series:
            return

        tmdb_id = normalized.get('tmdb_id') or normalized.get('provider_id')
        if not tmdb_id:
            return

        season_number = None
        try:
            sn_val = normalized.get('season_number')
            season_number = int(sn_val) if sn_val is not None else None
        except Exception:
            season_number = None

        try:
            lang = get('metadata_language') or 'en-US'
        except Exception:
            lang = 'en-US'
        primary = 'es' if str(lang).lower().startswith('es') else 'en'

        include_all = True if season_number is None else False
        sns = [season_number] if season_number is not None else None

        # (disabled)
        return
        cleaned = fetch_tmdb_tv_i18n_clean(str(tmdb_id), primary=primary, season_numbers=sns, include_all_seasons=include_all)
        if not isinstance(cleaned, dict):
            return

        raw = normalized.get('raw')
        if not isinstance(raw, dict):
            raw = {}
            normalized['raw'] = raw
        raw['languages'] = cleaned.get('languages')
        raw['i18n'] = cleaned.get('i18n')
        raw['modal'] = cleaned.get('modal')
    except Exception:
        return


def _tmdb_configured() -> bool:
    try:
        key = get('tmdb_api_key') or os.environ.get('TMDB_API_KEY')
        token = get('tmdb_access_token') or os.environ.get('TMDB_ACCESS_TOKEN')
        use_v4 = bool(get('tmdb_use_v4'))
        if use_v4 and token:
            return True
        if key:
            return True
        try:
            from ..core.config import DATA_DIR
            import json as _json
            app_cfg_path = DATA_DIR / 'app_config.json'
            if app_cfg_path.exists():
                with open(app_cfg_path, 'r', encoding='utf-8') as fh:
                    ac = _json.load(fh) or {}
                md = ac.get('metadata') or {}
                if md.get('tmdb_api_key'):
                    return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _tmdb_image_candidates(image_url: str) -> list[str]:
    try:
        u = (image_url or "").strip()
    except Exception:
        u = ""
    if not u:
        return []

    def _full_url(path: str) -> str:
        p = path.strip()
        if p.startswith("http://") or p.startswith("https://"):
            return p
        if p.startswith("/t/p/"):
            return f"https://image.tmdb.org{p}"
        if p.startswith("/"):
            return f"https://image.tmdb.org/t/p/original{p}"
        return p

    # normalize to full url if needed
    full = _full_url(u)
    if "image.tmdb.org/t/p/" not in full:
        return [full]

    try:
        # split size segment
        prefix, rest = full.split("/t/p/", 1)
        if "/" not in rest:
            return [full]
        _, tail = rest.split("/", 1)
    except Exception:
        return [full]

    sizes = ["original", "w1280", "w780", "w500", "w342", "w300", "w185"]
    out = []
    for s in sizes:
        out.append(f"{prefix}/t/p/{s}/{tail}")
    # de-dup preserving order
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def _episode_still_web_path(episode_id: int, ext: str | None = None) -> tuple[str, str]:
    try:
        eid = int(episode_id)
    except Exception:
        eid = episode_id
    safe_ext = (ext or ".jpg").strip()
    if not safe_ext.startswith("."):
        safe_ext = f".{safe_ext}"
    if len(safe_ext) > 8:
        safe_ext = ".jpg"
    fn = f"still{safe_ext}"
    web_path = f"/media-assets/episodes/{eid}/{fn}"
    return fn, web_path


def _download_episode_still(session, episode: Episode, image_url: str | None) -> str | None:
    try:
        if not image_url:
            return None
        eid = getattr(episode, "id", None)
        if eid is None:
            return None
        try:
            from ..core.config import DATA_DIR
        except Exception:
            DATA_DIR = Path("data")
        ext = os.path.splitext(PurePath(str(image_url)).name)[1] or ".jpg"
        fn, web_path = _episode_still_web_path(int(eid), ext)
        assets_dir = Path(DATA_DIR) / "assets" / "episodes" / str(int(eid))
        assets_dir.mkdir(parents=True, exist_ok=True)
        local_path = str((assets_dir / fn).resolve())

        # If file already exists, just set the DB field when missing.
        if os.path.exists(local_path):
            cur = getattr(episode, "still_path", None)
            cur_s = cur.strip() if isinstance(cur, str) else ""
            if cur_s != web_path:
                try:
                    ep_any = _cast(_Any, episode)
                    ep_any.still_path = web_path
                    session.add(episode)
                    session.commit()
                except Exception:
                    session.rollback()
            return web_path

        # Respect global block for image downloads.
        try:
            if _image_download_blocked:
                return None
        except Exception:
            pass

        resp = None
        downloaded = False
        try:
            if _img_sema:
                _img_sema.acquire()
            for u in _tmdb_image_candidates(str(image_url)):
                try:
                    resp = _image_session.get(u, timeout=15, stream=True)
                    resp.raise_for_status()
                    with open(local_path, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                fh.write(chunk)
                    downloaded = True
                    break
                except Exception as e:
                    try:
                        msg = str(e)
                    except Exception:
                        msg = ""
                    if "WinError 10061" in msg:
                        try:
                            globals()["_image_download_blocked"] = True
                        except Exception:
                            pass
                        break
                    try:
                        if resp is not None:
                            resp.close()
                    except Exception:
                        pass
                    resp = None
                    continue
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass
            if _img_sema:
                _img_sema.release()
            if _IMG_DELAY_SEC > 0:
                time.sleep(_IMG_DELAY_SEC)

        if not downloaded:
            return None

        try:
            ep_any = _cast(_Any, episode)
            ep_any.still_path = web_path
            session.add(episode)
            session.commit()
        except Exception:
            session.rollback()
            return None
        return web_path
    except Exception:
        return None


def _download_image_and_record(
    session,
    media_item: MediaItem,
    image_url: str,
    source: str = 'provider',
    *,
    kind: str = "poster",
) -> str | None:
    try:
        if not image_url:
            return None
        kind0 = str(kind or "poster").strip().lower()
        if kind0 not in ("poster", "backdrop"):
            kind0 = "poster"

        try:
            from ..core.config import DATA_DIR
        except Exception:
            DATA_DIR = Path("data")
        assets_dir = Path(DATA_DIR) / "assets" / str(getattr(media_item, "id", "unknown"))
        assets_dir.mkdir(parents=True, exist_ok=True)

        ext = os.path.splitext(PurePath(image_url).name)[1] or ".jpg"
        if ext and len(ext) > 8:
            ext = ".jpg"
        fn = f"{kind0}{ext}"
        local_path = str((assets_dir / fn).resolve())
        web_path = f"/media-assets/{getattr(media_item, 'id', 'unknown')}/{fn}"

        mi_obj = media_item

        existing = None
        try:
            existing = (
                session.query(MediaImage)
                .filter(MediaImage.media_item_id == mi_obj.id, MediaImage.local_path == local_path)
                .order_by(MediaImage.id.desc())
                .first()
            )
        except Exception:
            existing = None

        def _same_source_url(existing_url: object, desired_url: str) -> bool:
            if not isinstance(existing_url, str) or not existing_url.strip():
                return False
            ex = existing_url.strip()
            des = str(desired_url or "").strip()
            if not des:
                return False
            if ex == des:
                return True
            # Consider TMDB size variants equal (original vs w780, etc).
            try:
                if ex in _tmdb_image_candidates(des):
                    return True
            except Exception:
                pass
            return False

        file_exists = os.path.exists(local_path)
        need_download = True
        if file_exists:
            # Idempotence: if we already have an image cached for the same URL, don't redownload.
            try:
                if existing is not None and _same_source_url(getattr(existing, "source_url", None), image_url):
                    need_download = False
                elif existing is None:
                    # Legacy/manual assets without MediaImage rows: keep the file.
                    need_download = False
            except Exception:
                need_download = False

        downloaded_this_call = False
        if need_download:
            tmp_path = f"{local_path}.tmp"
            # Respect global block for image downloads. If we already have a local file, keep it.
            try:
                blocked = bool(globals().get("_image_download_blocked"))
            except Exception:
                blocked = False
            if blocked and not file_exists:
                # fallback: keep remote URL if downloads are blocked
                try:
                    if kind0 == "poster":
                        cur = getattr(mi_obj, "poster_path", None)
                        cur_s = cur.strip() if isinstance(cur, str) else ""
                        if not cur_s:
                            mi_obj.poster_path = image_url  # pyright: ignore[reportAttributeAccessIssue]
                            session.add(mi_obj)
                            session.commit()
                    else:
                        cur = getattr(mi_obj, "backdrop_path", None)
                        cur_s = cur.strip() if isinstance(cur, str) else ""
                        if not cur_s:
                            mi_obj.backdrop_path = image_url  # pyright: ignore[reportAttributeAccessIssue]
                            session.add(mi_obj)
                            session.commit()
                except Exception:
                    session.rollback()
                return None

            resp = None
            downloaded = False
            used_url = None
            try:
                if _img_sema:
                    _img_sema.acquire()
                for u in _tmdb_image_candidates(str(image_url)):
                    try:
                        resp = _image_session.get(u, timeout=15, stream=True)
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=8192):
                                if chunk:
                                    fh.write(chunk)
                        downloaded = True
                        used_url = u
                        break
                    except Exception as e:
                        try:
                            msg = str(e)
                        except Exception:
                            msg = ""
                        if "WinError 10061" in msg:
                            try:
                                globals()["_image_download_blocked"] = True
                            except Exception:
                                pass
                            break
                        try:
                            if resp is not None:
                                resp.close()
                        except Exception:
                            pass
                        resp = None
                        continue
            finally:
                try:
                    if resp is not None:
                        resp.close()
                except Exception:
                    pass
                if _img_sema:
                    _img_sema.release()
                if _IMG_DELAY_SEC > 0:
                    time.sleep(_IMG_DELAY_SEC)

            if downloaded and used_url:
                try:
                    os.replace(tmp_path, local_path)
                except Exception:
                    # If atomic replace fails (e.g. file in use), fall back to best-effort overwrite.
                    try:
                        with open(tmp_path, "rb") as src_fh, open(local_path, "wb") as dst_fh:
                            dst_fh.write(src_fh.read())
                    except Exception:
                        pass
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                image_url = used_url
                file_exists = True
                downloaded_this_call = True
            else:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                if not file_exists:
                    # fallback: keep remote URL if no local download possible
                    try:
                        if kind0 == "poster":
                            cur = getattr(mi_obj, "poster_path", None)
                            cur_s = cur.strip() if isinstance(cur, str) else ""
                            if not cur_s:
                                mi_obj.poster_path = image_url  # pyright: ignore[reportAttributeAccessIssue]
                                session.add(mi_obj)
                                session.commit()
                        else:
                            cur = getattr(mi_obj, "backdrop_path", None)
                            cur_s = cur.strip() if isinstance(cur, str) else ""
                            if not cur_s:
                                mi_obj.backdrop_path = image_url  # pyright: ignore[reportAttributeAccessIssue]
                                session.add(mi_obj)
                                session.commit()
                    except Exception:
                        session.rollback()
                    return None
                # Keep existing cached image/record when refresh failed.
                try:
                    if existing is not None:
                        old_url = getattr(existing, "source_url", None)
                        if isinstance(old_url, str) and old_url.strip():
                            image_url = old_url.strip()
                except Exception:
                    pass

        # Record/update MediaImage row (best-effort). If we couldn't download but a file already exists,
        # we keep it and avoid overwriting the previous record.
        try:
            if file_exists:
                if existing is None:
                    # Only create a MediaImage row when we know we wrote the file in this call.
                    if downloaded_this_call:
                        mi_img = MediaImage(media_item_id=mi_obj.id, source=source, source_url=image_url, local_path=local_path, priority=50)
                        session.add(mi_img)
                        session.commit()
                else:
                    changed = False
                    if downloaded_this_call and not _same_source_url(getattr(existing, "source_url", None), image_url):
                        try:
                            existing.source_url = image_url
                            changed = True
                        except Exception:
                            pass
                    try:
                        cur_src = getattr(existing, "source", None)
                    except Exception:
                        cur_src = None
                    if (cur_src or "") != (source or ""):
                        try:
                            existing.source = source
                            changed = True
                        except Exception:
                            pass
                    try:
                        if getattr(existing, "priority", None) != 50:
                            existing.priority = 50
                            changed = True
                    except Exception:
                        pass
                    if changed:
                        try:
                            session.add(existing)
                            session.commit()
                        except Exception:
                            session.rollback()
        except Exception:
            session.rollback()

        # Ensure MediaItem points at the local /media-assets path when we have a local file.
        if file_exists:
            try:
                if kind0 == "poster":
                    cur = getattr(mi_obj, "poster_path", None)
                    cur_s = cur.strip() if isinstance(cur, str) else ""
                    if (not cur_s) or (not cur_s.startswith("/media-assets/")):
                        mi_obj.poster_path = web_path  # pyright: ignore[reportAttributeAccessIssue]
                        session.add(mi_obj)
                        session.commit()
                else:
                    cur = getattr(mi_obj, "backdrop_path", None)
                    cur_s = cur.strip() if isinstance(cur, str) else ""
                    if (not cur_s) or (not cur_s.startswith("/media-assets/")):
                        mi_obj.backdrop_path = web_path  # pyright: ignore[reportAttributeAccessIssue]
                        session.add(mi_obj)
                        session.commit()
            except Exception:
                session.rollback()

        return local_path if file_exists else None
    except Exception:
        return None


def _pick_best_image_url(images: object, *, kind: str = "poster") -> str | None:
    """Pick the best available image URL from a provider `images` payload.

    Supports common shapes:
    - Jikan: {"jpg": {"large_image_url": .., "image_url": .., "small_image_url": ..}, "webp": {...}}
    - TMDB (our provider wrapper): {"jpg": {"large_image_url": ..}, "backdrop": {"large_image_url": ..}}
    - TMDB raw images lists: {"posters": [{"file_path": ..}], "backdrops": [{"file_path": ..}]}
    """
    try:
        kind0 = str(kind or "poster").strip().lower()
    except Exception:
        kind0 = "poster"
    if kind0 not in ("poster", "backdrop"):
        kind0 = "poster"

    if not isinstance(images, dict):
        return None

    def _as_url(v: object) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s or None

    def _pick_from_bucket(bucket: object) -> str | None:
        if not isinstance(bucket, dict):
            return None
        for k in ("large_image_url", "image_url", "small_image_url"):
            u = _as_url(bucket.get(k))
            if u:
                return u
        return None

    if kind0 == "poster":
        for fmt in ("jpg", "webp"):
            u = _pick_from_bucket(images.get(fmt))
            if u:
                return u
        # Some TMDB payloads keep posters in a list.
        try:
            posters = images.get("posters")
            if isinstance(posters, list) and posters:
                fp = posters[0].get("file_path") if isinstance(posters[0], dict) else None
                u = _as_url(fp)
                if u:
                    return u
        except Exception:
            pass
        # Last-resort keys some payloads may use.
        for k in ("poster", "poster_url", "url"):
            u = _as_url(images.get(k))
            if u:
                return u
        return None

    # backdrop
    u = _pick_from_bucket(images.get("backdrop"))
    if u:
        return u
    try:
        backs = images.get("backdrops")
        if isinstance(backs, list) and backs:
            fp = backs[0].get("file_path") if isinstance(backs[0], dict) else None
            u = _as_url(fp)
            if u:
                return u
    except Exception:
        pass
    for k in ("backdrop", "backdrop_url"):
        u = _as_url(images.get(k))
        if u:
            return u
    return None


def _persist_metadata(session, media_item: MediaItem, normalized: dict):  # pyright: ignore[reportGeneralTypeIssues]
    from ..core.translations import upsert_translation, normalize_translation_language  # local import to avoid import cycles

    def pick_title_en(norm: dict) -> str | None:
        try:
            prov = (norm.get('provider') or '').strip().lower()
        except Exception:
            prov = ''
        try:
            if prov == 'jikan':
                te = norm.get('title_english') or None
                if isinstance(te, str) and te.strip():
                    return te.strip()
        except Exception:
            pass
        try:
            t = norm.get('title')
            if isinstance(t, str) and t.strip():
                return t.strip()
        except Exception:
            pass
        return None

    def should_localize() -> bool:
        try:
            if not bool(get('localization_in_enrichment')):
                return False
        except Exception:
            return False
        try:
            if not bool(get('tmdb_localization')):
                return False
        except Exception:
            return False
        try:
            lang = str(get('metadata_language') or '').strip().lower()
        except Exception:
            lang = ''
        # If metadata_language is English (or unset), no need to store a localized track.
        return bool(lang and not lang.startswith('en'))

    def get_localization_language() -> str | None:
        try:
            lang = str(get('metadata_language') or '').strip()
            return lang or None
        except Exception:
            return None

    def _apply_jikan_anchor(mi_obj: MediaItem, normalized: dict) -> None:
        try:
            prov = (normalized.get("provider") or "").strip().lower()
        except Exception:
            prov = ""
        if prov != "jikan":
            return
        mi_any = _cast(_Any, mi_obj)
        try:
            mal = normalized.get("mal_id") or normalized.get("provider_id") or getattr(mi_any, "mal_id", None)
        except Exception:
            mal = getattr(mi_any, "mal_id", None)
        try:
            if mal and not getattr(mi_any, "mal_id", None):
                mi_any.mal_id = str(mal)
        except Exception:
            pass
        try:
            if mal:
                mi_any.jikan_anchor_mal_id = str(mal)
        except Exception:
            pass
        try:
            jtype = normalized.get("anime_type")
            if jtype:
                mi_any.jikan_type = str(jtype)
        except Exception:
            pass

    def _commit_with_telemetry() -> None:
        try:
            task_id, _task_kind = telemetry.get_task_context()
        except Exception:
            task_id = None
        if not task_id:
            try:
                res = session.commit()
                if res is False:
                    raise RuntimeError("commit_failed")
            except IntegrityError as e:
                try:
                    telemetry.emit_log('error', f"DB IntegrityError on commit: {e}")
                    telemetry._emit('duplicate.error', {'error': str(e), 'suggested_actions': ['suggest_alternative_id','merge_records']})
                except Exception:
                    pass
                raise
            return
        with telemetry.phase("DB_Commit"):
            try:
                res = session.commit()
                if res is False:
                    raise RuntimeError("commit_failed")
            except IntegrityError as e:
                try:
                    telemetry.emit_log('error', f"DB IntegrityError on commit: {e}")
                    telemetry._emit('duplicate.error', {'error': str(e), 'suggested_actions': ['suggest_alternative_id','merge_records']})
                except Exception:
                    pass
                raise

    def merge_movie_tmdb_duplicates(mi_obj: MediaItem, tmdb_id: str | None) -> MediaItem:
        # A local movie library may legitimately contain multiple files for the
        # same TMDB title (different cuts, qualities, languages, or duplicates).
        # Do not merge them; the filesystem path is the local identity.
        return mi_obj
        if not tmdb_id:
            return mi_obj
        try:
            mt = (getattr(mi_obj, "media_type", None) or "").strip().lower()
        except Exception:
            mt = ""
        if mt not in ("movie", "film"):
            return mi_obj
        try:
            other = (
                session.query(MediaItem)
                .filter(MediaItem.tmdb_id == str(tmdb_id))
                .filter(MediaItem.id != mi_obj.id)
                .order_by(MediaItem.id.asc())
                .first()
            )
        except Exception:
            other = None
        if not other:
            return mi_obj
        try:
            count_self = session.query(FileRecord).filter(FileRecord.media_item_id == mi_obj.id).count()
        except Exception:
            count_self = 0
        try:
            count_other = session.query(FileRecord).filter(FileRecord.media_item_id == other.id).count()
        except Exception:
            count_other = 0
        keep = mi_obj if count_self >= count_other else other
        drop = other if keep is mi_obj else mi_obj
        drop_id = getattr(drop, "id", None)
        keep_id = getattr(keep, "id", None)
        if drop_id is None or keep_id is None:
            return mi_obj
        try:
            session.query(FileRecord).filter(FileRecord.media_item_id == drop_id).update(
                {FileRecord.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaMetadata).filter(MediaMetadata.media_item_id == drop_id).update(
                {MediaMetadata.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaImage).filter(MediaImage.media_item_id == drop_id).update(
                {MediaImage.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaTranslation).filter(MediaTranslation.path_id == drop_id).update(
                {MediaTranslation.path_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(ManualMapping).filter(ManualMapping.media_item_id == drop_id).update(
                {ManualMapping.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(ManualOverride).filter(ManualOverride.media_item_id == drop_id).update(
                {ManualOverride.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(UserList).filter(UserList.media_item_id == drop_id).update(
                {UserList.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(PlayHistory).filter(PlayHistory.media_item_id == drop_id).update(
                {PlayHistory.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(SeasonItem).filter(SeasonItem.media_item_id == drop_id).update(
                {SeasonItem.media_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaRelation).filter(MediaRelation.from_item_id == drop_id).update(
                {MediaRelation.from_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaRelation).filter(MediaRelation.to_item_id == drop_id).update(
                {MediaRelation.to_item_id: keep_id}, synchronize_session=False
            )
        except Exception:
            pass
        try:
            session.query(MediaItem).filter(MediaItem.id == drop_id).delete(synchronize_session=False)
        except Exception:
            pass
        return keep

    def sync_episode_localized_titles(mi_obj: MediaItem, tmdb_id: str, media_type: str | None, language: str, season_number: int | None):
        if not tmdb or not hasattr(tmdb, 'tmdb_fetch_season'):
            return
        if season_number is None or not isinstance(season_number, int):
            return
        if (media_type or '').strip().lower() not in ('series', 'tv', 'show'):
            return
        series, season = get_series_and_season_for_item(mi_obj, session)
        if not season:
            return

        def _normalize_tmdb_lang(v: str | None) -> str:
            s = str(v or "").strip().replace("_", "-")
            if not s:
                return "en-US"
            low = s.lower()
            if low == "es-419":
                return "es-MX"
            if low == "es":
                return "es-ES"
            if low == "en":
                return "en-US"
            return s

        def _build_lang_chain(primary_lang: str) -> list[str]:
            base = _normalize_tmdb_lang(primary_lang)
            chain = [base]
            if base.lower().startswith("es-") and base.lower() != "es-es":
                chain.append("es-ES")
            chain.append("en-US")
            seen = set()
            out: list[str] = []
            for x in chain:
                k = x.lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(x)
            return out

        def _norm_text(v: str | None) -> str:
            if not isinstance(v, str):
                return ""
            s = v.strip().lower()
            if not s:
                return ""
            return re.sub(r"\s+", " ", s)

        def _is_english_fallback(loc_text: str | None, en_text: str | None) -> bool:
            a = _norm_text(loc_text)
            b = _norm_text(en_text)
            if not a or not b:
                return False
            return a == b

        lang_chain = _build_lang_chain(language)
        season_data_by_lang: dict[str, dict | None] = {}
        for lkey in lang_chain:
            try:
                season_data_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                    tmdb_id, season_number=season_number, language=lkey
                )
            except Exception:
                season_data_by_lang[lkey] = None

        offset = 0

        # Localize season title using the same language chain.
        try:
            def _season_title(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                for k in ("name", "title", "season_name"):
                    v = d.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                return None

            if offset <= 0:
                if not (getattr(season, "title_localized", None) or "").strip():
                    for lkey in lang_chain:
                        tloc = _season_title(season_data_by_lang.get(lkey))
                        if lkey != "en-US":
                            ten = _season_title(season_data_by_lang.get("en-US"))
                            if _is_english_fallback(tloc, ten):
                                continue
                        if tloc:
                            season.title_localized = tloc
                            break
                if not (getattr(season, "title_en", None) or "").strip():
                    ten = _season_title(season_data_by_lang.get("en-US"))
                    if ten:
                        season.title_en = ten
                try:
                    session.add(season)
                except Exception:
                    pass
        except Exception:
            pass

        # Localize season synopsis + poster/backdrop (best-effort) using the same language chain.
        try:
            def _season_synopsis(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("synopsis") or d.get("overview") or None
                if isinstance(v, str) and v.strip():
                    return v.strip()
                return None

            def _season_poster(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("poster_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
                imgs = d.get("images") if isinstance(d, dict) else None
                try:
                    return _pick_best_image_url(imgs, kind="poster") if imgs else None
                except Exception:
                    return None

            def _season_backdrop(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("backdrop_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
                imgs = d.get("images") if isinstance(d, dict) else None
                try:
                    return _pick_best_image_url(imgs, kind="backdrop") if imgs else None
                except Exception:
                    return None

            try:
                cur_syn = getattr(season, "synopsis_localized", None)
            except Exception:
                cur_syn = None
            if not (isinstance(cur_syn, str) and cur_syn.strip()):
                en_syn = _season_synopsis(season_data_by_lang.get("en-US"))
                for lkey in lang_chain:
                    syn = _season_synopsis(season_data_by_lang.get(lkey))
                    if not syn:
                        continue
                    if lkey != "en-US" and _is_english_fallback(syn, en_syn):
                        continue
                    season_any = _cast(_Any, season)
                    season_any.synopsis_localized = syn
                    break
                if not (getattr(season, "synopsis_localized", None) or "").strip():
                    if isinstance(en_syn, str) and en_syn.strip():
                        season_any = _cast(_Any, season)
                        season_any.synopsis_localized = en_syn.strip()
                # If TMDB has no season-level overview (common for split seasons / some shows),
                # fall back to the series overview so the UI never shows an empty synopsis panel.
                if not (getattr(season, "synopsis_localized", None) or "").strip():
                    try:
                        en_ov = ""
                        try:
                            d_en = tmdb.tmdb_fetch_by_id(tmdb_id, media_type="tv", language="en-US")  # type: ignore[arg-type]
                        except Exception:
                            d_en = None
                        if isinstance(d_en, dict):
                            en_ov = str(d_en.get("synopsis") or d_en.get("overview") or "").strip()
                        for lkey in lang_chain:
                            try:
                                d_loc = tmdb.tmdb_fetch_by_id(tmdb_id, media_type="tv", language=lkey)  # type: ignore[arg-type]
                            except Exception:
                                d_loc = None
                            if not isinstance(d_loc, dict):
                                continue
                            ov = str(d_loc.get("synopsis") or d_loc.get("overview") or "").strip()
                            if not ov:
                                continue
                            if lkey != "en-US" and _is_english_fallback(ov, en_ov):
                                continue
                            season_any = _cast(_Any, season)
                            season_any.synopsis_localized = ov
                            break
                        if not (getattr(season, "synopsis_localized", None) or "").strip() and en_ov:
                            season_any = _cast(_Any, season)
                            season_any.synopsis_localized = en_ov
                    except Exception:
                        pass

            try:
                cur_poster = getattr(season, "poster_path", None)
            except Exception:
                cur_poster = None
            if not (isinstance(cur_poster, str) and cur_poster.strip()):
                poster = None
                for lkey in lang_chain:
                    poster = _season_poster(season_data_by_lang.get(lkey))
                    if poster:
                        break
                if not poster:
                    try:
                        poster = getattr(mi_obj, "poster_path", None)
                    except Exception:
                        poster = None
                if not poster:
                    try:
                        poster = getattr(series, "main_poster", None) if series else None
                    except Exception:
                        poster = None
                if isinstance(poster, str) and poster.strip():
                    season_any = _cast(_Any, season)
                    season_any.poster_path = poster.strip()

            try:
                cur_backdrop = getattr(season, "backdrop_path", None)
            except Exception:
                cur_backdrop = None
            if not (isinstance(cur_backdrop, str) and cur_backdrop.strip()):
                backdrop = None
                for lkey in lang_chain:
                    backdrop = _season_backdrop(season_data_by_lang.get(lkey))
                    if backdrop:
                        break
                if not backdrop:
                    try:
                        backdrop = getattr(mi_obj, "backdrop_path", None)
                    except Exception:
                        backdrop = None
                if not backdrop:
                    try:
                        backdrop = getattr(mi_obj, "poster_path", None)
                    except Exception:
                        backdrop = None
                if not backdrop:
                    try:
                        backdrop = getattr(series, "main_poster", None) if series else None
                    except Exception:
                        backdrop = None
                if isinstance(backdrop, str) and backdrop.strip():
                    season_any = _cast(_Any, season)
                    season_any.backdrop_path = backdrop.strip()

            try:
                session.add(season)
            except Exception:
                pass
        except Exception:
            pass

        def _episode_map(season_data: dict | None) -> dict[int, dict]:
            out: dict[int, dict] = {}
            if not isinstance(season_data, dict):
                return out
            eps0 = season_data.get("episodes") or []
            if not isinstance(eps0, list):
                return out
            for e in eps0:
                if not isinstance(e, dict):
                    continue
                n = e.get("episode_number")
                if not isinstance(n, int):
                    if n is None:
                        continue
                    try:
                        n = int(n)
                    except Exception:
                        continue
                out[n] = e
            return out

        def _split_offset(season_data: dict | None) -> int | None:
            if not isinstance(season_data, dict):
                return None
            eps = season_data.get("episodes") or []
            if not isinstance(eps, list) or not eps:
                return None
            # Prefer explicit TMDB markers that indicate the end of a "part"/cour.
            # NOTE: "finale" is *always* the last episode of the season and is not useful as a split point.
            for e in eps:
                if not isinstance(e, dict):
                    continue
                et = (e.get("episode_type") or "").strip().lower()
                if et in ("mid_season", "midseason", "mid-season"):
                    n = e.get("episode_number")
                    if n is not None:
                        try:
                            return int(n)
                        except Exception:
                            return None
            prev_date = None
            prev_num = None
            for e in eps:
                if not isinstance(e, dict):
                    continue
                ad = e.get("air_date")
                n = e.get("episode_number")
                if n is not None:
                    try:
                        n = int(n)
                    except Exception:
                        n = None
                if not isinstance(ad, str) or not ad.strip():
                    continue
                try:
                    dt = datetime.fromisoformat(ad.strip())
                except Exception:
                    try:
                        dt = datetime.strptime(ad.strip(), "%Y-%m-%d")
                    except Exception:
                        dt = None
                if dt is None:
                    continue
                if prev_date is not None and prev_num is not None:
                    try:
                        gap = (dt - prev_date).days
                    except Exception:
                        gap = None
                    if gap is not None and gap >= 28:
                        return int(prev_num)
                prev_date = dt
                prev_num = n
            return None

        episode_maps: dict[str, dict[int, dict]] = {}
        for lkey in lang_chain:
            episode_maps[lkey] = _episode_map(season_data_by_lang.get(lkey))
        en_episode_map = episode_maps.get("en-US", {})

        offset = 0
        has_current = any(episode_maps.values())
        if not has_current and isinstance(season_number, int) and season_number == 2:
            season1_by_lang: dict[str, dict | None] = {}
            for lkey in lang_chain:
                try:
                    season1_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                        tmdb_id, season_number=1, language=lkey
                    )
                except Exception:
                    season1_by_lang[lkey] = None
            base_season = season1_by_lang.get("en-US") or next((v for v in season1_by_lang.values() if isinstance(v, dict)), None)
            off = _split_offset(base_season if isinstance(base_season, dict) else None)
            if off:
                offset = int(off)
                episode_maps = {lkey: _episode_map(season1_by_lang.get(lkey)) for lkey in lang_chain}
                has_current = any(episode_maps.values())

        if not has_current:
            return
        try:
            existing_eps = session.query(Episode).filter(Episode.season_id == season.id).all()
        except Exception:
            existing_eps = []
        by_num: dict[int, Episode] = {}
        for ep in existing_eps:
            try:
                n = getattr(ep, 'episode_number', None)
                if isinstance(n, int):
                    by_num[n] = ep
            except Exception:
                continue

        def _is_generic_episode_title(v: str | None) -> bool:
            if v is None:
                return True
            s = str(v).strip()
            if not s:
                return True
            if re.fullmatch(r"\d{1,4}", s):
                return True
            if s.lower().startswith("episode ") or s.lower().startswith("episodio "):
                return True
            return False

        force_overwrite = bool(isinstance(offset, int) and offset > 0)
        changed = False
        for n, row in by_num.items():
            target_n = int(n) + int(offset) if isinstance(offset, int) and offset > 0 else int(n)
            row_any = _cast(_Any, row)
            # Title localized with per-episode fallback chain: es-MX -> es-ES -> en-US.
            try:
                cur_t = getattr(row, "title_localized", None)
            except Exception:
                cur_t = None
            if force_overwrite or _is_generic_episode_title(cur_t):
                for lkey in lang_chain:
                    e = episode_maps.get(lkey, {}).get(target_n)
                    if not isinstance(e, dict):
                        continue
                    tloc = e.get("title") or e.get("name")
                    if lkey != "en-US":
                        en_e = en_episode_map.get(target_n)
                        if isinstance(en_e, dict):
                            en_t = en_e.get("title") or en_e.get("name")
                            if _is_english_fallback(tloc, en_t):
                                continue
                    if isinstance(tloc, str) and tloc.strip():
                        row_any.title_localized = tloc.strip()
                        if force_overwrite:
                            row_any.title = tloc.strip()
                        changed = True
                        break
            # Synopsis localized with per-episode fallback chain.
            try:
                cur_syn = getattr(row, "synopsis_localized", None)
            except Exception:
                cur_syn = None
            if force_overwrite or not (isinstance(cur_syn, str) and cur_syn.strip()):
                for lkey in lang_chain:
                    e = episode_maps.get(lkey, {}).get(target_n)
                    if not isinstance(e, dict):
                        continue
                    ov = e.get("overview") or ""
                    if lkey != "en-US":
                        en_e = en_episode_map.get(target_n)
                        if isinstance(en_e, dict):
                            en_ov = en_e.get("overview") or ""
                            if _is_english_fallback(ov, en_ov):
                                continue
                    if isinstance(ov, str) and ov.strip():
                        row_any.synopsis_localized = ov.strip()
                        changed = True
                        break
            # Episode still_path (download/cache locally)
            try:
                cur_still = getattr(row, "still_path", None)
            except Exception:
                cur_still = None
            if not (isinstance(cur_still, str) and cur_still.strip()):
                still_url = None
                for lkey in lang_chain:
                    e = episode_maps.get(lkey, {}).get(target_n)
                    if not isinstance(e, dict):
                        continue
                    su = e.get("still_path")
                    if isinstance(su, str) and su.strip():
                        still_url = su.strip()
                        break
                if still_url:
                    _download_episode_still(session, row, still_url)
            try:
                session.add(row)
            except Exception:
                pass

        # Also backfill stable English anchor titles for episodes when current title_en is generic.
        # This improves UX for libraries where filenames are purely numeric (01.mkv) and prevents
        # regressions when localized fetch is temporarily unavailable.
        try:
            need_en = any(_is_generic_episode_title(getattr(r, "title_en", None)) for r in by_num.values())
        except Exception:
            need_en = False
        if need_en or force_overwrite:
            season_data_en = season_data_by_lang.get("en-US")
            if isinstance(season_data_en, dict):
                en_eps = season_data_en.get("episodes") or []
                if isinstance(en_eps, list):
                    for e in en_eps:
                        if not isinstance(e, dict):
                            continue
                        n = e.get("episode_number")
                        if not isinstance(n, int):
                            if n is None:
                                continue
                            try:
                                n = int(n)
                            except Exception:
                                continue
                        n2 = int(n) - int(offset) if isinstance(offset, int) and offset > 0 else int(n)
                        row = by_num.get(n2)
                        if not row:
                            continue
                        row_any = _cast(_Any, row)
                        try:
                            ten = e.get("title") or e.get("name")
                            if isinstance(ten, str) and ten.strip() and (force_overwrite or _is_generic_episode_title(getattr(row, "title_en", None))):
                                row_any.title_en = ten.strip()
                                if force_overwrite:
                                    row_any.title = ten.strip()
                                changed = True
                                session.add(row)
                        except Exception:
                            continue
        if changed:
            try:
                session.flush()
            except Exception:
                pass

    def maybe_apply_tmdb_localization(mi_obj: MediaItem, norm: dict, media_type: str | None):
        if not should_localize():
            return
        if not tmdb:
            return
        try:
            prov_norm = str((norm or {}).get("provider") or "").strip().lower()
        except Exception:
            prov_norm = ""
        if not _tmdb_allowed_for_media_item(session, mi_obj, manual_override=(prov_norm == "tmdb")):
            return
        # Allow TMDB localization even for Jikan items if a TMDB ID is resolved.
        try:
            if not _tmdb_configured():
                return
        except Exception:
            return
        lang = get_localization_language()
        if not lang:
            return
        mi_any = _cast(_Any, mi_obj)
        title_en = getattr(mi_obj, 'title_en', None) or pick_title_en(norm) or getattr(mi_obj, 'title', None) or ''
        year = getattr(mi_obj, 'release_year', None) or norm.get('year') or None
        try:
            year = int(year) if year is not None else None
        except Exception:
            year = None

        tmdb_id = getattr(mi_obj, 'tmdb_id', None)
        tmdb_media_type = 'tv' if (media_type or '').strip().lower() in ('series', 'tv', 'show') else 'movie'
        mi_id_int = None
        try:
            mi_id = getattr(mi_obj, "id", None)
            if mi_id is not None:
                mi_id_int = int(mi_id)
        except Exception:
            mi_id_int = None

        # If we don't have tmdb_id yet (common for anime identity via Jikan), try to resolve it from TMDB.
        if not tmdb_id:
            try:
                q = f"{title_en} {year}" if year else str(title_en)
                res = _tmdb_search_with_optional_season(q, tmdb_media_type)
                if res and isinstance(res, dict):
                    tmdb_id = res.get('tmdb_id') or res.get('provider_id')
                    if tmdb_id:
                        mi_any.tmdb_id = str(tmdb_id)
            except Exception:
                tmdb_id = None

        if not tmdb_id:
            return

        # Fetch localized details for title/overview.
        #
        # For TV libraries with normalized seasons, prefer season-level localized titles/overview for
        # season folders (Season 2, etc.). Otherwise we end up storing the show overview everywhere,
        # and season titles can be wrong (or override series title).
        season_number = None
        try:
            _series0, _season0 = get_series_and_season_for_item(mi_obj, session)
            season_number = getattr(_season0, "season_number", None) if _season0 else None
        except Exception:
            season_number = None

        if tmdb_media_type == "tv" and isinstance(season_number, int) and (season_number == 0 or season_number > 1) and hasattr(tmdb, "tmdb_fetch_season"):
            # Season-specific localized fields
            try:
                sdata = tmdb.tmdb_fetch_season(str(tmdb_id), season_number=season_number, language=lang)  # type: ignore[arg-type]
            except Exception:
                sdata = None
            if isinstance(sdata, dict):
                try:
                    tloc = sdata.get("title")
                    if isinstance(tloc, str) and tloc.strip():
                        mi_any.title_localized = tloc.strip()
                except Exception:
                    pass
                try:
                    ov = sdata.get("synopsis") or ""
                    if isinstance(ov, str) and ov.strip():
                        mi_any.synopsis_localized = ov.strip()
                except Exception:
                    pass
            # Persist TMDB translation rows (requested language + English fallback) for series/movie-level UI.
            try:
                tloc0 = getattr(mi_obj, "title_localized", None) or None
                ov0 = getattr(mi_obj, "synopsis_localized", None) or None
                if mi_id_int is not None:
                    upsert_translation(session, path_id=mi_id_int, language=lang, source="tmdb", title=tloc0, overview=ov0)
            except Exception:
                pass
            try:
                # English fallback row (do not overwrite localized fields; UI will fallback by chain).
                sdata_en = tmdb.tmdb_fetch_season(str(tmdb_id), season_number=season_number, language="en-US")  # type: ignore[arg-type]
                if isinstance(sdata_en, dict):
                    ten = sdata_en.get("title") or None
                    ov_en = sdata_en.get("synopsis") or ""
                    if mi_id_int is not None:
                        upsert_translation(
                            session,
                            path_id=mi_id_int,
                            language="en",
                            source="tmdb",
                            title=ten if isinstance(ten, str) and ten.strip() else None,
                            overview=ov_en if isinstance(ov_en, str) and ov_en.strip() else None,
                        )
            except Exception:
                pass
            # Season anchor English title (helps when UI falls back)
            try:
                if not getattr(mi_obj, "title_en", None):
                    sdata_en = tmdb.tmdb_fetch_season(str(tmdb_id), season_number=season_number, language="en-US")  # type: ignore[arg-type]
                    if isinstance(sdata_en, dict):
                        ten = sdata_en.get("title")
                        if isinstance(ten, str) and ten.strip():
                            mi_any.title_en = ten.strip()
            except Exception:
                pass
        else:
            # Series/movie-level localized fields
            try:
                details = tmdb.tmdb_fetch_by_id(tmdb_id, media_type=tmdb_media_type, language=lang)  # type: ignore[arg-type]
            except Exception:
                details = None
            if isinstance(details, dict):
                try:
                    tloc = details.get('title') or details.get('series_title')
                    if isinstance(tloc, str) and tloc.strip():
                        mi_any.title_localized = tloc.strip()
                except Exception:
                    pass
                try:
                    ov = details.get('synopsis') or ''
                    if isinstance(ov, str) and ov.strip():
                        mi_any.synopsis_localized = ov.strip()
                except Exception:
                    pass
                # Persist TMDB translations (requested language + English fallback).
                try:
                    tloc0 = getattr(mi_obj, "title_localized", None) or None
                    ov0 = getattr(mi_obj, "synopsis_localized", None) or None
                    if mi_id_int is not None:
                        upsert_translation(session, path_id=mi_id_int, language=lang, source="tmdb", title=tloc0, overview=ov0)
                except Exception:
                    pass
                try:
                    details_en = tmdb.tmdb_fetch_by_id(tmdb_id, media_type=tmdb_media_type, language="en-US")  # type: ignore[arg-type]
                except Exception:
                    details_en = None
                if isinstance(details_en, dict):
                    try:
                        ten = details_en.get("title") or details_en.get("series_title") or None
                        ov_en = details_en.get("synopsis") or ""
                        if mi_id_int is not None:
                            upsert_translation(
                                session,
                                path_id=mi_id_int,
                                language="en",
                                source="tmdb",
                                title=ten if isinstance(ten, str) and ten.strip() else None,
                                overview=ov_en if isinstance(ov_en, str) and ov_en.strip() else None,
                            )
                    except Exception:
                        pass
                try:
                    import difflib
                    tloc0 = (details.get('title') or details.get('series_title') or '').strip()
                    if title_en and tloc0:
                        conf = difflib.SequenceMatcher(None, str(title_en).lower(), str(tloc0).lower()).ratio()
                        logger.info(
                            "tmdb_localization: media_item=%s tmdb_id=%s lang=%s confidence=%.2f",
                            getattr(mi_obj, "id", None),
                            str(tmdb_id),
                            lang,
                            float(conf),
                        )
                except Exception:
                    pass

        # Episode titles localization (only for series + known season number).
        try:
            series, season = get_series_and_season_for_item(mi_obj, session)
            season_number = getattr(season, 'season_number', None) if season else None
        except Exception:
            season_number = None
        try:
            sync_episode_localized_titles(mi_obj, str(tmdb_id), media_type, lang, season_number if isinstance(season_number, int) else None)
        except Exception:
            pass

        try:
            # Back-compat: keep `title` as best display.
            mi_any.title = mi_any.title_localized or mi_any.title_en or mi_any.title
        except Exception:
            pass

        try:
            session.add(mi_obj)
        except Exception:
            pass

    data_json = json.dumps(normalized or {}, ensure_ascii=False, sort_keys=True)
    checksum = hashlib.sha256(data_json.encode('utf-8')).hexdigest()
    mi_obj = _cast(_Any, media_item)
    prov_name = _normalize_provider_name(normalized.get('provider') if isinstance(normalized, dict) else None, 'manual')
    existing = session.query(MediaMetadata).filter(
        MediaMetadata.media_item_id == mi_obj.id,
        MediaMetadata.provider == prov_name,
        MediaMetadata.checksum == checksum,
    ).first()
    if existing:
        try:
            session.query(MediaMetadata).filter(
                MediaMetadata.media_item_id == mi_obj.id,
                MediaMetadata.provider == prov_name,
                MediaMetadata.id != existing.id,
            ).delete(synchronize_session=False)
        except Exception:
            pass
        try:
            _apply_jikan_anchor(mi_obj, normalized)
        except Exception:
            pass
        title_en = pick_title_en(normalized)
        if title_en:
            try:
                mi_obj.title_en = title_en  # pyright: ignore[reportAttributeAccessIssue]
            except Exception:
                pass
        if normalized.get('title'):
            # Back-compat display title: prefer localized, else English anchor, else provider title.
            try:
                mi_obj.title = getattr(mi_obj, 'title_localized', None) or getattr(mi_obj, 'title_en', None) or normalized.get('title')
            except Exception:
                mi_obj.title = normalized.get('title')
        if normalized.get('media_type'):
            mi_obj.media_type = normalized.get('media_type')
        if normalized.get('provider'):
            mi_obj.provider = normalized.get('provider')
        if normalized.get('provider_id'):
            mi_obj.provider_id = str(normalized.get('provider_id'))
        mi_obj.status = 'ENRICHED'
        try:
            mi_obj.is_identified = True
        except Exception:
            pass
        session.add(mi_obj)
        _commit_with_telemetry()
        mi_id = getattr(mi_obj, "id", None)
        # Persist translations for UI fallback (prefer TMDB over Jikan at query time).
        try:
            prov0 = str(normalized.get("provider") or "").strip().lower() or "manual"
            lang0 = "en" if prov0 == "jikan" else normalize_translation_language(get('metadata_language') or "en")
            title0 = getattr(mi_obj, "title_en", None) or getattr(mi_obj, "title_localized", None) or getattr(mi_obj, "title", None)
            overview0 = normalized.get("synopsis") or normalized.get("overview") or None
            if mi_id is not None:
                upsert_translation(session, path_id=int(mi_id), language=lang0, source=prov0, title=title0, overview=overview0)
                _commit_with_telemetry()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass

        try:
            with telemetry.phase("Translation"):
                maybe_apply_tmdb_localization(mi_obj, normalized, str(getattr(mi_obj, "media_type", None) or "movie"))
                sync_series_season_fields(mi_obj, session)
            _commit_with_telemetry()
        except Exception:
            session.rollback()

        # Ensure per-season synopsis + episode overviews/stills are persisted for TV season folders.
        #
        # Why: the UI consumes Season/Episode tables (hierarchy Series -> Season -> Episode). If TMDB
        # localization is disabled, we still want season-level synopsis and episode summaries to exist,
        # and we must NOT rely on filename-derived titles (episode/channel names) for provider matching.
        try:
            try:
                mt0 = str(getattr(mi_obj, "media_type", None) or "").strip().lower()
            except Exception:
                mt0 = ""
            if mt0 in ("series", "tv", "show") and _tmdb_allowed_for_media_item(session, mi_obj, manual_override=(str(normalized.get("provider") or "").strip().lower() == "tmdb")) and tmdb is not None and hasattr(tmdb, "tmdb_fetch_season") and _tmdb_configured():
                tmdb_id0 = None
                try:
                    tmdb_id0 = (getattr(mi_obj, "tmdb_id", None) or "").strip()
                except Exception:
                    tmdb_id0 = None
                if tmdb_id0:
                    series0, season0 = get_series_and_season_for_item(mi_obj, session)
                    sn0 = None
                    try:
                        sn0 = getattr(season0, "season_number", None) if season0 is not None else None
                    except Exception:
                        sn0 = None

                    need_sync = False
                    # Season fields
                    if season0 is not None:
                        try:
                            need_sync = need_sync or not (getattr(season0, "synopsis_localized", None) or "").strip()
                        except Exception:
                            need_sync = True
                        try:
                            need_sync = need_sync or not (getattr(season0, "poster_path", None) or "").strip()
                        except Exception:
                            need_sync = True
                        try:
                            need_sync = need_sync or not (getattr(season0, "backdrop_path", None) or "").strip()
                        except Exception:
                            need_sync = True

                    # Episode fields (only if we have a Season row)
                    if not need_sync and season0 is not None:
                        try:
                            eps0 = session.query(Episode).filter(Episode.season_id == season0.id).all()
                        except Exception:
                            eps0 = []
                        if eps0:
                            try:
                                need_sync = need_sync or any(not (getattr(ep, "synopsis_localized", None) or "").strip() for ep in eps0)
                            except Exception:
                                need_sync = True
                            try:
                                need_sync = need_sync or any(not (getattr(ep, "still_path", None) or "").strip() for ep in eps0)
                            except Exception:
                                need_sync = True

                    if need_sync and isinstance(sn0, int):
                        try:
                            lang0 = str(get("metadata_language") or "").strip() or "en-US"
                        except Exception:
                            lang0 = "en-US"
                        try:
                            sync_episode_localized_titles(mi_obj, str(tmdb_id0), mt0, lang0, int(sn0))
                            _commit_with_telemetry()
                        except Exception:
                            session.rollback()
        except Exception:
            # Best-effort: season/episode metadata should not fail the main enrichment flow.
            try:
                session.rollback()
            except Exception:
                pass

        # Even when metadata is identical (same checksum), we still may need to (re)download
        # posters/backdrops. Common case: user enables "download images" after an initial enrich
        # run, but the provider payload hasn't changed so we'd otherwise skip downloads forever.
        try:
            if get('download_images'):
                try:
                    images0 = normalized.get("images")
                except Exception:
                    images0 = None
                poster_url = _pick_best_image_url(images0, kind="poster")
                backdrop_url = _pick_best_image_url(images0, kind="backdrop")

                cur_p = getattr(mi_obj, "poster_path", None)
                cur_b = getattr(mi_obj, "backdrop_path", None)
                need_p = not (isinstance(cur_p, str) and cur_p.strip().startswith("/media-assets/"))
                need_b = not (isinstance(cur_b, str) and cur_b.strip().startswith("/media-assets/"))

                poster_ok = True
                backdrop_ok = True
                # Always call `_download_image_and_record` when we have a URL; it's idempotent and will
                # also refresh stale local assets if the source URL changed (e.g. after manual mapping).
                if poster_url:
                    poster_ok = bool(_download_image_and_record(session, mi_obj, str(poster_url), source='provider', kind="poster"))
                if backdrop_url:
                    backdrop_ok = bool(_download_image_and_record(session, mi_obj, str(backdrop_url), source='provider', kind="backdrop"))

                # If Jikan image downloads fail, optionally try TMDB as an images-only fallback (if configured).
                try:
                    prov0 = (normalized.get("provider") or "").strip().lower()
                except Exception:
                    prov0 = ""
                if prov0 == "jikan" and _tmdb_allowed_for_media_item(session, mi_obj, manual_override=False) and (not poster_ok or not backdrop_ok):
                    try:
                        if tmdb is not None and hasattr(tmdb, "tmdb_search") and hasattr(tmdb, "tmdb_fetch_by_id") and _tmdb_configured():
                            import difflib

                            mt0 = (getattr(mi_obj, "media_type", None) or "").strip().lower()
                            media_pref = "tv" if mt0 in ("series", "tv", "show", "anime") else "movie"

                            queries: list[str] = []
                            for q in (
                                normalized.get("title_english"),
                                getattr(mi_obj, "title_en", None),
                                normalized.get("series_title"),
                                normalized.get("title"),
                                getattr(mi_obj, "base_title", None),
                                getattr(mi_obj, "title", None),
                            ):
                                if isinstance(q, str) and q.strip():
                                    queries.append(q.strip())
                            seen = set()
                            out_q: list[str] = []
                            for q in queries:
                                k = q.lower()
                                if k in seen:
                                    continue
                                seen.add(k)
                                out_q.append(q)
                            out_q = out_q[:5]

                            res = None
                            try:
                                res = tmdb.tmdb_search(out_q, media_preference=media_pref, allow_when_config_is_jikan=True)  # type: ignore[attr-defined]
                            except Exception:
                                res = None
                            if isinstance(res, dict) and res:
                                tmdb_id = res.get("tmdb_id") or res.get("provider_id") or res.get("id")
                                tmdb_title = (res.get("title") or res.get("series_title") or "").strip()
                                anchor = (out_q[0] if out_q else "") or tmdb_title
                                conf = 0.0
                                if anchor and tmdb_title:
                                    try:
                                        conf = difflib.SequenceMatcher(None, anchor.lower(), tmdb_title.lower()).ratio()
                                    except Exception:
                                        conf = 0.0
                                if tmdb_id and conf >= 0.72:
                                    try:
                                        lang = str(get("metadata_language") or "").strip() or "en-US"
                                    except Exception:
                                        lang = "en-US"
                                    try:
                                        tmdb_norm = tmdb.tmdb_fetch_by_id(str(tmdb_id), media_type=media_pref, language=lang)  # type: ignore[arg-type]
                                    except Exception:
                                        tmdb_norm = None
                                    if isinstance(tmdb_norm, dict) and tmdb_norm:
                                        try:
                                            t_imgs = tmdb_norm.get("images")
                                        except Exception:
                                            t_imgs = None
                                        t_poster = _pick_best_image_url(t_imgs, kind="poster")
                                        t_back = _pick_best_image_url(t_imgs, kind="backdrop")
                                        if t_poster and (need_p or not poster_ok):
                                            _download_image_and_record(session, mi_obj, str(t_poster), source="tmdb", kind="poster")
                                        if t_back and (need_b or not backdrop_ok):
                                            _download_image_and_record(session, mi_obj, str(t_back), source="tmdb", kind="backdrop")
                    except Exception:
                        pass

                # If we backfilled images after the Translation step, propagate poster to Series/Season tables.
                try:
                    sync_series_season_fields(mi_obj, session)
                    _commit_with_telemetry()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
        except Exception:
            pass

        logger.info('Skipping identical metadata for media_item=%s provider=%s', media_item.id, normalized.get('provider'))
        return
    try:
        session.query(MediaMetadata).filter(
            MediaMetadata.media_item_id == media_item.id,
            MediaMetadata.provider == prov_name,
        ).delete(synchronize_session=False)
    except Exception:
        pass
    maxv = (
        session.query(func.max(MediaMetadata.version))
        .filter(MediaMetadata.media_item_id == media_item.id)
        .filter(MediaMetadata.provider == prov_name)
        .scalar()
        or 0
    )
    next_version = int(maxv or 0) + 1
    mm = MediaMetadata(
        media_item_id=media_item.id,
        provider=prov_name,
        provider_id=str(normalized.get('provider_id') or ''),
        data=data_json,
        version=next_version,
        checksum=checksum,
    )
    session.add(mm)
    title_en = pick_title_en(normalized)
    if title_en:
        try:
            mi_obj.title_en = title_en  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            pass
    if normalized.get('title'):
        try:
            mi_obj.title = getattr(mi_obj, 'title_localized', None) or getattr(mi_obj, 'title_en', None) or normalized.get('title')
        except Exception:
            mi_obj.title = normalized.get('title')
    if normalized.get('media_type'):
        mi_obj.media_type = normalized.get('media_type')
    if normalized.get('provider'):
        mi_obj.provider = normalized.get('provider')
    if normalized.get('provider_id'):
        mi_obj.provider_id = str(normalized.get('provider_id'))
    try:
        _apply_jikan_anchor(mi_obj, normalized)
    except Exception:
        pass
    raw = normalized.get('raw') or {}
    try:
        rd = None
        if isinstance(raw, dict):
            rd = raw.get('release_date') or raw.get('first_air_date') or raw.get('aired')
        if rd and isinstance(rd, str) and len(rd) >= 4:
            try:
                mi_obj.release_year = int(str(rd)[:4])
            except Exception:
                pass
    except Exception:
        pass
    try:
        rt = None
        if isinstance(raw, dict):
            rt = raw.get('runtime') or (raw.get('episode_run_time')[0] if isinstance(raw.get('episode_run_time'), list) and raw.get('episode_run_time') else None) # pyright: ignore[reportOptionalSubscript] # pyright: ignore[reportOptionalSubscript]
        if rt:
            try:
                mi_obj.runtime = int(rt)
            except Exception:
                pass
    except Exception:
        pass
    try:
        cast_arr = None
        if isinstance(raw, dict):
            cast_arr = (raw.get('credits') or {}).get('cast') if isinstance(raw.get('credits'), dict) else raw.get('cast')
        if cast_arr and isinstance(cast_arr, list):
            names = []
            for c in cast_arr[:10]:
                if isinstance(c, dict):
                    if c.get('name'):
                        names.append(c.get('name'))
                    elif c.get('person') and isinstance(c.get('person'), dict) and c.get('person').get('name'): # pyright: ignore[reportOptionalMemberAccess]
                        names.append(c.get('person').get('name')) # pyright: ignore[reportOptionalMemberAccess]
                elif isinstance(c, str):
                    names.append(c)
            if names:
                mi_obj.cast = ', '.join(names)
    except Exception:
        pass
    try:
        prov = (normalized.get('provider') or '').lower()
        genres = normalized.get('genres') or []
        is_anim = False
        origin = None
        if prov == 'jikan':
            is_anim = True
            raw_source = (raw.get('source') or '').lower() if isinstance(raw.get('source'), str) else ''
            if raw_source:
                origin = raw_source
        if any('animation' in (g or '').lower() for g in genres):
            is_anim = True
        mi_obj.is_animated = True if is_anim else (mi_obj.is_animated or False)
        if origin:
            mi_obj.origin = origin
    except Exception:
        pass
    try:
        prov = (normalized.get('provider') or '').lower()
        if prov == 'jikan':
            try:
                mal = normalized.get('provider_id')
                if mal:
                    mi_obj.mal_id = str(mal)
            except Exception:
                pass
        tmdb_id = None
        try:
            if prov == 'tmdb':
                tmdb_id = normalized.get('tmdb_id') or normalized.get('provider_id')
            else:
                tmdb_id = normalized.get('tmdb_id')
            if tmdb_id:
                mi_obj.tmdb_id = str(tmdb_id)
        except Exception:
            tmdb_id = None
        try:
            if tmdb_id:
                mi_obj = _cast(_Any, merge_movie_tmdb_duplicates(mi_obj, str(tmdb_id)))
        except Exception:
            pass
        if prov == 'tmdb':
            try:
                raw_local = normalized.get('raw') or {}
                backdrop = raw_local.get('backdrop_path') or raw_local.get('backdrop') or ''
                if backdrop:
                    mi_obj.backdrop_path = backdrop
            except Exception:
                pass
            try:
                vote = None
                raw_local = normalized.get('raw') or {}
                vote = raw_local.get('vote_average') or raw_local.get('rating') or raw_local.get('vote')
                if vote is not None:
                    try:
                        mi_obj.rating = float(vote)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                g = normalized.get('genres') or []
                if isinstance(g, list) and g:
                    mi_obj.genres = ', '.join([str(x) for x in g if x])
            except Exception:
                pass
        try:
            cr = normalized.get('content_rating') or normalized.get('rating')
            if cr:
                mi_obj.content_rating = str(cr).strip()
        except Exception:
            pass
        try:
            g = normalized.get('genres') or []
            if isinstance(g, list) and g:
                mi_obj.genres = ', '.join([str(x) for x in g if x])
        except Exception:
            pass
    except Exception:
        pass
    mi_obj.status = 'ENRICHED'
    try:
        mi_obj.is_identified = True
    except Exception:
        pass
    session.add(mi_obj)
    _commit_with_telemetry()
    try:
        with telemetry.phase("Translation"):
            maybe_apply_tmdb_localization(mi_obj, normalized, str(getattr(mi_obj, "media_type", None) or "movie"))
            sync_series_season_fields(mi_obj, session)
    except Exception:
        safe_id = None
        try:
            safe_id = mi_obj.id
        except Exception:
            try:
                safe_id = media_item.id
            except Exception:
                safe_id = None
        logger.debug("Failed applying TMDB localization/sync for media_item=%s", safe_id, exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass
    _commit_with_telemetry()

    # Ensure per-season synopsis + episode overviews/stills are persisted for TV season folders.
    #
    # Note: this must run on the "new metadata inserted" path as well (not only when checksums match),
    # otherwise a first-time enrichment will leave Season/Episode tables empty (seen in "Inside Job").
    try:
        try:
            mt0 = str(getattr(mi_obj, "media_type", None) or "").strip().lower()
        except Exception:
            mt0 = ""
        if mt0 in ("series", "tv", "show") and _tmdb_allowed_for_media_item(session, mi_obj, manual_override=(str(normalized.get("provider") or "").strip().lower() == "tmdb")) and tmdb is not None and hasattr(tmdb, "tmdb_fetch_season") and _tmdb_configured():
            tmdb_id0 = None
            try:
                tmdb_id0 = (getattr(mi_obj, "tmdb_id", None) or "").strip()
            except Exception:
                tmdb_id0 = None
            if tmdb_id0:
                _series0, _season0 = get_series_and_season_for_item(mi_obj, session)
                sn0 = None
                try:
                    sn0 = getattr(_season0, "season_number", None) if _season0 is not None else None
                except Exception:
                    sn0 = None

                need_sync = False
                if _season0 is not None:
                    try:
                        need_sync = need_sync or not (getattr(_season0, "synopsis_localized", None) or "").strip()
                    except Exception:
                        need_sync = True
                    try:
                        need_sync = need_sync or not (getattr(_season0, "poster_path", None) or "").strip()
                    except Exception:
                        need_sync = True
                    try:
                        need_sync = need_sync or not (getattr(_season0, "backdrop_path", None) or "").strip()
                    except Exception:
                        need_sync = True

                if not need_sync and _season0 is not None:
                    try:
                        eps0 = session.query(Episode).filter(Episode.season_id == _season0.id).all()
                    except Exception:
                        eps0 = []
                    if eps0:
                        try:
                            need_sync = need_sync or any(not (getattr(ep, "synopsis_localized", None) or "").strip() for ep in eps0)
                        except Exception:
                            need_sync = True
                        try:
                            need_sync = need_sync or any(not (getattr(ep, "still_path", None) or "").strip() for ep in eps0)
                        except Exception:
                            need_sync = True

                if need_sync and isinstance(sn0, int):
                    try:
                        lang0 = str(get("metadata_language") or "").strip() or "en-US"
                    except Exception:
                        lang0 = "en-US"
                    try:
                        sync_episode_localized_titles(mi_obj, str(tmdb_id0), mt0, lang0, int(sn0))
                        _commit_with_telemetry()
                    except Exception:
                        session.rollback()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass

    mi_id = getattr(mi_obj, "id", None)
    # Persist base translation row for the provider that produced these normalized fields.
    try:
        prov0 = str(normalized.get("provider") or "").strip().lower() or "manual"
        lang0 = "en" if prov0 == "jikan" else normalize_translation_language(get('metadata_language') or "en")
        title0 = getattr(mi_obj, "title_en", None) or getattr(mi_obj, "title_localized", None) or getattr(mi_obj, "title", None)
        overview0 = normalized.get("synopsis") or normalized.get("overview") or None
        with telemetry.phase("Translation"):
            if mi_id is not None:
                upsert_translation(session, path_id=int(mi_id), language=lang0, source=prov0, title=title0, overview=overview0)
        _commit_with_telemetry()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    try:
        if get('download_images'):
            try:
                images0 = normalized.get("images")
            except Exception:
                images0 = None
            img_url = _pick_best_image_url(images0, kind="poster")
            backdrop_url = _pick_best_image_url(images0, kind="backdrop")
            poster_ok = False
            backdrop_ok = False
            if img_url:
                poster_ok = bool(_download_image_and_record(session, mi_obj, str(img_url), source='provider', kind="poster"))
            if backdrop_url:
                backdrop_ok = bool(_download_image_and_record(session, mi_obj, str(backdrop_url), source='provider', kind="backdrop"))

            # If Jikan image downloads fail (common in locked-down networks), optionally use TMDB as an
            # "images-only" fallback without replacing the provider metadata.
            try:
                prov0 = (normalized.get("provider") or "").strip().lower()
            except Exception:
                prov0 = ""
            if prov0 == "jikan" and _tmdb_allowed_for_media_item(session, mi_obj, manual_override=False) and (not poster_ok or not backdrop_ok):
                try:
                    if tmdb is not None and hasattr(tmdb, "tmdb_search") and hasattr(tmdb, "tmdb_fetch_by_id") and _tmdb_configured():
                        import difflib

                        mt0 = (getattr(mi_obj, "media_type", None) or "").strip().lower()
                        media_pref = "tv" if mt0 in ("series", "tv", "show", "anime") else "movie"

                        queries: list[str] = []
                        for q in (
                            normalized.get("title_english"),
                            getattr(mi_obj, "title_en", None),
                            normalized.get("series_title"),
                            normalized.get("title"),
                            getattr(mi_obj, "base_title", None),
                            getattr(mi_obj, "title", None),
                        ):
                            if isinstance(q, str) and q.strip():
                                queries.append(q.strip())
                        # de-dupe preserving order
                        seen = set()
                        out_q: list[str] = []
                        for q in queries:
                            k = q.lower()
                            if k in seen:
                                continue
                            seen.add(k)
                            out_q.append(q)
                        out_q = out_q[:5]

                        res = None
                        try:
                            res = tmdb.tmdb_search(out_q, media_preference=media_pref, allow_when_config_is_jikan=True)  # type: ignore[attr-defined]
                        except Exception:
                            res = None
                        if isinstance(res, dict) and res:
                            tmdb_id = res.get("tmdb_id") or res.get("provider_id") or res.get("id")
                            tmdb_title = (res.get("title") or res.get("series_title") or "").strip()
                            anchor = (out_q[0] if out_q else "") or tmdb_title
                            conf = 0.0
                            if anchor and tmdb_title:
                                try:
                                    conf = difflib.SequenceMatcher(None, anchor.lower(), tmdb_title.lower()).ratio()
                                except Exception:
                                    conf = 0.0
                            if tmdb_id and conf >= 0.72:
                                try:
                                    lang = str(get("metadata_language") or "").strip() or "en-US"
                                except Exception:
                                    lang = "en-US"
                                try:
                                    tmdb_norm = tmdb.tmdb_fetch_by_id(str(tmdb_id), media_type=media_pref, language=lang)  # type: ignore[arg-type]
                                except Exception:
                                    tmdb_norm = None
                                if isinstance(tmdb_norm, dict) and tmdb_norm:
                                    try:
                                        t_imgs = tmdb_norm.get("images")
                                    except Exception:
                                        t_imgs = None
                                    t_poster = _pick_best_image_url(t_imgs, kind="poster")
                                    t_back = _pick_best_image_url(t_imgs, kind="backdrop")
                                    if t_poster and not poster_ok:
                                        poster_ok = bool(_download_image_and_record(session, mi_obj, str(t_poster), source="tmdb", kind="poster"))
                                    if t_back and not backdrop_ok:
                                        backdrop_ok = bool(_download_image_and_record(session, mi_obj, str(t_back), source="tmdb", kind="backdrop"))
                except Exception:
                    pass
            try:
                sync_series_season_fields(mi_obj, session)
                _commit_with_telemetry()
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
    except Exception:
        pass
    safe_id = None
    try:
        safe_id = mi_obj.id
    except Exception:
        try:
            safe_id = media_item.id
        except Exception:
            safe_id = None
    logger.info('Persisted metadata for media_item=%s provider=%s id=%s version=%s', safe_id, normalized.get('provider'), normalized.get('provider_id'), next_version)

    try:
        _persist_relations(session, media_item, normalized)
    except Exception:
        logger.debug("Failed persisting relations for media_item=%s", media_item.id, exc_info=True)

    try:
        try:
            mi_id = getattr(mi_obj, 'id', None)
            telemetry._emit('ITEM_ENRICHED', {'id': int(mi_id) if mi_id is not None else 0, 'poster': getattr(mi_obj, 'poster_path', None), 'backdrop': getattr(mi_obj, 'backdrop_path', None)})
        except Exception:
            pass
    except Exception:
        pass


def _persist_relations(session, media_item: MediaItem, normalized: dict) -> None:
    try:
        prov = (normalized.get('provider') or '').lower()
    except Exception:
        prov = ''
    if prov != 'jikan':
        return
    rels = normalized.get('relations') or []
    if not isinstance(rels, list):
        return

    mi_obj = _cast(_Any, media_item)
    cur_mal = None
    try:
        cur_mal = str(getattr(mi_obj, 'mal_id', None) or normalized.get('provider_id') or '').strip()
    except Exception:
        cur_mal = None

    for rel in rels:
        if not isinstance(rel, dict):
            continue
        rel_name = str(rel.get('relation') or '').strip().lower()
        if rel_name not in ('prequel', 'sequel'):
            continue
        entries = rel.get('entry') or []
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            mid = e.get('mal_id') or e.get('id')
            if not mid:
                continue
            try:
                target_mal = str(int(mid))
            except Exception:
                target_mal = str(mid).strip()
            if not target_mal:
                continue

            target_item = None
            try:
                target_item = (
                    session.query(MediaItem)
                    .filter(MediaItem.mal_id == target_mal)
                    .order_by(MediaItem.id.desc())
                    .first()
                )
            except Exception:
                target_item = None

            existing = None
            try:
                existing = (
                    session.query(MediaRelation)
                    .filter(
                        MediaRelation.from_item_id == mi_obj.id,
                        MediaRelation.relation_type == rel_name,
                        MediaRelation.external_id == target_mal,
                    )
                    .first()
                )
            except Exception:
                existing = None

            if existing:
                if target_item and existing.to_item_id is None:
                    existing.to_item_id = target_item.id
                    session.add(existing)
                    session.commit()
                continue

            mr = MediaRelation(
                from_item_id=mi_obj.id,
                to_item_id=target_item.id if target_item else None,
                relation_type=rel_name,
                provider='jikan',
                external_id=target_mal,
            )
            session.add(mr)
            session.commit()

    if cur_mal:
        try:
            pending = (
                session.query(MediaRelation)
                .filter(MediaRelation.external_id == cur_mal, MediaRelation.to_item_id == None)
                .all()
            )
            for rel in pending:
                rel.to_item_id = mi_obj.id
                session.add(rel)
            if pending:
                session.commit()
        except Exception:
            session.rollback()


def enrich_one_item(session, mi: MediaItem, cfg_movies: str, cfg_anime: str) -> bool:
    """Enrich a single media item. Returns True if successfully processed (even if no match)."""
    mi_any = _cast(_Any, mi)
    try:
        try:
            set_state({'running': True, 'current_id': getattr(mi_any, "id", None), 'current_title': getattr(mi_any, "title", None), 'current_step': 'enrich_one_item'})
        except Exception:
            pass
        inferred = _decide_media_type(mi_any, session) or 'movie'
        cur_mt = str(getattr(mi_any, "media_type", "") or "").strip().lower()
        if inferred == 'series' and cur_mt not in ('series', 'tv', 'show'):
            mi_any.media_type = 'series'
        else:
            mi_any.media_type = getattr(mi_any, "media_type", None) or inferred
        media_type = str(getattr(mi_any, "media_type", None) or inferred)
        title = str(getattr(mi_any, "title", "") or "")
        normalized = {}
        candidate_titles = _candidate_titles_for_item(mi_any)
        logger.info(f"enrich_one_item: candidate_titles for {title}: {candidate_titles}")
        root_type = _root_type_for_media_item(session, mi_any)
        try:
            cfg_anime_s = str(cfg_anime or "").strip().lower()
        except Exception:
            cfg_anime_s = ""
        is_anime_context = False
        try:
            if (root_type or "").strip().lower() == "anime":
                is_anime_context = True
        except Exception:
            pass
        if not is_anime_context:
            try:
                is_anime_context = str(getattr(mi_any, "library_type", "") or "").strip().lower() == "anime"
            except Exception:
                is_anime_context = False
        if not is_anime_context:
            try:
                is_anime_context = bool(getattr(mi_any, "mal_id", None))
            except Exception:
                is_anime_context = False
        if not is_anime_context:
            try:
                is_anime_context = str(media_type or "").strip().lower() == "anime"
            except Exception:
                is_anime_context = False
        providers_order = _provider_order_for_item(
            media_type,
            getattr(mi_any, 'mal_id', None),
            cfg_movies,
            cfg_anime,
            getattr(mi_any, 'media_root', None),
            root_type,
        )
        tmdb_media_type = _tmdb_search_media_type_for_item(mi_any, media_type) or media_type
        provider_error = False
        try:
            tmdb_ok = _tmdb_configured()
        except Exception:
            tmdb_ok = False
        root_allows_tmdb = _tmdb_allowed_for_media_item(session, mi_any, manual_override=False)
        if "tmdb" in providers_order and not root_allows_tmdb:
            providers_order = [p for p in providers_order if p != "tmdb"]

        inherited = _inherit_tmdb_identity_from_parent_if_season(mi_any, session)
        if root_allows_tmdb and inherited and tmdb is not None and hasattr(tmdb, "tmdb_fetch_season"):
            parent_tmdb_id, season_number = inherited
            try:
                lang = str(get("metadata_language") or "").strip() or "en-US"
            except Exception:
                lang = "en-US"
            try:
                mi_any.tmdb_id = str(parent_tmdb_id)
                mi_any.media_type = 'series'
                session.add(mi_any)
                session.commit()
            except Exception:
                session.rollback()
            try:
                normalized = tmdb.tmdb_fetch_season(str(parent_tmdb_id), season_number=season_number, language=lang)
            except Exception:
                normalized = None
            # Split-season fallback (common: "Part 2" is stored as local Season 2 but TMDB keeps it under Season 1).
            # If TMDB has no /season/2, still persist a TMDB identity so the later sync step can map episodes by offset.
            if not (isinstance(normalized, dict) and normalized) and int(season_number) == 2 and hasattr(tmdb, "tmdb_fetch_by_id"):
                try:
                    normalized = tmdb.tmdb_fetch_by_id(str(parent_tmdb_id), media_type="tv", language=lang)
                except Exception:
                    normalized = None
            if isinstance(normalized, dict) and normalized:
                normalized.setdefault("season_number", int(season_number))
                _attach_tmdb_i18n(normalized, 'series')
                _attach_episodes('tmdb', normalized, 'series', mi_any)
                _persist_metadata(session, mi_any, normalized)
            try:
                _map_episode_files_for_item(mi_any, session)
            except Exception:
                pass
            try:
                if isinstance(normalized, dict):
                    _apply_jikan_episode_titles(mi_any, normalized, session)
            except Exception:
                pass
            return True

        # Fast-path: when we already have a stable TMDB identity, avoid name-based searches entirely.
        #
        # Critical for season items: some libraries keep Season 1 episodes directly in the parent folder.
        # In that case the folder name doesn't contain "Season 1", so `_inherit_tmdb_identity_from_parent_if_season`
        # won't trigger. However, the DB SeasonItem bridge tells us the season_number, so we can fetch
        # `/tv/{tmdb_id}/season/{season_number}` deterministically (no drift to unrelated titles).
        try:
            if root_allows_tmdb and tmdb_ok and tmdb is not None and _tmdb_configured():
                mt0 = str(getattr(mi_any, "media_type", None) or "").strip().lower()
                tmdb_id0 = str(getattr(mi_any, "tmdb_id", None) or "").strip() or None

                # If this MediaItem is mapped to a Season row, fetch that season directly.
                if mt0 in ("series", "tv", "show") and hasattr(tmdb, "tmdb_fetch_season"):
                    series0, season0 = get_series_and_season_for_item(mi_any, session)
                    sn0 = None
                    if season0 is not None:
                        try:
                            sn0 = getattr(season0, "season_number", None)
                        except Exception:
                            sn0 = None
                    sn_int = None
                    if sn0 is not None:
                        try:
                            sn_int = int(sn0)
                        except Exception:
                            sn_int = None

                    series_tmdb = None
                    try:
                        series_tmdb = str(getattr(series0, "tmdb_id", None) or "").strip() or None
                    except Exception:
                        series_tmdb = None

                    tmdb_id_eff = tmdb_id0 or series_tmdb
                    if tmdb_id_eff and sn_int is not None:
                        try:
                            lang0 = str(get("metadata_language") or "").strip() or "en-US"
                        except Exception:
                            lang0 = "en-US"
                        try:
                            # Persist identity on the MediaItem for future runs (best-effort).
                            if not tmdb_id0:
                                mi_any.tmdb_id = str(tmdb_id_eff)
                                session.add(mi_any)
                                session.commit()
                        except Exception:
                            session.rollback()

                        try:
                            normalized = tmdb.tmdb_fetch_season(str(tmdb_id_eff), season_number=int(sn_int), language=lang0)
                        except Exception:
                            normalized = None
                        if not (isinstance(normalized, dict) and normalized) and int(sn_int) == 2 and hasattr(tmdb, "tmdb_fetch_by_id"):
                            # Same split-season fallback as above.
                            try:
                                normalized = tmdb.tmdb_fetch_by_id(str(tmdb_id_eff), media_type="tv", language=lang0)
                            except Exception:
                                normalized = None
                        if isinstance(normalized, dict) and normalized and _tmdb_match_allowed_for_item(normalized, mi_any):
                            normalized.setdefault("season_number", int(sn_int))
                            _attach_tmdb_i18n(normalized, 'series')
                            _attach_episodes('tmdb', normalized, 'series', mi_any)
                            _persist_metadata(session, mi_any, normalized)
                            try:
                                _map_episode_files_for_item(mi_any, session)
                            except Exception:
                                pass
                            return True

                # Non-season items with an explicit TMDB id: fetch by id (tv/movie) without searching.
                if tmdb_id0 and hasattr(tmdb, "tmdb_fetch_by_id"):
                    try:
                        lang0 = str(get("metadata_language") or "").strip() or "en-US"
                    except Exception:
                        lang0 = "en-US"
                    media_pref0 = "tv" if mt0 in ("series", "tv", "show", "anime") else "movie"
                    try:
                        normalized = tmdb.tmdb_fetch_by_id(str(tmdb_id0), media_type=media_pref0, language=lang0)
                    except Exception:
                        normalized = None
                    if isinstance(normalized, dict) and normalized and _tmdb_match_allowed_for_item(normalized, mi_any):
                        _attach_tmdb_i18n(normalized, media_pref0)
                        _attach_episodes('tmdb', normalized, mt0, mi_any)
                        _persist_metadata(session, mi_any, normalized)
                        try:
                            _map_episode_files_for_item(mi_any, session)
                        except Exception:
                            pass
                        return True
        except Exception:
            pass

        from ..tools.cache import cache_get_negative, cache_set_negative
        # Versioned negative-cache key: candidate generation & provider strategies evolve.
        # Bumping prevents stale misses (e.g. when we start trying >1 Jikan candidate).
        search_query_key = f"search:v2:{media_type}:{candidate_titles[0]}" if candidate_titles else None
        if search_query_key and cache_get_negative(search_query_key):
            mi_any.status = 'NO_MATCH'
            session.add(mi_any)
            session.commit()
            return True

        for p in providers_order:
            try:
                res = None
                if p == 'jikan' and jikan is not None:
                    if not is_anime_context:
                        continue
                    if getattr(mi_any, 'mal_id', None) and hasattr(jikan, 'fetch_by_id'):
                        res = jikan.fetch_by_id(str(getattr(mi_any, "mal_id")))
                    if not res:
                        # Jikan can be sensitive to season-number-first queries ("Title 2", "Title 1"),
                        # so try a small window of candidates to avoid false NO_MATCH.
                        for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                            r = jikan.search(cand) if hasattr(jikan, 'search') else None
                            if isinstance(r, list) and r:
                                res = r[0]
                                break
                            if r and not isinstance(r, list):
                                res = r
                elif p == "kitsu" and kitsu is not None:
                    if not is_anime_context:
                        continue
                    # Kitsu matching is title-based (no MAL id). Use a strict similarity threshold
                    # by default to avoid wrong metadata injection.
                    try:
                        min_conf = float(os.environ.get("ARCANEA_KITSU_MIN_CONF", "0.92") or 0.92)
                    except Exception:
                        min_conf = 0.92
                    # If the MediaItem already has a Kitsu identity, fetch directly (no name drift).
                    try:
                        prov0 = str(getattr(mi_any, "provider", "") or "").strip().lower()
                        pid0 = str(getattr(mi_any, "provider_id", "") or "").strip()
                    except Exception:
                        prov0 = ""
                        pid0 = ""
                    if prov0 == "kitsu" and pid0 and hasattr(kitsu, "fetch_by_id"):
                        try:
                            d = kitsu.fetch_by_id(pid0)  # type: ignore[attr-defined]
                            if d and _kitsu_match_allowed_for_item(d, mi):
                                res = d
                        except Exception:
                            res = None
                    if not res:
                        for cand in candidate_titles:
                            try:
                                if hasattr(kitsu, "best_match"):
                                    r = kitsu.best_match(cand, min_conf=min_conf)  # type: ignore[attr-defined]
                                else:
                                    rows = kitsu.search(cand) if hasattr(kitsu, "search") else None
                                    r = rows[0] if isinstance(rows, list) and rows else None
                            except Exception:
                                r = None
                            if r and _kitsu_match_allowed_for_item(r, mi):
                                res = r
                                break
                elif p == 'tmdb' and root_allows_tmdb and tmdb is not None and tmdb_ok:
                    for cand in candidate_titles:
                        r = _tmdb_search_with_optional_season(cand, tmdb_media_type)
                        if isinstance(r, list) and r:
                            res = r[0]
                            break
                        if r and not isinstance(r, list):
                            # Guard: when anime is configured to use TMDB (or auto fell back), require near-exact
                            # title similarity against the folder title to avoid bad matches.
                            try:
                                mt_a = (media_type or "").strip().lower()
                                is_anime_item = is_anime_context or (mt_a == "anime")
                            except Exception:
                                is_anime_item = bool(is_anime_context)
                            try:
                                cfg_a = str(cfg_anime_s or "").strip().lower()
                            except Exception:
                                cfg_a = ""
                            if is_anime_item and cfg_a in ("tmdb", "auto"):
                                try:
                                    min_conf = float(os.environ.get("ARCANEA_ANIME_FALLBACK_TMDB_MIN_CONF", "0.97") or 0.97)
                                except Exception:
                                    min_conf = 0.97
                                base0, _sn0 = _extract_season_from_title(cand)
                                conf = _tmdb_title_confidence(base0 or cand, r)
                                if conf < float(min_conf):
                                    continue
                            res = r
                            break
                if not res:
                    continue
                normalized = _normalize_result(p, res)
                if normalized:
                    break
            except Exception:
                provider_error = True

        if not normalized and not provider_error and search_query_key:
            cache_set_negative(search_query_key, ttl=(7 * 24 * 60 * 60))

        # Last-resort TMDB fallback.
        #
        # Default: for anime libraries when cfg_anime=jikan, avoid falling back to TMDB to prevent incorrect
        # matches. However, when Jikan is unreachable (timeouts/5xx) the user may prefer a strict TMDB
        # fallback based on folder name to avoid large NO_MATCH waves.
        #
        # Policy:
        # - If NOT anime-context: allow TMDB fallback (legacy behavior).
        # - If anime-context AND cfg_anime=jikan: allow TMDB fallback ONLY when a provider error occurred
        #   (i.e. Jikan threw), and ONLY when title similarity >= 0.97.
        if not normalized and root_allows_tmdb and tmdb_ok:
            try:
                enable_anime_fallback = str(os.environ.get("ARCANEA_ANIME_FALLBACK_TMDB_ON_JIKAN_ERROR", "1") or "1").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            except Exception:
                enable_anime_fallback = True
            try:
                min_conf = float(os.environ.get("ARCANEA_ANIME_FALLBACK_TMDB_MIN_CONF", "0.97") or 0.97)
            except Exception:
                min_conf = 0.97

            def _tmdb_title_conf(base_title: str, tmdb_payload: dict) -> float:
                try:
                    import difflib
                    import re
                except Exception:
                    return 0.0

                def _n(s: str | None) -> str:
                    if not s:
                        return ""
                    ss = str(s).strip().lower()
                    ss = re.sub(r"[^a-z0-9]+", " ", ss)
                    ss = re.sub(r"\s{2,}", " ", ss).strip()
                    return ss

                bt = _n(base_title)
                if not bt:
                    return 0.0
                best = 0.0
                for k in (
                    "title",
                    "name",
                    "series_title",
                    "original_title",
                    "original_name",
                    "title_en",
                    "title_localized",
                ):
                    try:
                        v = tmdb_payload.get(k)
                    except Exception:
                        v = None
                    if not isinstance(v, str) or not v.strip():
                        continue
                    try:
                        r = difflib.SequenceMatcher(None, bt, _n(v)).ratio()
                    except Exception:
                        r = 0.0
                    if r > best:
                        best = r
                return float(best)

            # Non-anime context: legacy fallback (no strict gate).
            if not (is_anime_context and cfg_anime_s == "jikan"):
                try:
                    cand0 = str(candidate_titles[0] if candidate_titles else title).strip()
                    if cand0:
                        r = _tmdb_search_with_optional_season(str(cand0), tmdb_media_type)
                        if isinstance(r, list) and r:
                            r = r[0]
                        if r:
                            normalized = _normalize_result("tmdb", r)
                except Exception:
                    pass
            else:
                # Anime context: strict fallback only on provider errors.
                if enable_anime_fallback and provider_error:
                    try:
                        # Use folder name first to avoid accidentally using episode-like titles.
                        try:
                            cp0 = getattr(mi_any, "canonical_path", None)
                            folder0 = os.path.basename(str(cp0)).strip() if cp0 else ""
                        except Exception:
                            folder0 = ""
                        cand0 = str(folder0 or (candidate_titles[0] if candidate_titles else title)).strip()
                        if cand0:
                            base0, season0 = _extract_season_from_title(cand0)
                            # Force a TV search for season-like folders; otherwise use the standard heuristic.
                            tv_pref = "tv" if season0 else ("tv" if str(tmdb_media_type or "").strip().lower() in ("tv", "series", "show", "anime") else "movie")
                            # Ignore negative cache: provider error may have caused stale misses.
                            r = None
                            if tmdb is not None and hasattr(tmdb, "search_by_type"):
                                r = tmdb.search_by_type(base0 or cand0, media_preference=tv_pref, ignore_negative_cache=True)  # type: ignore[attr-defined]
                            if isinstance(r, list) and r:
                                r = r[0]
                            if isinstance(r, dict) and r:
                                conf = _tmdb_title_conf(base0 or cand0, r)
                                if conf >= float(min_conf):
                                    # If we have a season hint and can fetch season metadata, do it.
                                    if season0 and hasattr(tmdb, "tmdb_fetch_season"):
                                        try:
                                            tid = r.get("tmdb_id") or r.get("provider_id") or r.get("id")
                                        except Exception:
                                            tid = None
                                        if tid:
                                            try:
                                                lang = str(get("metadata_language") or "").strip() or "en-US"
                                            except Exception:
                                                lang = "en-US"
                                            try:
                                                sd = tmdb.tmdb_fetch_season(str(tid), season_number=int(season0), language=lang)  # type: ignore[attr-defined]
                                            except Exception:
                                                sd = None
                                            if isinstance(sd, dict) and sd:
                                                r = sd
                                    logger.info(
                                        "enrichment: anime fallback jikan->tmdb accepted (conf=%.3f, min=%.3f) media_item=%s query=%s base=%s",
                                        conf,
                                        float(min_conf),
                                        getattr(mi_any, "id", None),
                                        cand0,
                                        base0 or cand0,
                                    )
                                    normalized = _normalize_result("tmdb", r)
                                else:
                                    logger.info(
                                        "enrichment: anime fallback jikan->tmdb rejected (conf=%.3f < %.3f) media_item=%s query=%s tmdb_title=%s",
                                        conf,
                                        float(min_conf),
                                        getattr(mi_any, "id", None),
                                        cand0,
                                        (r.get("title") or r.get("name") or r.get("series_title") or ""),
                                    )
                    except Exception:
                        pass

        if not normalized:
            if not provider_error:
                mi_any.title = _fallback_title_for_item(mi_any)
                mi_any.status = 'NO_MATCH'
                mi_any.is_identified = False
                session.add(mi_any)
                session.commit()
                return True
            return False

        _attach_tmdb_i18n(normalized, media_type)
        _attach_episodes(normalized.get('provider') or '', normalized, media_type, mi_any)
        normalized = _supplement_jikan_with_kitsu(mi_any, normalized, media_type)
        if str(normalized.get("provider") or "").strip().lower() == "jikan" and str(getattr(mi_any, "mal_id", "") or "").strip() == str(normalized.get("provider_id") or normalized.get("mal_id") or "").strip() and not normalized.get("_episodes_from") and isinstance(normalized.get("episodes"), list) and normalized.get("episodes"):
            normalized["_episodes_from"] = "jikan_id"
        _persist_metadata(session, mi_any, normalized)
        try:
            _map_episode_files_for_item(mi_any, session)
        except Exception:
            pass
        try:
            _apply_jikan_episode_titles(mi_any, normalized, session)
        except Exception:
            pass
        return True
    except Exception:
        logger.exception('Failed enriching media_item %s', getattr(mi_any, "id", None))
        return False
    finally:
        clear_state()


def enrich_pending(limit: Optional[int] = None) -> int:
    processed = 0
    session = SessionLocal()
    task_id = None
    task_status = "ok"
    ctx_tokens = None
    try:
        try:
            task_id = f"enrich_pending:{int(time.time())}"
        except Exception:
            task_id = None
        try:
            if task_id:
                ctx_tokens = telemetry.set_task_context(task_id, "enrich_pending")
        except Exception:
            ctx_tokens = None
        try:
            if task_id:
                telemetry.task_start(task_id, "enrich_pending", limit=limit)
        except Exception:
            pass
        subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
        retry_no_match = str(os.environ.get("ARCANEA_ENRICH_RETRY_NO_MATCH", "")).strip().lower() in ("1", "true", "yes", "on")
        if retry_no_match:
            q = session.query(MediaItem).filter(~MediaItem.id.in_(subq)).filter(
                (MediaItem.status == None)
                | (
                    (MediaItem.status != 'ERROR')
                    & (MediaItem.status != 'MANUAL')
                    & (MediaItem.status != 'OMITTED')
                )
            )
        else:
            q = session.query(MediaItem).filter(~MediaItem.id.in_(subq)).filter(
                (MediaItem.status == None)
                | (
                    (MediaItem.status != 'ERROR')
                    & (MediaItem.status != 'NO_MATCH')
                    & (MediaItem.status != 'MANUAL')
                    & (MediaItem.status != 'OMITTED')
                )
            )
        if limit:
            q = q.limit(limit)
        candidates = q.all()
        cfg_movies = get('metadata_movies_provider') or 'tmdb'
        cfg_anime = get('metadata_anime_provider') or 'kitsu'

        for mi in candidates:
            mi = _cast(_Any, mi)
            try:
                try:
                    set_state({'running': True, 'current_id': mi.id, 'current_title': mi.title, 'current_step': 'enrich_pending'})
                except Exception:
                    pass
                inferred = _decide_media_type(mi, session) or 'movie'
                cur_mt = (mi.media_type or '').strip().lower()
                # If a folder has multiple files, treat it as a series even if an older scan marked it as movie.
                # (Avoids TMDB movie-first mis-matches like "Halo Temporada 2" -> "Halo Wars".)
                if inferred == 'series' and cur_mt not in ('series', 'tv', 'show'):
                    mi.media_type = 'series'
                else:
                    mi.media_type = mi.media_type or inferred
                media_type = mi.media_type or inferred
                title = mi.title or ''
                normalized = {}
                candidate_titles = _candidate_titles_for_item(mi)
                root_type = _root_type_for_media_item(session, mi)
                is_anime_context = False
                try:
                    is_anime_context = str(root_type or "").strip().lower() == "anime"
                except Exception:
                    is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = str(getattr(mi, "library_type", "") or "").strip().lower() == "anime"
                    except Exception:
                        is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = bool(getattr(mi, "mal_id", None))
                    except Exception:
                        is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = str(media_type or "").strip().lower() == "anime"
                    except Exception:
                        is_anime_context = False
                providers_order = _provider_order_for_item(
                    media_type,
                    getattr(mi, 'mal_id', None),
                    cfg_movies,
                    cfg_anime,
                    getattr(mi, 'media_root', None),
                    root_type,
                )
                provider_error = False
                try:
                    tmdb_ok = _tmdb_configured()
                except Exception:
                    tmdb_ok = False
                root_allows_tmdb = _tmdb_allowed_for_media_item(session, mi, manual_override=False)
                logger.info("enrichment: media_item=%s media_type=%s providers_order=%s tmdb_configured=%s", getattr(mi, 'id', None), media_type, providers_order, tmdb_ok)

                # Strict hierarchy: if this looks like a season subfolder and parent already has TMDB identity,
                # skip TMDB search and fetch the season directly.
                try:
                    inherited = _inherit_tmdb_identity_from_parent_if_season(mi, session)
                except Exception:
                    inherited = None
                if root_allows_tmdb and inherited and tmdb is not None and hasattr(tmdb, "tmdb_fetch_season"):
                    parent_tmdb_id, season_number = inherited
                    try:
                        lang = str(get("metadata_language") or "").strip() or "en-US"
                    except Exception:
                        lang = "en-US"
                    try:
                        mi.tmdb_id = str(parent_tmdb_id)
                        mi.media_type = 'series'
                        session.add(mi)
                        session.commit()
                    except Exception:
                        session.rollback()
                    try:
                        normalized = tmdb.tmdb_fetch_season(str(parent_tmdb_id), season_number=season_number, language=lang)  # type: ignore[arg-type]
                    except Exception:
                        normalized = None
                    if isinstance(normalized, dict) and normalized:
                        try:
                            # Ensure season_number is present so i18n/episode attachment behave.
                            normalized.setdefault("season_number", int(season_number))
                        except Exception:
                            pass
                        try:
                            set_state({'current_step': 'persist_metadata'})
                        except Exception:
                            pass
                        try:
                            _attach_tmdb_i18n(normalized, 'series')
                            _attach_episodes('tmdb', normalized, 'series', mi)
                        except Exception:
                            pass
                        _persist_metadata(session, mi, normalized)
                        processed += 1
                        continue

                if providers_order == ['tmdb'] and (not root_allows_tmdb or not tmdb_ok):
                    logger.warning(
                        "enrichment: TMDB not allowed/configured; skipping enrichment for media_item=%s",
                        getattr(mi, "id", None),
                    )
                    try:
                        try:
                            mi.title = _fallback_title_for_item(mi)
                        except Exception:
                            pass
                        mi.status = "NO_MATCH"
                        try:
                            mi.is_identified = False
                        except Exception:
                            pass
                        session.add(mi)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue
                try:
                    if 'tmdb' in providers_order and (not root_allows_tmdb or not tmdb_ok):
                        logger.info('enrichment: removing TMDB from providers_order for media_item=%s', getattr(mi, 'id', None))
                        providers_order = [p for p in providers_order if p != 'tmdb']
                except Exception:
                    pass
                if not providers_order:
                    logger.warning(
                        "enrichment: empty providers_order; skipping enrichment for media_item=%s",
                        getattr(mi, "id", None),
                    )
                    try:
                        try:
                            mi.title = _fallback_title_for_item(mi)
                        except Exception:
                            pass
                        mi.status = "NO_MATCH"
                        try:
                            mi.is_identified = False
                        except Exception:
                            pass
                        session.add(mi)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                # Negative cache check for search queries
                from ..tools.cache import cache_get_negative, cache_set_negative
                # Versioned negative-cache key: candidate generation & provider strategies evolve.
                search_query_key = f"search:v2:{media_type}:{candidate_titles[0]}" if candidate_titles else None
                if search_query_key and cache_get_negative(search_query_key):
                    logger.info("enrichment: search query in negative cache, skipping: %s", search_query_key)
                    try:
                        mi.status = 'NO_MATCH'
                        session.add(mi)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                for p in providers_order:
                    try:
                        res = None
                        if p == 'jikan' and jikan is not None:
                            if not is_anime_context:
                                continue
                            if getattr(mi, 'mal_id', None) and hasattr(jikan, 'fetch_by_id'):
                                try:
                                    logger.info('enrichment: calling jikan.fetch_by_id mal_id=%s media_item=%s', getattr(mi, 'mal_id', None), getattr(mi, 'id', None))
                                    res = jikan.fetch_by_id(str(mi.mal_id))
                                except Exception:
                                    logger.exception('enrichment: jikan.fetch_by_id failed for media_item=%s', getattr(mi, 'id', None))
                                    provider_error = True
                                    res = None
                            if not res:
                                # Jikan can be sensitive to season-number-first queries ("Title 2", "Title 1"),
                                # so try a small window of candidates to avoid false NO_MATCH.
                                for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                                    try:
                                        logger.info('enrichment: jikan.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                        r = jikan.search(cand) if hasattr(jikan, 'search') else None
                                    except Exception:
                                        logger.warning('enrichment: jikan.search failed for query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                        provider_error = True
                                        r = None
                                        break
                                    if isinstance(r, list) and r:
                                        res = r[0]
                                        break
                                    if r and not isinstance(r, list):
                                        res = r
                        elif p == "kitsu" and kitsu is not None:
                            if not is_anime_context:
                                continue
                            try:
                                min_conf = float(os.environ.get("ARCANEA_KITSU_MIN_CONF", "0.92") or 0.92)
                            except Exception:
                                min_conf = 0.92
                            # If the MediaItem already has a Kitsu identity, fetch directly.
                            try:
                                prov0 = str(getattr(mi, "provider", "") or "").strip().lower()
                                pid0 = str(getattr(mi, "provider_id", "") or "").strip()
                            except Exception:
                                prov0 = ""
                                pid0 = ""
                            if prov0 == "kitsu" and pid0 and hasattr(kitsu, "fetch_by_id"):
                                try:
                                    d = kitsu.fetch_by_id(pid0)  # type: ignore[attr-defined]
                                    if d and _kitsu_match_allowed_for_item(d, mi):
                                        res = d
                                except Exception:
                                    provider_error = True
                                    res = None
                            if not res:
                                for cand in candidate_titles:
                                    try:
                                        if hasattr(kitsu, "best_match"):
                                            r = kitsu.best_match(cand, min_conf=min_conf)  # type: ignore[attr-defined]
                                        else:
                                            rows = kitsu.search(cand) if hasattr(kitsu, "search") else None
                                            r = rows[0] if isinstance(rows, list) and rows else None
                                    except Exception:
                                        provider_error = True
                                        r = None
                                    if r and _kitsu_match_allowed_for_item(r, mi):
                                        res = r
                                        break
                        elif p == 'tmdb' and root_allows_tmdb and tmdb is not None:
                            try:
                                for cand in candidate_titles:
                                    try:
                                        logger.info('enrichment: tmdb.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                        r = _tmdb_search_with_optional_season(cand, media_type)
                                        if r:
                                            logger.info('enrichment: tmdb.search FOUND result for query="%s"', cand)
                                        else:
                                            logger.info('enrichment: tmdb.search NO result for query="%s"', cand)
                                    except Exception:
                                        logger.exception('enrichment: tmdb.search failed for query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                        provider_error = True
                                        r = None
                                    if isinstance(r, list) and r:
                                        res = r[0]
                                        break
                                    if r and not isinstance(r, list):
                                        # Guard: when anime is configured to use TMDB (or auto fell back), require near-exact
                                        # title similarity against the folder title to avoid bad matches.
                                        try:
                                            is_anime_item = str(getattr(mi, "library_type", "") or "").strip().lower() == "anime"
                                        except Exception:
                                            is_anime_item = False
                                        try:
                                            cfg_a = str(cfg_anime or "").strip().lower()
                                        except Exception:
                                            cfg_a = ""
                                        if is_anime_item and cfg_a in ("tmdb", "auto"):
                                            try:
                                                min_conf = float(os.environ.get("ARCANEA_ANIME_FALLBACK_TMDB_MIN_CONF", "0.97") or 0.97)
                                            except Exception:
                                                min_conf = 0.97
                                            base0, _sn0 = _extract_season_from_title(cand)
                                            conf = _tmdb_title_confidence(base0 or cand, r)
                                            if conf < float(min_conf):
                                                continue
                                        if not _tmdb_match_allowed_for_item(r, mi, cand):
                                            continue
                                        res = r
                                        break
                            except Exception:
                                logger.exception('enrichment: unexpected tmdb provider error for media_item=%s', getattr(mi, 'id', None))
                                provider_error = True
                                pass

                        if not res:
                            continue
                        normalized = _normalize_result(p, res)
                        if normalized:
                            break
                    except Exception:
                        logger.debug('Provider %s failed for title=%s', p, title, exc_info=True)

                # Set negative cache if no provider matched
                if not normalized and not provider_error and search_query_key:
                    cache_set_negative(search_query_key, ttl=(7 * 24 * 60 * 60))

                if not normalized:
                    # Bridge: if Jikan couldn't match and TMDB is configured, try a single TMDB lookup as TV.
                    # This is useful for folders with noisy suffixes (Part/Cour/Especiales) or misclassified libraries.
                    try:
                        if root_allows_tmdb and tmdb_ok and ('tmdb' not in providers_order) and ('jikan' in providers_order or providers_order == ['jikan']):
                            is_series = (media_type or '').strip().lower() in ('series', 'tv', 'anime')
                            if not is_series:
                                try:
                                    is_series = session.query(FileRecord).filter(FileRecord.media_item_id == mi.id).count() > 1
                                except Exception:
                                    is_series = False
                            if is_series:
                                cand0 = candidate_titles[0] if candidate_titles else (mi.title or '')
                                cand0 = str(cand0 or '').strip()
                                if cand0:
                                    logger.info('enrichment: bridge jikan->tmdb (tv) query="%s" media_item=%s', cand0, getattr(mi, 'id', None))
                                    r = _tmdb_search_with_optional_season(cand0, 'tv')
                                    if isinstance(r, list) and r:
                                        r = r[0]
                                    if r and isinstance(r, dict):
                                        normalized = _normalize_result('tmdb', r)
                    except Exception:
                        pass

                if not normalized:
                    try:
                        logger.info('enrichment: no-match for media_item=%s candidates=%s', getattr(mi, 'id', None), candidate_titles)
                    except Exception:
                        pass
                    try:
                        set_state({'current_step': 'provider_error' if provider_error else 'no_match'})
                    except Exception:
                        pass
                    # If provider calls failed (rate limit / network), keep it pending to retry later.
                    # If provider calls succeeded but no match was found, mark as NO_MATCH to avoid endless retries.
                    if not provider_error:
                        try:
                            try:
                                mi.title = _fallback_title_for_item(mi)
                            except Exception:
                                pass
                            mi.status = 'NO_MATCH'
                            try:
                                mi.is_identified = False
                            except Exception:
                                pass
                            session.add(mi)
                            session.commit()
                        except Exception:
                            session.rollback()
                    processed += 1 if not provider_error else 0
                    continue

                try:
                    suggested = classify_metadata(normalized)
                    if suggested:
                        mi.media_type = suggested
                except Exception:
                    pass
                try:
                    set_state({'current_step': 'persist_metadata'})
                except Exception:
                    pass
                try:
                    _attach_tmdb_i18n(normalized, media_type)
                    _attach_episodes(normalized.get('provider') or '', normalized, media_type, mi)
                    normalized = _supplement_jikan_with_kitsu(mi, normalized, media_type)
                    if str(normalized.get("provider") or "").strip().lower() == "jikan" and str(getattr(mi, "mal_id", "") or "").strip() == str(normalized.get("provider_id") or normalized.get("mal_id") or "").strip() and not normalized.get("_episodes_from") and isinstance(normalized.get("episodes"), list) and normalized.get("episodes"):
                        normalized["_episodes_from"] = "jikan_id"
                except Exception:
                    pass
                _persist_metadata(session, mi, normalized)
                processed += 1
            except Exception:
                logger.exception('Failed enriching media_item %s', mi.id)
            finally:
                try:
                    clear_state()
                except Exception:
                    pass
        return processed
    except Exception:
        task_status = "error"
        raise
    finally:
        try:
            if task_id:
                telemetry.task_end(task_id, task_status)
        except Exception:
            pass
        try:
            if ctx_tokens is not None:
                telemetry.reset_task_context(ctx_tokens)
        except Exception:
            pass
        session.close()


def enrich_from_tmdb_id(
    *,
    media_item_id: int,
    tmdb_id: str,
    media_type: str | None = None,
    season_number: int | None = None,
    language: str | None = None,
    download_assets: bool | None = None,
) -> dict:
    session = SessionLocal()
    task_id = f"rematch:{media_item_id}:{int(time.time())}"
    task_status = "ok"
    ctx_tokens = None
    try:
        try:
            ctx_tokens = telemetry.set_task_context(task_id, "rematch")
        except Exception:
            ctx_tokens = None
        try:
            telemetry.task_start(task_id, "rematch", media_item_id=media_item_id, tmdb_id=tmdb_id)
        except Exception:
            pass

        mi = session.query(MediaItem).filter(MediaItem.id == int(media_item_id)).first()
        if not mi:
            task_status = "no_match"
            return {"ok": False, "detail": "media_item_not_found"}
        mi_any = _cast(_Any, mi)

        # normalize media_type for TMDB
        mt = str(media_type or getattr(mi, "media_type", "") or "").strip().lower()
        if mt in ("series", "tv", "show", "anime"):
            tmdb_mt = "tv"
        elif mt in ("movie", "film"):
            tmdb_mt = "movie"
        else:
            tmdb_mt = "tv"

        try:
            if tmdb_id:
                mi_any.tmdb_id = str(tmdb_id)
            if media_type:
                mi_any.media_type = str(media_type)
            session.add(mi)
        except Exception:
            pass

        res = None
        if tmdb is None:
            task_status = "error"
            return {"ok": False, "detail": "tmdb_not_available"}
        if season_number is not None and hasattr(tmdb, "tmdb_fetch_season"):
            try:
                with telemetry.phase("TMDB_Enrichment"):
                    res = tmdb.tmdb_fetch_season(str(tmdb_id), season_number=int(season_number), language=language)
            except Exception:
                res = None
        if res is None:
            try:
                with telemetry.phase("TMDB_Enrichment"):
                    res = tmdb.tmdb_fetch_by_id(str(tmdb_id), media_type=tmdb_mt, language=language)
            except Exception:
                res = None

        if not isinstance(res, dict) or not res:
            task_status = "no_match"
            return {"ok": False, "detail": "tmdb_not_found"}

        normalized = _normalize_result("tmdb", res)
        if not normalized:
            task_status = "no_match"
            return {"ok": False, "detail": "tmdb_not_normalized"}

        try:
            _attach_tmdb_i18n(normalized, tmdb_mt)
            _attach_episodes("tmdb", normalized, tmdb_mt, mi)
        except Exception:
            pass

        try:
            _persist_metadata(session, mi, normalized)
        except Exception as e:
            task_status = "error"
            return {"ok": False, "detail": str(e)}
        try:
            _map_episode_files_for_item(mi, session)
        except Exception:
            pass
        try:
            if _apply_jikan_episode_titles(mi, normalized, session):
                session.commit()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass

        if download_assets:
            try:
                img_url = _pick_best_image_url(normalized.get("images"), kind="poster")
            except Exception:
                img_url = None
            if img_url:
                try:
                    _download_image_and_record(session, mi, str(img_url), source="manual", kind="poster")
                except Exception:
                    pass

        return {"ok": True, "media_item_id": int(media_item_id), "tmdb_id": str(tmdb_id)}
    except Exception as e:
        task_status = "error"
        return {"ok": False, "detail": str(e)}
    finally:
        try:
            telemetry.task_end(task_id, task_status)
        except Exception:
            pass
        try:
            if ctx_tokens is not None:
                telemetry.reset_task_context(ctx_tokens)
        except Exception:
            pass
        session.close()


def reconcile_tmdb_season_mapping(
    *,
    tmdb_id: str,
    series_id: int | None = None,
    gap_days: int = 45,
) -> dict[str, int | str | bool]:
    if tmdb is None or not hasattr(tmdb, "tmdb_fetch_season"):
        return {"ok": False, "detail": "tmdb_not_available", "seasons_updated": 0}
    try:
        if not _tmdb_configured():
            return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0}
    except Exception:
        return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0}

    tid = str(tmdb_id or "").strip()
    if tid.lower() in ("none", "null"):
        tid = ""
    if not tid:
        return {"ok": False, "detail": "tmdb_id_required", "seasons_updated": 0}

    session = SessionLocal()
    seasons_updated = 0
    try:
        series_row = None
        try:
            if series_id is not None:
                series_row = (
                    session.query(Series)
                    .filter(Series.id == int(series_id))
                    .order_by(Series.id.asc())
                    .first()
                )
        except Exception:
            series_row = None

        if series_id is None:
            try:
                srow = session.query(Series).filter(Series.tmdb_id == tid).order_by(Series.id.asc()).first()
            except Exception:
                srow = None
            if srow is None:
                return {"ok": False, "detail": "series_not_found", "seasons_updated": 0}
            series_id = int(getattr(srow, "id"))
            series_row = srow

        seasons = []
        try:
            seasons = session.query(Season).filter(Season.series_id == int(series_id)).order_by(Season.season_number.asc(), Season.id.asc()).all()
        except Exception:
            seasons = []
        if not seasons:
            return {"ok": True, "detail": "no_seasons", "seasons_updated": 0}

        # Preload SeasonItem -> MediaItem and Jikan metadata (if any) for validation/split hints.
        season_to_media: dict[int, int] = {}
        jikan_meta_by_media: dict[int, dict] = {}
        try:
            season_ids = [int(getattr(s, "id")) for s in seasons if getattr(s, "id", None) is not None]
        except Exception:
            season_ids = []
        if season_ids:
            try:
                si_rows = session.query(SeasonItem).filter(SeasonItem.season_id.in_(season_ids)).all()
            except Exception:
                si_rows = []
            for si in si_rows:
                try:
                    sid = int(getattr(si, "season_id"))
                    mid = int(getattr(si, "media_item_id"))
                except Exception:
                    continue
                if sid not in season_to_media:
                    season_to_media[sid] = mid
            try:
                mi_ids = list(set(season_to_media.values()))
            except Exception:
                mi_ids = []
            if mi_ids:
                try:
                    md_rows = (
                        session.query(MediaMetadata)
                        .filter(MediaMetadata.media_item_id.in_(mi_ids))
                        .filter(MediaMetadata.provider == "jikan")
                        .order_by(MediaMetadata.id.desc())
                        .all()
                    )
                except Exception:
                    md_rows = []
                for md in md_rows:
                    try:
                        mid = int(getattr(md, "media_item_id"))
                    except Exception:
                        continue
                    if mid in jikan_meta_by_media:
                        continue
                    data = {}
                    try:
                        raw = getattr(md, "data", None)
                        if isinstance(raw, str) and raw.strip():
                            data = json.loads(raw)
                        elif isinstance(raw, dict):
                            data = raw
                    except Exception:
                        data = {}
                    jikan_meta_by_media[mid] = data

        try:
            details = tmdb.tmdb_fetch_by_id(tid, media_type="tv", language="en-US")  # type: ignore[arg-type]
        except Exception:
            details = None
        tmdb_num_seasons = None
        try:
            ns = (details or {}).get("number_of_seasons")
            tmdb_num_seasons = int(ns) if ns is not None else None
        except Exception:
            tmdb_num_seasons = None
        def _parse_date(v: str | None):
            if not isinstance(v, str) or not v.strip():
                return None
            s = v.strip()
            try:
                dt = datetime.fromisoformat(s)
                try:
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                except Exception:
                    pass
                return dt
            except Exception:
                try:
                    return datetime.strptime(s, "%Y-%m-%d")
                except Exception:
                    return None

        tmdb_season_air_dates: dict[int, datetime] = {}
        try:
            seasons_raw = (details or {}).get("seasons") or []
            if isinstance(seasons_raw, list):
                for srow in seasons_raw:
                    if not isinstance(srow, dict):
                        continue
                    sn = srow.get("season_number")
                    if sn is None:
                        continue
                    try:
                        sn_i = int(sn)
                    except Exception:
                        continue
                    dt = _parse_date(srow.get("air_date"))
                    if dt:
                        tmdb_season_air_dates[sn_i] = dt
        except Exception:
            tmdb_season_air_dates = {}

        def _date_to_iso(dt: datetime | None) -> str | None:
            try:
                return dt.date().isoformat() if dt else None
            except Exception:
                return None

        def _jikan_type(meta: dict | None, fallback: str | None = None) -> str:
            try:
                t = str((meta or {}).get("anime_type") or (meta or {}).get("type") or fallback or "").strip().lower()
            except Exception:
                t = ""
            return t

        def _jikan_start(meta: dict | None) -> datetime | None:
            if not isinstance(meta, dict):
                return None
            aired = meta.get("aired")
            if not isinstance(aired, dict):
                return None
            return _parse_date(aired.get("from"))

        def _detect_split_cour(season_data: dict | None, gap_threshold: int = 60) -> bool:
            if not isinstance(season_data, dict):
                return False
            eps = season_data.get("episodes") or []
            if not isinstance(eps, list) or not eps:
                return False
            seq = []
            for e in eps:
                if not isinstance(e, dict):
                    continue
                n = e.get("episode_number")
                if n is not None:
                    try:
                        n = int(n)
                    except Exception:
                        continue
                else:
                    continue
                dt = _parse_date(e.get("air_date"))
                if dt is None:
                    continue
                seq.append((n, dt))
            if not seq:
                return False
            seq.sort(key=lambda x: x[0])
            prev_dt = None
            for _n, dt in seq:
                if prev_dt is not None:
                    try:
                        if (dt - prev_dt).days >= int(gap_threshold):
                            return True
                    except Exception:
                        pass
                prev_dt = dt
            return False

        def _build_tmdb_episode_index(max_seasons: int | None) -> tuple[list[dict], dict[int, dict]]:
            index: list[dict] = []
            season_meta: dict[int, dict] = {}
            if max_seasons is None:
                max_seasons = 1
            try:
                max_seasons = int(max_seasons)
            except Exception:
                max_seasons = 1
            max_seasons = max(0, max_seasons)
            for sn in range(0, max_seasons + 1):
                try:
                    sdata = tmdb.tmdb_fetch_season(tid, season_number=sn, language="en-US")  # type: ignore[arg-type]
                except Exception:
                    sdata = None
                if not isinstance(sdata, dict):
                    continue
                eps = sdata.get("episodes") or []
                if not isinstance(eps, list) or not eps:
                    continue
                air_by_ep: dict[int, str] = {}
                max_ep = None
                for e in eps:
                    if not isinstance(e, dict):
                        continue
                    n = e.get("episode_number")
                    if n is None:
                        continue
                    try:
                        n = int(n)
                    except Exception:
                        continue
                    ad = e.get("air_date")
                    dt = _parse_date(ad)
                    if dt:
                        air_by_ep[int(n)] = _date_to_iso(dt) or str(ad or "").strip()
                    et = e.get("episode_type")
                    index.append(
                        {
                            "season": int(sn),
                            "episode": int(n),
                            "air_date": dt,
                            "air_date_raw": (str(ad).strip() if isinstance(ad, str) else None),
                            "episode_type": (str(et).strip().lower() if et else None),
                        }
                    )
                    try:
                        max_ep = int(n) if max_ep is None or int(n) > max_ep else max_ep
                    except Exception:
                        pass
                season_meta[int(sn)] = {
                    "air_by_ep": air_by_ep,
                    "max_episode": max_ep,
                    "split_cour": _detect_split_cour(sdata, gap_threshold=60),
                }
            return index, season_meta

        def _find_anchor(index: list[dict], target: datetime, window_days: int = 3) -> dict | None:
            best = None
            best_diff = None
            for e in index:
                dt = e.get("air_date")
                if dt is None:
                    continue
                try:
                    diff = abs((dt - target).days)
                except Exception:
                    continue
                if diff > int(window_days):
                    continue
                if best is None or (best_diff is not None and diff < best_diff):
                    best = e
                    best_diff = diff
            return best

        def _find_season_by_airdate(target: datetime, season_dates: dict[int, datetime], window_days: int = 14) -> int | None:
            best_sn = None
            best_diff = None
            for sn, dt in season_dates.items():
                try:
                    diff = abs((dt - target).days)
                except Exception:
                    continue
                if diff > int(window_days):
                    continue
                if best_sn is None or (best_diff is not None and diff < best_diff):
                    best_sn = int(sn)
                    best_diff = diff
            return best_sn

        def _segments_from_tmdb_season(season_data: dict | None, force_split_at: int | None = None) -> list[dict]:
            if not isinstance(season_data, dict):
                return []
            eps = season_data.get("episodes") or []
            if not isinstance(eps, list) or not eps:
                return []
            seq = []
            for e in eps:
                if not isinstance(e, dict):
                    continue
                n = e.get("episode_number")
                if n is not None:
                    try:
                        n = int(n)
                    except Exception:
                        continue
                else:
                    continue
                ad = e.get("air_date")
                et = e.get("episode_type")
                seq.append((n, _parse_date(ad), (str(et).strip().lower() if et else None)))
            seq.sort(key=lambda x: x[0])
            if not seq:
                return []

            # Optional forced split by known episode count (e.g., Jikan episodes_count).
            if force_split_at is not None:
                try:
                    split_n = int(force_split_at)
                except Exception:
                    split_n = None
                if split_n is not None:
                    max_n = seq[-1][0]
                    if split_n >= 1 and max_n and split_n < int(max_n):
                        by_num = {n: dt for n, dt, _ in seq}
                        def _seg(start_n: int, end_n: int) -> dict:
                            sdt = by_num.get(start_n)
                            edt = by_num.get(end_n)
                            return {
                                "start": int(start_n),
                                "end": int(end_n),
                                "air_start": sdt.date().isoformat() if sdt else None,
                                "air_end": edt.date().isoformat() if edt else None,
                            }
                        return [_seg(1, split_n), _seg(int(split_n) + 1, int(max_n))]

            segments = []
            start_idx = 0

            for i in range(1, len(seq)):
                n, dt, et = seq[i]
                prev_n, prev_dt, _prev_et = seq[i - 1]
                is_finale = False
                try:
                    if et and "final" in et:
                        is_finale = True
                except Exception:
                    is_finale = False

                is_gap = False
                if not is_finale and prev_dt is not None and dt is not None:
                    try:
                        if (dt - prev_dt).days >= int(gap_days):
                            is_gap = True
                    except Exception:
                        pass

                if is_finale:
                    end_idx = i
                    if end_idx >= start_idx:
                        seg = seq[start_idx : end_idx + 1]
                        s_n, s_dt, _ = seg[0]
                        e_n, e_dt, _ = seg[-1]
                        segments.append(
                            {
                                "start": int(s_n),
                                "end": int(e_n),
                                "air_start": s_dt.date().isoformat() if s_dt else None,
                                "air_end": e_dt.date().isoformat() if e_dt else None,
                            }
                        )
                    start_idx = i + 1
                elif is_gap:
                    end_idx = i - 1
                    if end_idx >= start_idx:
                        seg = seq[start_idx : end_idx + 1]
                        s_n, s_dt, _ = seg[0]
                        e_n, e_dt, _ = seg[-1]
                        segments.append(
                            {
                                "start": int(s_n),
                                "end": int(e_n),
                                "air_start": s_dt.date().isoformat() if s_dt else None,
                                "air_end": e_dt.date().isoformat() if e_dt else None,
                            }
                        )
                    start_idx = i

            if start_idx < len(seq):
                seg = seq[start_idx:]
                s_n, s_dt, _ = seg[0]
                e_n, e_dt, _ = seg[-1]
                segments.append(
                    {
                        "start": int(s_n),
                        "end": int(e_n),
                        "air_start": s_dt.date().isoformat() if s_dt else None,
                        "air_end": e_dt.date().isoformat() if e_dt else None,
                    }
                )
            return segments

        try:
            season1 = tmdb.tmdb_fetch_season(tid, season_number=1, language="en-US")  # type: ignore[arg-type]
        except Exception:
            season1 = None

        def _tmdb_image_url(path: str | None) -> str | None:
            if not path:
                return None
            p = str(path).strip()
            if not p:
                return None
            if p.startswith("http://") or p.startswith("https://"):
                return p
            return f"https://image.tmdb.org/t/p/original{p}"

        local = []
        for s in seasons:
            sn = getattr(s, "season_number", None)
            if sn is None:
                continue
            try:
                sn = int(sn)
            except Exception:
                continue
            eps = []
            try:
                eps = session.query(Episode).filter(Episode.season_id == s.id).all()
            except Exception:
                eps = []
            if not eps:
                continue
            nums = []
            for e in eps:
                try:
                    n = getattr(e, "episode_number", None)
                    if n is not None:
                        nums.append(int(n))
                except Exception:
                    continue
            nums = sorted(set(nums))
            if not nums:
                continue
            local.append({"season": s, "season_number": sn, "count": len(nums)})

        if not local:
            return {"ok": True, "detail": "no_episodes", "seasons_updated": 0}

        local.sort(key=lambda x: (x["season_number"], getattr(x["season"], "id", 0)))

        # Anchor mapping by Jikan start_date -> TMDB episode air_date (Â±3 days).
        anchor_map: dict[int, dict] = {}
        anchored_ids: set[int] = set()
        tmdb_episode_index: list[dict] = []
        tmdb_season_meta: dict[int, dict] = {}
        try:
            for ent in local:
                s = ent.get("season")
                if not s:
                    continue
                sid = getattr(s, "id", None)
                if sid is None:
                    continue
                try:
                    sid_int = int(sid)
                except Exception:
                    continue
                mid = season_to_media.get(sid_int)
                meta = jikan_meta_by_media.get(int(mid)) if mid is not None else None
                ent["jikan_start"] = _jikan_start(meta)
                ent["jikan_type"] = _jikan_type(meta, None)
        except Exception:
            pass

        try:
            has_anchor_input = any(ent.get("jikan_start") for ent in local)
        except Exception:
            has_anchor_input = False

        # If TMDB reports a single season but we have multiple local seasons, ensure local season_numbers
        # are sequential (1..N) based on Jikan start date. This is for frontend separation only.
        try:
            if (tmdb_num_seasons is None or int(tmdb_num_seasons) <= 1) and len(local) >= 2:
                nums = [int(ent.get("season_number") or 0) for ent in local]
                if len(set(nums)) != len(nums) or min(nums or [1]) <= 0:
                    # Sort by Jikan start date (fallback to season id).
                    ordered = sorted(
                        local,
                        key=lambda ent: (
                            ent.get("jikan_start") or datetime.min,
                            getattr(ent.get("season"), "id", 0),
                        ),
                    )
                    for idx, ent in enumerate(ordered, start=1):
                        srow = ent.get("season")
                        if not srow:
                            continue
                        try:
                            if int(getattr(srow, "season_number", 0) or 0) != idx:
                                srow.season_number = int(idx)
                                session.add(srow)
                                ent["season_number"] = int(idx)
                                # Keep media_id in sync (anchor + season number)
                                try:
                                    mid = season_to_media.get(int(getattr(srow, "id")))
                                except Exception:
                                    mid = None
                                mi = None
                                if mid is not None:
                                    try:
                                        mi = session.query(MediaItem).filter(MediaItem.id == int(mid)).first()
                                    except Exception:
                                        mi = None
                                try:
                                    anchor = (
                                        (getattr(series_row, "mal_id", None) if series_row else None)
                                        or (getattr(series_row, "tmdb_id", None) if series_row else None)
                                        or (getattr(series_row, "provider_id", None) if series_row else None)
                                        or (getattr(mi, "tmdb_id", None) if mi else None)
                                        or (getattr(mi, "mal_id", None) if mi else None)
                                        or tid
                                    )
                                    if anchor:
                                        anchor = str(anchor).strip()
                                except Exception:
                                    anchor = None
                                if anchor and mi is not None:
                                    media_id = f"{anchor}:s{idx}"
                                    try:
                                        if getattr(mi, "media_id", None) != media_id:
                                            mi_any = _cast(_Any, mi)
                                            mi_any.media_id = media_id
                                            session.add(mi)
                                    except Exception:
                                        pass
                                    try:
                                        si_row = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi.id).first()
                                    except Exception:
                                        si_row = None
                                    if si_row is not None:
                                        try:
                                            if getattr(si_row, "media_id", None) != media_id:
                                                si_any = _cast(_Any, si_row)
                                                si_any.media_id = media_id
                                                session.add(si_row)
                                        except Exception:
                                            pass
                        except Exception:
                            continue
        except Exception:
            pass

        if has_anchor_input:
            try:
                tmdb_episode_index, tmdb_season_meta = _build_tmdb_episode_index(tmdb_num_seasons)
                used = set()
                for ent in local:
                    jstart = ent.get("jikan_start")
                    if not isinstance(jstart, datetime):
                        continue
                    anchor = _find_anchor(tmdb_episode_index, jstart, window_days=3)
                    if not anchor:
                        # Fallback: match by TMDB season air_date when episode-level anchor fails.
                        sn_fallback = _find_season_by_airdate(jstart, tmdb_season_air_dates, window_days=14)
                        if sn_fallback is None:
                            continue
                        anchor = {
                            "season": int(sn_fallback),
                            "episode": 0,
                            "air_date": None,
                            "air_date_raw": None,
                            "episode_type": None,
                            "season_only": True,
                        }
                    else:
                        key = (int(anchor.get("season") or 0), int(anchor.get("episode") or 0))
                        if key in used:
                            continue
                        used.add(key)
                    s = ent.get("season")
                    if not s:
                        continue
                    try:
                        sid = int(getattr(s, "id"))
                    except Exception:
                        continue
                    anchor_map[sid] = anchor
            except Exception:
                anchor_map = {}
                tmdb_episode_index = []
                tmdb_season_meta = {}

        # Determine Jikan episodes_count for Season 1 (if available) to validate/force split.
        jikan_count = None
        try:
            for ent in local:
                if ent.get("season_number") == 1:
                    sid = getattr(ent.get("season"), "id", None)
                    mid = season_to_media.get(int(sid)) if sid is not None else None
                    data = jikan_meta_by_media.get(int(mid)) if mid is not None else None
                    if isinstance(data, dict):
                        try:
                            c = data.get("episodes_count") or data.get("episodes") or data.get("episode_count")
                            if c is not None:
                                c = int(c)
                            if c and c > 0:
                                jikan_count = int(c)
                                break
                        except Exception:
                            pass
        except Exception:
            jikan_count = None

        force_split = None
        try:
            if (tmdb_num_seasons is None or int(tmdb_num_seasons) <= 1) and len(local) >= 2 and jikan_count:
                force_split = int(jikan_count)
        except Exception:
            force_split = None

        split_cour_s1 = _detect_split_cour(season1 if isinstance(season1, dict) else None, gap_threshold=60)

        # Apply anchor mapping (Jikan start_date -> TMDB episode air_date).
        if anchor_map:
            for ent in local:
                s = ent.get("season")
                if not s:
                    continue
                try:
                    sid = int(getattr(s, "id"))
                except Exception:
                    continue
                jtype = str(ent.get("jikan_type") or "").strip().lower()
                is_special = jtype in ("ova", "ona", "special", "specials") or (getattr(s, "season_number", None) == 0)
                anchor = anchor_map.get(sid)
                if not anchor:
                    if is_special:
                        try:
                            s.tmdb_season_number = 0
                            s.tmdb_episode_start = None
                            s.tmdb_episode_end = None
                            s.air_date_start = _date_to_iso(ent.get("jikan_start"))
                            s.air_date_end = None
                            s.split_cour = None
                            session.add(s)
                            seasons_updated += 1
                            anchored_ids.add(sid)
                        except Exception:
                            pass
                    continue
                try:
                    tmdb_sn = int(anchor.get("season") or 0)
                    tmdb_ep = int(anchor.get("episode") or 0)
                except Exception:
                    continue
                if tmdb_sn < 0 or tmdb_ep <= 0:
                    # Season-only anchor (matched by air_date). Set season number but leave episode range unset.
                    if tmdb_sn >= 0:
                        try:
                            s.tmdb_season_number = tmdb_sn
                            s.tmdb_episode_start = None
                            s.tmdb_episode_end = None
                            s.air_date_start = _date_to_iso(ent.get("jikan_start"))
                            try:
                                air_by_ep = tmdb_season_meta.get(tmdb_sn, {}).get("air_by_ep") or {}
                                if isinstance(air_by_ep, dict) and air_by_ep:
                                    last_ep = tmdb_season_meta.get(tmdb_sn, {}).get("max_episode")
                                    if last_ep is not None:
                                        s.air_date_end = air_by_ep.get(int(last_ep)) or s.air_date_end
                            except Exception:
                                pass
                            try:
                                s.split_cour = tmdb_season_meta.get(tmdb_sn, {}).get("split_cour")
                            except Exception:
                                s.split_cour = None
                            session.add(s)
                            seasons_updated += 1
                            anchored_ids.add(sid)
                        except Exception:
                            pass
                    continue
                cnt = int(ent.get("count") or 0)
                try:
                    max_ep = tmdb_season_meta.get(tmdb_sn, {}).get("max_episode")
                except Exception:
                    max_ep = None
                end_ep = tmdb_ep + max(0, cnt - 1)
                try:
                    if max_ep is not None:
                        end_ep = min(int(end_ep), int(max_ep))
                except Exception:
                    pass
                try:
                    s.tmdb_season_number = tmdb_sn
                    s.tmdb_episode_start = tmdb_ep
                    s.tmdb_episode_end = int(end_ep) if end_ep else None
                    s.air_date_start = anchor.get("air_date_raw") or _date_to_iso(ent.get("jikan_start"))
                    try:
                        air_by_ep = tmdb_season_meta.get(tmdb_sn, {}).get("air_by_ep") or {}
                        if isinstance(air_by_ep, dict):
                            s.air_date_end = air_by_ep.get(int(end_ep)) or s.air_date_end
                    except Exception:
                        pass
                    try:
                        s.split_cour = tmdb_season_meta.get(tmdb_sn, {}).get("split_cour")
                    except Exception:
                        s.split_cour = None
                    session.add(s)
                    seasons_updated += 1
                    anchored_ids.add(sid)
                except Exception:
                    pass

        segments = _segments_from_tmdb_season(season1 if isinstance(season1, dict) else None, force_split_at=force_split)

        local_for_segments = [ent for ent in local if int(getattr(ent.get("season"), "id", -1) or -1) not in anchored_ids]

        # Drop segments already covered by anchored seasons (avoid reusing ep 1..N for part 2).
        used_ranges: list[tuple[int, int]] = []
        try:
            for ent in local:
                s = ent.get("season")
                if not s:
                    continue
                try:
                    sid = int(getattr(s, "id"))
                except Exception:
                    continue
                if sid not in anchored_ids:
                    continue
                try:
                    tsn = int(getattr(s, "tmdb_season_number", None) or 0)
                except Exception:
                    tsn = 0
                if tsn != 1:
                    continue
                try:
                    st = getattr(s, "tmdb_episode_start", None)
                    en = getattr(s, "tmdb_episode_end", None)
                    if st is not None and en is not None:
                        used_ranges.append((int(st), int(en)))
                except Exception:
                    continue
        except Exception:
            used_ranges = []

        def _seg_overlaps(seg: dict, ranges: list[tuple[int, int]]) -> bool:
            a_raw = seg.get("start")
            b_raw = seg.get("end")
            if a_raw is None or b_raw is None:
                return False
            try:
                a = int(a_raw)
                b = int(b_raw)
            except Exception:
                return False
            for rs, re_ in ranges:
                if a <= re_ and b >= rs:
                    return True
            return False

        if used_ranges and segments:
            segments = [seg for seg in segments if not _seg_overlaps(seg, used_ranges)]

        if segments and len(segments) >= len(local_for_segments):
            for idx, ent in enumerate(local_for_segments):
                s = ent["season"]
                sn = ent["season_number"]
                if tmdb_num_seasons is not None and sn <= tmdb_num_seasons:
                    s.tmdb_season_number = sn
                    s.tmdb_episode_start = None
                    s.tmdb_episode_end = None
                    s.air_date_start = None
                    s.air_date_end = None
                    try:
                        sc = tmdb_season_meta.get(int(sn), {}).get("split_cour")
                        if sc is None and int(sn) == 1:
                            sc = split_cour_s1
                        s.split_cour = sc
                    except Exception:
                        s.split_cour = None
                else:
                    seg = segments[idx]
                    s.tmdb_season_number = 1
                    s.tmdb_episode_start = int(seg["start"])
                    s.tmdb_episode_end = int(seg["end"])
                    s.air_date_start = seg.get("air_start")
                    s.air_date_end = seg.get("air_end")
                    s.split_cour = split_cour_s1 or None
                session.add(s)
                seasons_updated += 1
        else:
            start = 1
            if used_ranges:
                try:
                    start = max(e for _s, e in used_ranges) + 1
                except Exception:
                    start = 1
            for ent in local_for_segments:
                s = ent["season"]
                sn = ent["season_number"]
                cnt = int(ent["count"])
                if tmdb_num_seasons is not None and sn <= tmdb_num_seasons:
                    s.tmdb_season_number = sn
                    s.tmdb_episode_start = None
                    s.tmdb_episode_end = None
                    s.air_date_start = None
                    s.air_date_end = None
                    try:
                        sc = tmdb_season_meta.get(int(sn), {}).get("split_cour")
                        if sc is None and int(sn) == 1:
                            sc = split_cour_s1
                        s.split_cour = sc
                    except Exception:
                        s.split_cour = None
                else:
                    s.tmdb_season_number = 1
                    s.tmdb_episode_start = int(start)
                    s.tmdb_episode_end = int(start + cnt - 1)
                    s.split_cour = split_cour_s1 or None
                start = start + cnt
                session.add(s)
                seasons_updated += 1

        # For split seasons mapped to TMDB Season 1 (and Season 1 itself), inject Jikan English synopsis as manual EN fallback.
        try:
            from ..core.translations import upsert_translation
        except Exception:
            upsert_translation = None
        if upsert_translation and season_to_media and jikan_meta_by_media:
            try:
                for ent in local:
                    s = ent.get("season")
                    if not s:
                        continue
                    try:
                        sn_raw = getattr(s, "season_number", None)
                        if sn_raw is None:
                            continue
                        sn = int(sn_raw)
                    except Exception:
                        sn = None
                    if sn is None:
                        continue
                    try:
                        tsn = getattr(s, "tmdb_season_number", None)
                        tstart = getattr(s, "tmdb_episode_start", None)
                    except Exception:
                        tsn = None
                        tstart = None
                    if tsn is None or int(tsn) != 1:
                        continue
                    mid = season_to_media.get(int(getattr(s, "id")))
                    if not mid:
                        continue
                    data = jikan_meta_by_media.get(int(mid)) or {}
                    if not isinstance(data, dict):
                        continue
                    try:
                        jtype = data.get("anime_type") or data.get("type")
                        if isinstance(jtype, str) and jtype.strip().lower() in ("ova", "ona", "special", "specials"):
                            continue
                    except Exception:
                        pass
                    title_en = None
                    synopsis_en = None
                    try:
                        title_en = (data.get("title_english") or data.get("title") or None)
                    except Exception:
                        title_en = None
                    try:
                        synopsis_en = (data.get("synopsis") or data.get("overview") or None)
                    except Exception:
                        synopsis_en = None
                    if title_en or synopsis_en:
                        upsert_translation(
                            session,
                            path_id=int(mid),
                            language="en",
                            source="manual",
                            title=title_en,
                            overview=synopsis_en,
                        )
            except Exception:
                pass

        # Update posters/backdrops from TMDB (best-effort).
        try:
            download_images = bool(get("download_images"))
        except Exception:
            download_images = False
        series_poster = None
        series_backdrop = None
        if isinstance(details, dict):
            try:
                series_poster = _tmdb_image_url(details.get("poster_path"))
                series_backdrop = _tmdb_image_url(details.get("backdrop_path"))
            except Exception:
                series_poster = None
                series_backdrop = None
        season_cache: dict[int, dict | None] = {}
        try:
            for ent in local:
                s = ent.get("season")
                if not s:
                    continue
                sid = getattr(s, "id", None)
                if sid is None:
                    continue
                mid = season_to_media.get(int(sid))
                if not mid:
                    continue
                mi = session.query(MediaItem).filter(MediaItem.id == int(mid)).first()
                if not mi:
                    continue
                try:
                    tsn = int(getattr(s, "tmdb_season_number", None) or getattr(s, "season_number", None) or 1)
                except Exception:
                    tsn = 1
                if tsn not in season_cache:
                    try:
                        season_cache[tsn] = tmdb.tmdb_fetch_season(tid, season_number=int(tsn), language="en-US")  # type: ignore[arg-type]
                    except Exception:
                        season_cache[tsn] = None
                sdata = season_cache.get(tsn) if isinstance(season_cache.get(tsn), dict) else {}
                poster_url = _tmdb_image_url(sdata.get("poster_path")) if isinstance(sdata, dict) else None
                if not poster_url:
                    poster_url = series_poster
                backdrop_url = series_backdrop

                def _needs_img(v: str | None) -> bool:
                    if not isinstance(v, str) or not v.strip():
                        return True
                    return not v.strip().startswith("/media-assets/")

                if poster_url:
                    try:
                        if download_images:
                            _download_image_and_record(session, mi, poster_url, source="tmdb", kind="poster")
                        elif _needs_img(getattr(mi, "poster_path", None)):
                            mi_any = _cast(_Any, mi)
                            mi_any.poster_path = poster_url
                            session.add(mi)
                    except Exception:
                        pass
                if backdrop_url:
                    try:
                        if download_images:
                            _download_image_and_record(session, mi, backdrop_url, source="tmdb", kind="backdrop")
                        elif _needs_img(getattr(mi, "backdrop_path", None)):
                            mi_any = _cast(_Any, mi)
                            mi_any.backdrop_path = backdrop_url
                            session.add(mi)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            session.commit()
        except Exception:
            session.rollback()
            return {"ok": False, "detail": "commit_failed", "seasons_updated": 0}
        return {"ok": True, "tmdb_id": tid, "series_id": int(series_id), "seasons_updated": int(seasons_updated)}
    finally:
        session.close()

    seasons_updated = 0
    episodes_updated = 0

    session = SessionLocal()
    try:
        jikan_meta_cache: dict[int, dict] = {}
        def _get_jikan_meta(item_id: int | None) -> dict | None:
            if item_id is None:
                return None
            try:
                iid = int(item_id)
            except Exception:
                return None
            if iid in jikan_meta_cache:
                return jikan_meta_cache[iid]
            meta = None
            try:
                md = (
                    session.query(MediaMetadata)
                    .filter(MediaMetadata.media_item_id == iid)
                    .filter(MediaMetadata.provider == "jikan")
                    .order_by(MediaMetadata.id.desc())
                    .first()
                )
            except Exception:
                md = None
            if md is not None:
                try:
                    raw = getattr(md, "data", None)
                    if isinstance(raw, str) and raw.strip():
                        meta = json.loads(raw)
                    elif isinstance(raw, dict):
                        meta = raw
                except Exception:
                    meta = None
            if meta is None:
                meta = {}
            jikan_meta_cache[iid] = meta
            return meta
        jikan_meta_cache: dict[int, dict] = {}
        def _get_jikan_meta(item_id: int | None) -> dict | None:
            if item_id is None:
                return None
            try:
                iid = int(item_id)
            except Exception:
                return None
            if iid in jikan_meta_cache:
                return jikan_meta_cache[iid]
            meta = None
            try:
                md = (
                    session.query(MediaMetadata)
                    .filter(MediaMetadata.media_item_id == iid)
                    .filter(MediaMetadata.provider == "jikan")
                    .order_by(MediaMetadata.id.desc())
                    .first()
                )
            except Exception:
                md = None
            if md is not None:
                try:
                    raw = getattr(md, "data", None)
                    if isinstance(raw, str) and raw.strip():
                        meta = json.loads(raw)
                    elif isinstance(raw, dict):
                        meta = raw
                except Exception:
                    meta = None
            if meta is None:
                meta = {}
            jikan_meta_cache[iid] = meta
            return meta
        if manage_state:
            try:
                set_state(
                    {
                        "running": True,
                        "current_id": None,
                        "current_title": "TMDB: episodios",
                        "current_step": "backfill_episodes",
                        "backfill_lang": lang,
                        "backfill_localized": bool(localize),
                        "backfill_total": 0,
                        "backfill_done": 0,
                        "backfill_seasons_updated": 0,
                        "backfill_episodes_updated": 0,
                    }
                )
            except Exception:
                pass

        q = (
            session.query(SeasonItem, Season, MediaItem, Series)
            .join(Season, Season.id == SeasonItem.season_id)
            .join(MediaItem, MediaItem.id == SeasonItem.media_item_id)
            .join(Series, Series.id == Season.series_id, isouter=True)
            .filter(or_(MediaItem.tmdb_id != None, Series.tmdb_id != None))  # noqa: E711
        )
        if media_item_id is not None:
            try:
                q = q.filter(MediaItem.id == int(media_item_id))
            except Exception:
                pass
        if series_id is not None:
            try:
                q = q.filter(Season.series_id == int(series_id))
            except Exception:
                pass
        if tmdb_id:
            try:
                tid = str(tmdb_id).strip()
                if tid:
                    q = q.filter(or_(MediaItem.tmdb_id == tid, Series.tmdb_id == tid))
            except Exception:
                pass
        if limit_seasons and int(limit_seasons) > 0:
            q = q.limit(int(limit_seasons))
        rows = q.all()

        total = len(rows)
        try:
            set_state({"backfill_total": int(total), "backfill_done": 0})
        except Exception:
            pass

        done = 0

        for si, season, mi, series in rows:
            done += 1
            try:
                season_number = getattr(season, "season_number", None)
                if not isinstance(season_number, int) or season_number < 0:
                    continue

                tmdb_id = getattr(mi, "tmdb_id", None) or (getattr(series, "tmdb_id", None) if series else None)
                if not tmdb_id:
                    continue
                tmdb_id_s = str(tmdb_id)

                tmdb_season_number = None
                try:
                    tsn = getattr(season, "tmdb_season_number", None)
                    tmdb_season_number = int(tsn) if tsn is not None else None
                except Exception:
                    tmdb_season_number = None
                if tmdb_season_number is None:
                    tmdb_season_number = int(season_number)

                mapped_offset = 0
                try:
                    tstart = getattr(season, "tmdb_episode_start", None)
                    if tstart is not None:
                        mapped_offset = max(0, int(tstart) - 1)
                except Exception:
                    mapped_offset = 0
                force_overwrite = bool(isinstance(mapped_offset, int) and mapped_offset > 0)

                eps = session.query(Episode).filter(Episode.season_id == season.id).all()
                if not eps:
                    continue

                need_loc = False
                if localize:
                    try:
                        need_loc = any(not (getattr(ep, "title_localized", None) or "").strip() for ep in eps)
                    except Exception:
                        need_loc = True

                need_en = False
                try:
                    need_en = any(is_generic_episode_title(getattr(ep, "title_en", None)) for ep in eps)
                except Exception:
                    need_en = False

                need_syn = False
                try:
                    need_syn = any(not (getattr(ep, "synopsis_localized", None) or "").strip() for ep in eps)
                except Exception:
                    need_syn = False

                if not need_loc and not need_en and not need_syn and not force_overwrite:
                    continue

                try:
                    # keep the UI responsive with a human-readable step
                    set_state(
                        {
                            "running": True,
                            "current_id": getattr(mi, "id", None),
                            "current_title": (getattr(mi, "title_localized", None) or getattr(mi, "title_en", None) or getattr(mi, "title", None) or "TMDB"),
                            "current_step": f"backfill_episodes {done}/{total}",
                            "backfill_done": int(done),
                            "backfill_seasons_updated": int(seasons_updated),
                            "backfill_episodes_updated": int(episodes_updated),
                        }
                    )
                except Exception:
                    pass

                lang_chain = _build_lang_chain(lang)

                season_data_by_lang: dict[str, dict | None] = {}
                if need_loc or need_syn or need_en or force_overwrite:
                    for lkey in lang_chain:
                        try:
                            season_data_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                tmdb_id_s, season_number=tmdb_season_number, language=lkey
                            )
                        except Exception:
                            season_data_by_lang[lkey] = None

                offset = int(mapped_offset or 0)

                season_data_loc = season_data_by_lang.get(lang_chain[0]) if lang_chain else None
                season_data_en = season_data_by_lang.get("en-US")

                # Global synopsis fallback (series-level) when episode overviews are missing.
                global_synopsis = None
                if need_syn:
                    for lkey in lang_chain:
                        try:
                            details_loc = tmdb.tmdb_fetch_by_id(  # type: ignore[arg-type]
                                tmdb_id_s, media_type="tv", language=lkey
                            )
                        except Exception:
                            details_loc = None
                        if isinstance(details_loc, dict):
                            try:
                                gs = details_loc.get("synopsis") or details_loc.get("overview") or ""
                                if isinstance(gs, str) and gs.strip():
                                    global_synopsis = gs.strip()
                                    break
                            except Exception:
                                global_synopsis = None
                    if not global_synopsis:
                        for lkey in lang_chain:
                            sdat = season_data_by_lang.get(lkey)
                            if isinstance(sdat, dict):
                                try:
                                    gs = sdat.get("synopsis") or sdat.get("overview") or ""
                                    if isinstance(gs, str) and gs.strip():
                                        global_synopsis = gs.strip()
                                        break
                                except Exception:
                                    pass

                def _split_offset(season_data: dict | None) -> int | None:
                    if not isinstance(season_data, dict):
                        return None
                    eps = season_data.get("episodes") or []
                    if not isinstance(eps, list) or not eps:
                        return None
                    # Prefer explicit mid-season split markers when present; otherwise infer by air-date gaps.
                    for e in eps:
                        if isinstance(e, dict) and e.get("episode_type") == "mid_season":
                            n = e.get("episode_number")
                            try:
                                return int(n)
                            except Exception:
                                return None
                    prev_date = None
                    prev_num = None
                    for e in eps:
                        if not isinstance(e, dict):
                            continue
                        ad = e.get("air_date")
                        n = e.get("episode_number")
                        try:
                            n = int(n)
                        except Exception:
                            n = None
                        if not isinstance(ad, str) or not ad.strip():
                            continue
                        try:
                            dt = datetime.fromisoformat(ad.strip())
                        except Exception:
                            try:
                                dt = datetime.strptime(ad.strip(), "%Y-%m-%d")
                            except Exception:
                                dt = None
                        if dt is None:
                            continue
                        if prev_date is not None and prev_num is not None:
                            try:
                                gap = (dt - prev_date).days
                            except Exception:
                                gap = None
                            if gap is not None and gap >= 28:
                                return int(prev_num)
                        prev_date = dt
                        prev_num = n
                    return None

                def _episode_map(season_data: dict | None) -> dict[int, dict]:
                    out: dict[int, dict] = {}
                    if not isinstance(season_data, dict):
                        return out
                    eps = season_data.get("episodes") or []
                    if not isinstance(eps, list):
                        return out
                    for e in eps:
                        if not isinstance(e, dict):
                            continue
                        n = e.get("episode_number")
                        if not isinstance(n, int):
                            if n is None:
                                continue
                            try:
                                n = int(n)
                            except Exception:
                                continue
                        out[n] = e
                    return out

                episode_maps: dict[str, dict[int, dict]] = {}
                for lkey in lang_chain:
                    episode_maps[lkey] = _episode_map(season_data_by_lang.get(lkey))

                has_current = any(episode_maps.values())
                if not has_current and mapped_offset <= 0 and series is not None:
                    try:
                        sid = int(getattr(series, "id"))
                    except Exception:
                        sid = None
                    if sid is not None:
                        try:
                            reconcile_tmdb_season_mapping(tmdb_id=tmdb_id_s, series_id=sid)
                            try:
                                session.refresh(season)
                            except Exception:
                                pass
                            try:
                                tsn = getattr(season, "tmdb_season_number", None)
                                tmdb_season_number = int(tsn) if tsn is not None else tmdb_season_number
                            except Exception:
                                pass
                            try:
                                tstart = getattr(season, "tmdb_episode_start", None)
                                if tstart is not None:
                                    offset = max(0, int(tstart) - 1)
                            except Exception:
                                pass
                            season_data_by_lang = {}
                            for lkey in lang_chain:
                                try:
                                    season_data_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                        tmdb_id_s, season_number=tmdb_season_number, language=lkey
                                    )
                                except Exception:
                                    season_data_by_lang[lkey] = None
                            episode_maps = {lkey: _episode_map(season_data_by_lang.get(lkey)) for lkey in lang_chain}
                            has_current = any(episode_maps.values())
                        except Exception:
                            pass
                if not has_current and mapped_offset <= 0 and isinstance(season_number, int) and season_number >= 2:
                    season1_by_lang: dict[str, dict | None] = {}
                    for lkey in lang_chain:
                        try:
                            season1_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                tmdb_id_s, season_number=1, language=lkey
                            )
                        except Exception:
                            season1_by_lang[lkey] = None
                    base_season = season1_by_lang.get("en-US") or next((v for v in season1_by_lang.values() if isinstance(v, dict)), None)
                    off = _split_offset(base_season if isinstance(base_season, dict) else None)
                    if off:
                        offset = int(off)
                        season_data_by_lang = season1_by_lang
                        episode_maps = {lkey: _episode_map(season_data_by_lang.get(lkey)) for lkey in lang_chain}
                        has_current = any(episode_maps.values())
                        try:
                            season_any = _cast(_Any, season)
                            season_any.tmdb_season_number = 1
                            season_any.tmdb_episode_start = int(offset) + 1
                            session.add(season)
                        except Exception:
                            pass

                # Ensure Season titles are localized for UI (skip when using season1 fallback for season2).
                try:
                    def _season_title(d: dict | None) -> str | None:
                        if not isinstance(d, dict):
                            return None
                        for k in ("name", "title", "season_name"):
                            v = d.get(k)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                        return None

                    if offset <= 0:
                        if not (getattr(season, "title_localized", None) or "").strip():
                            for lkey in lang_chain:
                                tloc = _season_title(season_data_by_lang.get(lkey))
                                if tloc:
                                    season.title_localized = tloc
                                    break
                        if not (getattr(season, "title_en", None) or "").strip():
                            ten = _season_title(season_data_en)
                            if ten:
                                season.title_en = ten
                        try:
                            session.add(season)
                        except Exception:
                            pass
                except Exception:
                    pass

                # When we map a local season onto a different TMDB season (common for anime arcs),
                # we must overwrite existing episode fields even if offset==0, otherwise stale titles linger.
                try:
                    force_overwrite = bool((isinstance(offset, int) and offset > 0) or (int(tmdb_season_number) != int(season_number)))
                except Exception:
                    force_overwrite = bool(isinstance(offset, int) and offset > 0)
                by_num = {}
                for ep in eps:
                    try:
                        n = getattr(ep, "episode_number", None)
                        if isinstance(n, int):
                            by_num[n] = ep
                    except Exception:
                        continue

                changed_this_season = False

                if (need_loc or need_syn) or force_overwrite:
                    for n, row in by_num.items():
                        target_n = int(n) + int(offset) if isinstance(offset, int) and offset > 0 else int(n)
                        row_any = _cast(_Any, row)
                        if (need_loc or force_overwrite) and (force_overwrite or not (getattr(row, "title_localized", None) or "").strip()):
                            fallback_generic = None
                            for lkey in lang_chain:
                                e = episode_maps.get(lkey, {}).get(target_n)
                                if not isinstance(e, dict):
                                    continue
                                tloc = e.get("title") or e.get("name")
                                if isinstance(tloc, str) and tloc.strip():
                                    if is_generic_episode_title(tloc):
                                        if fallback_generic is None:
                                            fallback_generic = tloc.strip()
                                        continue
                                    row_any.title_localized = tloc.strip()
                                    if force_overwrite:
                                        row_any.title = tloc.strip()
                                    changed_this_season = True
                                    episodes_updated += 1
                                    break
                            if not (getattr(row, "title_localized", None) or "").strip() and fallback_generic:
                                row_any.title_localized = fallback_generic
                                if force_overwrite:
                                    row_any.title = fallback_generic
                                changed_this_season = True
                                episodes_updated += 1
                        if (need_syn or force_overwrite) and (force_overwrite or not (getattr(row, "synopsis_localized", None) or "").strip()):
                            filled = False
                            for lkey in lang_chain:
                                e = episode_maps.get(lkey, {}).get(target_n)
                                if not isinstance(e, dict):
                                    continue
                                ov = e.get("overview") or ""
                                if isinstance(ov, str) and ov.strip():
                                    row_any.synopsis_localized = ov.strip()
                                    changed_this_season = True
                                    filled = True
                                    break
                            if not filled and isinstance(global_synopsis, str) and global_synopsis.strip():
                                # Requirement: when episode synopsis is missing, fall back to global series synopsis.
                                row_any.synopsis_localized = global_synopsis.strip()
                                changed_this_season = True
                        try:
                            e0 = episode_maps.get("en-US", {}).get(target_n) or episode_maps.get(lang_chain[0], {}).get(target_n)
                        except Exception:
                            e0 = None
                        if isinstance(e0, dict):
                            try:
                                ad = e0.get("air_date")
                                if isinstance(ad, str) and ad.strip() and not (getattr(row, "air_date", None) or "").strip():
                                    row_any.air_date = ad.strip()
                            except Exception:
                                pass
                            try:
                                et = e0.get("episode_type")
                                if isinstance(et, str) and et.strip() and not (getattr(row, "episode_type", None) or "").strip():
                                    row_any.episode_type = et.strip()
                            except Exception:
                                pass
                        session.add(row)

                if need_en or force_overwrite:
                    en_map = episode_maps.get("en-US", {})
                    for n, row in by_num.items():
                        target_n = int(n) + int(offset) if isinstance(offset, int) and offset > 0 else int(n)
                        e = en_map.get(target_n)
                        if not isinstance(e, dict):
                            continue
                        row_any = _cast(_Any, row)
                        try:
                            ten = e.get("title") or e.get("name")
                            if isinstance(ten, str) and ten.strip() and (force_overwrite or is_generic_episode_title(getattr(row, "title_en", None))):
                                row_any.title_en = ten.strip()
                                # keep `title` usable if it was also generic
                                if force_overwrite or is_generic_episode_title(getattr(row, "title", None)):
                                    row_any.title = ten.strip()
                                changed_this_season = True
                                episodes_updated += 1
                                session.add(row)
                        except Exception:
                            continue

                if changed_this_season:
                    try:
                        session.commit()
                    except Exception:
                        session.rollback()
                        continue
                    seasons_updated += 1
                    try:
                        set_state(
                            {
                                "backfill_seasons_updated": int(seasons_updated),
                                "backfill_episodes_updated": int(episodes_updated),
                            }
                        )
                    except Exception:
                        pass
            except Exception:
                session.rollback()
                continue

        res = {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": seasons_updated, "episodes_updated": episodes_updated}
        if manage_state:
            try:
                set_state(
                    {
                        "running": False,
                        "current_id": None,
                        "current_title": None,
                        "current_step": "idle",
                        "backfill_seasons_updated": int(seasons_updated),
                        "backfill_episodes_updated": int(episodes_updated),
                    }
                )
            except Exception:
                pass
        return res
    finally:
        session.close()


def tmdb_sync_from_jikan_title_en(
    limit_series: int | None = None,
    manage_state: bool = True,
    min_confidence: float = 0.55,
    *,
    series_id: int | None = None,
) -> dict[str, int | str | bool]:
    """Resolve and persist TMDB ids for Jikan-identified anime series using the English anchor title.

    This enables the existing TMDB episode backfill to populate:
      - episode.title_localized
      - episode.synopsis_localized

    Why: older libraries may have been enriched with Jikan only, leaving tmdb_id empty and making
    localized episode sync impossible without re-enrichment.
    """
    if tmdb is None or not hasattr(tmdb, "tmdb_search"):
        return {"ok": False, "detail": "tmdb_not_available", "series_updated": 0, "items_updated": 0}
    try:
        if not _tmdb_configured():
            return {"ok": False, "detail": "tmdb_not_configured", "series_updated": 0, "items_updated": 0}
    except Exception:
        return {"ok": False, "detail": "tmdb_not_configured", "series_updated": 0, "items_updated": 0}

    try:
        lang = str(get("metadata_language") or "").strip() or "en-US"
    except Exception:
        lang = "en-US"

    try:
        localize = bool(get("tmdb_localization")) and bool(lang) and not str(lang).lower().startswith("en")
    except Exception:
        localize = False

    if not localize:
        return {"ok": True, "detail": "localization_disabled", "lang": lang, "series_updated": 0, "items_updated": 0}

    session = SessionLocal()
    series_updated = 0
    items_updated = 0
    matched = 0
    skipped_low_conf = 0
    no_match = 0

    try:
        from sqlalchemy import or_
        # Only target anime series (identified via MAL/Jikan) that still don't have a TMDB identity.
        q = (
            session.query(Series)
            .filter(Series.mal_id != None)  # noqa: E711
            .filter(or_(Series.tmdb_id == None, Series.tmdb_id == ""))  # noqa: E711
            .filter(or_(Series.tmdb_no_match == None, Series.tmdb_no_match == False))  # noqa: E711
        )
        if series_id is not None:
            try:
                q = q.filter(Series.id == int(series_id))
            except Exception:
                pass
        if limit_series and int(limit_series) > 0:
            q = q.limit(int(limit_series))
        series_rows = q.all()

        total = len(series_rows)

        if manage_state:
            try:
                set_state(
                    {
                        "running": True,
                        "current_id": None,
                        "current_title": "TMDB: sync",
                        "current_step": "tmdb_sync",
                        "sync_lang": lang,
                        "sync_total": int(total),
                        "sync_done": 0,
                        "sync_series_updated": 0,
                        "sync_items_updated": 0,
                        "sync_matched": 0,
                        "sync_no_match": 0,
                        "sync_skipped_low_conf": 0,
                    }
                )
            except Exception:
                pass

        import difflib

        for idx, series in enumerate(series_rows, 1):
            series_any = _cast(_Any, series)
            try:
                # Fetch all season items for this series (one media_item per season).
                season_items = (
                    session.query(SeasonItem, Season, MediaItem)
                    .join(Season, Season.id == SeasonItem.season_id)
                    .join(MediaItem, MediaItem.id == SeasonItem.media_item_id)
                    .filter(Season.series_id == series.id)
                    .all()
                )

                # Determine if we already have a stable tmdb_id from either series or any season item.
                desired_tmdb_id = (getattr(series, "tmdb_id", None) or "").strip() or None
                if not desired_tmdb_id:
                    ids = []
                    for _si, _season, mi in season_items:
                        tid = (getattr(mi, "tmdb_id", None) or "").strip()
                        if tid:
                            ids.append(tid)
                    ids = sorted(set(ids))
                    if len(ids) == 1:
                        desired_tmdb_id = ids[0]

                # If a tmdb_id is already known, just propagate it to season items.
                if desired_tmdb_id:
                    # Avoid UNIQUE conflicts: if another Series already has this tmdb_id, merge seasons into it.
                    try:
                        existing = (
                            session.query(Series)
                            .filter(Series.tmdb_id == str(desired_tmdb_id))
                            .order_by(Series.id.asc())
                            .first()
                        )
                    except Exception:
                        existing = None
                    if existing and getattr(existing, "id", None) != getattr(series, "id", None):
                        try:
                            logger.warning(
                                "tmdb_sync: tmdb_id conflict (preknown); merging seasons tmdb_id=%s keep_series=%s drop_series=%s title_en='%s'",
                                str(desired_tmdb_id),
                                getattr(existing, "id", None),
                                getattr(series, "id", None),
                                (getattr(series, "title_en", None) or getattr(series, "title", None) or "").strip(),
                            )
                        except Exception:
                            pass
                        try:
                            srows = session.query(Season).filter(Season.series_id == series.id).all()
                        except Exception:
                            srows = []
                        for s in srows:
                            try:
                                if getattr(s, "series_id", None) != getattr(existing, "id", None):
                                    s.series_id = existing.id
                                    session.add(s)
                            except Exception:
                                pass
                        series = existing
                        series_any = _cast(_Any, series)
                    changed_any = False
                    for _si, _season, mi in season_items:
                        mi_any = _cast(_Any, mi)
                        cur = (getattr(mi, "tmdb_id", None) or "").strip()
                        if cur != desired_tmdb_id:
                            mi_any.tmdb_id = str(desired_tmdb_id)
                            session.add(mi)
                            items_updated += 1
                            changed_any = True
                    if (getattr(series, "tmdb_id", None) or "").strip() != desired_tmdb_id:
                        series_any.tmdb_id = str(desired_tmdb_id)
                        session.add(series)
                        series_updated += 1
                        changed_any = True
                    if changed_any:
                        try:
                            session.commit()
                        except Exception:
                            session.rollback()
                    if manage_state:
                        try:
                            set_state(
                                {
                                    "sync_done": int(idx),
                                    "sync_series_updated": int(series_updated),
                                    "sync_items_updated": int(items_updated),
                                }
                            )
                        except Exception:
                            pass
                    continue

                # Resolve anchor title/year.
                # Prefer Jikan title_synonyms (when present), but ALWAYS include series-level titles as fallbacks.
                anchor_title = ""
                anchor_candidates: list[str] = []
                synonym_candidates: list[str] = []

                def _get_jikan_meta_for_item(item_id: int | None) -> dict | None:
                    if item_id is None:
                        return None
                    try:
                        iid = int(item_id)
                    except Exception:
                        return None
                    try:
                        md = (
                            session.query(MediaMetadata)
                            .filter(MediaMetadata.media_item_id == iid)
                            .filter(MediaMetadata.provider == "jikan")
                            .order_by(MediaMetadata.id.desc())
                            .first()
                        )
                    except Exception:
                        md = None
                    if not md:
                        return None
                    try:
                        raw = getattr(md, "data", None)
                        if isinstance(raw, str) and raw.strip():
                            return json.loads(raw)
                        if isinstance(raw, dict):
                            return raw
                    except Exception:
                        return None
                    return None

                try:
                    for _si, _season, mi in season_items:
                        meta = _get_jikan_meta_for_item(getattr(mi, "id", None))
                        if not isinstance(meta, dict):
                            continue
                        syns = meta.get("title_synonyms") or meta.get("title_synonym") or []
                        if not isinstance(syns, list):
                            continue
                        for s in syns:
                            try:
                                s0 = str(s or "").strip()
                            except Exception:
                                s0 = ""
                            if not s0:
                                continue
                            # Ignore short aliases like "AZ"/"SAO" that frequently match unrelated TMDB entries.
                            try:
                                if len(s0) <= 3 and re.fullmatch(r"[A-Z0-9]{1,3}", s0):
                                    continue
                            except Exception:
                                pass
                            base, _season_hint = _extract_season_from_title(s0)
                            if base and base not in synonym_candidates:
                                synonym_candidates.append(base)
                            if s0 not in synonym_candidates:
                                synonym_candidates.append(s0)
                except Exception:
                    pass

                if synonym_candidates:
                    anchor_candidates.extend(synonym_candidates)

                # Prefer series-level English title for logging and as a stable query, even when synonyms exist.
                anchor_title = (getattr(series, "title_en", None) or "").strip()
                if anchor_title:
                    anchor_candidates.append(anchor_title)

                # Fallback: season media item titles (folder-derived), but still series-level, not episode titles.
                if not anchor_title:
                    try:
                        for _si, _season, mi in season_items:
                            t0 = (getattr(mi, "title_en", None) or "").strip()
                            if t0:
                                anchor_title = t0
                                anchor_candidates.append(t0)
                                break
                    except Exception:
                        pass

                # Fallback: series title.
                try:
                    t1 = (getattr(series, "title", None) or "").strip()
                except Exception:
                    t1 = ""
                if t1:
                    anchor_candidates.append(t1)

                # Ensure unique candidates while preserving order.
                seen0: set[str] = set()
                uniq: list[str] = []
                for c in anchor_candidates:
                    try:
                        c0 = str(c or "").strip()
                    except Exception:
                        c0 = ""
                    if not c0:
                        continue
                    k = c0.lower()
                    if k in seen0:
                        continue
                    seen0.add(k)
                    uniq.append(c0)
                anchor_candidates = uniq

                # Avoid using episode/file-derived search_titles for anchor resolution.
                # Only rely on series-level titles (title/title_en/title_localized) and Jikan synonyms.
                year = getattr(series, "year", None)
                if year is None:
                    try:
                        for _si, _season, mi in season_items:
                            y0 = getattr(mi, "release_year", None)
                            if isinstance(y0, int) and y0 > 0:
                                year = y0
                                break
                    except Exception:
                        year = None

                if not anchor_title and not anchor_candidates:
                    no_match += 1
                    try:
                        series_any.tmdb_no_match = True
                        series_any.tmdb_no_match_reason = "missing_anchor"
                        session.add(series)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                if manage_state:
                    try:
                        set_state(
                            {
                                "running": True,
                                "current_id": getattr(series, "id", None),
                                "current_title": anchor_title,
                                "current_step": f"tmdb_sync {idx}/{total}",
                                "sync_done": int(idx),
                                "sync_series_updated": int(series_updated),
                                "sync_items_updated": int(items_updated),
                                "sync_matched": int(matched),
                                "sync_no_match": int(no_match),
                                "sync_skipped_low_conf": int(skipped_low_conf),
                            }
                        )
                    except Exception:
                        pass

                queries: list[str] = []
                if not anchor_candidates and anchor_title:
                    anchor_candidates = [anchor_title]
                for cand in anchor_candidates:
                    if not cand:
                        continue
                    if year:
                        qy = f"{cand} {year}".strip()
                        if qy and qy not in queries:
                            queries.append(qy)
                    if cand not in queries:
                        queries.append(cand)

                res = None
                try:
                    res = tmdb.tmdb_search(queries, media_preference="tv")  # type: ignore[attr-defined]
                except Exception:
                    res = None

                if not isinstance(res, dict):
                    no_match += 1
                    try:
                        series_any.tmdb_no_match = True
                        series_any.tmdb_no_match_reason = "no_match"
                        session.add(series)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                mt = (res.get("media_type") or "").strip().lower()
                if mt != "tv":
                    no_match += 1
                    try:
                        series_any.tmdb_no_match = True
                        series_any.tmdb_no_match_reason = f"not_tv:{mt or 'unknown'}"
                        session.add(series)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                tmdb_id = res.get("tmdb_id") or res.get("provider_id")
                if not tmdb_id:
                    no_match += 1
                    try:
                        series_any.tmdb_no_match = True
                        series_any.tmdb_no_match_reason = "missing_tmdb_id"
                        session.add(series)
                        session.commit()
                    except Exception:
                        session.rollback()
                    continue

                tmdb_title = (res.get("title") or res.get("series_title") or "").strip()
                conf = 0.0
                if tmdb_title:
                    try:
                        # Use best confidence across all anchor candidates.
                        for cand in (anchor_candidates or [anchor_title]):
                            if not cand:
                                continue
                            cval = difflib.SequenceMatcher(None, str(cand).lower(), tmdb_title.lower()).ratio()
                            if cval > conf:
                                conf = cval
                    except Exception:
                        conf = 0.0

                if conf < float(min_confidence):
                    skipped_low_conf += 1
                    logger.warning(
                        "tmdb_sync: low confidence match series_id=%s title_en='%s' -> tmdb_id=%s tmdb_title='%s' conf=%.2f (skipping)",
                        getattr(series, "id", None),
                        anchor_title,
                        str(tmdb_id),
                        tmdb_title,
                        float(conf),
                    )
                    # Don't mark the series as "no match" on low-confidence results; allow future runs
                    # to try better anchors (e.g., when synonyms contain short aliases like "AZ").
                    continue

                logger.info(
                    "tmdb_sync: matched series_id=%s title_en='%s' -> tmdb_id=%s tmdb_title='%s' conf=%.2f",
                    getattr(series, "id", None),
                    anchor_title,
                    str(tmdb_id),
                    tmdb_title,
                    float(conf),
                )

                # Avoid UNIQUE conflicts: if another Series already has this tmdb_id, merge seasons into it.
                try:
                    existing = (
                        session.query(Series)
                        .filter(Series.tmdb_id == str(tmdb_id))
                        .order_by(Series.id.asc())
                        .first()
                    )
                except Exception:
                    existing = None
                if existing and getattr(existing, "id", None) != getattr(series, "id", None):
                    try:
                        logger.warning(
                            "tmdb_sync: tmdb_id conflict; merging seasons tmdb_id=%s keep_series=%s drop_series=%s title_en='%s'",
                            str(tmdb_id),
                            getattr(existing, "id", None),
                            getattr(series, "id", None),
                            anchor_title,
                        )
                    except Exception:
                        pass
                    try:
                        srows = session.query(Season).filter(Season.series_id == series.id).all()
                    except Exception:
                        srows = []
                    for s in srows:
                        try:
                            if getattr(s, "series_id", None) != getattr(existing, "id", None):
                                s.series_id = existing.id
                                session.add(s)
                        except Exception:
                            pass
                    series = existing
                    series_any = _cast(_Any, series)
                if not (getattr(series, "tmdb_id", None) or "").strip():
                    series_any.tmdb_id = str(tmdb_id)
                    session.add(series)

                for _si, _season, mi in season_items:
                    mi_any = _cast(_Any, mi)
                    mi_any.tmdb_id = str(tmdb_id)
                    session.add(mi)
                    items_updated += 1

                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    continue

                series_updated += 1
                matched += 1

                if manage_state:
                    try:
                        set_state(
                            {
                                "sync_series_updated": int(series_updated),
                                "sync_items_updated": int(items_updated),
                                "sync_matched": int(matched),
                                "sync_no_match": int(no_match),
                                "sync_skipped_low_conf": int(skipped_low_conf),
                            }
                        )
                    except Exception:
                        pass
            except Exception:
                session.rollback()
                continue

        res_out: dict[str, int | str | bool] = {
            "ok": True,
            "lang": lang,
            "series_total": int(total),
            "series_updated": int(series_updated),
            "items_updated": int(items_updated),
            "matched": int(matched),
            "no_match": int(no_match),
            "skipped_low_conf": int(skipped_low_conf),
        }
        if manage_state:
            try:
                set_state(
                    {
                        "running": False,
                        "current_id": None,
                        "current_title": None,
                        "current_step": "idle",
                        "sync_series_updated": int(series_updated),
                        "sync_items_updated": int(items_updated),
                        "sync_matched": int(matched),
                        "sync_no_match": int(no_match),
                        "sync_skipped_low_conf": int(skipped_low_conf),
                    }
                )
            except Exception:
                pass
        return res_out
    finally:
        session.close()


def backfill_cached_images(limit_items: int | None = None, manage_state: bool = False) -> dict:
    """Backfill locally cached posters/backdrops using *existing* provider metadata.

    Avoids extra provider API requests by reusing the `images` URLs already stored in
    `media_metadata.data`. Useful when the user enables `downloadImages` after an initial enrich.
    """
    if not get("download_images"):
        return {"ok": False, "detail": "download_images disabled", "items_examined": 0, "posters": 0, "backdrops": 0}

    session = SessionLocal()
    posters = 0
    backdrops = 0
    items_examined = 0
    errors = 0
    try:
        sub = (
            session.query(
                MediaMetadata.media_item_id.label("mid"),
                func.max(MediaMetadata.version).label("maxv"),
            )
            .filter(MediaMetadata.provider != None)  # noqa: E711
            .filter(MediaMetadata.provider != "")
            .group_by(MediaMetadata.media_item_id)
            .subquery()
        )

        q = (
            session.query(MediaItem, MediaMetadata)
            .join(sub, sub.c.mid == MediaItem.id)
            .join(
                MediaMetadata,
                (MediaMetadata.media_item_id == MediaItem.id) & (MediaMetadata.version == sub.c.maxv),
            )
            .filter(
                or_(
                    (MediaItem.poster_path == None),  # noqa: E711
                    (~MediaItem.poster_path.like("/media-assets/%")),
                    (MediaItem.backdrop_path == None),  # noqa: E711
                    (~MediaItem.backdrop_path.like("/media-assets/%")),
                )
            )
            .order_by(MediaItem.id.asc())
        )
        if limit_items:
            q = q.limit(int(limit_items))

        for mi, mm in q.all():
            items_examined += 1
            try:
                if manage_state:
                    try:
                        set_state(
                            {
                                "running": True,
                                "current_step": "backfill_images",
                                "current_id": getattr(mi, "id", None),
                                "current_title": getattr(mi, "title", None) or getattr(mi, "base_title", None),
                                "last_updated": int(time.time() * 1000),
                            }
                        )
                    except Exception:
                        pass

                try:
                    data = json.loads(mm.data) if getattr(mm, "data", None) else None
                except Exception:
                    data = getattr(mm, "data", None)
                if not isinstance(data, dict):
                    continue
                imgs = data.get("images")
                if not imgs:
                    continue

                cur_p = getattr(mi, "poster_path", None)
                cur_b = getattr(mi, "backdrop_path", None)
                need_p = not (isinstance(cur_p, str) and cur_p.strip().startswith("/media-assets/"))
                need_b = not (isinstance(cur_b, str) and cur_b.strip().startswith("/media-assets/"))

                if need_p:
                    u = _pick_best_image_url(imgs, kind="poster")
                    if u:
                        ok = bool(_download_image_and_record(session, mi, str(u), source=str(getattr(mm, "provider", None) or "provider"), kind="poster"))
                        if ok:
                            posters += 1
                if need_b:
                    u = _pick_best_image_url(imgs, kind="backdrop")
                    if u:
                        ok = bool(_download_image_and_record(session, mi, str(u), source=str(getattr(mm, "provider", None) or "provider"), kind="backdrop"))
                        if ok:
                            backdrops += 1

                try:
                    sync_series_season_fields(mi, session)
                    session.add(mi)
                    session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            except Exception:
                errors += 1
                try:
                    session.rollback()
                except Exception:
                    pass
                continue

        return {"ok": True, "items_examined": items_examined, "posters": posters, "backdrops": backdrops, "errors": errors}
    finally:
        try:
            if manage_state:
                clear_state()
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass


def backfill_tmdb_episode_titles_for_media_item(
    media_item_id: int,
    manage_state: bool = True,
) -> dict[str, int | str | bool]:
    """Backfill episode titles/synopsis for a single media_item's season.

    Used for "lazy" fixes when the UI opens a modal/player and detects generic episode titles.
    """
    if tmdb is None or not hasattr(tmdb, "tmdb_fetch_season"):
        return {"ok": False, "detail": "tmdb_not_available", "seasons_updated": 0, "episodes_updated": 0}
    try:
        if not _tmdb_configured():
            return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0, "episodes_updated": 0}
    except Exception:
        return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0, "episodes_updated": 0}

    try:
        lang = str(get("metadata_language") or "").strip() or "en-US"
    except Exception:
        lang = "en-US"

    try:
        localize = bool(get("tmdb_localization")) and bool(lang) and not str(lang).lower().startswith("en")
    except Exception:
        localize = False

    def is_generic_episode_title(v: str | None) -> bool:
        if v is None:
            return True
        s = str(v).strip()
        if not s:
            return True
        if re.fullmatch(r"\d{1,4}", s):
            return True
        if s.lower().startswith("episode ") or s.lower().startswith("episodio "):
            return True
        return False

    def _norm_text(v: str | None) -> str:
        if not isinstance(v, str):
            return ""
        s = v.strip().lower()
        if not s:
            return ""
        return re.sub(r"\s+", " ", s)

    def _is_english_fallback(loc_text: str | None, en_text: str | None) -> bool:
        a = _norm_text(loc_text)
        b = _norm_text(en_text)
        if not a or not b:
            return False
        return a == b

    def _sanitize_title(v: str | None) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        if not s:
            return None
        s = re.sub(r"[\(\[]\s*(uncut|dub|dual\s*audio|multi|batch|1080p|720p|480p|bdrip|bluray|webrip|web[- ]?dl|x264|h264|hevc)[^\)\]]*[\)\]]", "", s, flags=re.IGNORECASE)
        s = re.sub(r"(?i)\b(?:episode|episodio)\s*0*\d{1,4}\b", "", s)
        s = re.sub(r"\s{2,}", " ", s).strip(" -_")
        return s or None

    def _sanitize_overview(v: str | None) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        if not s:
            return None
        try:
            from ..utils.utils import limpiar_traduccion  # local import
            s = limpiar_traduccion(s, silent=True)
        except Exception:
            pass
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s or None

    def _get_jikan_meta(item_id: int | None) -> dict | None:
        if item_id is None:
            return None
        try:
            iid = int(item_id)
        except Exception:
            return None
        try:
            md = (
                session.query(MediaMetadata)
                .filter(MediaMetadata.media_item_id == iid)
                .filter(MediaMetadata.provider == "jikan")
                .order_by(MediaMetadata.id.desc())
                .first()
            )
        except Exception:
            md = None
        if not md:
            return None
        try:
            raw = getattr(md, "data", None)
            if isinstance(raw, str) and raw.strip():
                return json.loads(raw)
            if isinstance(raw, dict):
                return raw
        except Exception:
            return None
        return None

    seasons_updated = 0
    episodes_updated = 0

    session = SessionLocal()
    try:
        si = session.query(SeasonItem).filter(SeasonItem.media_item_id == int(media_item_id)).first()
        if not si:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}
        season = session.query(Season).filter(Season.id == si.season_id).first()
        if not season:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}

        season_number = getattr(season, "season_number", None)
        if not isinstance(season_number, int) or season_number < 0:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}
        tmdb_season_number = None
        try:
            tsn = getattr(season, "tmdb_season_number", None)
            tmdb_season_number = int(tsn) if tsn is not None else None
        except Exception:
            tmdb_season_number = None
        if tmdb_season_number is None:
            tmdb_season_number = int(season_number)

        mi = session.query(MediaItem).filter(MediaItem.id == int(media_item_id)).first()
        if not mi:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}

        series = None
        try:
            if getattr(season, "series_id", None) is not None:
                series = session.query(Series).filter(Series.id == season.series_id).first()
        except Exception:
            series = None

        tmdb_id = getattr(mi, "tmdb_id", None) or (getattr(series, "tmdb_id", None) if series else None)
        if not tmdb_id:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}
        tmdb_id_s = str(tmdb_id)

        mapped_offset = 0
        try:
            tstart = getattr(season, "tmdb_episode_start", None)
            if tstart is not None:
                mapped_offset = max(0, int(tstart) - 1)
        except Exception:
            mapped_offset = 0
        force_overwrite = bool(isinstance(mapped_offset, int) and mapped_offset > 0)

        eps = session.query(Episode).filter(Episode.season_id == season.id).all()
        if not eps:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}

        need_loc = False
        if localize:
            try:
                need_loc = any(not (getattr(ep, "title_localized", None) or "").strip() for ep in eps)
            except Exception:
                need_loc = True

        need_en = False
        try:
            need_en = any(is_generic_episode_title(getattr(ep, "title_en", None)) for ep in eps)
        except Exception:
            need_en = False

        need_syn = False
        try:
            need_syn = any(not (getattr(ep, "synopsis_localized", None) or "").strip() for ep in eps)
        except Exception:
            need_syn = False

        if not need_loc and not need_en and not need_syn and not force_overwrite:
            return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": 0, "episodes_updated": 0}

        if manage_state:
            try:
                set_state(
                    {
                        "running": True,
                        "current_id": getattr(mi, "id", None),
                        "current_title": (getattr(mi, "title_localized", None) or getattr(mi, "title_en", None) or getattr(mi, "title", None) or "TMDB"),
                        "current_step": "backfill_episodes_one",
                        "backfill_lang": lang,
                        "backfill_localized": bool(localize),
                        "backfill_total": 1,
                        "backfill_done": 0,
                        "backfill_seasons_updated": 0,
                        "backfill_episodes_updated": 0,
                    }
                )
            except Exception:
                pass

        def _split_offset(season_data: dict | None) -> int | None:
            if not isinstance(season_data, dict):
                return None
            eps = season_data.get("episodes") or []
            if not isinstance(eps, list) or not eps:
                return None
            # Some shows split a single TMDB season into "Part 1/2". Prefer explicit mid-season markers
            # if present; otherwise infer by a big air-date gap.
            for e in eps:
                if isinstance(e, dict) and e.get("episode_type") == "mid_season":
                    n = e.get("episode_number")
                    if n is not None:
                        try:
                            return int(n)
                        except Exception:
                            return None
                    return None
            prev_date = None
            prev_num = None
            for e in eps:
                if not isinstance(e, dict):
                    continue
                ad = e.get("air_date")
                n = e.get("episode_number")
                if n is not None:
                    try:
                        n = int(n)
                    except Exception:
                        n = None
                if not isinstance(ad, str) or not ad.strip():
                    continue
                try:
                    dt = datetime.fromisoformat(ad.strip())
                except Exception:
                    try:
                        dt = datetime.strptime(ad.strip(), "%Y-%m-%d")
                    except Exception:
                        dt = None
                if dt is None:
                    continue
                if prev_date is not None and prev_num is not None:
                    try:
                        gap = (dt - prev_date).days
                    except Exception:
                        gap = None
                    if gap is not None and gap >= 28:
                        return int(prev_num)
                prev_date = dt
                prev_num = n
            return None

        def _episode_map(season_data: dict | None) -> dict[int, dict]:
            out: dict[int, dict] = {}
            if not isinstance(season_data, dict):
                return out
            eps = season_data.get("episodes") or []
            if not isinstance(eps, list):
                return out
            for e in eps:
                if not isinstance(e, dict):
                    continue
                n = e.get("episode_number")
                if not isinstance(n, int):
                    if n is None:
                        continue
                    try:
                        n = int(n)
                    except Exception:
                        continue
                out[n] = e
            return out

        season_data_loc = None
        if need_loc or force_overwrite:
            try:
                season_data_loc = tmdb.tmdb_fetch_season(tmdb_id_s, season_number=tmdb_season_number, language=lang)  # type: ignore[arg-type]
            except Exception:
                season_data_loc = None

        season_data_en = None
        if need_en or force_overwrite or (isinstance(season_number, int) and season_number == 2):
            try:
                season_data_en = tmdb.tmdb_fetch_season(tmdb_id_s, season_number=tmdb_season_number, language="en-US")  # type: ignore[arg-type]
            except Exception:
                season_data_en = None

        # Season synopsis + poster (best-effort) using localized + EN fallback.
        try:
            def _season_synopsis(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("synopsis") or d.get("overview") or None
                if isinstance(v, str) and v.strip():
                    return v.strip()
                return None

            def _season_poster(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("poster_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
                imgs = d.get("images") if isinstance(d, dict) else None
                try:
                    return _pick_best_image_url(imgs, kind="poster") if imgs else None
                except Exception:
                    return None
            
            def _season_backdrop(d: dict | None) -> str | None:
                if not isinstance(d, dict):
                    return None
                v = d.get("backdrop_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
                imgs = d.get("images") if isinstance(d, dict) else None
                try:
                    return _pick_best_image_url(imgs, kind="backdrop") if imgs else None
                except Exception:
                    return None

            try:
                cur_syn = getattr(season, "synopsis_localized", None)
            except Exception:
                cur_syn = None
            if not (isinstance(cur_syn, str) and cur_syn.strip()):
                syn = _season_synopsis(season_data_loc) or _season_synopsis(season_data_en)
                if syn:
                    season_any = _cast(_Any, season)
                    season_any.synopsis_localized = syn

            series_poster = None
            try:
                series_poster = getattr(series, "main_poster", None) if series else None
            except Exception:
                series_poster = None
            if not (isinstance(series_poster, str) and series_poster.strip()):
                try:
                    series_poster = getattr(mi, "poster_path", None)
                except Exception:
                    series_poster = None

            series_backdrop = None
            try:
                series_backdrop = getattr(mi, "backdrop_path", None)
            except Exception:
                series_backdrop = None
            if not (isinstance(series_backdrop, str) and series_backdrop.strip()):
                series_backdrop = None

            try:
                cur_poster = getattr(season, "poster_path", None)
            except Exception:
                cur_poster = None
            if not (isinstance(cur_poster, str) and cur_poster.strip()):
                poster = _season_poster(season_data_loc) or _season_poster(season_data_en)
                if not poster and isinstance(series_poster, str) and series_poster.strip():
                    poster = series_poster.strip()
                if poster:
                    season_any = _cast(_Any, season)
                    season_any.poster_path = poster

            try:
                cur_backdrop = getattr(season, "backdrop_path", None)
            except Exception:
                cur_backdrop = None
            if not (isinstance(cur_backdrop, str) and cur_backdrop.strip()):
                backdrop = _season_backdrop(season_data_loc) or _season_backdrop(season_data_en)
                if not backdrop and isinstance(series_backdrop, str) and series_backdrop.strip():
                    backdrop = series_backdrop.strip()
                if not backdrop and isinstance(series_poster, str) and series_poster.strip():
                    backdrop = series_poster.strip()
                if backdrop:
                    season_any = _cast(_Any, season)
                    season_any.backdrop_path = backdrop

            try:
                session.add(season)
            except Exception:
                pass
        except Exception:
            pass

        map_loc = _episode_map(season_data_loc)
        map_en = _episode_map(season_data_en)
        offset = int(mapped_offset or 0)
        if offset <= 0 and isinstance(season_number, int) and season_number == 2 and not map_loc and not map_en:
            season1_loc = None
            season1_en = None
            try:
                season1_loc = tmdb.tmdb_fetch_season(tmdb_id_s, season_number=1, language=lang)  # type: ignore[arg-type]
            except Exception:
                season1_loc = None
            try:
                season1_en = tmdb.tmdb_fetch_season(tmdb_id_s, season_number=1, language="en-US")  # type: ignore[arg-type]
            except Exception:
                season1_en = None
            base_season = season1_en if isinstance(season1_en, dict) else (season1_loc if isinstance(season1_loc, dict) else None)
            off = _split_offset(base_season if isinstance(base_season, dict) else None)
            if off:
                offset = int(off)
                map_loc = _episode_map(season1_loc if isinstance(season1_loc, dict) else None)
                map_en = _episode_map(season1_en if isinstance(season1_en, dict) else None)
                try:
                    season_any = _cast(_Any, season)
                    season_any.tmdb_season_number = 1
                    season_any.tmdb_episode_start = int(offset) + 1
                    session.add(season)
                except Exception:
                    pass
        force_overwrite = bool(isinstance(offset, int) and offset > 0)
        if force_overwrite:
            need_en = True
        # Ensure we have EN data for forced overwrite cases.
        if force_overwrite and not map_en:
            try:
                season_data_en = tmdb.tmdb_fetch_season(tmdb_id_s, season_number=1 if season_number == 2 else season_number, language="en-US")  # type: ignore[arg-type]
                map_en = _episode_map(season_data_en if isinstance(season_data_en, dict) else None)
            except Exception:
                pass

        # Global synopsis fallback (series-level) when episode overviews are missing.
        global_synopsis = None
        if need_syn:
            try:
                details_loc = tmdb.tmdb_fetch_by_id(tmdb_id_s, media_type="tv", language=lang)  # type: ignore[arg-type]
            except Exception:
                details_loc = None
            if isinstance(details_loc, dict):
                try:
                    gs = _sanitize_overview(details_loc.get("synopsis") or details_loc.get("overview") or "")
                    if isinstance(gs, str) and gs.strip():
                        global_synopsis = gs.strip()
                except Exception:
                    global_synopsis = None
            # Fallback to TMDB English (not Jikan) if localized synopsis is missing.
            if not global_synopsis:
                try:
                    details_en = tmdb.tmdb_fetch_by_id(tmdb_id_s, media_type="tv", language="en-US")  # type: ignore[arg-type]
                except Exception:
                    details_en = None
                if isinstance(details_en, dict):
                    try:
                        gs = _sanitize_overview(details_en.get("synopsis") or details_en.get("overview") or "")
                        if isinstance(gs, str) and gs.strip():
                            global_synopsis = gs.strip()
                    except Exception:
                        global_synopsis = None
            if not global_synopsis and isinstance(season_data_loc, dict):
                try:
                    gs = _sanitize_overview(season_data_loc.get("synopsis") or season_data_loc.get("overview") or "")
                    if isinstance(gs, str) and gs.strip():
                        global_synopsis = gs.strip()
                except Exception:
                    global_synopsis = None
            if not global_synopsis:
                try:
                    jmeta = _get_jikan_meta(getattr(mi, "id", None))
                except Exception:
                    jmeta = None
                if isinstance(jmeta, dict):
                    js = _sanitize_overview(jmeta.get("synopsis") or "")
                    if isinstance(js, str) and js.strip():
                        global_synopsis = js.strip()

        by_num: dict[int, Episode] = {}
        for ep in eps:
            try:
                n = getattr(ep, "episode_number", None)
                if isinstance(n, int):
                    by_num[n] = ep
            except Exception:
                continue

        changed_this_season = False

        for n, row in by_num.items():
            target_n = int(n) + int(offset) if isinstance(offset, int) and offset > 0 else int(n)
            row_any = _cast(_Any, row)
            en_ep = map_en.get(target_n)
            en_title = None
            en_ov = None
            if isinstance(en_ep, dict):
                en_title = _sanitize_title(en_ep.get("title") or en_ep.get("name") or "")
                en_ov = _sanitize_overview(en_ep.get("overview") or "")
            if (need_loc or force_overwrite) and (force_overwrite or not (getattr(row, "title_localized", None) or "").strip()):
                e = map_loc.get(target_n)
                if isinstance(e, dict):
                    try:
                        tloc = _sanitize_title(e.get("title") or e.get("name") or "")
                        if isinstance(tloc, str) and tloc.strip():
                            if not _is_english_fallback(tloc, en_title):
                                if is_generic_episode_title(tloc):
                                    if isinstance(en_title, str) and en_title.strip() and not is_generic_episode_title(en_title):
                                        tloc = en_title
                                row_any.title_localized = tloc.strip()
                                if force_overwrite:
                                    row_any.title = tloc.strip()
                                changed_this_season = True
                                episodes_updated += 1
                    except Exception:
                        pass
                if force_overwrite and (getattr(row_any, "title_localized", None) or "").strip():
                    row_any.title = row_any.title_localized
            if (need_syn or force_overwrite) and (force_overwrite or not (getattr(row, "synopsis_localized", None) or "").strip()):
                e = map_loc.get(target_n)
                if isinstance(e, dict):
                    try:
                        ov = _sanitize_overview(e.get("overview") or "")
                        if isinstance(ov, str) and ov.strip():
                            if not _is_english_fallback(ov, en_ov):
                                row_any.synopsis_localized = ov.strip()
                                changed_this_season = True
                    except Exception:
                        pass
                if not (getattr(row, "synopsis_localized", None) or "").strip():
                    if isinstance(global_synopsis, str) and global_synopsis.strip():
                        # Requirement: when episode synopsis is missing, fall back to global series synopsis.
                        row_any.synopsis_localized = global_synopsis.strip()
                        changed_this_season = True
            # Episode still_path (download/cache locally)
            try:
                cur_still = getattr(row, "still_path", None)
            except Exception:
                cur_still = None
            if not (isinstance(cur_still, str) and cur_still.strip()):
                still_url = None
                e = map_loc.get(target_n)
                if isinstance(e, dict):
                    su = e.get("still_path")
                    if isinstance(su, str) and su.strip():
                        still_url = su.strip()
                if not still_url:
                    e2 = map_en.get(target_n)
                    if isinstance(e2, dict):
                        su = e2.get("still_path")
                        if isinstance(su, str) and su.strip():
                            still_url = su.strip()
                if still_url:
                    _download_episode_still(session, row, still_url)
            session.add(row)

        if need_en or force_overwrite:
            for n, row in by_num.items():
                target_n = int(n) + int(offset) if isinstance(offset, int) and offset > 0 else int(n)
                e = map_en.get(target_n)
                if not isinstance(e, dict):
                    continue
                row_any = _cast(_Any, row)
                try:
                    ten = _sanitize_title(e.get("title") or e.get("name") or "")
                    if isinstance(ten, str) and ten.strip() and (force_overwrite or is_generic_episode_title(getattr(row, "title_en", None))):
                        row_any.title_en = ten.strip()
                        if force_overwrite or is_generic_episode_title(getattr(row, "title", None)):
                            row_any.title = ten.strip()
                        changed_this_season = True
                        episodes_updated += 1
                        session.add(row)
                except Exception:
                    continue

        if changed_this_season:
            try:
                session.commit()
                seasons_updated = 1
            except Exception:
                session.rollback()

        if manage_state:
            try:
                set_state(
                    {
                        "backfill_done": 1,
                        "backfill_seasons_updated": int(seasons_updated),
                        "backfill_episodes_updated": int(episodes_updated),
                        "running": False,
                        "current_step": "idle",
                    }
                )
            except Exception:
                pass

        return {"ok": True, "localized": bool(localize), "lang": lang, "seasons_updated": seasons_updated, "episodes_updated": episodes_updated}
    finally:
        session.close()

def repair_series_grouping_by_tmdb(dry_run: bool = False, include_details: bool = False) -> dict[str, int | bool | list]:
    """Repair normalized Series/Season grouping when a single TMDB show was split into multiple Series rows.

    Root cause: earlier scans could create a Series keyed by a generic folder title like "Temporada 2".
    If enrichment later sets `series.tmdb_id`, we can safely merge those Series by tmdb_id.
    """
    session = SessionLocal()
    try:
        dup_tmdb_ids = [
            r[0]
            for r in session.query(Series.tmdb_id)
            .filter(Series.tmdb_id != None)  # noqa: E711
            .filter(Series.tmdb_id != "")
            .group_by(Series.tmdb_id)
            .having(func.count(Series.id) > 1)
            .all()
        ]

        def _is_generic_series_title(v: str | None) -> bool:
            if v is None:
                return True
            s = str(v).strip().lower()
            if not s:
                return True
            return bool(re.fullmatch(r"(?:temporada|season|part|cour|s)\s*0*\d{1,2}", s))

        merges = 0
        seasons_moved = 0
        series_deleted = 0
        details: list[dict] = []

        for tid in dup_tmdb_ids:
            rows = session.query(Series).filter(Series.tmdb_id == tid).order_by(Series.id.asc()).all()
            if len(rows) < 2:
                continue

            # Pick canonical series: prefer non-generic title/provider_id, then lowest id.
            def score(sr: Series) -> tuple[int, int, int]:
                title = getattr(sr, "title", None)
                provider_id = getattr(sr, "provider_id", None)
                generic = 1 if (_is_generic_series_title(title) or _is_generic_series_title(provider_id)) else 0
                # Longer titles tend to be more descriptive; invert for sorting.
                ln = len(str(title or "")) if title else 0
                return (generic, -ln, int(getattr(sr, "id", 0) or 0))

            rows_sorted = sorted(rows, key=score)
            keep = rows_sorted[0]
            keep_any = _cast(_Any, keep)
            drop = [r for r in rows_sorted[1:] if getattr(r, "id", None) != getattr(keep, "id", None)]

            drop_ids = [int(getattr(r, "id")) for r in drop if getattr(r, "id", None) is not None]
            if not drop_ids:
                continue

            # Move seasons.
            srows = session.query(Season).filter(Season.series_id.in_(drop_ids)).all()
            if not srows:
                # nothing to move; safe to delete duplicates
                pass
            else:
                moved_ids: list[int] = []
                for s in srows:
                    if getattr(s, "series_id", None) != keep.id:
                        s.series_id = keep.id
                        seasons_moved += 1
                        try:
                            moved_ids.append(int(getattr(s, "id", 0) or 0))
                        except Exception:
                            pass
                        session.add(s)

            # Improve kept series fields using best available values across rows.
            try:
                if not getattr(keep, "title_en", None):
                    for r in rows_sorted:
                        te = getattr(r, "title_en", None)
                        if te and isinstance(te, str) and te.strip() and not _is_generic_series_title(te):
                            keep_any.title_en = te.strip()
                            break
            except Exception:
                pass
            try:
                if not getattr(keep, "title_localized", None):
                    for r in rows_sorted:
                        tl = getattr(r, "title_localized", None)
                        if tl and isinstance(tl, str) and tl.strip() and not _is_generic_series_title(tl):
                            keep_any.title_localized = tl.strip()
                            break
            except Exception:
                pass
            try:
                # Ensure `title` is not generic.
                if _is_generic_series_title(getattr(keep, "title", None)):
                    best = getattr(keep, "title_localized", None) or getattr(keep, "title_en", None)
                    if best and isinstance(best, str) and best.strip():
                        keep_any.title = best.strip()
            except Exception:
                pass
            session.add(keep)

            if dry_run:
                session.rollback()
                merges += 1
                continue

            try:
                session.commit()
            except Exception:
                session.rollback()
                continue

            merges += 1

            # Delete orphan duplicate Series rows (now with no seasons).
            for r in drop:
                try:
                    sid = int(getattr(r, "id"))
                except Exception:
                    continue
                try:
                    left = session.query(Season).filter(Season.series_id == sid).count()
                except Exception:
                    left = 1
                if left:
                    continue
                try:
                    session.query(Series).filter(Series.id == sid).delete(synchronize_session=False)
                    session.commit()
                    series_deleted += 1
                except Exception:
                    session.rollback()

            if include_details:
                try:
                    details.append(
                        {
                            "tmdb_id": str(tid),
                            "keep_series_id": int(getattr(keep, "id", 0) or 0),
                            "drop_series_ids": [int(x) for x in drop_ids],
                            "seasons_moved": int(len(moved_ids)),
                            "moved_season_ids": moved_ids[:200],
                        }
                    )
                except Exception:
                    pass

        out: dict[str, int | bool | list] = {
            "ok": True,
            "dry_run": bool(dry_run),
            "merges": merges,
            "seasons_moved": seasons_moved,
            "series_deleted": series_deleted,
        }
        if include_details:
            out["details"] = details
        return out
    finally:
        session.close()


def ensure_series_season_links_for_tmdb_items(
    *,
    limit_items: int | None = None,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Attach orphan MediaItems with tmdb_id to Series/Season/SeasonItem.

    Some scans/enrichments can set `media_item.tmdb_id` without creating
    Series/Season/SeasonItem rows. This links them globally by tmdb_id.
    """
    session = SessionLocal()
    created_series = 0
    created_seasons = 0
    created_links = 0
    skipped = 0
    try:
        q = (
            session.query(MediaItem)
            .filter(MediaItem.tmdb_id != None)  # noqa: E711
            .filter(MediaItem.tmdb_id != "")
            .filter(MediaItem.media_type != None)  # noqa: E711
            .filter(MediaItem.media_type.ilike("series"))
            .order_by(MediaItem.id.asc())
        )
        if limit_items and int(limit_items) > 0:
            q = q.limit(int(limit_items))
        rows = q.all()

        def _is_generic_title(v: str | None) -> bool:
            if v is None:
                return True
            s = str(v).strip().lower()
            if not s:
                return True
            return bool(re.fullmatch(r"(?:temporada|season|part|cour|s)\s*0*\d{1,2}", s))

        for mi in rows:
            try:
                si = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi.id).first()
            except Exception:
                si = None
            if si:
                skipped += 1
                continue

            try:
                tmdb_id = str(getattr(mi, "tmdb_id", "") or "").strip()
            except Exception:
                tmdb_id = ""
            if not tmdb_id:
                skipped += 1
                continue

            # Determine season number from title or folder name.
            season_number = None
            base_title = None
            try:
                base_title, season_number = _extract_season_from_title(getattr(mi, "title", None))
            except Exception:
                base_title, season_number = None, None
            if not season_number:
                try:
                    base_title, season_number = _extract_season_from_title(getattr(mi, "base_title", None))
                except Exception:
                    pass
            if not season_number:
                try:
                    cp = getattr(mi, "canonical_path", None)
                    if cp:
                        base_title, season_number = _extract_season_from_title(os.path.basename(str(cp)))
                except Exception:
                    pass
            if not season_number:
                season_number = 1

            # Respect Jikan specials -> Season 0
            try:
                jtype = str(getattr(mi, "jikan_type", "") or "").strip().lower()
                if jtype and (jtype.startswith("special") or jtype in {"ova", "ona", "special", "specials"}):
                    season_number = 0
            except Exception:
                pass

            # Find or create Series by tmdb_id.
            series = None
            try:
                series = session.query(Series).filter(Series.tmdb_id == tmdb_id).order_by(Series.id.asc()).first()
            except Exception:
                series = None
            if not series:
                # Create a new Series using best available title.
                title = None
                try:
                    title = (getattr(mi, "title_en", None) or "").strip() or None
                except Exception:
                    title = None
                if not title:
                    try:
                        title = (getattr(mi, "title", None) or "").strip() or None
                    except Exception:
                        title = None
                if not title and base_title:
                    title = base_title
                if not title:
                    title = tmdb_id
                series = Series(
                    title=title,
                    title_en=(getattr(mi, "title_en", None) or None),
                    tmdb_id=tmdb_id,
                    mal_id=(getattr(mi, "mal_id", None) or None),
                )
                if not dry_run:
                    session.add(series)
                    session.flush()
                created_series += 1
            else:
                # Improve series title if it's generic.
                try:
                    if _is_generic_title(getattr(series, "title", None)) and base_title:
                        series_any = _cast(_Any, series)
                        series_any.title = base_title
                        session.add(series)
                except Exception:
                    pass

            # Find or create Season.
            season = None
            try:
                season = (
                    session.query(Season)
                    .filter(Season.series_id == series.id)
                    .filter(Season.season_number == int(season_number))
                    .order_by(Season.id.asc())
                    .first()
                )
            except Exception:
                season = None

            if season:
                # If this season already linked to another item, create a new season row.
                try:
                    existing_si = session.query(SeasonItem).filter(SeasonItem.season_id == season.id).first()
                except Exception:
                    existing_si = None
                if existing_si and getattr(existing_si, "media_item_id", None) != mi.id:
                    season = None

            if not season:
                season = Season(series_id=series.id, season_number=int(season_number))
                try:
                    if base_title and not _is_generic_title(base_title):
                        season_any = _cast(_Any, season)
                        season_any.title_en = base_title
                except Exception:
                    pass
                if not dry_run:
                    session.add(season)
                    session.flush()
                created_seasons += 1

            if not dry_run:
                si = SeasonItem(season_id=season.id, media_item_id=mi.id)
                session.add(si)
                created_links += 1

            try:
                sync_series_season_fields(mi, session)
            except Exception:
                pass

        if dry_run:
            session.rollback()
        else:
            session.commit()

        return {
            "ok": True,
            "created_series": int(created_series),
            "created_seasons": int(created_seasons),
            "created_links": int(created_links),
            "skipped": int(skipped),
        }
    finally:
        session.close()


def enrich_one(media_item_id: int, providers_override: Optional[Iterable[str]] = None, force_media_type: Optional[str] = None) -> bool:
    session = SessionLocal()
    task_id = None
    task_status = "ok"
    ctx_tokens = None
    try:
        mi = session.query(MediaItem).filter(MediaItem.id == media_item_id).first()
        if not mi:
            return False
        mi = _cast(_Any, mi)
        try:
            task_id = f"enrich:{getattr(mi, 'id', None)}:{int(time.time())}"
        except Exception:
            task_id = None
        try:
            if task_id:
                ctx_tokens = telemetry.set_task_context(task_id, "enrich")
        except Exception:
            ctx_tokens = None
        try:
            if task_id:
                telemetry.task_start(task_id, "enrich", media_item_id=getattr(mi, "id", None), title=getattr(mi, "title", None))
        except Exception:
            pass
        try:
            payload = {'running': True, 'current_id': mi.id, 'current_title': mi.title or '', 'current_step': 'starting'}
            if task_id:
                payload['task_id'] = task_id
            set_state(payload)
        except Exception:
            pass
        try:
            if task_id:
                logger.info("[enrich:%s] start media_item=%s title=%s", task_id, getattr(mi, "id", None), getattr(mi, "title", None))
        except Exception:
            pass
        cfg_movies = get('metadata_movies_provider') or 'tmdb'
        cfg_anime = get('metadata_anime_provider') or 'kitsu'
        if force_media_type:
            media_type = force_media_type
        else:
            inferred = _decide_media_type(mi, session) or 'movie'
            cur_mt = (mi.media_type or '').strip().lower()
            if inferred == 'series' and cur_mt not in ('series', 'tv', 'show'):
                media_type = 'series'
            else:
                media_type = mi.media_type or inferred
        mi.media_type = media_type
        title = mi.title or ''
        normalized = {}
        candidate_titles = _candidate_titles_for_item(mi)
        try:
            providers_override_norm = [p.strip().lower() for p in (providers_override or []) if isinstance(p, str) and p]
        except Exception:
            providers_override_norm = []
        strict_provider_override = bool(providers_override_norm)
        tmdb_allowed_by_override = (not strict_provider_override) or ('tmdb' in providers_override_norm)
        has_mal_identity = bool(str(getattr(mi, "mal_id", "") or "").strip())
        force_tmdb_identity = bool(strict_provider_override and providers_override_norm == ["tmdb"])
        prefer_mal_identity = bool(has_mal_identity and not force_tmdb_identity)
        root_allows_tmdb = _tmdb_allowed_for_media_item(session, mi, manual_override=force_tmdb_identity)

        # Strict hierarchy: if this looks like a season subfolder and parent already has TMDB identity,
        # skip TMDB search and fetch the season directly.
        try:
            inherited = _inherit_tmdb_identity_from_parent_if_season(mi, session)
        except Exception:
            inherited = None
        if (not prefer_mal_identity) and root_allows_tmdb and tmdb_allowed_by_override and inherited and tmdb is not None and hasattr(tmdb, "tmdb_fetch_season"):
            parent_tmdb_id, season_number = inherited
            try:
                lang = str(get("metadata_language") or "").strip() or "en-US"
            except Exception:
                lang = "en-US"
            try:
                mi.tmdb_id = str(parent_tmdb_id)
                mi.media_type = 'series'
                session.add(mi)
                session.commit()
            except Exception:
                session.rollback()
            try:
                with telemetry.phase("TMDB_Enrichment"):
                    normalized2 = tmdb.tmdb_fetch_season(str(parent_tmdb_id), season_number=season_number, language=lang)  # type: ignore[arg-type]
            except Exception:
                normalized2 = None
            if isinstance(normalized2, dict) and normalized2:
                try:
                    normalized2.setdefault("season_number", int(season_number))
                except Exception:
                    pass
                try:
                    set_state({'current_step': 'persist_metadata'})
                except Exception:
                    pass
                try:
                    _attach_tmdb_i18n(normalized2, 'series')
                    _attach_episodes('tmdb', normalized2, 'series', mi)
                except Exception:
                    pass
                _persist_metadata(session, mi, normalized2)
                try:
                    clear_state()
                except Exception:
                    pass
                return True
        allow_tmdb_sidecar = bool(root_allows_tmdb)
        try:
            if providers_override:
                try:
                    providers_order = providers_override_norm
                except Exception:
                    providers_order = []
            else:
                root_type = _root_type_for_media_item(session, mi)
                providers_order = _provider_order_for_item(
                    media_type,
                    getattr(mi, 'mal_id', None),
                    cfg_movies,
                    cfg_anime,
                    getattr(mi, 'media_root', None),
                    root_type,
                )
            logger.info("enrich_one: media_item=%s media_type=%s providers_order=%s", getattr(mi, 'id', None), media_type, providers_order)
            root_type_eff = None
            try:
                root_type_eff = _root_type_for_media_item(session, mi)
            except Exception:
                root_type_eff = None
            is_anime_context = False
            try:
                is_anime_context = str(root_type_eff or "").strip().lower() == "anime"
            except Exception:
                is_anime_context = False
            if not is_anime_context:
                try:
                    is_anime_context = str(getattr(mi, "library_type", "") or "").strip().lower() == "anime"
                except Exception:
                    is_anime_context = False
            if not is_anime_context:
                try:
                    is_anime_context = bool(getattr(mi, "mal_id", None))
                except Exception:
                    is_anime_context = False
            if not is_anime_context:
                try:
                    is_anime_context = str(media_type or "").strip().lower() == "anime"
                except Exception:
                    is_anime_context = False
            # Explicit manual provider override should be respected even when the item was
            # misclassified outside the anime library. This avoids silently skipping Jikan/Kitsu.
            if strict_provider_override and any(p in ("jikan", "kitsu") for p in (providers_order or [])):
                is_anime_context = True
            try:
                tmdb_ok = _tmdb_configured()
            except Exception:
                tmdb_ok = False
            logger.debug('enrich_one: media_item=%s tmdb_configured=%s', getattr(mi, 'id', None), tmdb_ok)
            if providers_order == ['tmdb'] and (not root_allows_tmdb or not tmdb_ok):
                logger.warning(
                    "enrich_one: TMDB not allowed/configured; skipping enrichment for media_item=%s",
                    getattr(mi, "id", None),
                )
                try:
                    try:
                        mi.title = _fallback_title_for_item(mi)
                    except Exception:
                        pass
                    mi.status = "NO_MATCH"
                    try:
                        mi.is_identified = False
                    except Exception:
                        pass
                    session.add(mi)
                    session.commit()
                except Exception:
                    session.rollback()
                try:
                    clear_state()
                except Exception:
                    pass
                task_status = "no_match"
                return False
            try:
                if 'tmdb' in providers_order and (not root_allows_tmdb or not tmdb_ok):
                    providers_order = [p for p in providers_order if p != 'tmdb']
            except Exception:
                pass
            allow_tmdb_sidecar = bool(root_allows_tmdb and ((not strict_provider_override) or ('tmdb' in providers_order)))
            if not providers_order:
                logger.warning(
                    "enrich_one: empty providers_order; skipping enrichment for media_item=%s",
                    getattr(mi, "id", None),
                )
                try:
                    mi.status = "NO_MATCH"
                    session.add(mi)
                    session.commit()
                except Exception:
                    session.rollback()
                try:
                    clear_state()
                except Exception:
                    pass
                task_status = "no_match"
                return False
        except Exception:
            root_type = _root_type_for_media_item(session, mi)
            providers_order = _provider_order_for_item(
                media_type,
                getattr(mi, 'mal_id', None),
                cfg_movies,
                cfg_anime,
                getattr(mi, 'media_root', None),
                root_type,
            )

        provider_error_any = False
        for p in providers_order:
            try:
                res = None
                try:
                    set_state({'current_step': f'search:{p}'})
                except Exception:
                    pass
                if p == 'jikan' and jikan is not None:
                    if not is_anime_context:
                        continue
                    phase_t0 = time.perf_counter()
                    try:
                        telemetry.task_phase("Jikan_Search", "enter")
                    except Exception:
                        pass
                    try:
                        if getattr(mi, 'mal_id', None) and hasattr(jikan, 'fetch_by_id'):
                            try:
                                logger.info('enrich_one: calling jikan.fetch_by_id mal_id=%s media_item=%s', getattr(mi, 'mal_id', None), getattr(mi, 'id', None))
                                res = jikan.fetch_by_id(str(mi.mal_id))
                                logger.info('enrich_one: jikan.fetch_by_id result for media_item=%s -> %s', getattr(mi, 'id', None), 'ok' if res else 'none')
                            except Exception:
                                logger.exception('enrich_one: jikan.fetch_by_id failed for media_item=%s', getattr(mi, 'id', None))
                                provider_error_any = True
                                res = None
                        if not res:
                            # provider_jikan already expands name variants; do a single request per item.
                            # Jikan can be sensitive to season-number-first queries ("Title 2", "Title 1"),
                            # so try a small window of candidates to avoid false NO_MATCH.
                            for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                                try:
                                    logger.info('enrich_one: jikan.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                    r = jikan.search(cand) if hasattr(jikan, 'search') else None
                                    logger.info('enrich_one: jikan.search returned %s candidates for media_item=%s', (len(r) if isinstance(r, list) else (1 if r else 0)), getattr(mi, 'id', None))
                                except Exception:
                                    logger.warning('enrich_one: jikan.search failed for query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                    provider_error_any = True
                                    r = None
                                    break
                                if isinstance(r, list) and r:
                                    res = r[0]
                                    break
                                if r and not isinstance(r, list):
                                    res = r
                                    break
                    finally:
                        try:
                            telemetry.task_phase(
                                "Jikan_Search",
                                "exit",
                                duration_ms=(time.perf_counter() - phase_t0) * 1000.0,
                            )
                        except Exception:
                            pass
                elif p == "kitsu" and kitsu is not None:
                    if not is_anime_context:
                        continue
                    try:
                        min_conf = float(os.environ.get("ARCANEA_KITSU_MIN_CONF", "0.92") or 0.92)
                    except Exception:
                        min_conf = 0.92
                    # If the MediaItem already has a Kitsu identity, fetch directly.
                    try:
                        prov0 = str(getattr(mi, "provider", "") or "").strip().lower()
                        pid0 = str(getattr(mi, "provider_id", "") or "").strip()
                    except Exception:
                        prov0 = ""
                        pid0 = ""
                    if prov0 == "kitsu" and pid0 and hasattr(kitsu, "fetch_by_id"):
                        try:
                            d = kitsu.fetch_by_id(pid0)  # type: ignore[attr-defined]
                            # A previously persisted Kitsu id can be stale/wrong after a folder rename
                            # or bad match. Reuse it only when the local season hint still agrees.
                            if d and _kitsu_match_allowed_for_item(d, mi):
                                res = d
                        except Exception:
                            provider_error_any = True
                            res = None
                    if not res:
                        for cand in candidate_titles:
                            try:
                                if hasattr(kitsu, "best_match"):
                                    r = kitsu.best_match(cand, min_conf=min_conf)  # type: ignore[attr-defined]
                                else:
                                    rows = kitsu.search(cand) if hasattr(kitsu, "search") else None
                                    r = rows[0] if isinstance(rows, list) and rows else None
                            except Exception:
                                provider_error_any = True
                                r = None
                            if r and _kitsu_match_allowed_for_item(r, mi):
                                res = r
                                break
                elif p == 'tmdb' and tmdb is not None:
                    phase_t0 = time.perf_counter()
                    try:
                        telemetry.task_phase("TMDB_Enrichment", "enter")
                    except Exception:
                        pass
                    try:
                        for cand in candidate_titles:
                            try:
                                logger.info('enrich_one: tmdb.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                r = _tmdb_search_with_optional_season(cand, media_type)
                                logger.info('enrich_one: tmdb.search returned %s candidates for media_item=%s', (len(r) if isinstance(r, list) else (1 if r else 0)), getattr(mi, 'id', None))
                            except Exception:
                                logger.exception('enrich_one: tmdb.search failed for query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                provider_error_any = True
                                r = None
                            if isinstance(r, list) and r:
                                res = r[0]
                                break
                            if r and not isinstance(r, list):
                                res = r
                                break
                    finally:
                        try:
                            telemetry.task_phase(
                                "TMDB_Enrichment",
                                "exit",
                                duration_ms=(time.perf_counter() - phase_t0) * 1000.0,
                            )
                        except Exception:
                            pass
                if not res:
                    continue
                normalized = _normalize_result(p, res)
                if normalized:
                    break
            except Exception:
                logger.debug('Provider %s failed for title=%s', p, title, exc_info=True)

        if allow_tmdb_sidecar and normalized and (normalized.get('provider') or '').strip().lower() == 'jikan':
            # Step C: use Jikan English title to anchor a TMDB lookup (best-effort).
            try:
                tmdb_ok = _tmdb_configured()
            except Exception:
                tmdb_ok = False
            try:
                existing_tid = getattr(mi, 'tmdb_id', None)
                if existing_tid:
                    normalized['tmdb_id'] = str(existing_tid)
                    tmdb_ok = False
            except Exception:
                pass
            if tmdb_ok and tmdb is not None:
                try:
                    anchors: list[str] = []
                    te = normalized.get('title_english')
                    if isinstance(te, str) and te.strip():
                        anchors.append(te.strip())
                    t0 = normalized.get('title')
                    if isinstance(t0, str) and t0.strip() and t0.strip() not in anchors:
                        anchors.append(t0.strip())
                    syns = normalized.get('title_synonyms') or []
                    if isinstance(syns, list):
                        for s in syns:
                            if isinstance(s, str) and s.strip() and s.strip() not in anchors:
                                anchors.append(s.strip())
                except Exception:
                    anchors = []
                try:
                    mt = (normalized.get('media_type') or media_type or '').strip().lower()
                except Exception:
                    mt = ''
                tmdb_type = 'tv' if mt in ('series', 'tv', 'show', 'anime') else 'movie'
                with telemetry.phase("TMDB_Enrichment"):
                    for cand in anchors[:4]:
                        try:
                            logger.info('enrich_one: tmdb.anchor search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                            r = _tmdb_search_with_optional_season(cand, tmdb_type)
                            if isinstance(r, list) and r:
                                r = r[0]
                            if r and isinstance(r, dict):
                                norm_t = _normalize_result('tmdb', r)
                            else:
                                norm_t = None
                        except Exception:
                            norm_t = None
                        if norm_t:
                            tid = norm_t.get('tmdb_id') or norm_t.get('provider_id')
                            if tid:
                                normalized['tmdb_id'] = tid
                            normalized['tmdb_match'] = norm_t
                            break

        if allow_tmdb_sidecar and not normalized:
            # Bridge: if Jikan couldn't match and TMDB is configured, try a single TMDB lookup as TV.
            try:
                try:
                    tmdb_ok = _tmdb_configured()
                except Exception:
                    tmdb_ok = False
                if tmdb_ok and ('tmdb' not in providers_order) and ('jikan' in providers_order or providers_order == ['jikan']):
                    is_series = (media_type or '').strip().lower() in ('series', 'tv', 'anime')
                    if not is_series:
                        try:
                            is_series = session.query(FileRecord).filter(FileRecord.media_item_id == mi.id).count() > 1
                        except Exception:
                            is_series = False
                    if is_series:
                        cand0 = candidate_titles[0] if candidate_titles else (mi.title or '')
                        cand0 = str(cand0 or '').strip()
                        if cand0:
                            logger.info('enrich_one: bridge jikan->tmdb (tv) query="%s" media_item=%s', cand0, getattr(mi, 'id', None))
                            with telemetry.phase("TMDB_Enrichment"):
                                r = _tmdb_search_with_optional_season(cand0, 'tv')
                            if isinstance(r, list) and r:
                                r = r[0]
                            if r and isinstance(r, dict):
                                normalized = _normalize_result('tmdb', r)
            except Exception:
                pass

        if not normalized:
            try:
                set_state({'current_step': 'provider_error' if provider_error_any else 'no_match'})
            except Exception:
                pass
            if not provider_error_any:
                try:
                    try:
                        mi.title = _fallback_title_for_item(mi)
                    except Exception:
                        pass
                    mi.status = 'NO_MATCH'
                    try:
                        mi.is_identified = False
                    except Exception:
                        pass
                    session.add(mi)
                    session.commit()
                except Exception:
                    session.rollback()
            try:
                clear_state()
            except Exception:
                pass
            task_status = "no_match" if not provider_error_any else "error"
            return False
        try:
            set_state({'current_step': 'persist_metadata'})
        except Exception:
            pass
        try:
            _attach_tmdb_i18n(normalized, media_type)
            _attach_episodes(normalized.get('provider') or '', normalized, media_type, mi)
            normalized = _supplement_jikan_with_kitsu(mi, normalized, media_type)
            if str(normalized.get("provider") or "").strip().lower() == "jikan" and str(getattr(mi, "mal_id", "") or "").strip() == str(normalized.get("provider_id") or normalized.get("mal_id") or "").strip() and not normalized.get("_episodes_from") and isinstance(normalized.get("episodes"), list) and normalized.get("episodes"):
                normalized["_episodes_from"] = "jikan_id"
        except Exception:
            pass
        _persist_metadata(session, mi, normalized)
        try:
            _map_episode_files_for_item(mi, session)
        except Exception:
            pass
        try:
            if _apply_jikan_episode_titles(mi, normalized, session):
                session.commit()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
        try:
            clear_state()
        except Exception:
            pass
        return True
    except Exception:
        task_status = "error"
        raise
    finally:
        try:
            if task_id:
                telemetry.task_end(task_id, task_status)
        except Exception:
            pass
        try:
            if ctx_tokens is not None:
                telemetry.reset_task_context(ctx_tokens)
        except Exception:
            pass
        session.close()


def enrich_pending_detailed(limit: Optional[int] = None, dry_run: bool = False) -> dict:
    session = SessionLocal()
    report = {'total': 0, 'pending': 0, 'enriched': [], 'failed': [], 'items': []}
    try:
        report['total'] = session.query(MediaItem).count()
        subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
        q = session.query(MediaItem).filter(~MediaItem.id.in_(subq)).filter(
            (MediaItem.status == None)
            | (
                (MediaItem.status != 'ERROR')
                & (MediaItem.status != 'MANUAL')
                & (MediaItem.status != 'OMITTED')
            )
        )
        if limit:
            q = q.limit(limit)
        candidates = q.all()
        report['pending'] = len(candidates)
        cfg_movies = get('metadata_movies_provider') or 'tmdb'
        cfg_anime = get('metadata_anime_provider') or 'kitsu'

        for mi in candidates:
            mi = _cast(_Any, mi)
            try:
                media_type = mi.media_type or _decide_media_type(mi, session) or 'movie'
                title = mi.title or ''
                normalized = {}
                candidate_titles = _candidate_titles_for_item(mi)
                root_type = _root_type_for_media_item(session, mi)
                providers_order = _provider_order_for_item(
                    media_type,
                    getattr(mi, 'mal_id', None),
                    cfg_movies,
                    cfg_anime,
                    getattr(mi, 'media_root', None),
                    root_type,
                )
                is_anime_context = False
                try:
                    is_anime_context = str(root_type or "").strip().lower() == "anime"
                except Exception:
                    is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = str(getattr(mi, "library_type", "") or "").strip().lower() == "anime"
                    except Exception:
                        is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = bool(getattr(mi, "mal_id", None))
                    except Exception:
                        is_anime_context = False
                if not is_anime_context:
                    try:
                        is_anime_context = str(media_type or "").strip().lower() == "anime"
                    except Exception:
                        is_anime_context = False
                try:
                    if 'tmdb' in providers_order and not _tmdb_configured():
                        providers_order = [p for p in providers_order if p != 'tmdb']
                except Exception:
                    pass
                if not providers_order:
                    providers_order = ['jikan']
                if providers_order == ['tmdb'] and not _tmdb_configured():
                    report['failed'].append((mi.id, 'tmdb_not_configured'))
                    continue

                found = False
                last_exc = None
                provider_used = None
                provider_result_raw = None
                ambiguous = False
                start = time.perf_counter()
                for p in providers_order:
                    try:
                        res = None
                        if p == 'jikan' and jikan is not None:
                            if not is_anime_context:
                                continue
                            if getattr(mi, 'mal_id', None):
                                try:
                                    logger.info('enrich_pending_detailed: calling jikan.fetch_by_id mal_id=%s media_item=%s', getattr(mi, 'mal_id', None), getattr(mi, 'id', None))
                                    res = jikan.fetch_by_id(str(mi.mal_id))
                                    logger.info('enrich_pending_detailed: jikan.fetch_by_id result for media_item=%s -> %s', getattr(mi, 'id', None), 'ok' if res else 'none')
                                except Exception:
                                    res = None
                            if not res:
                                # provider_jikan already expands name variants; limit attempts.
                                # Jikan can be sensitive to season-number-first queries ("Title 2", "Title 1"),
                                # so try a small window of candidates to avoid false NO_MATCH.
                                for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                                    try:
                                        logger.info('enrich_pending_detailed: jikan.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                        r = jikan.search(cand) if hasattr(jikan, 'search') else None
                                        logger.info('enrich_pending_detailed: jikan.search returned %s candidates for media_item=%s', (len(r) if isinstance(r, list) else (1 if r else 0)), getattr(mi, 'id', None))
                                    except Exception:
                                        r = None
                                        break
                                    if isinstance(r, list) and r:
                                        res = r
                                        break
                                    if r and not isinstance(r, list):
                                        res = r
                                        break
                        elif p == "kitsu" and kitsu is not None:
                            if not is_anime_context:
                                continue
                            try:
                                min_conf = float(os.environ.get("ARCANEA_KITSU_MIN_CONF", "0.92") or 0.92)
                            except Exception:
                                min_conf = 0.92
                            for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                                try:
                                    if hasattr(kitsu, "best_match"):
                                        r = kitsu.best_match(cand, min_conf=min_conf)  # type: ignore[attr-defined]
                                    else:
                                        rows = kitsu.search(cand) if hasattr(kitsu, "search") else None
                                        r = rows[0] if isinstance(rows, list) and rows else None
                                except Exception:
                                    r = None
                                if r and _kitsu_match_allowed_for_item(r, mi):
                                    res = r
                                    break
                        elif p == 'tmdb' and tmdb is not None:
                            for cand in (candidate_titles[:2] if isinstance(candidate_titles, list) else []):
                                try:
                                    logger.info('enrich_pending_detailed: tmdb.search query="%s" media_item=%s', cand, getattr(mi, 'id', None))
                                    r = _tmdb_search_with_optional_season(cand, media_type)
                                    logger.info('enrich_pending_detailed: tmdb.search returned %s candidates for media_item=%s', (len(r) if isinstance(r, list) else (1 if r else 0)), getattr(mi, 'id', None))
                                except Exception:
                                    r = None
                                if isinstance(r, list) and r:
                                    res = r
                                    break
                                if r and not isinstance(r, list):
                                    res = r
                                    break

                        if isinstance(res, list):
                            provider_result_raw = res
                            if len(res) == 0:
                                continue
                            if len(res) > 1:
                                ambiguous = True
                            res_for_norm = res[0]
                        else:
                            provider_result_raw = res
                            res_for_norm = res

                        if not res_for_norm:
                            continue

                        normalized = _normalize_result(p, res_for_norm)
                        if normalized:
                            found = True
                            provider_used = p
                            break
                    except Exception as exc:
                        last_exc = exc
                        logger.debug('Provider %s failed for title=%s', p, title, exc_info=True)
                elapsed_ms = int((time.perf_counter() - start) * 1000)

                item_result = {
                    'id': mi.id,
                    'title': title,
                    'provider': provider_used or None,
                    'result': None,
                    'duration_ms': elapsed_ms,
                    'info': None,
                }

                if not found:
                    item_result['result'] = 'no-match'
                    if last_exc:
                        item_result['info'] = str(last_exc)
                    report['failed'].append((mi.id, 'no_match'))
                    report['items'].append(item_result)
                    continue

                if ambiguous:
                    item_result['result'] = 'ambiguous'
                    try:
                        item_result['info'] = f"{len(provider_result_raw)} candidates" # pyright: ignore[reportArgumentType]
                    except Exception:
                        item_result['info'] = 'multiple candidates'
                else:
                    item_result['result'] = 'success'

                if dry_run:
                    report['enriched'].append(mi.id)
                    report['items'].append(item_result)
                    continue

                try:
                    try:
                        _attach_tmdb_i18n(normalized, media_type)
                        _attach_episodes(normalized.get('provider') or '', normalized, media_type, mi)
                        normalized = _supplement_jikan_with_kitsu(mi, normalized, media_type)
                        if str(normalized.get("provider") or "").strip().lower() == "jikan" and str(getattr(mi, "mal_id", "") or "").strip() == str(normalized.get("provider_id") or normalized.get("mal_id") or "").strip() and not normalized.get("_episodes_from") and isinstance(normalized.get("episodes"), list) and normalized.get("episodes"):
                            normalized["_episodes_from"] = "jikan_id"
                    except Exception:
                        pass
                    _persist_metadata(session, mi, normalized)
                    report['enriched'].append(mi.id)
                    item_result['result'] = 'success'
                except Exception as exc:
                    report['failed'].append((mi.id, str(exc)))
                    item_result['result'] = 'error'
                    item_result['info'] = str(exc)

                report['items'].append(item_result)
            except Exception as exc:
                logger.exception('Failed enriching media_item %s', mi.id)
                report['failed'].append((mi.id, str(exc)))
        return report
    finally:
        session.close()


def backfill_tmdb_episode_titles(
    limit_seasons: int | None = None,
    manage_state: bool = True,
    *,
    tmdb_id: str | None = None,
    media_item_id: int | None = None,
    series_id: int | None = None,
) -> dict[str, int | str | bool]:
    if tmdb is None or not hasattr(tmdb, "tmdb_fetch_season"):
        return {"ok": False, "detail": "tmdb_not_available", "seasons_updated": 0, "episodes_updated": 0}
    try:
        if not _tmdb_configured():
            return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0, "episodes_updated": 0}
    except Exception:
        return {"ok": False, "detail": "tmdb_not_configured", "seasons_updated": 0, "episodes_updated": 0}

    try:
        lang_tmdb = normalize_metadata_language(str(get("metadata_language") or "").strip() or "en-US")
    except Exception:
        lang_tmdb = "en-US"

    try:
        chain_keys = preferred_lang_chain_from_config(get)
    except Exception:
        chain_keys = []
    if not chain_keys:
        chain_keys = ["en"]

    tmdb_lang_chain: list[str] = []
    for k in chain_keys:
        kk = str(k or "").strip()
        if not kk:
            continue
        low = kk.lower()
        if low == "en":
            tmdb_lang_chain.append("en-US")
        elif low == "es":
            tmdb_lang_chain.append("es-ES")
        else:
            try:
                tmdb_lang_chain.append(normalize_metadata_language(kk))
            except Exception:
                tmdb_lang_chain.append(kk)
    if lang_tmdb and lang_tmdb not in tmdb_lang_chain:
        tmdb_lang_chain.insert(0, lang_tmdb)
    if "en-US" not in tmdb_lang_chain:
        tmdb_lang_chain.append("en-US")
    seen = set()
    tmdb_lang_chain = [x for x in tmdb_lang_chain if not (x.lower() in seen or seen.add(x.lower()))]

    try:
        localize = bool(get("tmdb_localization")) and bool(tmdb_lang_chain) and not tmdb_lang_chain[0].lower().startswith("en")
    except Exception:
        localize = False

    def is_generic_episode_title(v: str | None) -> bool:
        if v is None:
            return True
        s = str(v).strip()
        if not s:
            return True
        if re.fullmatch(r"\d{1,4}", s):
            return True
        if s.lower().startswith("episode ") or s.lower().startswith("episodio "):
            return True
        return False

    def _norm_text(v: str | None) -> str:
        if not isinstance(v, str):
            return ""
        s = v.strip().lower()
        if not s:
            return ""
        return re.sub(r"\s+", " ", s)

    def _is_english_fallback(loc_text: str | None, en_text: str | None) -> bool:
        a = _norm_text(loc_text)
        b = _norm_text(en_text)
        if not a or not b:
            return False
        return a == b

    def _sanitize_title(v: str | None) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        if not s:
            return None
        # Drop common tag noise in episode titles.
        s = re.sub(
            r"[\(\[]\s*(uncut|dub|dual\s*audio|multi|batch|1080p|720p|480p|bdrip|bluray|webrip|web[- ]?dl|x264|h264|hevc)[^\)\]]*[\)\]]",
            "",
            s,
            flags=re.IGNORECASE,
        )
        # Remove generic episode prefix/suffix.
        s = re.sub(r"(?i)\b(?:episode|episodio)\s*0*\d{1,4}\b", "", s)
        s = re.sub(r"\s{2,}", " ", s).strip(" -_")
        return s or None

    def _sanitize_overview(v: str | None) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        if not s:
            return None
        try:
            from ..utils.utils import limpiar_traduccion  # local import
            s = limpiar_traduccion(s, silent=True)
        except Exception:
            pass
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s or None

    def _episode_map(season_data: dict | None) -> dict[int, dict]:
        out: dict[int, dict] = {}
        if not isinstance(season_data, dict):
            return out
        eps = season_data.get("episodes") or []
        if not isinstance(eps, list):
            return out
        for e in eps:
            if not isinstance(e, dict):
                continue
            n = e.get("episode_number")
            if n is not None:
                try:
                    n = int(n)
                except Exception:
                    continue
            else:
                continue
            out[n] = e
        return out

    seasons_updated = 0
    episodes_updated = 0

    session = SessionLocal()
    try:
        def _get_jikan_meta(item_id: int | None) -> dict | None:
            if item_id is None:
                return None
            try:
                iid = int(item_id)
            except Exception:
                return None
            try:
                md = (
                    session.query(MediaMetadata)
                    .filter(MediaMetadata.media_item_id == iid)
                    .filter(MediaMetadata.provider == "jikan")
                    .order_by(MediaMetadata.id.desc())
                    .first()
                )
            except Exception:
                md = None
            if not md:
                return None
            try:
                raw = getattr(md, "data", None)
                if isinstance(raw, str) and raw.strip():
                    return json.loads(raw)
                if isinstance(raw, dict):
                    return raw
            except Exception:
                return None
            return None

        if manage_state:
            try:
                set_state(
                    {
                        "running": True,
                        "current_id": None,
                        "current_title": "TMDB: episodios",
                        "current_step": "backfill_episodes",
                        "backfill_lang": tmdb_lang_chain[0] if tmdb_lang_chain else "en-US",
                        "backfill_localized": bool(localize),
                        "backfill_total": 0,
                        "backfill_done": 0,
                        "backfill_seasons_updated": 0,
                        "backfill_episodes_updated": 0,
                    }
                )
            except Exception:
                pass

        q = (
            session.query(SeasonItem, Season, MediaItem, Series)
            .join(Season, Season.id == SeasonItem.season_id)
            .join(MediaItem, MediaItem.id == SeasonItem.media_item_id)
            .join(Series, Series.id == Season.series_id, isouter=True)
            .filter(or_(MediaItem.tmdb_id != None, Series.tmdb_id != None))  # noqa: E711
        )
        if media_item_id is not None:
            try:
                q = q.filter(MediaItem.id == int(media_item_id))
            except Exception:
                pass
        if series_id is not None:
            try:
                q = q.filter(Series.id == int(series_id))
            except Exception:
                pass
        if tmdb_id:
            try:
                tid = str(tmdb_id).strip()
                if tid:
                    q = q.filter(or_(MediaItem.tmdb_id == tid, Series.tmdb_id == tid))
            except Exception:
                pass
        if limit_seasons and int(limit_seasons) > 0:
            q = q.limit(int(limit_seasons))
        rows = q.all()

        total = len(rows)
        try:
            set_state({"backfill_total": int(total), "backfill_done": 0})
        except Exception:
            pass

        lang_chain = list(tmdb_lang_chain)

        done = 0
        for _si, season, mi, series in rows:
            done += 1
            try:
                season_number = getattr(season, "season_number", None)
                if not isinstance(season_number, int) or season_number < 0:
                    continue

                tmdb_id_v = getattr(mi, "tmdb_id", None) or (getattr(series, "tmdb_id", None) if series else None)
                if not tmdb_id_v:
                    continue
                tmdb_id_s = str(tmdb_id_v).strip()
                if not tmdb_id_s:
                    continue

                tmdb_season_number = None
                try:
                    tsn = getattr(season, "tmdb_season_number", None)
                    tmdb_season_number = int(tsn) if tsn is not None else None
                except Exception:
                    tmdb_season_number = None
                if tmdb_season_number is None:
                    tmdb_season_number = int(season_number)

                offset = 0
                try:
                    tstart = getattr(season, "tmdb_episode_start", None)
                    if tstart is not None:
                        offset = max(0, int(tstart) - 1)
                except Exception:
                    offset = 0
                # When a local season maps to a different TMDB season (anime arcs / split seasons),
                # overwrite existing episode fields even if offset==0 to avoid stale titles.
                try:
                    force_overwrite = bool((isinstance(offset, int) and offset > 0) or (int(tmdb_season_number) != int(season_number)))
                except Exception:
                    force_overwrite = bool(isinstance(offset, int) and offset > 0)

                eps = session.query(Episode).filter(Episode.season_id == season.id).all()
                if not eps:
                    continue

                need_loc = False
                if localize:
                    try:
                        need_loc = any(not (getattr(ep, "title_localized", None) or "").strip() for ep in eps)
                    except Exception:
                        need_loc = True

                need_en = False
                try:
                    need_en = any(is_generic_episode_title(getattr(ep, "title_en", None)) for ep in eps)
                except Exception:
                    need_en = False

                need_syn = False
                try:
                    need_syn = any(not (getattr(ep, "synopsis_localized", None) or "").strip() for ep in eps)
                except Exception:
                    need_syn = False

                if not need_loc and not need_en and not need_syn and not force_overwrite:
                    continue

                if manage_state:
                    try:
                        set_state(
                            {
                                "running": True,
                                "current_id": getattr(mi, "id", None),
                                "current_title": (getattr(mi, "title_localized", None) or getattr(mi, "title_en", None) or getattr(mi, "title", None) or "TMDB"),
                                "current_step": f"backfill_episodes {done}/{total}",
                                "backfill_done": int(done),
                                "backfill_seasons_updated": int(seasons_updated),
                                "backfill_episodes_updated": int(episodes_updated),
                            }
                        )
                    except Exception:
                        pass

                season_data_by_lang: dict[str, dict | None] = {}
                if need_loc or need_en or need_syn or force_overwrite:
                    for lkey in lang_chain:
                        try:
                            season_data_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                tmdb_id_s, season_number=tmdb_season_number, language=lkey
                            )
                        except Exception:
                            season_data_by_lang[lkey] = None

                # Season synopsis + poster (best-effort).
                try:
                    def _season_synopsis(d: dict | None) -> str | None:
                        if not isinstance(d, dict):
                            return None
                        v = d.get("synopsis") or d.get("overview") or None
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                        return None

                    def _season_poster(d: dict | None) -> str | None:
                        if not isinstance(d, dict):
                            return None
                        v = d.get("poster_path")
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                        imgs = d.get("images") if isinstance(d, dict) else None
                        try:
                            return _pick_best_image_url(imgs, kind="poster") if imgs else None
                        except Exception:
                            return None

                    def _season_backdrop(d: dict | None) -> str | None:
                        if not isinstance(d, dict):
                            return None
                        v = d.get("backdrop_path")
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                        imgs = d.get("images") if isinstance(d, dict) else None
                        try:
                            return _pick_best_image_url(imgs, kind="backdrop") if imgs else None
                        except Exception:
                            return None

                    try:
                        cur_syn = getattr(season, "synopsis_localized", None)
                    except Exception:
                        cur_syn = None
                    if not (isinstance(cur_syn, str) and cur_syn.strip()):
                        syn = None
                        for lkey in lang_chain:
                            syn = _season_synopsis(season_data_by_lang.get(lkey))
                            if syn:
                                break
                        if syn:
                            season_any = _cast(_Any, season)
                            season_any.synopsis_localized = syn

                    series_poster = None
                    try:
                        series_poster = getattr(series, "main_poster", None) if series else None
                    except Exception:
                        series_poster = None
                    if not (isinstance(series_poster, str) and series_poster.strip()):
                        try:
                            series_poster = getattr(mi, "poster_path", None)
                        except Exception:
                            series_poster = None

                    series_backdrop = None
                    try:
                        series_backdrop = getattr(mi, "backdrop_path", None)
                    except Exception:
                        series_backdrop = None
                    if not (isinstance(series_backdrop, str) and series_backdrop.strip()):
                        series_backdrop = None

                    try:
                        cur_poster = getattr(season, "poster_path", None)
                    except Exception:
                        cur_poster = None
                    if not (isinstance(cur_poster, str) and cur_poster.strip()):
                        poster = None
                        for lkey in lang_chain:
                            poster = _season_poster(season_data_by_lang.get(lkey))
                            if poster:
                                break
                        if not poster and isinstance(series_poster, str) and series_poster.strip():
                            poster = series_poster.strip()
                        if poster:
                            season_any = _cast(_Any, season)
                            season_any.poster_path = poster

                    try:
                        cur_backdrop = getattr(season, "backdrop_path", None)
                    except Exception:
                        cur_backdrop = None
                    if not (isinstance(cur_backdrop, str) and cur_backdrop.strip()):
                        backdrop = None
                        for lkey in lang_chain:
                            backdrop = _season_backdrop(season_data_by_lang.get(lkey))
                            if backdrop:
                                break
                        if not backdrop and isinstance(series_backdrop, str) and series_backdrop.strip():
                            backdrop = series_backdrop.strip()
                        if not backdrop and isinstance(series_poster, str) and series_poster.strip():
                            backdrop = series_poster.strip()
                        if backdrop:
                            season_any = _cast(_Any, season)
                            season_any.backdrop_path = backdrop

                    try:
                        session.add(season)
                    except Exception:
                        pass
                except Exception:
                    pass

                episode_maps = {lkey: _episode_map(season_data_by_lang.get(lkey)) for lkey in lang_chain}
                has_current = any(episode_maps.values())
                if not has_current and offset <= 0 and series is not None:
                    try:
                        sid_raw = getattr(series, "id", None)
                        if sid_raw is not None:
                            reconcile_tmdb_season_mapping(tmdb_id=tmdb_id_s, series_id=int(sid_raw))
                        try:
                            session.refresh(season)
                        except Exception:
                            pass
                        try:
                            tsn = getattr(season, "tmdb_season_number", None)
                            tmdb_season_number = int(tsn) if tsn is not None else tmdb_season_number
                        except Exception:
                            pass
                        try:
                            tstart = getattr(season, "tmdb_episode_start", None)
                            if tstart is not None:
                                offset = max(0, int(tstart) - 1)
                        except Exception:
                            pass
                        season_data_by_lang = {}
                        for lkey in lang_chain:
                            try:
                                season_data_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                    tmdb_id_s, season_number=tmdb_season_number, language=lkey
                                )
                            except Exception:
                                season_data_by_lang[lkey] = None
                        episode_maps = {lkey: _episode_map(season_data_by_lang.get(lkey)) for lkey in lang_chain}
                        has_current = any(episode_maps.values())
                    except Exception:
                        pass

                if not has_current and offset <= 0 and isinstance(season_number, int) and season_number >= 2:
                    season1_by_lang: dict[str, dict | None] = {}
                    for lkey in lang_chain:
                        try:
                            season1_by_lang[lkey] = tmdb.tmdb_fetch_season(  # type: ignore[arg-type]
                                tmdb_id_s, season_number=1, language=lkey
                            )
                        except Exception:
                            season1_by_lang[lkey] = None
                    base_season = season1_by_lang.get("en-US") or next((v for v in season1_by_lang.values() if isinstance(v, dict)), None)
                    off = None
                    if isinstance(base_season, dict):
                        eps0 = base_season.get("episodes") or []
                        if isinstance(eps0, list) and eps0:
                            # Prefer explicit mid-season markers, otherwise infer by air-date gaps.
                            for e in eps0:
                                if isinstance(e, dict) and e.get("episode_type") == "mid_season":
                                    en = e.get("episode_number")
                                    if en is not None:
                                        try:
                                            off = int(en)
                                        except Exception:
                                            off = None
                                    break
                            if not off:
                                prev_date = None
                                prev_num = None
                                for e in eps0:
                                    if not isinstance(e, dict):
                                        continue
                                    ad = e.get("air_date")
                                    n = e.get("episode_number")
                                    try:
                                        n = int(n) # pyright: ignore[reportArgumentType]
                                    except Exception:
                                        n = None
                                    if not isinstance(ad, str) or not ad.strip():
                                        continue
                                    try:
                                        dt = datetime.fromisoformat(ad.strip())
                                    except Exception:
                                        try:
                                            dt = datetime.strptime(ad.strip(), "%Y-%m-%d")
                                        except Exception:
                                            dt = None
                                    if dt is None:
                                        continue
                                    if prev_date is not None and prev_num is not None:
                                        try:
                                            gap = (dt - prev_date).days
                                        except Exception:
                                            gap = None
                                        if gap is not None and gap >= 28:
                                            off = int(prev_num)
                                            break
                                    prev_date = dt
                                    prev_num = n
                    if off:
                        offset = int(off)
                        season_data_by_lang = season1_by_lang
                        episode_maps = {lkey: _episode_map(season_data_by_lang.get(lkey)) for lkey in lang_chain}
                        has_current = any(episode_maps.values())
                        try:
                            season_any = _cast(_Any, season)
                            season_any.tmdb_season_number = 1
                            season_any.tmdb_episode_start = int(offset) + 1
                            session.add(season)
                        except Exception:
                            pass

                if not has_current:
                    continue

                by_num = {}
                for ep in eps:
                    ep_num = getattr(ep, "episode_number", None)
                    if ep_num is not None:
                        try:
                            n = int(ep_num)
                        except Exception:
                            continue
                    else:
                        continue
                    by_num[n] = ep

                changed_this_season = False
                for n, row in by_num.items():
                    target_n = int(n) + int(offset) if offset > 0 else int(n)
                    en_ep = episode_maps.get("en-US", {}).get(target_n)
                    en_title = None
                    en_ov = None
                    if isinstance(en_ep, dict):
                        en_title = _sanitize_title(en_ep.get("title") or en_ep.get("name") or "")
                        en_ov = _sanitize_overview(en_ep.get("overview") or "")
                    if (need_en or force_overwrite) and (force_overwrite or is_generic_episode_title(getattr(row, "title_en", None))):
                        e = episode_maps.get("en-US", {}).get(target_n)
                        if isinstance(e, dict):
                            t = _sanitize_title(e.get("title") or e.get("name") or "")
                            if isinstance(t, str) and t.strip():
                                row.title_en = t.strip()
                                if force_overwrite or is_generic_episode_title(getattr(row, "title", None)):
                                    row.title = t.strip()
                                changed_this_season = True
                                episodes_updated += 1
                    if (need_loc or force_overwrite) and (force_overwrite or not (getattr(row, "title_localized", None) or "").strip()):
                        fallback_generic = None
                        for lkey in lang_chain:
                            e = episode_maps.get(lkey, {}).get(target_n)
                            if not isinstance(e, dict):
                                continue
                            t = _sanitize_title(e.get("title") or e.get("name") or "")
                            if isinstance(t, str) and t.strip():
                                if not lkey.lower().startswith("en") and _is_english_fallback(t, en_title):
                                    continue
                                if is_generic_episode_title(t):
                                    if fallback_generic is None:
                                        fallback_generic = t.strip()
                                    continue
                                row.title_localized = t.strip()
                                if force_overwrite:
                                    row.title = t.strip()
                                changed_this_season = True
                                episodes_updated += 1
                                break
                        if not (getattr(row, "title_localized", None) or "").strip() and fallback_generic:
                            row.title_localized = fallback_generic
                            if force_overwrite:
                                row.title = fallback_generic
                            changed_this_season = True
                            episodes_updated += 1
                    if (need_syn or force_overwrite) and (force_overwrite or not (getattr(row, "synopsis_localized", None) or "").strip()):
                        filled = False
                        for lkey in lang_chain:
                            e = episode_maps.get(lkey, {}).get(target_n)
                            if not isinstance(e, dict):
                                continue
                            ov = _sanitize_overview(e.get("overview") or "")
                            if isinstance(ov, str) and ov.strip():
                                if not lkey.lower().startswith("en") and _is_english_fallback(ov, en_ov):
                                    continue
                                row.synopsis_localized = ov.strip()
                                changed_this_season = True
                                episodes_updated += 1
                                filled = True
                                break
                        if not filled:
                            try:
                                details_loc = tmdb.tmdb_fetch_by_id(  # type: ignore[arg-type]
                                    tmdb_id_s, media_type="tv", language=lang_chain[0]
                                )
                            except Exception:
                                details_loc = None
                            if isinstance(details_loc, dict):
                                gs = _sanitize_overview(details_loc.get("synopsis") or details_loc.get("overview") or "")
                                if isinstance(gs, str) and gs.strip():
                                    row.synopsis_localized = gs.strip()
                                    changed_this_season = True
                                    episodes_updated += 1
                        if not (getattr(row, "synopsis_localized", None) or "").strip():
                            try:
                                jmeta = _get_jikan_meta(getattr(mi, "id", None))
                            except Exception:
                                jmeta = None
                            if isinstance(jmeta, dict):
                                js = _sanitize_overview(jmeta.get("synopsis") or "")
                                if isinstance(js, str) and js.strip():
                                    row.synopsis_localized = js.strip()
                                    changed_this_season = True
                                    episodes_updated += 1
                    e0 = episode_maps.get("en-US", {}).get(target_n) or episode_maps.get(lang_chain[0], {}).get(target_n)
                    if isinstance(e0, dict):
                        try:
                            ad = e0.get("air_date")
                            if isinstance(ad, str) and ad.strip() and (force_overwrite or not (getattr(row, "air_date", None) or "").strip()):
                                row.air_date = ad.strip()
                                changed_this_season = True
                        except Exception:
                            pass
                        try:
                            et = e0.get("episode_type")
                            if isinstance(et, str) and et.strip() and (force_overwrite or not (getattr(row, "episode_type", None) or "").strip()):
                                row.episode_type = et.strip()
                                changed_this_season = True
                        except Exception:
                            pass
                    # Episode still_path (download/cache locally)
                    try:
                        cur_still = getattr(row, "still_path", None)
                    except Exception:
                        cur_still = None
                    if not (isinstance(cur_still, str) and cur_still.strip()):
                        still_url = None
                        for lkey in lang_chain:
                            e = episode_maps.get(lkey, {}).get(target_n)
                            if not isinstance(e, dict):
                                continue
                            su = e.get("still_path")
                            if isinstance(su, str) and su.strip():
                                still_url = su.strip()
                                break
                        if still_url:
                            _download_episode_still(session, row, still_url)
                    session.add(row)

                if changed_this_season:
                    try:
                        session.commit()
                    except Exception:
                        session.rollback()
                        continue
                    seasons_updated += 1
                    try:
                        episodes_updated += len(by_num)
                    except Exception:
                        pass
            except Exception:
                continue
        return {"ok": True, "seasons_updated": int(seasons_updated), "episodes_updated": int(episodes_updated), "detail": "ok"}
    finally:
        try:
            if manage_state:
                set_state(
                    {
                        "running": False,
                        "current_id": None,
                        "current_title": None,
                        "current_step": "idle",
                        "backfill_total": 0,
                        "backfill_done": 0,
                        "backfill_seasons_updated": int(seasons_updated),
                        "backfill_episodes_updated": int(episodes_updated),
                    }
                )
        except Exception:
            pass
        session.close()





