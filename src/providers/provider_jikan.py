import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import time
import os
import re
try:
    from src.services import telemetry  # type: ignore
except Exception:
    try:
        from ..services import telemetry  # type: ignore
    except Exception:
        telemetry = None

# Prefer project cache utilities but fall back gracefully
try:
    from src.tools.cache import cache_get, cache_set, cache_get_negative, cache_set_negative
except Exception:
    try:
        from src.tools.cache import cache_get, cache_set, cache_get_negative, cache_set_negative
    except Exception:
        def cache_get(key):
            return None
        def cache_set(key, value):
            return None
        def cache_get_negative(key):
            return None
        def cache_set_negative(key, value=True):
            return None
try:
    from src.utils.rate_limiter import get_limiter
    _rate_limiter = get_limiter("jikan")
except Exception:
    _rate_limiter = None

API_BASE = "https://api.jikan.moe/v4"

# Throttling controls to avoid network saturation.
_API_CONCURRENCY = max(1, int(os.environ.get("ARCANEA_API_CONCURRENCY", "2") or 2))
_API_DELAY_SEC = max(0.0, float(os.environ.get("ARCANEA_API_DELAY_MS", "1500") or 1500) / 1000.0)
_api_sema = None
try:
    import threading as _threading
    _api_sema = _threading.BoundedSemaphore(_API_CONCURRENCY)
except Exception:
    _api_sema = None

def _throttle_acquire():
    try:
        if _api_sema:
            _api_sema.acquire()
    except Exception as e:
        _debug_suppressed(e)
def _throttle_release():
    try:
        if _api_sema:
            _api_sema.release()
    except Exception as e:
        _debug_suppressed(e)
    # When rate limiter is active, avoid double-throttling here.
    if _API_DELAY_SEC > 0 and _rate_limiter is None:
        time.sleep(_API_DELAY_SEC)

# Simple circuit breaker to avoid hammering Jikan when it's returning 429/504.
_cooldown_until_ts = 0.0
_cooldown_lock = None
try:
    import threading as _threading
    _cooldown_lock = _threading.Lock()
except Exception:
    _cooldown_lock = None


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
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session_with_retries()
logger = logging.getLogger(__name__)

# Lightweight provider health cache to avoid spamming the API with pings.
_health_cache = {"ts": 0.0, "ok": True, "status": None}


def _debug_suppressed(exc: Exception) -> None:
    try:
        logger.debug("provider_jikan: suppressed exception: %s", exc)
    except Exception:
        return


def healthcheck(timeout_s: float = 3.0, ttl_s: float = 20.0) -> dict:
    """Best-effort healthcheck for Jikan.

    Returns: {"ok": bool, "status_code": int|None, "detail": str|None}

    We intentionally treat any response < 500 as "ok" (including 404) because the
    goal is to detect server-side outages (5xx) quickly during auto-fallback flows.
    """
    try:
        now = time.time()
        try:
            ts = float(_health_cache.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if (now - ts) < float(ttl_s):
            return {
                "ok": bool(_health_cache.get("ok", True)),
                "status_code": _health_cache.get("status"),
                "detail": None,
                "cached": True,
            }
    except Exception:
        pass

    status_code = None
    ok = False
    detail = None
    try:
        # Use a stable, cheap endpoint (MAL id 1 exists). We only care about 5xx vs non-5xx.
        resp = session.get(f"{API_BASE}/anime/1", timeout=float(timeout_s))
        status_code = getattr(resp, "status_code", None)
        try:
            ok = bool(status_code is not None and int(status_code) < 500)
        except Exception:
            ok = False
    except Exception as e:
        ok = False
        detail = f"{type(e).__name__}: {e}"

    try:
        _health_cache["ts"] = time.time()
        _health_cache["ok"] = bool(ok)
        _health_cache["status"] = status_code
    except Exception:
        pass

    return {"ok": bool(ok), "status_code": status_code, "detail": detail, "cached": False}


def find_local_mal_id(folder_path):
    """Search for common local files that may contain a MAL id and return the normalized id string or None."""
    candidates = [
        'id_mal.json', 'id_mal.txt',
        'mal_id.json', 'mal_id.txt',
        'mal.json', 'id.json'
    ]
    import json as _json
    for fn in candidates:
        path = os.path.join(folder_path, fn)
        if not os.path.exists(path):
            continue
        try:
            if fn.endswith('.json'):
                with open(path, 'r', encoding='utf-8') as fh:
                    jd = _json.load(fh)
                    mid = jd.get('id_mal') or jd.get('id_mal') or jd.get('id') or jd.get('mal')
            else:
                with open(path, 'r', encoding='utf-8') as fh:
                    mid = fh.read().strip()
            if mid:
                try:
                    return str(int(mid))
                except Exception:
                    m = re.search(r"(\d+)", str(mid))
                    if m:
                        return m.group(1)
                    return str(mid).strip()
        except Exception:
            continue
    return None


def fetch_anime_by_id(mal_id):
    """Fetch anime object by MAL id.

    Try to use the `/anime/{id}/full` endpoint (returns more fields in a single call)
    as a first option to reduce round-trips. Fall back to `/anime/{id}` if the
    full endpoint is unavailable or fails.
    """
    last_error = None
    cache_key = f"jikan:anime:{mal_id}"
    try:
        if cache_get_negative(cache_key):
            return None
    except Exception as e:
        _debug_suppressed(e)
    # Prefer the /full endpoint to retrieve as much metadata as possible in one request.
    try:
        resp = None
        _throttle_acquire()
        try:
            status_code = None
            try:
                if _rate_limiter is not None:
                    _rate_limiter.before_request()
            except Exception as e:
                _debug_suppressed(e)
            t0 = time.perf_counter()
            resp = session.get(f"{API_BASE}/anime/{mal_id}/full", timeout=10)
            status_code = getattr(resp, "status_code", None)
            if resp.status_code == 404:
                try:
                    cache_set_negative(cache_key, True)
                except Exception as e:
                    _debug_suppressed(e)
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_failure(status_code=404)
                except Exception as e:
                    _debug_suppressed(e)
                return None
            resp.raise_for_status()
            data = resp.json()
            if data and data.get('data'):
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    _debug_suppressed(e)
                return data['data']
            try:
                cache_set_negative(cache_key, True)
            except Exception as e:
                _debug_suppressed(e)
        finally:
            try:
                try:
                    if telemetry is not None:
                        dt = (time.perf_counter() - t0) * 1000.0 if 't0' in locals() else 0.0
                        telemetry.emit_api_timing("jikan", f"/anime/{mal_id}/full", dt, status_code=status_code)
                except Exception as e:
                    _debug_suppressed(e)
                if resp is not None:
                    resp.close()
            except Exception as e:
                _debug_suppressed(e)
            _throttle_release()
    except Exception as e:
        try:
            if _rate_limiter is not None:
                _rate_limiter.record_failure(status_code=getattr(resp, "status_code", None) if "resp" in locals() else None, exc=e)
        except Exception as e:
            _debug_suppressed(e)
        last_error = e
    # Fallback to the standard endpoint
    try:
        resp = None
        _throttle_acquire()
        try:
            status_code = None
            try:
                if _rate_limiter is not None:
                    _rate_limiter.before_request()
            except Exception as e:
                _debug_suppressed(e)
            t0 = time.perf_counter()
            resp = session.get(f"{API_BASE}/anime/{mal_id}/", timeout=10)
            status_code = getattr(resp, "status_code", None)
            if resp.status_code == 404:
                try:
                    cache_set_negative(cache_key, True)
                except Exception as e:
                    _debug_suppressed(e)
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_failure(status_code=404)
                except Exception as e:
                    _debug_suppressed(e)
                return None
            resp.raise_for_status()
            data = resp.json()
            if data and data.get('data'):
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    _debug_suppressed(e)
                return data['data']
            try:
                cache_set_negative(cache_key, True)
            except Exception as e:
                _debug_suppressed(e)
        finally:
            try:
                try:
                    if telemetry is not None:
                        dt = (time.perf_counter() - t0) * 1000.0 if 't0' in locals() else 0.0
                        telemetry.emit_api_timing("jikan", f"/anime/{mal_id}", dt, status_code=status_code)
                except Exception as e:
                    _debug_suppressed(e)
                if resp is not None:
                    resp.close()
            except Exception as e:
                _debug_suppressed(e)
            _throttle_release()
    except Exception as e:
        try:
            if _rate_limiter is not None:
                _rate_limiter.record_failure(status_code=getattr(resp, "status_code", None) if "resp" in locals() else None, exc=e)
        except Exception as e:
            _debug_suppressed(e)
        last_error = e

    if last_error is not None:
        logging.debug("provider_jikan.fetch_anime_by_id: failed for %s: %s", mal_id, last_error)
        raise last_error
    return None


def _extract_genres_and_themes_from_obj(anime_obj):
    """Return tuple (genres, themes) as lists of names extracted from a Jikan anime object.

    Each element in the original lists is usually a dict with a 'name' key. We normalize to
    simple lists of strings and defend against missing keys or unexpected shapes.
    """
    genres = []
    themes = []
    try:
        for g in anime_obj.get('genres', []) or []:
            try:
                name = g.get('name') if isinstance(g, dict) else str(g)
                if name:
                    genres.append(name)
            except Exception:
                continue
    except Exception:
        genres = []
    try:
        for t in anime_obj.get('themes', []) or []:
            try:
                name = t.get('name') if isinstance(t, dict) else str(t)
                if name:
                    themes.append(name)
            except Exception:
                continue
    except Exception:
        themes = []
    return genres, themes


def fetch_anime_metadata(mal_id, use_cache=True):
    """Fetch anime object from Jikan and return a small dict with cached genres and themes.

    Returns a dict with keys: 'anime' (raw jikan object or None), 'genres' (list of names),
    'themes' (list of names). Results are cached under key `anime:jikan:{mal_id}` to avoid
    repeated calls when generating JSON/pages.
    """
    cache_key = f"anime:jikan:{mal_id}"
    if use_cache:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    try:
        anime = fetch_anime_by_id(mal_id)
    except Exception:
        anime = None
    genres, themes = ([], [])
    if anime:
        genres, themes = _extract_genres_and_themes_from_obj(anime)

    result = {"anime": anime, "genres": genres, "themes": themes}
    try:
        cache_set(cache_key, result)
    except Exception:
        # cache_set is best-effort
        pass
    return result


def obtener_episodios(anime_id):
    cache_key = f"episodes:jikan:{anime_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    episodios = []
    def _find_mal_link(obj):
        """Recursively search JSON-like object for a string containing 'myanimelist.net' or '#episodes' and return it."""
        if isinstance(obj, str):
            if 'myanimelist.net' in obj or '#episodes' in obj:
                return obj
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    res = _find_mal_link(v)
                except Exception:
                    res = None
                if res:
                    return res
            return None
        if isinstance(obj, (list, tuple)):
            for v in obj:
                try:
                    res = _find_mal_link(v)
                except Exception:
                    res = None
                if res:
                    return res
            return None
        return None

    logger.debug("provider_jikan.obtener_episodios: starting fetch for anime %s", anime_id)
    try:
        page = 1
        while True:
            # per-page retry loop for transient errors / rate limits
            attempt = 0
            max_attempts = 4
            data = None
            while attempt < max_attempts:
                try:
                    response = None
                    _throttle_acquire()
                    try:
                        try:
                            if _rate_limiter is not None:
                                _rate_limiter.before_request()
                        except Exception as e:
                            _debug_suppressed(e)
                        response = session.get(f"{API_BASE}/anime/{anime_id}/episodes", params={"page": page}, timeout=15)
                        # Handle explicit 429 with Retry-After
                        if response.status_code == 429:
                            ra = response.headers.get('Retry-After')
                            if ra is not None:
                                try:
                                    wait = int(ra)
                                except Exception:
                                    wait = min(2 ** attempt, 10)
                            else:
                                wait = min(2 ** attempt, 10)
                            logging.warning("provider_jikan.obtener_episodios: rate limited on page %s for %s, sleeping %s seconds", page, anime_id, wait)
                            time.sleep(wait)
                            attempt += 1
                            try:
                                if _rate_limiter is not None:
                                    _rate_limiter.record_failure(status_code=429)
                            except Exception as e:
                                _debug_suppressed(e)
                            continue
                        response.raise_for_status()
                        data = response.json()
                        try:
                            if _rate_limiter is not None:
                                _rate_limiter.record_success()
                        except Exception as e:
                            _debug_suppressed(e)
                        break
                    finally:
                        try:
                            if response is not None:
                                response.close()
                        except Exception as e:
                            _debug_suppressed(e)
                        _throttle_release()
                except Exception as e:
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.record_failure(exc=e)
                    except Exception as e:
                        _debug_suppressed(e)
                    logging.debug("provider_jikan.obtener_episodios: transient error fetching page %s for %s: %s", page, anime_id, e)
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
                    continue

            if data is None:
                logging.warning("provider_jikan.obtener_episodios: failed to fetch page %s for %s after %s attempts", page, anime_id, max_attempts)
                break
            if data and "data" in data:
                page_items = data.get("data") or []
                episodios.extend(page_items)
                # Log per-page fetch count
                logger.debug("provider_jikan.obtener_episodios: fetched %d episodes from page %s for anime %s", len(page_items), page, anime_id)

                # Detect possible MyAnimeList redirect/link in the response payload and log it
                try:
                    mal_link = _find_mal_link(data)
                    if mal_link:
                        logger.debug("provider_jikan.obtener_episodios: detected MyAnimeList link in Jikan response for anime %s: %s", anime_id, mal_link)
                        # Also log how many episodes were present in that same response/page
                        logger.debug("provider_jikan.obtener_episodios: episodes in this Jikan response/page: %d", len(page_items))
                except Exception as e:
                    logger.debug("provider_jikan.obtener_episodios: error while scanning for MAL links: %s", e)
                # pagination may exist; be defensive if missing
                has_next = False
                try:
                    pag = data.get("pagination", {}) or {}
                    # Jikan may include 'has_next_page' or 'last_visible_page'
                    if pag.get('has_next_page') is not None:
                        has_next = bool(pag.get('has_next_page'))
                    else:
                        # fallback: compare page to last_visible_page if present
                        last = pag.get('last_visible_page')
                        if isinstance(last, int):
                            has_next = page < int(last)
                except Exception:
                    has_next = False
                if not has_next:
                    break
                page += 1
            else:
                break

        if episodios:
            cache_set(cache_key, episodios)
            logger.debug("provider_jikan.obtener_episodios: total episodes fetched for %s: %d", anime_id, len(episodios))
    except Exception as e:
        logging.warning("provider_jikan.obtener_episodios: No se pudieron obtener todos los episodios para %s: %s", anime_id, e)
    return episodios


def obtener_episodio_detalle(anime_id, episode_number):
    """Fetch detailed episode info for an anime episode number.

    Jikan's `/anime/{id}/episodes` listing does not always include synopsis/summary.
    This endpoint can provide additional details (best-effort, rate-limited).
    """
    try:
        aid = str(anime_id).strip()
    except Exception:
        aid = ""
    if not aid:
        return None
    try:
        en = int(episode_number)
    except Exception:
        return None
    if en <= 0:
        return None

    cache_key = f"episodes:jikan:{aid}:detail:{en}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if cache_get_negative(cache_key):
            return None
    except Exception as e:
        _debug_suppressed(e)

    last_error = None
    for attempt in range(4):
        resp = None
        try:
            _throttle_acquire()
            try:
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.before_request()
                except Exception as e:
                    _debug_suppressed(e)

                t0 = time.perf_counter()
                resp = session.get(f"{API_BASE}/anime/{aid}/episodes/{en}", timeout=12)
                status_code = getattr(resp, "status_code", None)
                if status_code == 404:
                    try:
                        cache_set_negative(cache_key, True)
                    except Exception as e:
                        _debug_suppressed(e)
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.record_failure(status_code=404)
                    except Exception as e:
                        _debug_suppressed(e)
                    return None
                if status_code == 429:
                    ra = resp.headers.get("Retry-After")
                    try:
                        wait = int(ra) if ra is not None else min(2 ** attempt, 10)
                    except Exception:
                        wait = min(2 ** attempt, 10)
                    time.sleep(wait)
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.record_failure(status_code=429)
                    except Exception as e:
                        _debug_suppressed(e)
                    continue
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                out = data.get("data") if isinstance(data, dict) else None
                if isinstance(out, dict) and out:
                    try:
                        cache_set(cache_key, out)
                    except Exception as e:
                        _debug_suppressed(e)
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                    except Exception as e:
                        _debug_suppressed(e)
                    return out
                try:
                    cache_set_negative(cache_key, True)
                except Exception as e:
                    _debug_suppressed(e)
                return None
            finally:
                try:
                    if telemetry is not None:
                        dt = (time.perf_counter() - t0) * 1000.0 if "t0" in locals() else 0.0
                        telemetry.emit_api_timing("jikan", f"/anime/{aid}/episodes/{en}", dt, status_code=status_code if "status_code" in locals() else None)
                except Exception as e:
                    _debug_suppressed(e)
                try:
                    if resp is not None:
                        resp.close()
                except Exception as e:
                    _debug_suppressed(e)
                _throttle_release()
        except Exception as e:
            last_error = e
            try:
                if _rate_limiter is not None:
                    _rate_limiter.record_failure(status_code=getattr(resp, "status_code", None) if resp is not None else None, exc=e)
            except Exception as e:
                _debug_suppressed(e)
            time.sleep(min(2 ** (attempt + 1), 10))
            continue

    if last_error is not None:
        logging.debug("provider_jikan.obtener_episodio_detalle: failed for %s ep=%s: %s", aid, en, last_error)
    return None


def buscar_anime_candidates(titulo, limit_per_variant=8, overall_limit=20):
    """Return a list of candidate anime result dicts from Jikan for the given folder name/title."""
    from src.utils.utils import normalize_folder_name_for_search
    candidates = []
    seen_ids = set()
    variants = normalize_folder_name_for_search(titulo)
    try:
        for v in variants:
            if not v:
                continue
            try:
                resp = None
                _throttle_acquire()
                try:
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.before_request()
                    except Exception as e:
                        _debug_suppressed(e)
                    resp = session.get(f"{API_BASE}/anime", params={"q": v, "limit": limit_per_variant}, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                    try:
                        if _rate_limiter is not None:
                            _rate_limiter.record_success()
                    except Exception as e:
                        _debug_suppressed(e)
                finally:
                    try:
                        if resp is not None:
                            resp.close()
                    except Exception as e:
                        _debug_suppressed(e)
                    _throttle_release()
                results = data.get("data") or []
                for r in results:
                    mid = r.get('mal_id')
                    if mid and mid not in seen_ids:
                        candidates.append(r)
                        seen_ids.add(mid)
                        if len(candidates) >= overall_limit:
                            return candidates
            except Exception as e:
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_failure(exc=e)
                except Exception as e:
                    _debug_suppressed(e)
                continue
    except Exception as e:
        _debug_suppressed(e)
    return candidates


def buscar_anime_por_titulo_jikan(titulo):
    """Search Jikan by title (used when no local id is present). Returns best match or None."""
    global _cooldown_until_ts
    from src.utils.utils import normalize_folder_name_for_search
    # Versioned cache key: scoring rules evolve (season detection, tokenization, etc.).
    # Bumping the key prevents stale/wrong season matches from sticking for 1h TTL.
    cache_key = f"search:jikan:v2:{titulo}"
    try:
        if cache_get_negative(cache_key):
            return None
    except Exception as e:
        _debug_suppressed(e)
    cached = cache_get(cache_key)
    if cached:
        return cached

    best_result = None
    best_score = 0.0
    last_error = None
    try:
        import re
        from difflib import SequenceMatcher

        season_num_rx_local = re.compile(
            r"\bseason\s*0*(\d{1,2})\b"
            r"|\b0*(\d{1,2})(?:st|nd|rd|th)\s*season\b"
            r"|\btemporada\s*0*(\d{1,2})\b"
            r"|\b0*(\d{1,2})(?:a|ª|da|ra|ta)\s*temporada\b"
            r"|\btemp(?:\.|orada)?\s*0*(\d{1,2})\b"
            r"|\bs0*(\d{1,2})\b"
            r"|\b(?!19\d{2}\b)(?!20\d{2}\b)0*(\d{1,2})\b\s*$",
            re.IGNORECASE,
        )
        roman_map_local = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

        variants = normalize_folder_name_for_search(titulo)
        q0 = (variants[0] if isinstance(variants, list) and variants else None) or str(titulo or "").strip()
        if not q0:
            return None

        # Circuit breaker: if Jikan is currently unhealthy, fail fast and let the caller retry later.
        try:
            import time as _time
            now = _time.time()
            if _cooldown_lock:
                with _cooldown_lock:
                    cd = _cooldown_until_ts
            else:
                cd = _cooldown_until_ts
            if cd and now < cd:
                raise RuntimeError(f"Jikan cooldown active ({int(cd - now)}s)")
        except Exception as e:
            # if this is our cooldown exception, re-raise; otherwise ignore
            if isinstance(e, RuntimeError) and "Jikan cooldown active" in str(e):
                raise

        # One HTTP request per title: fetch a larger candidate set once and score locally against variants.
        try:
            response = None
            _throttle_acquire()
            try:
                status_code = None
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.before_request()
                except Exception as e:
                    _debug_suppressed(e)
                t0 = time.perf_counter()
                response = session.get(f"{API_BASE}/anime", params={"q": q0, "limit": 25}, timeout=10)
                status_code = getattr(response, "status_code", None)
                response.raise_for_status()
                data = response.json()
                try:
                    if _rate_limiter is not None:
                        _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    _debug_suppressed(e)
            finally:
                try:
                    try:
                        if telemetry is not None:
                            dt = (time.perf_counter() - t0) * 1000.0 if 't0' in locals() else 0.0
                            telemetry.emit_api_timing("jikan", "/anime", dt, status_code=status_code)
                    except Exception as e:
                        _debug_suppressed(e)
                    if response is not None:
                        response.close()
                except Exception as e:
                    _debug_suppressed(e)
                _throttle_release()
            results = data.get("data") or []
        except Exception as e:
            last_error = e
            try:
                if _rate_limiter is not None:
                    _rate_limiter.record_failure(status_code=status_code if "status_code" in locals() else None, exc=e)
            except Exception as e:
                _debug_suppressed(e)
            # Trip cooldown on rate-limit / gateway issues to prevent immediate repeated failures.
            try:
                import time as _time
                should_cooldown = False
                msg = str(e).lower()
                if "too many 429" in msg or " 429 " in msg or "rate" in msg:
                    should_cooldown = True
                if "504" in msg or "gateway" in msg or "too many 504" in msg:
                    should_cooldown = True
                if should_cooldown:
                    until = _time.time() + 45.0
                    if _cooldown_lock:
                        with _cooldown_lock:
                            _cooldown_until_ts = max(_cooldown_until_ts, until)
                    else:
                        _cooldown_until_ts = max(_cooldown_until_ts, until)
            except Exception as e:
                _debug_suppressed(e)
            raise

        if not results:
            try:
                cache_set_negative(cache_key, True)
            except Exception as e:
                _debug_suppressed(e)
            return None

        best_season_match = False

        for v in (variants if isinstance(variants, list) and variants else [q0]):
            if not v:
                continue

            season_num = None
            mseason = season_num_rx_local.search(v)
            if mseason:
                for g in mseason.groups():
                    if g:
                        try:
                            season_num = int(g)
                            break
                        except Exception as e:
                            _debug_suppressed(e)
            if season_num is None:
                try:
                    m_rn = re.search(r"\b(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b\s*(?:season|temporada)?\b", str(v), re.IGNORECASE)
                    if m_rn:
                        season_num = roman_map_local.get(m_rn.group(1).upper())
                except Exception:
                    season_num = None

            mroman = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", v, re.IGNORECASE)
            if not season_num and mroman:
                rn = mroman.group(1).upper()
                season_num = roman_map_local.get(rn)

            try:
                for cand in results:
                    cand_titles = []
                    try:
                        for t in [cand.get('title') or '', cand.get('title_english') or '', cand.get('title_japanese') or '']:
                            if t:
                                cand_titles.append(t.strip().lower())
                    except Exception:
                        cand_titles = [((cand.get('title') or '') or '').strip().lower()]

                    try:
                        syns = cand.get('titles') or []
                        for s in syns:
                            if isinstance(s, dict):
                                t = s.get('title') or ''
                            else:
                                t = str(s)
                            if t:
                                cand_titles.append(t.strip().lower())
                    except Exception as e:
                        _debug_suppressed(e)
                    query_norm = v.strip().lower()
                    sim_scores = [SequenceMatcher(None, query_norm, ct).ratio() for ct in cand_titles if ct]
                    base_sim = max(sim_scores) if sim_scores else 0.0

                    tokens = re.findall(r"\w+", query_norm)
                    # Keep numeric tokens too (e.g. "Season 3"), otherwise season queries often
                    # collapse to the base series and match the first/most popular result.
                    tokens = [t for t in tokens if t and (len(t) > 1 or t.isdigit())]
                    token_ratio = 0.0
                    if tokens:
                        joined = ' '.join(cand_titles)
                        matched = sum(1 for t in tokens if t in joined)
                        token_ratio = matched / len(tokens)

                    score = 0.6 * token_ratio + 0.35 * base_sim

                    season_found = False
                    if season_num:
                        season_found = False
                        for ct in cand_titles:
                            if f"season {season_num}" in ct or re.search(rf"\b{season_num}(st|nd|rd|th)?\b", ct):
                                if not re.search(rf"\b(19|20)\d{{2}}\b", ct):
                                    season_found = True
                                    break
                            m_r = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", ct, re.IGNORECASE)
                            if m_r:
                                rn = m_r.group(1).upper()
                                try:
                                    if roman_map_local.get(rn) == season_num:
                                        season_found = True
                                        break
                                except Exception as e:
                                    _debug_suppressed(e)
                        if season_found:
                            score += 0.40
                        else:
                            # When the query clearly implies a season, penalize results that don't.
                            score -= 0.22

                    try:
                        my = re.search(r"\b(19|20)\d{2}\b", v)
                        if my:
                            year_match_val = my.group(0)
                            aired = cand.get('year') or ''
                            if aired and str(aired).startswith(year_match_val):
                                score += 0.15
                    except Exception as e:
                        _debug_suppressed(e)
                    try:
                        te = (cand.get('title_english') or '').strip().lower()
                        if te and te == query_norm:
                            score += 0.25
                    except Exception as e:
                        _debug_suppressed(e)
                    if score > best_score:
                        best_score = score
                        best_result = cand
                        best_season_match = bool(season_found) if season_num else False

                if best_score >= 0.78 and (not season_num or best_season_match):
                    break
            except Exception as e:
                # Keep the last provider/network error so callers can distinguish "no match"
                # from "provider unreachable/rate-limited".
                last_error = e
                continue

        if best_result:
            cache_set(cache_key, best_result)
            return best_result
        if last_error is not None:
            raise last_error
        try:
            cache_set_negative(cache_key, True)
        except Exception as e:
            _debug_suppressed(e)
        return None
    except Exception as e:
        logging.warning("provider_jikan.buscar_anime_por_titulo_jikan '%s' (variants=%s): %s", titulo, normalize_folder_name_for_search(titulo), e)
        raise


# --- Compatibility wrappers used by enrichment layer and tools ---
def fetch_by_id(mal_id):
    if mal_id is None:
        return None
    mid = str(mal_id).strip()
    if not mid:
        return None
    # Allow exceptions to bubble so callers can distinguish "no match" vs "provider error".
    return fetch_anime_by_id(mid)


def search(query):
    if query is None:
        return None
    q = str(query).strip()
    if not q:
        return None
    # Return the best single match (dict) for the enrichment layer.
    # Allow exceptions to bubble so callers can distinguish "no match" vs "provider error".
    return buscar_anime_por_titulo_jikan(q)
