import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import os
import hashlib
import re
import time
from src.tools.cache import cache_get, cache_set, cache_get_negative, cache_set_negative  # type: ignore
from src.core.config import config
from src.utils.utils import map_tmdb_genres  # type: ignore
try:
    from src.utils.rate_limiter import get_limiter
    _rate_limiter = get_limiter("tmdb")
except Exception:
    _rate_limiter = None
try:
    from src.services import telemetry  # type: ignore
except Exception:
    try:
        from ..services import telemetry  # type: ignore
    except Exception:
        telemetry = None

TMDB_API_BASE = "https://api.themoviedb.org/3"


def _debug_suppressed(exc: Exception) -> None:
    try:
        logging.debug("provider_tmdb: suppressed exception: %s", exc)
    except Exception:
        return

# Throttling controls to avoid network saturation.
_API_CONCURRENCY = max(1, int(os.environ.get("ARCANEA_API_CONCURRENCY", "2") or 2))
_API_DELAY_SEC = max(0.0, float(os.environ.get("ARCANEA_API_DELAY_MS", "100") or 100) / 1000.0)
_api_sema = None
try:
    import threading as _threading
    _api_sema = _threading.BoundedSemaphore(_API_CONCURRENCY)
except Exception:
    _api_sema = None

# Simple circuit breaker to avoid hammering TMDB when it's returning 429/5xx.
_cooldown_until_ts = 0.0
_cooldown_lock = None
try:
    import threading as _threading
    _cooldown_lock = _threading.Lock()
except Exception:
    _cooldown_lock = None


def _pick_tmdb_tv_rating(data):
    try:
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None
        for r in results:
            if isinstance(r, dict) and r.get("iso_3166_1") == "US":
                rating = r.get("rating")
                if rating:
                    return str(rating).strip()
        for r in results:
            if isinstance(r, dict):
                rating = r.get("rating")
                if rating:
                    return str(rating).strip()
    except Exception:
        return None
    return None


def _pick_tmdb_movie_rating(data):
    try:
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None
        for r in results:
            if isinstance(r, dict) and r.get("iso_3166_1") == "US":
                for rd in (r.get("release_dates") or []):
                    if isinstance(rd, dict):
                        cert = rd.get("certification")
                        if cert:
                            return str(cert).strip()
        for r in results:
            if isinstance(r, dict):
                for rd in (r.get("release_dates") or []):
                    if isinstance(rd, dict):
                        cert = rd.get("certification")
                        if cert:
                            return str(cert).strip()
    except Exception:
        return None
    return None


def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    # In some dev environments, HTTP(S)_PROXY is set to a non-working local proxy
    # (e.g. http://127.0.0.1:9). Allow opting out to avoid breaking metadata fetch.
    try:
        disable_proxy = str(os.environ.get("ARCANEA_DISABLE_PROXY", "")).strip().lower() in ("1", "true", "yes")
        if disable_proxy:
            session.trust_env = False
        else:
            hp = str(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip().lower()
            hsp = str(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip().lower()
            if hp.startswith("http://127.0.0.1:9") or hsp.startswith("http://127.0.0.1:9"):
                session.trust_env = False
    except Exception as e:
        _debug_suppressed(e)
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET', 'POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session_with_retries()

def _normalize_tmdb_language(language: str | None) -> str | None:
    """Normalize common app language tags to ones TMDB actually supports.

    TMDB expects IETF BCP 47 tags like `es-MX`, `es-ES`, `en-US`.
    Some UI languages (e.g. `es-419`) are valid BCP 47 but not accepted by TMDB.
    """
    if not language:
        return None
    try:
        lang = str(language).strip().replace("_", "-")
    except Exception:
        return None
    if not lang:
        return None
    low = lang.lower()
    # Latin America Spanish -> Mexico Spanish (closest practical default for TMDB).
    if low == "es-419":
        return "es-MX"
    # Generic Spanish without region is acceptable.
    if low == "es":
        return "es-ES"
    # Ensure casing for region subtag when present: xx-YY
    try:
        parts = lang.split("-")
        if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) in (2, 3):
            return f"{parts[0].lower()}-{parts[1].upper()}"
    except Exception as e:
        _debug_suppressed(e)
    return lang

def tmdb_check_connection(force: bool = False, api_key: str | None = None, access_token: str | None = None) -> bool:
    """Lightweight connectivity check against TMDB using configured credentials.

    Used by the API to validate that the configured API key / v4 access token works.
    """
    try:
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        use_v4 = config.get('tmdb_use_v4', False)
        access = access_token or (config.get('tmdb_access_token') if use_v4 else None)

        if not key and not access:
            return False

        headers = {}
        params = {}
        if access:
            headers['Authorization'] = f"Bearer {access}"
        else:
            params['api_key'] = key

        # `/configuration` is a small unauthenticated-like endpoint gated only by auth.
        data = _tmdb_get('configuration', params=params or None, headers=headers or None, timeout=8)
        return bool(isinstance(data, dict) and data.get('images'))
    except Exception:
        return False


def _parse_tmdb_url(url):
    """Parse TMDB URL. Returns (tmdb_id, media_type, season_number_or_None).
    Examples:
      /tv/52814 -> ("52814", "tv", None)
      /tv/52814/season/2 -> ("52814", "tv", "2")
    """
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        path = (p.path or '')
        # try to extract tv/movie id (allow slug after id) and optional season
        # matches: /tv/52814 or /tv/52814-halo or /tv/52814-halo/season/2
        m = re.search(r"/(tv|movie)/([0-9]+)(?:-[^/]+)?(?:/season/([0-9]+))?", path)
        if m:
            season = m.group(3) if m.group(3) else None
            return m.group(2), m.group(1), season
        # fallback: extract first numeric id
        m2 = re.search(r"([0-9]{3,})", path)
        if m2:
            return m2.group(1), None, None
    except Exception as e:
        _debug_suppressed(e)
    return None, None, None


def _tmdb_timeout(default: float = 10.0) -> float:
    try:
        raw = os.environ.get("ARCANEA_TMDB_TIMEOUT_SEC") or os.environ.get("ARCANEA_TMDB_TIMEOUT")
        if raw is None:
            return float(default)
        val = float(raw)
        return val if val > 0 else float(default)
    except Exception:
        return float(default)


def _tmdb_get(path, params=None, headers=None, timeout=10):
    """Internal helper: perform GET to TMDB ensuring params are copied and errors are logged."""
    global _cooldown_until_ts
    try:
        now = time.time()
        try:
            cd = None
            if _cooldown_lock:
                with _cooldown_lock:
                    cd = _cooldown_until_ts
            else:
                cd = _cooldown_until_ts
            if cd and cd > now:
                raise RuntimeError(f"TMDB cooldown active ({int(cd - now)}s)")
        except Exception as e:
            # If this is our cooldown exception, re-raise; otherwise ignore.
            if isinstance(e, RuntimeError) and "TMDB cooldown active" in str(e):
                raise

        p = dict(params or {})
        h = dict(headers or {})
        try:
            # mask api_key value in logs
            log_params = {k: (v if k != 'api_key' else '***') for k, v in p.items()}
        except Exception:
            log_params = {}
        logging.debug("provider_tmdb._tmdb_get: GET %s params=%s headers=%s", path, log_params, list(h.keys()))
        resp = None
        status_code = None
        err_msg = None
        try:
            if timeout is None:
                timeout = _tmdb_timeout()
            else:
                timeout = _tmdb_timeout(float(timeout))
        except Exception:
            timeout = _tmdb_timeout()
        if _api_sema:
            _api_sema.acquire()
        try:
            t0 = time.perf_counter()
            try:
                if _rate_limiter is not None:
                    _rate_limiter.before_request()
            except Exception as e:
                _debug_suppressed(e)
            resp = session.get(f"{TMDB_API_BASE}/{path}", params=p or None, headers=h or None, timeout=timeout)
            try:
                status_code = int(getattr(resp, "status_code", 0) or 0)
            except Exception:
                status_code = None
            logging.debug("provider_tmdb._tmdb_get: response status=%s for %s", getattr(resp, 'status_code', None), path)
            try:
                sc = int(getattr(resp, 'status_code', 0) or 0)
            except Exception:
                sc = 0
            if sc in (429, 500, 502, 503, 504):
                # Trip cooldown on rate-limit/gateway issues to prevent immediate repeated failures.
                wait = 30
                try:
                    if sc == 429:
                        ra = resp.headers.get('Retry-After')
                        if ra is not None:
                            try:
                                wait = max(wait, int(ra))
                            except Exception as e:
                                _debug_suppressed(e)
                except Exception as e:
                    _debug_suppressed(e)
                until = time.time() + float(wait)
                try:
                    if _cooldown_lock:
                        with _cooldown_lock:
                            _cooldown_until_ts = max(_cooldown_until_ts, until)
                    else:
                        _cooldown_until_ts = max(_cooldown_until_ts, until)
                except Exception as e:
                    _debug_suppressed(e)
            resp.raise_for_status()
            try:
                j = resp.json()
                if isinstance(j, dict):
                    logging.debug("provider_tmdb._tmdb_get: returned keys=%s for %s", list(j.keys()), path)
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    _debug_suppressed(e)
                return j
            except Exception:
                logging.debug("provider_tmdb._tmdb_get: response not JSON for %s", path)
                return None
        except Exception as e:
            err_msg = str(e)
            try:
                if _rate_limiter is not None:
                    _rate_limiter.record_failure(status_code=status_code, exc=e)
            except Exception as e:
                _debug_suppressed(e)
            raise
        finally:
            try:
                try:
                    if telemetry is not None:
                        dt = (time.perf_counter() - t0) * 1000.0 if 't0' in locals() else 0.0
                        telemetry.emit_api_timing("tmdb", str(path), dt, status_code=status_code, error=err_msg)
                except Exception as e:
                    _debug_suppressed(e)
                if resp is not None:
                    resp.close()
            except Exception as e:
                _debug_suppressed(e)
            if _api_sema:
                _api_sema.release()
            # When rate limiter is active, avoid double-throttling here.
            if _API_DELAY_SEC > 0 and _rate_limiter is None:
                time.sleep(_API_DELAY_SEC)
    except Exception as e:
        # Best-effort cooldown on "429/504" like failures even if we don't have a response object here.
        try:
            msg = str(e).lower()
            should_cooldown = (" 429 " in msg) or ("too many" in msg and "429" in msg) or ("504" in msg) or ("gateway" in msg)
            if should_cooldown:
                until = time.time() + 30.0
                if _cooldown_lock:
                    with _cooldown_lock:
                        _cooldown_until_ts = max(_cooldown_until_ts, until)
                else:
                    _cooldown_until_ts = max(_cooldown_until_ts, until)
        except Exception as e:
            _debug_suppressed(e)
        logging.debug("provider_tmdb._tmdb_get: GET %s failed: %s", path, e)
        return None


def tmdb_fetch_by_id(tmdb_id, media_type=None, api_key=None, access_token=None, language=None):
    """Fetch TMDB details directly by numeric id. media_type may be 'tv' or 'movie' or None.
    Returns normalized dict like tmdb_search or None on failure. Direct id lookups do not use cache.
    """
    key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
    use_v4 = config.get('tmdb_use_v4', False)
    access = access_token or (config.get('tmdb_access_token') if use_v4 else None)
    if not key and not access:
        logging.debug("provider_tmdb.tmdb_fetch_by_id: no API key or access token available for id=%s", tmdb_id)
        return None

    headers = {}
    params_base = {}
    if access:
        headers['Authorization'] = f"Bearer {access}"
    else:
        params_base['api_key'] = key
    lang_norm = _normalize_tmdb_language(language)
    if lang_norm:
        try:
            params_base['language'] = str(lang_norm)
        except Exception as e:
            _debug_suppressed(e)
    if media_type and str(media_type).lower().startswith('tv'):
        candidates = ['tv', 'movie']
    elif media_type:
        candidates = ['movie', 'tv']
    else:
        candidates = ['tv', 'movie']

    details = None
    used_type = None
    for t in candidates:
        try:
            logging.debug("provider_tmdb.tmdb_fetch_by_id: trying %s/%s (params_keys=%s headers=%s)", t, tmdb_id, list(params_base.keys()), list(headers.keys()))
            d = _tmdb_get(f"{t}/{tmdb_id}", params=params_base, headers=headers)
            if not d:
                logging.debug("provider_tmdb.tmdb_fetch_by_id: no details for %s/%s", t, tmdb_id)
                continue
            details = d
            used_type = t
            logging.debug("provider_tmdb.tmdb_fetch_by_id: fetched details for %s/%s", t, tmdb_id)
            break
        except Exception as e:
            logging.warning("provider_tmdb.tmdb_fetch_by_id: failed fetch %s/%s: %s", t, tmdb_id, e)
            continue

    if not details:
        return None

    poster = details.get('poster_path')
    poster_url = f"https://image.tmdb.org/t/p/original{poster}" if poster else ''
    backdrop = details.get("backdrop_path")
    backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else ""
    genre_ids = [g.get('id') for g in (details.get('genres') or []) if isinstance(g, dict)]
    mapped_genres = map_tmdb_genres(",".join(str(gid) for gid in genre_ids))
    # Additional helpful fields for UI (dates/status/counts) while keeping response compact.
    try:
        release_date = details.get('release_date') or None
    except Exception:
        release_date = None
    try:
        first_air_date = details.get('first_air_date') or None
    except Exception:
        first_air_date = None
    try:
        last_air_date = details.get('last_air_date') or None
    except Exception:
        last_air_date = None
    try:
        status = details.get('status') or None
    except Exception:
        status = None
    try:
        number_of_seasons = details.get('number_of_seasons') if isinstance(details.get('number_of_seasons'), int) else None
    except Exception:
        number_of_seasons = None
    try:
        number_of_episodes = details.get('number_of_episodes') if isinstance(details.get('number_of_episodes'), int) else None
    except Exception:
        number_of_episodes = None
    content_rating = None
    try:
        if used_type and str(used_type).lower().startswith("tv"):
            cr = _tmdb_get(f"tv/{tmdb_id}/content_ratings", params=params_base, headers=headers)
            content_rating = _pick_tmdb_tv_rating(cr)
        else:
            rd = _tmdb_get(f"movie/{tmdb_id}/release_dates", params=params_base, headers=headers)
            content_rating = _pick_tmdb_movie_rating(rd)
    except Exception:
        content_rating = None

    normalized = {
        'provider': 'tmdb',
        'provider_id': int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id,
        'tmdb_id': int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id,
        'media_type': used_type or (media_type or 'tv'),
        'title': details.get('title') or details.get('name') or '',
        'series_title': details.get('name') or details.get('title') or '',
        'synopsis': details.get('overview') or '',
        'genre_ids': genre_ids,
        'genres': mapped_genres,
        'images': {
            **({'jpg': {'large_image_url': poster_url}} if poster_url else {}),
            **({'backdrop': {'large_image_url': backdrop_url}} if backdrop_url else {}),
        },
        'release_date': release_date,
        'first_air_date': first_air_date,
        'last_air_date': last_air_date,
        'status': status,
        'number_of_seasons': number_of_seasons,
        'number_of_episodes': number_of_episodes,
        'content_rating': content_rating,
    }
    return normalized


def tmdb_from_url(url, api_key=None, access_token=None):
    try:
        tid, mtype, season = _parse_tmdb_url(url)
        if not tid:
            return None
        logging.debug("provider_tmdb.tmdb_from_url: parsed url=%s -> id=%s type=%s season=%s", url, tid, mtype, season)
        # if season specified and it's a tv resource, fetch season details
        if season and (mtype is None or str(mtype).lower().startswith('tv')):
            logging.debug("provider_tmdb.tmdb_from_url: fetching season %s for tv id %s", season, tid)
            season_data = tmdb_fetch_season(tid, season_number=season, api_key=api_key, access_token=access_token)
            if season_data:
                logging.debug("provider_tmdb.tmdb_from_url: returned season data for %s season %s", tid, season)
                return season_data
        logging.debug("provider_tmdb.tmdb_from_url: fetching series data for id=%s type=%s", tid, mtype)
        return tmdb_fetch_by_id(tid, media_type=mtype, api_key=api_key, access_token=access_token)
    except Exception as e:
        logging.warning("provider_tmdb.tmdb_from_url: failed to parse/fetch url %s: %s", url, e)
        return None


def tmdb_fetch_season(tmdb_id, season_number, api_key=None, access_token=None, language=None):
    """Fetch season-level details for a TV series: episodes, overview, images."""
    key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
    use_v4 = config.get('tmdb_use_v4', False)
    access = access_token or (config.get('tmdb_access_token') if use_v4 else None)
    if not key and not access:
        logging.debug("provider_tmdb.tmdb_fetch_season: no API key or access token available for id=%s season=%s", tmdb_id, season_number)
        return None

    headers = {}
    params = {}
    if access:
        headers['Authorization'] = f"Bearer {access}"
    else:
        params['api_key'] = key
    lang_norm = _normalize_tmdb_language(language)
    if lang_norm:
        try:
            params['language'] = str(lang_norm)
        except Exception as e:
            _debug_suppressed(e)
    try:
        logging.debug("provider_tmdb.tmdb_fetch_season: GET tv/%s/season/%s", tmdb_id, season_number)
        data = _tmdb_get(f"tv/{tmdb_id}/season/{season_number}", params=params, headers=headers)
        if not data:
            logging.debug("provider_tmdb.tmdb_fetch_season: no data returned for tv/%s/season/%s", tmdb_id, season_number)
            return None

        series_title = None
        try:
            series_meta = _tmdb_get(f"tv/{tmdb_id}", params=params, headers=headers)
            if series_meta:
                series_title = (
                    series_meta.get('name')
                    or series_meta.get('original_name')
                    or series_meta.get('title')
                )
        except Exception as e:
            logging.debug("provider_tmdb.tmdb_fetch_season: failed to fetch series meta for %s: %s", tmdb_id, e)

        episodes = []
        for ep in data.get('episodes', []) or []:
            still = ep.get('still_path')
            still_url = f"https://image.tmdb.org/t/p/original{still}" if still else None
            episodes.append({
                'episode_number': ep.get('episode_number'),
                'title': ep.get('name') or '',
                'overview': ep.get('overview') or '',
                'air_date': ep.get('air_date') or None,
                'episode_type': ep.get('episode_type') or None,
                'still_path': still_url,
            })

        poster = data.get('poster_path')
        poster_url = f"https://image.tmdb.org/t/p/original{poster}" if poster else ''
        backdrop = None
        try:
            backdrop = (series_meta or {}).get("backdrop_path") if isinstance(series_meta, dict) else None
        except Exception:
            backdrop = None
        backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else ""
        season_num_str = str(season_number) if season_number is not None else ""
        try:
            lang0 = str(lang_norm or "").lower()
        except Exception:
            lang0 = ""
        season_word = "Temporada" if (lang0.startswith("es") or not lang0) else "Season"
        season_label = data.get('name') or (f"{season_word} {season_num_str}".strip())
        if series_title and season_num_str:
            display_title = f"{series_title} - {season_word} {season_num_str}"
        elif series_title:
            display_title = series_title
        else:
            display_title = season_label or f"Temporada {season_num_str}".strip()

        normalized = {
            'provider': 'tmdb',
            'provider_id': int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id,
            'tmdb_id': int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id,
            'media_type': 'tv',
            'season_number': int(season_number) if str(season_number).isdigit() else season_number,
            'title': display_title,
            'series_title': series_title or (season_label or ''),
            'synopsis': data.get('overview') or '',
            'poster_path': poster_url or None,
            'backdrop_path': backdrop_url or None,
            'episodes': episodes,
            'images': {
                **({'jpg': {'large_image_url': poster_url}} if poster_url else {}),
                **({'backdrop': {'large_image_url': backdrop_url}} if backdrop_url else {}),
            },
        }
        return normalized
    except Exception as e:
        logging.debug("tmdb_fetch_season: failed to fetch tv/%s/season/%s : %s", tmdb_id, season_number, e)
        return None


def tmdb_get_genres(api_key=None):
    try:
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        if not key:
            return {}
        cache_key = f"tmdb_genres:{key}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        mapping = {}
        try:
            data_tv = _tmdb_get('genre/tv/list', params={'api_key': key})
            if data_tv and isinstance(data_tv, dict):
                for g in data_tv.get('genres', []):
                    mapping[g['id']] = g['name']
        except Exception as e:
            logging.debug("tmdb_get_genres: tv list failed: %s", e)

        try:
            data_mv = _tmdb_get('genre/movie/list', params={'api_key': key})
            if data_mv and isinstance(data_mv, dict):
                for g in data_mv.get('genres', []):
                    mapping[g['id']] = g['name']
        except Exception as e:
            logging.debug("tmdb_get_genres: movie list failed: %s", e)

        if mapping:
            cache_set(cache_key, mapping)
        return mapping
    except Exception as e:
        logging.warning("tmdb_get_genres: failed to fetch genres: %s", e)
        return {}


def tmdb_search(query, media_preference='auto', api_key=None, access_token=None, allow_when_config_is_jikan=False, ignore_negative_cache: bool = False):
    # Deterministic TMDB search flow:
    # 1) explicit TMDB URL -> resolve directly (no cache)
    # 2) explicit numeric id -> fetch directly (no cache)
    # 3) fuzzy textual search -> cached
    try:
        # Normalize incoming api credentials
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        use_v4 = config.get('tmdb_use_v4', False)
        access_token = access_token or (config.get('tmdb_access_token') if use_v4 else None)

        # 1) URL
        if isinstance(query, str):
            q0 = query.strip()
            if 'themoviedb.org' in q0.lower():
                resolved = tmdb_from_url(q0, api_key=api_key, access_token=access_token)
                if resolved:
                    return resolved

        # 2) numeric id
        if isinstance(query, str) and re.fullmatch(r"\d+", query.strip()):
            # Respect caller preference: if we were asked for TV-only or Movie-only, don't allow
            # cross-type results (this can otherwise mis-map season marker strings like "2").
            try:
                pref0 = str(media_preference or "auto").strip().lower()
            except Exception:
                pref0 = "auto"
            numeric_is_movie_title = bool(pref0 == "movie" and re.fullmatch(r"(?:19|20)\d{2}", query.strip()))
            mt_hint = None
            if pref0 == "tv":
                mt_hint = "tv"
            elif pref0 == "movie" and not numeric_is_movie_title:
                mt_hint = "movie"
            if not numeric_is_movie_title:
                resolved = tmdb_fetch_by_id(query.strip(), media_type=mt_hint, api_key=api_key, access_token=access_token)
                if resolved:
                    try:
                        if mt_hint and str(resolved.get("media_type") or "").strip().lower() != mt_hint:
                            return None
                    except Exception:
                        pass
                    return resolved

        # If no API credentials available for fuzzy search, bail out
        if not key and not access_token:
            logging.debug("tmdb_search: no API credentials for fuzzy search")
            return None

        headers = {}
        params_base = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            params_base["api_key"] = key
        params_base["page"] = 1
        # Use the configured metadata language for search. TMDB still returns
        # original_title/original_name, so English filenames remain matchable while
        # Spanish/localized filenames can resolve to the intended translated title.
        try:
            lang_norm = _normalize_tmdb_language(config.get("metadata_language"))
        except Exception:
            lang_norm = None
        if lang_norm:
            params_base["language"] = str(lang_norm)

        def make_variants(q):
            variants = [q]
            if not q:
                return variants
            cleaned = re.sub(r"[_\.-]+", " ", q)
            cleaned = re.sub(r"\(.*?\)|\[.*?\]", "", cleaned).strip()
            cleaned = re.sub(
                r"(?i)\b(?:hdts|cam|latino|castellano|espanol|dual|multi|webrip|web[- ]?dl|bdrip|brrip|bluray|hdrip|hdr|x264|x265|hevc|aac|mkv|mp4|tg)\b",
                " ",
                cleaned,
            )
            cleaned = re.sub(r"\b(19|20)\d{2}\b", "", cleaned).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned not in variants:
                variants.append(cleaned)
            # Handle sequels encoded as leading numbers: "3 John Wick" -> "John Wick", "John Wick 3".
            try:
                m_lead = re.match(r"^\s*(\d{1,2})\s+(.+)$", cleaned)
                if m_lead:
                    num = int(m_lead.group(1))
                    rest = (m_lead.group(2) or "").strip()
                    if rest and rest not in variants:
                        variants.append(rest)
                    with_num = f"{rest} {num}".strip()
                    if with_num and with_num not in variants:
                        variants.append(with_num)
            except Exception as e:
                _debug_suppressed(e)
            no_articles = re.sub(r"\b(the|a|an)\b", "", cleaned, flags=re.IGNORECASE).strip()
            no_articles = re.sub(r"\s+", " ", no_articles)
            if no_articles and no_articles not in variants:
                variants.append(no_articles)
            return variants

        def _extract_release_year(cand: dict) -> int | None:
            try:
                rd = cand.get("release_date") or cand.get("first_air_date") or ""
                if not rd:
                    return None
                m = re.match(r"^\s*((?:19|20)\d{2})", str(rd))
                if not m:
                    return None
                return int(m.group(1))
            except Exception as e:
                _debug_suppressed(e)
                return None

        # Only cache fuzzy textual searches
        def cache_key_for(query_text, media_pref, anchor_year_hint=None):
            norm = (query_text or '').strip().lower()
            y = ""
            try:
                if anchor_year_hint is not None:
                    y = str(int(anchor_year_hint))
            except Exception:
                y = ""
            lang_key = str(params_base.get("language") or "").strip().lower()
            key_src = f"tmdb:search:{media_pref}:lang:{lang_key}:{norm}:y:{y}"
            return hashlib.sha256(key_src.encode('utf-8')).hexdigest()

        queries = [query] if not isinstance(query, (list, tuple)) else [q for q in query if q]
        # Keep deterministic order but prioritize candidates that carry an explicit year token.
        # This avoids stale no-year cache hits overriding more precise "Title 2024" anchors.
        try:
            dedup: list[str] = []
            seen_q: set[str] = set()
            for q in queries:
                s = str(q).strip()
                if not s:
                    continue
                k = s.lower()
                if k in seen_q:
                    continue
                seen_q.add(k)
                dedup.append(s)
            queries = sorted(
                dedup,
                key=lambda s: (0 if re.search(r"\b(?:19|20)\d{2}\b", s) else 1, len(s)),
            )
        except Exception:
            try:
                queries = [str(q) for q in queries if str(q).strip()]
            except Exception:
                pass

        global_anchor_year = None
        try:
            for q in queries:
                m = re.search(r"\b(19|20)\d{2}\b", str(q or ""))
                if m:
                    global_anchor_year = int(m.group(0))
                    break
        except Exception:
            global_anchor_year = None
        overall_best = None
        overall_best_score = -1.0
        overall_media_type = None

        for candidate in queries:
            if not candidate:
                continue
            candidate = str(candidate)
            cache_key = None
            m_year = re.search(r"\b(19|20)\d{2}\b", candidate or "")
            year_match_val = m_year.group(0) if m_year else None
            try:
                anchor_year = int(year_match_val) if year_match_val else (
                    int(global_anchor_year) if global_anchor_year is not None else None
                )
            except Exception:
                anchor_year = None
            year_filter_val = year_match_val or (str(anchor_year) if anchor_year is not None else None)
            try:
                cache_key = cache_key_for(candidate, media_preference, anchor_year)
                if cache_get_negative(cache_key) and not ignore_negative_cache:
                    continue
                cached = cache_get(cache_key)
                if cached is not None:
                    return cached
            except Exception as e:
                _debug_suppressed(e)
            variants = make_variants(candidate)

            # Try movie or tv based on preference
            pref = (media_preference or 'auto').lower()

            # Build search order deterministically.
            # IMPORTANT: When caller requests a specific type ('tv' or 'movie'), do NOT fall back
            # to the other type. This avoids mis-identifications from generic folder names like
            # "Temp 1" or "Season 2" that can match unrelated movies.
            if pref == 'movie':
                endpoints = ['search/movie']
            elif pref == 'tv':
                endpoints = ['search/tv']
            else:
                endpoints = ['search/movie', 'search/tv']

            results = []
            media_type = None

            for ep in endpoints:
                for qv in variants:
                    try:
                        params = dict(params_base)
                        params['query'] = qv
                        if year_filter_val:
                            if ep.endswith('/movie'):
                                params['year'] = year_filter_val
                            else:
                                params['first_air_date_year'] = year_filter_val
                        data = _tmdb_get(ep, params=params, headers=headers)
                        results = data.get('results') or [] if data else []
                        # If this variant already contains an explicit year token, skip the immediate
                        # no-year fallback so the next cleaned variant can be tried (e.g. "Dune 1984" -> "Dune").
                        has_year_in_variant = bool(re.search(r"\b(?:19|20)\d{2}\b", str(qv or "")))
                        if not results and year_filter_val and not has_year_in_variant:
                            params_fallback = dict(params_base)
                            params_fallback['query'] = qv
                            data = _tmdb_get(ep, params=params_fallback, headers=headers)
                            results = data.get('results') or [] if data else []
                        if results:
                            media_type = 'movie' if ep.endswith('/movie') else 'tv'
                            break
                    except Exception as e:
                        logging.debug("tmdb_search: search %s for '%s' failed: %s", ep, qv, e)
                        results = []
                if results:
                    break

            if not results:
                try:
                    if cache_key:
                        cache_set_negative(cache_key, True)
                except Exception as e:
                    _debug_suppressed(e)
                continue

            # scoring heuristics preserved
            import difflib
            qnorm = (candidate or "").strip().lower()
            qnorm_no_year = re.sub(r"\b(?:19|20)\d{2}\b", " ", qnorm)
            qnorm_no_year = re.sub(r"\s{2,}", " ", qnorm_no_year).strip()
            tv_hint = bool(re.search(r"season\b|s\d{1,2}\b|temporada\b", (candidate or "").lower()))

            best = None
            best_score = -1.0
            tokens = re.findall(r"\w+", qnorm)
            tokens = [t for t in tokens if t and len(t) > 1]

            for cand in results:
                title_candidates = [
                    cand.get('title') or cand.get('name') or '',
                    cand.get('original_title') or cand.get('original_name') or '',
                ]
                title_candidates = [t.strip().lower() for t in title_candidates if t and str(t).strip()]
                cand_title = title_candidates[0] if title_candidates else ''
                short_query = len(qnorm) <= 3

                if anchor_year is not None:
                    try:
                        cy = _extract_release_year(cand)
                        tol = 1 if str(media_type or "").strip().lower() == "tv" else 0
                        if cy is None or abs(int(cy) - int(anchor_year)) > tol:
                            continue
                    except Exception as e:
                        _debug_suppressed(e)
                        continue

                def _score_for(title_text: str) -> tuple[float, float]:
                    token_ratio = 0.0
                    if tokens:
                        try:
                            words = set(re.findall(r"\w+", title_text))
                            matched = sum(1 for t in tokens if t in words)
                        except Exception:
                            matched = 0
                        token_ratio = matched / len(tokens) if tokens else 0.0
                    sim = difflib.SequenceMatcher(None, qnorm, title_text).ratio()
                    return token_ratio, sim

                if cand_title == qnorm or (short_query and any(t == qnorm for t in title_candidates)):
                    best = cand
                    best_score = 1.0
                    break
                if qnorm_no_year and any(t == qnorm_no_year for t in title_candidates):
                    # Query contains a year but title may not; boost exact title-no-year matches.
                    token_ratio, sim = _score_for(cand_title)
                    score = 0.6 * token_ratio + 0.3 * sim + 0.28
                    if score > best_score:
                        best_score = score
                        best = cand
                    continue

                if title_candidates:
                    ratios = [_score_for(t) for t in title_candidates]
                    token_ratio = max(r[0] for r in ratios)
                    sim = max(r[1] for r in ratios)
                else:
                    token_ratio, sim = _score_for(cand_title)

                year_bonus = 0.0
                try:
                    release = cand.get('release_date') or cand.get('first_air_date') or ''
                    if release and year_filter_val and release.startswith(str(year_filter_val)):
                        year_bonus = 0.2
                except Exception as e:
                    logging.debug("tmdb_search: year bonus compute failed: %s", e)

                score = 0.6 * token_ratio + 0.3 * sim + year_bonus
                if tv_hint and (cand.get('media_type') == 'tv' or cand.get('first_air_date')):
                    score += 0.05

                if score > best_score:
                    best_score = score
                    best = cand

            if best is None:
                # If the query includes an explicit year, do not force-pick a mismatched candidate.
                if anchor_year is not None:
                    continue
                best = results[0]

            # prefer most recent among exact-title matches
            try:
                if best_score == 1.0 and results:
                    exacts = [r for r in results if ((r.get('title') or r.get('name') or '').strip().lower() == qnorm)]
                    if len(exacts) > 1:
                        def parse_date(d):
                            try:
                                from datetime import datetime
                                return datetime.strptime(d, '%Y-%m-%d') if d else None
                            except Exception:
                                return None

                        best_date = None
                        best_candidate = None
                        for r in exacts:
                            rd = r.get('release_date') or r.get('first_air_date') or ''
                            pd = parse_date(rd)
                            if pd is None:
                                continue
                            if best_date is None or pd > best_date:
                                best_date = pd
                                best_candidate = r
                        if best_candidate:
                            best = best_candidate
                            best_score = 1.0
            except Exception as e:
                logging.debug("tmdb_search: exact-date selection failed: %s", e)

            try:
                score_for_candidate = float(best_score)
            except Exception:
                score_for_candidate = 0.0

            replaced = False
            if score_for_candidate > overall_best_score:
                replaced = True
            elif score_for_candidate == overall_best_score and overall_best is not None:
                try:
                    from datetime import datetime

                    def parse(d):
                        try:
                            return datetime.strptime(d, '%Y-%m-%d') if d else None
                        except Exception:
                            return None

                    best_rd = best.get('release_date') or best.get('first_air_date') or ''
                    overall_rd = overall_best.get('release_date') or overall_best.get('first_air_date') or ''
                    pd_best = parse(best_rd)
                    pd_over = parse(overall_rd)
                    if pd_best and pd_over:
                        if pd_best > pd_over:
                            replaced = True
                    elif pd_best and not pd_over:
                        replaced = True
                except Exception as e:
                    logging.debug("tmdb_search: tie-break parse failed: %s", e)

            if replaced:
                overall_best_score = score_for_candidate
                overall_best = best
                overall_media_type = media_type

        if overall_best is None:
            return None

        best = overall_best
        media_type = overall_media_type or 'movie'
        tmdb_id = best.get('id')
        details = best
        # fetch full details for selected id (do not cache this explicit fetch)
        try:
            params = dict(params_base)
            if media_type == 'movie':
                details_det = _tmdb_get(f"movie/{tmdb_id}", params=params, headers=headers)
                if details_det:
                    details = details_det
            else:
                details_det = _tmdb_get(f"tv/{tmdb_id}", params=params, headers=headers)
                if details_det:
                    details = details_det
        except Exception as e:
            logging.debug("tmdb_search: details fetch failed for %s/%s: %s", media_type, tmdb_id, e)
            details = best

        poster = details.get('poster_path') or best.get('poster_path')
        poster_url = f"https://image.tmdb.org/t/p/original{poster}" if poster else ''
        backdrop = details.get("backdrop_path") or best.get("backdrop_path")
        backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else ""

        genre_ids = [g.get('id') for g in (details.get('genres') or []) if isinstance(g, dict)]
        mapped_genres = map_tmdb_genres(",".join(str(gid) for gid in genre_ids))
        # Helpful date/status/count fields for UI.
        try:
            release_date = details.get('release_date') or None
        except Exception:
            release_date = None
        try:
            first_air_date = details.get('first_air_date') or None
        except Exception:
            first_air_date = None
        try:
            last_air_date = details.get('last_air_date') or None
        except Exception:
            last_air_date = None
        try:
            status = details.get('status') or None
        except Exception:
            status = None
        try:
            number_of_seasons = details.get('number_of_seasons') if isinstance(details.get('number_of_seasons'), int) else None
        except Exception:
            number_of_seasons = None
        try:
            number_of_episodes = details.get('number_of_episodes') if isinstance(details.get('number_of_episodes'), int) else None
        except Exception:
            number_of_episodes = None
        content_rating = None
        try:
            params = dict(params_base)
            if media_type == 'tv':
                cr = _tmdb_get(f"tv/{tmdb_id}/content_ratings", params=params, headers=headers)
                content_rating = _pick_tmdb_tv_rating(cr)
            else:
                rd = _tmdb_get(f"movie/{tmdb_id}/release_dates", params=params, headers=headers)
                content_rating = _pick_tmdb_movie_rating(rd)
        except Exception:
            content_rating = None
        normalized = {
            'provider': 'tmdb',
            'provider_id': tmdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': details.get('title') or details.get('name') or best.get('title') or best.get('name'),
            'synopsis': details.get('overview') or best.get('overview') or '',
            'genre_ids': genre_ids,
            'genres': mapped_genres,
            'images': {
                **({'jpg': {'large_image_url': poster_url}} if poster_url else {}),
                **({'backdrop': {'large_image_url': backdrop_url}} if backdrop_url else {}),
            },
            'release_date': release_date,
            'first_air_date': first_air_date,
            'last_air_date': last_air_date,
            'status': status,
            'number_of_seasons': number_of_seasons,
            'number_of_episodes': number_of_episodes,
            'content_rating': content_rating,
        }
        normalized['series_title'] = normalized.get('title') or ''

        # Cache only fuzzy search results (not direct id/url lookups)
        try:
            # use hashed cache key to avoid collisions
            cache_key = cache_key_for(candidate, media_preference)
            cache_set(cache_key, normalized)
        except Exception as e:
            logging.debug("tmdb_search: cache set failed: %s", e)

        return normalized
    except Exception as e:
        logging.warning("tmdb_search: unexpected error: %s", e)
        raise


def tmdb_search_by_type(title, type_hint=None, api_key=None, access_token=None, allow_when_config_is_jikan=False, ignore_negative_cache: bool = False):
    try:
        pref = 'auto'
        if type_hint:
            th = str(type_hint).strip().lower()
            if any(k in th for k in ('serie', 'series', 'tv', 'season')):
                pref = 'tv'
            elif any(k in th for k in ('pelicula', 'movie', 'film')):
                pref = 'movie'
        return tmdb_search(title, media_preference=pref, api_key=api_key, access_token=access_token, allow_when_config_is_jikan=allow_when_config_is_jikan, ignore_negative_cache=ignore_negative_cache)
    except Exception:
        return None


def tmdb_get_episodes(tmdb_id, api_key=None):
    try:
        # prefer explicit api_key, then environment variable (from .env), then config value
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        if not key:
            return []
        cache_key = f"tmdb_episodes:{tmdb_id}:{key}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        data = _tmdb_get(f"tv/{tmdb_id}", params={"api_key": key}, timeout=10)
        if not data:
            return []
        episodes = []
        seasons = data.get("seasons", []) or []
        for s in seasons:
            season_number = s.get("season_number")
            if season_number == 0:
                continue
            try:
                season_data = _tmdb_get(f"tv/{tmdb_id}/season/{season_number}", params={"api_key": key}, timeout=10)
                if not season_data:
                    count = s.get("episode_count") or 0
                    for i in range(1, count + 1):
                        episodes.append({"title": f"Ep {i}"})
                    continue
                for ep in season_data.get("episodes", []) or []:
                    title = ep.get("name") or ep.get("overview") or f"Ep {ep.get('episode_number') or ''}"
                    episodes.append({"title": title})
            except Exception:
                count = s.get("episode_count") or 0
                for i in range(1, count + 1):
                    episodes.append({"title": f"Ep {i}"})

        cache_set(cache_key, episodes)
        return episodes
    except Exception as e:
        logging.warning("provider_tmdb.tmdb_get_episodes: unexpected error for %s: %s", tmdb_id, e)
        return []


# --- Compatibility wrappers used by enrichment layer ---
def fetch_by_id(tmdb_id, media_type=None, api_key=None, access_token=None):
    if tmdb_id is None:
        return None
    # Allow exceptions to bubble so callers can distinguish "no match" vs "provider error".
    return tmdb_fetch_by_id(tmdb_id, media_type=media_type, api_key=api_key, access_token=access_token)


def search(query, api_key=None, access_token=None):
    if query is None:
        return None
    q = str(query).strip()
    if not q:
        return None
    # Allow exceptions to bubble so callers can distinguish "no match" vs "provider error".
    return tmdb_search(q, media_preference="auto", api_key=api_key, access_token=access_token)


def search_by_type(title, media_preference="auto", api_key=None, access_token=None, allow_when_config_is_jikan=False, ignore_negative_cache: bool = False):
    if title is None:
        return None
    t = str(title).strip()
    if not t:
        return None
    pref = (media_preference or "auto").strip().lower()
    if pref in ("series", "tv", "show", "episode"):
        pref = "tv"
    elif pref in ("movie", "film"):
        pref = "movie"
    else:
        pref = "auto"
    # Allow exceptions to bubble so callers can distinguish "no match" vs "provider error".
    return tmdb_search(
        t,
        media_preference=pref,
        api_key=api_key,
        access_token=access_token,
        allow_when_config_is_jikan=allow_when_config_is_jikan,
        ignore_negative_cache=ignore_negative_cache,
    )
