import os
import logging
from pathlib import Path
import json
import re
import sqlite3


_logger = logging.getLogger(__name__)
_temp_overrides: dict[str, object] = {}


def _debug_suppressed(exc: Exception) -> None:
    try:
        _logger.debug("config: suppressed exception: %s", exc)
    except Exception:
        return


def _load_dotenv_file(path: Path) -> None:
    try:
        if not path.exists() or not path.is_file():
            return
    except Exception:
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        return

def normalize_metadata_language(lang: str | None) -> str:
    """Normalize provider metadata language tags for external APIs (TMDB/Jikan).

    Notes:
    - UI locales like `es-419` are valid BCP-47 but TMDB often rejects them; prefer a concrete region.
    - We only store a single active localized track (`title_localized`, etc.), so one resolved tag is enough.
    """
    s = str(lang or "").strip()
    if not s:
        return "en-US"
    low = s.lower()
    if low == "es-419":
        return "es-MX"
    if low == "es":
        return "es-ES"
    # normalize underscores to dashes (e.g. es_MX -> es-MX)
    s = s.replace("_", "-")
    # Basic casing normalization: xx-yy -> xx-YY
    parts = s.split("-", 1)
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        return f"{parts[0].lower()}-{parts[1].upper()}"
    return s

try:
    _default_base = Path(__file__).resolve().parents[2]
    _load_dotenv_file(_default_base / ".env")
    _load_dotenv_file(_default_base / ".env.local")
except Exception as e:
    _debug_suppressed(e)
# Environment override for scanner keywords (JSON)
try:
    raw_kw = os.environ.get("ARCANEA_SCANNER_KEYWORDS", "") or ""
except Exception:
    raw_kw = ""
if raw_kw:
    try:
        kw_env = json.loads(raw_kw)
    except Exception:
        kw_env = None
    if isinstance(kw_env, dict):
        try:
            def _norm_kw_list_env(v):
                if v is None:
                    return []
                if isinstance(v, str):
                    v = [x for x in v.split(",")]
                if not isinstance(v, (list, tuple)):
                    return []
                out = []
                for x in v:
                    try:
                        s = str(x or "").strip().lower()
                    except Exception:
                        s = ""
                    if s and s not in out:
                        out.append(s)
                return out

            defaults = dict(config.get('scanner_keywords') or {})
            merged = dict(defaults)
            for k, v in kw_env.items():
                if k in defaults:
                    lst = _norm_kw_list_env(v)
                    if lst:
                        merged[k] = lst
            config['scanner_keywords'] = merged
        except Exception as e:
            _debug_suppressed(e)
try:
    _base_override = os.environ.get("ARCANEA_BASE_DIR", "") or ""
except Exception:
    _base_override = ""

BASE_DIR = Path(_base_override).resolve() if _base_override.strip() else Path(__file__).resolve().parents[2]

try:
    _data_override = os.environ.get("ARCANEA_DATA_DIR", "") or ""
except Exception:
    _data_override = ""

DATA_DIR = Path(_data_override).resolve() if _data_override.strip() else (BASE_DIR / 'data')
DATA_DIR.mkdir(parents=True, exist_ok=True)

config = {
    # include environment override first; then default media folder; then local 'Anime' attachment if present
    'media_roots': [],
    'db_url': os.environ.get('ARCANEA_DB_URL', f"sqlite:///{DATA_DIR / 'arcanea.db'}"),
    'watch_poll_interval': int(os.environ.get('ARCANEA_WATCH_POLL', '5')),
    'media_extensions': ['.mp4', '.mkv', '.avi', '.webm', '.mov'],
    'cache_dir': os.environ.get('ARCANEA_CACHE_DIR', str(DATA_DIR / 'cache')),
    # Roots where every file should be treated as a standalone movie (no grouping by folder).
    'loose_movie_roots': [],
    # Scanner keyword sets (global, configurable)
    'scanner_keywords': {
        # "specials" folders are often named in multiple languages.
        'specials': ["special", "specials", "especial", "especiales", "ova", "onas", "ona", "movie", "extra", "extras", "bonus"],
        'anime': ["anime", "animes"],
        'series': ["series", "serie", "tv", "show", "shows"],
        'movies': ["movie", "movies", "pelicula", "peliculas", "film", "films"],
        'generic': ["sample", "test", "trailer"],
    },
}

# metadata defaults (may be overridden by app_config.json)
config['metadata_movies_provider'] = 'tmdb'
# Prefer Kitsu as primary anime metadata provider; Jikan is used as secondary fallback.
config['metadata_anime_provider'] = 'kitsu'
config['metadata_language'] = 'en-US'
config['fetch_cast'] = False
config['download_images'] = True
config['target_lang'] = 'en'
config['tmdb_localization'] = False
config['localization_in_enrichment'] = False
# Enrichment tuning defaults
config['jikan_fetch_episodes'] = False
config['enrichment_rate_per_min'] = 60
config['enrichment_batch_size'] = 20
config['enrichment_max_retries'] = 1

# TMDB provider configuration: prefer explicit environment variables
# Read `TMDB_API_KEY` from environment (was incorrectly using the key string as env name)
config['tmdb_api_key'] = os.environ.get('TMDB_API_KEY', '')
config['tmdb_access_token'] = os.environ.get('TMDB_ACCESS_TOKEN', '')
# If TMDB_USE_V4 is set (truthy), the provider will prefer v4 Bearer access token
config['tmdb_use_v4'] = bool(os.environ.get('TMDB_USE_V4', '').lower() in ('1', 'true', 'yes'))

# Compose media_roots with sensible defaults
_roots = []
env_root = os.environ.get('ARCANEA_MEDIA_ROOT')
if env_root:
    _roots.append(env_root)
try:
    env_loose = os.environ.get('ARCANEA_LOOSE_MOVIE_ROOTS', '') or ''
except Exception:
    env_loose = ''
if env_loose:
    for chunk in env_loose.split(';'):
        if not chunk:
            continue
        for part in str(chunk).split(','):
            p = part.strip()
            if p:
                config['loose_movie_roots'].append(p)
# If an app-level config exists in DATA_DIR/app_config.json, prefer its media_roots.
try:
    import json
    app_cfg_path = DATA_DIR / 'app_config.json'
    if app_cfg_path.exists():
        with open(app_cfg_path, 'r', encoding='utf-8') as fh:
            _app_cfg = json.load(fh)
            app_roots = _app_cfg.get('media_roots') or []
            for ar in app_roots:
                if ar:
                    _roots.append(str(Path(ar).resolve()))
            try:
                loose_roots = _app_cfg.get('loose_movie_roots') or _app_cfg.get('movie_loose_roots') or []
                for lr in loose_roots:
                    try:
                        if isinstance(lr, dict):
                            p = lr.get('path') or lr.get('root') or lr.get('dir')
                        else:
                            p = lr
                        if p:
                            config['loose_movie_roots'].append(str(Path(p).resolve()))
                    except Exception:
                        continue
            except Exception as e:
                _debug_suppressed(e)
            # Scanner keyword overrides (from app_config)
            try:
                def _norm_kw_list(v):
                    if v is None:
                        return []
                    if isinstance(v, str):
                        v = [x for x in v.split(",")]
                    if not isinstance(v, (list, tuple)):
                        return []
                    out = []
                    for x in v:
                        try:
                            s = str(x or "").strip().lower()
                        except Exception:
                            s = ""
                        if s and s not in out:
                            out.append(s)
                    return out

                defaults = dict(config.get('scanner_keywords') or {})
                sk = _app_cfg.get('scanner_keywords')
                if isinstance(sk, dict):
                    merged = dict(defaults)
                    for k, v in sk.items():
                        if k in defaults:
                            lst = _norm_kw_list(v)
                            if lst:
                                merged[k] = lst
                    config['scanner_keywords'] = merged
            except Exception as e:
                _debug_suppressed(e)
            # Also load optional metadata credentials (e.g. TMDB api key) from app_config
            try:
                app_metadata = _app_cfg.get('metadata') or {}
                # Back-compat: older app_config.json stores tmdb keys at the top-level.
                try:
                    if not config.get("tmdb_api_key") and _app_cfg.get("tmdb_api_key"):
                        config["tmdb_api_key"] = _app_cfg.get("tmdb_api_key")
                    if not config.get("tmdb_access_token") and _app_cfg.get("tmdb_access_token"):
                        config["tmdb_access_token"] = _app_cfg.get("tmdb_access_token")
                    if not config.get("tmdb_use_v4") and _app_cfg.get("tmdb_use_v4") is not None:
                        config["tmdb_use_v4"] = bool(_app_cfg.get("tmdb_use_v4"))
                except Exception as e:
                    _debug_suppressed(e)
                # Only set keys if not provided via environment (env already used above)
                if app_metadata:
                    if not config.get('tmdb_api_key') and app_metadata.get('tmdb_api_key'):
                        config['tmdb_api_key'] = app_metadata.get('tmdb_api_key')
                    if not config.get('tmdb_access_token') and app_metadata.get('tmdb_access_token'):
                        config['tmdb_access_token'] = app_metadata.get('tmdb_access_token')
                    if not config.get('tmdb_use_v4') and app_metadata.get('tmdb_use_v4') is not None:
                        # coerce to bool
                        config['tmdb_use_v4'] = bool(app_metadata.get('tmdb_use_v4'))
                    # metadata provider preferences
                    movies_provider = app_metadata.get('moviesProvider')
                    anime_provider = app_metadata.get('animeProvider')
                    if movies_provider:
                        movies_provider = str(movies_provider).strip().lower()
                        if movies_provider in ('tvdb', 'omdb'):
                            logging.getLogger(__name__).warning("metadata.moviesProvider '%s' not supported; falling back to 'tmdb'", movies_provider)
                            movies_provider = 'tmdb'
                        elif movies_provider not in ('tmdb', 'jikan'):
                            logging.getLogger(__name__).warning("metadata.moviesProvider '%s' invalid; falling back to 'tmdb'", movies_provider)
                            movies_provider = 'tmdb'
                        config['metadata_movies_provider'] = movies_provider
                    if anime_provider:
                        anime_provider = str(anime_provider).strip().lower()
                        # Support "auto": prefer Jikan when healthy, otherwise fall back to TMDB with strict matching.
                        if anime_provider not in ('jikan', 'tmdb', 'auto', 'anilist', 'kitsu'):
                            logging.getLogger(__name__).warning("metadata.animeProvider '%s' invalid; falling back to 'kitsu'", anime_provider)
                            anime_provider = 'kitsu'
                        config['metadata_anime_provider'] = anime_provider

                    # additional runtime toggles from frontend config
                    if app_metadata.get('downloadImages') is not None:
                        config['download_images'] = bool(app_metadata.get('downloadImages'))
                    if app_metadata.get('fetchCast') is not None:
                        config['fetch_cast'] = bool(app_metadata.get('fetchCast'))
                    if app_metadata.get('language'):
                        config['metadata_language'] = normalize_metadata_language(app_metadata.get('language'))
            except Exception as e:
                _debug_suppressed(e)
            # Onboarding: active UI/metadata localization language (single active localized track).
            try:
                if _app_cfg.get('target_lang'):
                    config['target_lang'] = str(_app_cfg.get('target_lang')).strip().lower()
            except Exception as e:
                _debug_suppressed(e)
            try:
                if _app_cfg.get('tmdb_localization') is not None:
                    config['tmdb_localization'] = bool(_app_cfg.get('tmdb_localization'))
            except Exception as e:
                _debug_suppressed(e)
            try:
                if _app_cfg.get('localization_in_enrichment') is not None:
                    config['localization_in_enrichment'] = bool(_app_cfg.get('localization_in_enrichment'))
            except Exception as e:
                _debug_suppressed(e)
            try:
                if _app_cfg.get('anime_season_auto') is not None:
                    config['anime_season_auto'] = bool(_app_cfg.get('anime_season_auto'))
            except Exception as e:
                _debug_suppressed(e)
except Exception as e:
    _debug_suppressed(e)
# dedupe preserving order
seen = set()
final_roots = []
for r in _roots:
    if not r:
        continue
    rr = os.path.abspath(r)
    if rr in seen:
        continue
    seen.add(rr)
    final_roots.append(rr)

config['media_roots'] = final_roots
try:
    # Normalize loose_movie_roots (dedupe + abs)
    seen = set()
    out = []
    for r in (config.get('loose_movie_roots') or []):
        if not r:
            continue
        rr = os.path.abspath(str(r))
        if rr in seen:
            continue
        seen.add(rr)
        out.append(rr)
    config['loose_movie_roots'] = out
except Exception as e:
    _debug_suppressed(e)
def get(key, default=None):
    # Honor temporary overrides (e.g., manual mapping forcing Jikan episodes).
    try:
        if key in _temp_overrides:
            return _temp_overrides.get(key)
    except Exception as e:
        _debug_suppressed(e)
    # Keep onboarding settings in sync with SQLite while the server is running.
    # This prevents stale values like `tmdb_localization=False` from disabling jobs.
    try:
        if key in (
            "tmdb_localization",
            "localization_in_enrichment",
            "anime_season_auto",
            "target_lang",
            "metadata_language",
            "metadata_movies_provider",
            "metadata_anime_provider",
            "jikan_fetch_episodes",
            "enrichment_rate_per_min",
            "enrichment_batch_size",
            "enrichment_max_retries",
            "loose_movie_roots",
            "tmdb_api_key",
            "tmdb_access_token",
            "tmdb_use_v4",
            "scanner_keywords",
        ):
            fn = globals().get("_maybe_refresh_runtime_settings")
            if callable(fn):
                fn()
    except Exception as e:
        _debug_suppressed(e)
    return config.get(key, default)


def set_temp_override(key: str, value):
    """Set or clear a temporary config override for the current process."""
    try:
        k = str(key or "").strip()
    except Exception:
        k = ""
    if not k:
        return
    if value is None:
        try:
            _temp_overrides.pop(k, None)
        except Exception as e:
            _debug_suppressed(e)
        return
    _temp_overrides[k] = value


def _resolve_sqlite_db_path() -> Path | None:
    """Resolve sqlite file path from ARCANEA_DB_URL / config.db_url."""
    try:
        db_url = os.environ.get("ARCANEA_DB_URL", "") or ""
    except Exception:
        db_url = ""
    if not db_url:
        try:
            db_url = str(config.get("db_url") or "")
        except Exception:
            db_url = ""
    db_url = (db_url or "").strip().split("?", 1)[0]
    if not db_url.lower().startswith("sqlite") or ":memory:" in db_url:
        return None
    path_part = ""
    if db_url.lower().startswith("sqlite:////"):
        path_part = db_url[len("sqlite:////") :]
    elif db_url.lower().startswith("sqlite:///"):
        path_part = db_url[len("sqlite:///") :]
    elif db_url.lower().startswith("sqlite://"):
        path_part = db_url[len("sqlite://") :]
    path_part = (path_part or "").strip().strip('"').strip("'")
    if not path_part:
        return None
    if re.match(r"^/[A-Za-z]:[\\/]", path_part):
        path_part = path_part[1:]
    p = Path(path_part)
    try:
        return p if p.is_absolute() else p.resolve()
    except Exception:
        return p


def _read_setting(key: str):
    """Read a single settings.key value (JSON decoded) from sqlite (best-effort)."""
    try:
        db_path = _resolve_sqlite_db_path() or (DATA_DIR / "arcanea.db")
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            cur = conn.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (str(key),))
            row = cur.fetchone()
            if not row:
                return None
            raw = row[0]
            if raw is None:
                return None
            try:
                return json.loads(raw)
            except Exception:
                return raw
        finally:
            conn.close()
    except Exception:
        return None


# Prefer DB settings (source of truth) for onboarding toggles that affect runtime jobs.
# This fixes cases where legacy app_config.json got overwritten without these keys.
try:
    v = _read_setting("tmdb_localization")
    if v is not None:
        config["tmdb_localization"] = bool(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("localization_in_enrichment")
    if v is not None:
        config["localization_in_enrichment"] = bool(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("jikan_fetch_episodes")
    if v is not None:
        config["jikan_fetch_episodes"] = bool(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("enrichment_rate_per_min")
    if v is not None:
        config["enrichment_rate_per_min"] = int(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("enrichment_batch_size")
    if v is not None:
        config["enrichment_batch_size"] = int(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("enrichment_max_retries")
    if v is not None:
        config["enrichment_max_retries"] = int(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("target_lang")
    if isinstance(v, str) and v.strip():
        config["target_lang"] = v.strip().lower()
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("tmdb_api_key")
    if isinstance(v, str) and v.strip() and not str(config.get("tmdb_api_key") or "").strip():
        config["tmdb_api_key"] = v.strip()
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("tmdb_access_token")
    if isinstance(v, str) and v.strip() and not str(config.get("tmdb_access_token") or "").strip():
        config["tmdb_access_token"] = v.strip()
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("tmdb_use_v4")
    if v is not None:
        config["tmdb_use_v4"] = bool(v)
except Exception as e:
    _debug_suppressed(e)
try:
    v = _read_setting("loose_movie_roots")
    if isinstance(v, list):
        try:
            seen = set()
            out = []
            for r in v:
                if not r:
                    continue
                rr = os.path.abspath(str(r))
                if rr in seen:
                    continue
                seen.add(rr)
                out.append(rr)
            config["loose_movie_roots"] = out
        except Exception:
            config["loose_movie_roots"] = v
except Exception as e:
    _debug_suppressed(e)
_settings_refresh_lock = None
_settings_last_refresh_ts: int | None = None


def _maybe_refresh_runtime_settings() -> None:
    """Refresh a subset of settings from sqlite periodically (best-effort).

    The frontend persists settings into SQLite. Long-running servers must refresh critical keys
    without requiring a restart.
    """
    global _settings_refresh_lock, _settings_last_refresh_ts

    try:
        if _settings_refresh_lock is None:
            import threading

            _settings_refresh_lock = threading.Lock()
    except Exception:
        return

    import time as _time

    try:
        now = int(_time.time())
    except Exception:
        return

    # refresh at most every 2 seconds
    try:
        if _settings_last_refresh_ts is not None and (now - int(_settings_last_refresh_ts)) < 2:
            return
    except Exception as e:
        _debug_suppressed(e)
    with _settings_refresh_lock:
        try:
            if _settings_last_refresh_ts is not None and (now - int(_settings_last_refresh_ts)) < 2:
                return
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("tmdb_localization")
            if v is not None:
                config["tmdb_localization"] = bool(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("localization_in_enrichment")
            if v is not None:
                config["localization_in_enrichment"] = bool(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("jikan_fetch_episodes")
            if v is not None:
                config["jikan_fetch_episodes"] = bool(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("enrichment_rate_per_min")
            if v is not None:
                config["enrichment_rate_per_min"] = int(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("enrichment_batch_size")
            if v is not None:
                config["enrichment_batch_size"] = int(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("enrichment_max_retries")
            if v is not None:
                config["enrichment_max_retries"] = int(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("target_lang")
            if isinstance(v, str) and v.strip():
                config["target_lang"] = v.strip().lower()
        except Exception as e:
            _debug_suppressed(e)
        try:
            md = _read_setting("metadata")
            if isinstance(md, dict) and md.get("language"):
                config["metadata_language"] = normalize_metadata_language(md.get("language"))
        except Exception as e:
            _debug_suppressed(e)
        # Provider preferences can be updated live from the frontend (settings.metadata.*).
        try:
            md = _read_setting("metadata")
            if isinstance(md, dict):
                mp = md.get("moviesProvider")
                ap = md.get("animeProvider")
                if isinstance(mp, str) and mp.strip():
                    v = mp.strip().lower()
                    if v in ("tmdb", "jikan"):
                        config["metadata_movies_provider"] = v
                if isinstance(ap, str) and ap.strip():
                    v = ap.strip().lower()
                    if v in ("jikan", "tmdb", "auto", "anilist", "kitsu"):
                        config["metadata_anime_provider"] = v
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("tmdb_api_key")
            if isinstance(v, str) and v.strip():
                config["tmdb_api_key"] = v.strip()
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("tmdb_access_token")
            if isinstance(v, str) and v.strip():
                config["tmdb_access_token"] = v.strip()
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("tmdb_use_v4")
            if v is not None:
                config["tmdb_use_v4"] = bool(v)
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("loose_movie_roots")
            if isinstance(v, list):
                config["loose_movie_roots"] = v
        except Exception as e:
            _debug_suppressed(e)
        try:
            v = _read_setting("scanner_keywords")
            if isinstance(v, dict) and v:
                defaults = dict(config.get('scanner_keywords') or {})
                merged = dict(defaults)
                for k, lst in v.items():
                    if k not in defaults:
                        continue
                    if isinstance(lst, str):
                        lst = [x for x in lst.split(",")]
                    if isinstance(lst, (list, tuple)):
                        out = []
                        for x in lst:
                            try:
                                s = str(x or "").strip().lower()
                            except Exception:
                                s = ""
                            if s and s not in out:
                                out.append(s)
                        if out:
                            merged[k] = out
                config["scanner_keywords"] = merged
        except Exception as e:
            _debug_suppressed(e)
        _settings_last_refresh_ts = now

    # Normalize loose_movie_roots after refresh
    try:
        seen = set()
        out = []
        for r in (config.get("loose_movie_roots") or []):
            if not r:
                continue
            rr = os.path.abspath(str(r))
            if rr in seen:
                continue
            seen.add(rr)
            out.append(rr)
        config["loose_movie_roots"] = out
    except Exception as e:
        _debug_suppressed(e)
