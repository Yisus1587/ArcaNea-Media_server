import logging
import os
import re
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from src.services import telemetry  # type: ignore
except Exception:
    try:
        from ..services import telemetry  # type: ignore
    except Exception:
        telemetry = None

try:
    from src.tools.cache import cache_get, cache_set, cache_get_negative, cache_set_negative  # type: ignore
except Exception:
    try:
        from ..tools.cache import cache_get, cache_set, cache_get_negative, cache_set_negative  # type: ignore
    except Exception:
        def cache_get(key, ttl: int | None = None, allow_negative: bool = False):
            return None

        def cache_set(key, value, ttl: int | None = None, negative: bool = False):
            return None

        def cache_get_negative(key):
            return None

        def cache_set_negative(key, value=True, ttl: int | None = None):
            return None

try:
    from src.utils.rate_limiter import get_limiter

    _rate_limiter = get_limiter("kitsu")
except Exception:
    _rate_limiter = None

logger = logging.getLogger(__name__)

KITSU_BASE = "https://kitsu.io/api/edge"
_health_cache = {"ts": 0.0, "ok": True, "status": None, "detail": None}
_EDITORIAL_BLOCK_RE = re.compile(
    r"^[\[\(\{]?\s*(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)\b",
    re.IGNORECASE,
)

# Throttling controls to avoid network saturation.
_API_CONCURRENCY = max(1, int(os.environ.get("ARCANEA_API_CONCURRENCY", "2") or 2))
_API_DELAY_SEC = max(0.0, float(os.environ.get("ARCANEA_API_DELAY_MS", "300") or 300) / 1000.0)
_api_sema = None
try:
    import threading as _threading

    _api_sema = _threading.BoundedSemaphore(_API_CONCURRENCY)
except Exception:
    _api_sema = None


def _debug_suppressed(exc: Exception) -> None:
    try:
        logger.debug("provider_kitsu: suppressed exception: %s", exc)
    except Exception:
        return


def clean_synopsis_text(text: str | None) -> str:
    try:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").strip()
    except Exception:
        raw = ""
    if not raw:
        return ""

    kept: list[str] = []
    for block in re.split(r"\n{2,}", raw):
        cleaned = re.sub(r"[ \t]+", " ", str(block or "")).strip()
        if not cleaned:
            continue
        if _EDITORIAL_BLOCK_RE.match(cleaned):
            continue
        kept.append(cleaned)

    out = "\n\n".join(kept).strip()
    out = re.sub(
        r"\s*[\[\(\{]\s*(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)\s*:[^\]\)\}\n]*(?:[\]\)\}])?\s*$",
        "",
        out,
        flags=re.IGNORECASE,
    ).strip()
    out = re.sub(
        r"\s*\[(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)[:\]\-][^\]]*\]\s*$",
        "",
        out,
        flags=re.IGNORECASE,
    ).strip()
    out = re.sub(
        r"\s*(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)\s*:\s*[^\n]+$",
        "",
        out,
        flags=re.IGNORECASE,
    ).strip()
    return out


def create_session_with_retries(
    total_retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
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
    retries = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


session = create_session_with_retries()


def healthcheck(timeout_s: float = 3.0, ttl_s: float = 20.0) -> dict:
    try:
        now = time.time()
        ts = float(_health_cache.get("ts") or 0.0)
        if (now - ts) < float(ttl_s):
            return {
                "ok": bool(_health_cache.get("ok", True)),
                "status_code": _health_cache.get("status"),
                "detail": _health_cache.get("detail"),
                "cached": True,
            }
    except Exception:
        pass

    status_code = None
    ok = False
    detail = None
    try:
        probe = _http_get(f"{KITSU_BASE}/anime/1", timeout_s=timeout_s)
        ok = isinstance(probe, dict) and bool(probe.get("data"))
        status_code = 200 if ok else None
    except Exception as e:
        ok = False
        detail = f"{type(e).__name__}: {e}"

    try:
        _health_cache["ts"] = time.time()
        _health_cache["ok"] = bool(ok)
        _health_cache["status"] = status_code
        _health_cache["detail"] = detail
    except Exception:
        pass

    return {"ok": bool(ok), "status_code": status_code, "detail": detail, "cached": False}


def _throttle_before_request() -> None:
    try:
        if _api_sema:
            _api_sema.acquire()
    except Exception as e:
        _debug_suppressed(e)

    try:
        if _rate_limiter is not None:
            _rate_limiter.before_request()
    except Exception as e:
        _debug_suppressed(e)


def _throttle_after_request() -> None:
    try:
        if _api_sema:
            _api_sema.release()
    except Exception as e:
        _debug_suppressed(e)
    if _rate_limiter is None and _API_DELAY_SEC > 0:
        time.sleep(_API_DELAY_SEC)


def _http_get(url: str, *, params: dict[str, Any] | None = None, timeout_s: float = 15.0) -> dict | None:
    _throttle_before_request()
    try:
        t0 = time.perf_counter()
        status_code = None
        try:
            r = session.get(
                url,
                params=params,
                timeout=float(timeout_s),
                headers={"Accept": "application/vnd.api+json"},
            )
            status_code = getattr(r, "status_code", None)
        except Exception as e:
            try:
                if _rate_limiter is not None:
                    _rate_limiter.record_failure(exc=e)
            except Exception:
                pass
            raise
        status_code = getattr(r, "status_code", None)
        try:
            if telemetry is not None:
                telemetry.track_event(
                    "provider_http",
                    {
                        "provider": "kitsu",
                        "url": url,
                        "status": int(status_code) if status_code is not None else None,
                        "ms": int((time.perf_counter() - t0) * 1000.0),
                    },
                )
        except Exception:
            pass
        try:
            if _rate_limiter is not None:
                if status_code is not None and int(status_code) >= 400:
                    _rate_limiter.record_failure(status_code=int(status_code))
                else:
                    _rate_limiter.record_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
        except Exception:
            pass
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return None
    finally:
        _throttle_after_request()


_rx_non_alnum = re.compile(r"[^a-z0-9]+")


def _norm(s: str | None) -> str:
    if not s:
        return ""
    try:
        ss = str(s).strip().lower()
    except Exception:
        return ""
    ss = _rx_non_alnum.sub(" ", ss)
    ss = re.sub(r"\s{2,}", " ", ss).strip()
    return ss


def _sim(a: str | None, b: str | None) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    try:
        import difflib

        return float(difflib.SequenceMatcher(None, aa, bb).ratio())
    except Exception:
        return 0.0


def _pick_best_title(attrs: dict[str, Any], query_base: str) -> tuple[str, float]:
    best_t = ""
    best_s = 0.0
    try:
        v = attrs.get("canonicalTitle")
        if isinstance(v, str) and v.strip():
            s = _sim(query_base, v)
            best_s = s
            best_t = v
    except Exception:
        pass
    titles = attrs.get("titles")
    if isinstance(titles, dict):
        for v in titles.values():
            if isinstance(v, str) and v.strip():
                s = _sim(query_base, v)
                if s > best_s:
                    best_s = s
                    best_t = v
    return best_t, float(best_s)


def search(query: str, *, limit: int = 8, timeout_s: float = 15.0) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    cache_key = f"kitsu:search:{_norm(q)}:{int(limit)}"
    try:
        cached = cache_get(cache_key, ttl=(12 * 60 * 60))
        if isinstance(cached, list):
            return cached
    except Exception as e:
        _debug_suppressed(e)

    data = _http_get(
        f"{KITSU_BASE}/anime",
        params={"filter[text]": q, "page[limit]": str(int(limit))},
        timeout_s=timeout_s,
    )
    rows = (data or {}).get("data")
    out = rows if isinstance(rows, list) else []
    try:
        cache_set(cache_key, out, ttl=(12 * 60 * 60))
    except Exception as e:
        _debug_suppressed(e)
    return out


def best_match(query: str, *, limit: int = 8, min_conf: float | None = None, timeout_s: float = 15.0) -> dict | None:
    """Return the best Kitsu anime hit for `query`, or None.

    The returned dict is a Kitsu `data` element (id + attributes). If `min_conf`
    is provided, returns None unless similarity >= min_conf.
    """
    q = str(query or "").strip()
    if not q:
        return None
    try:
        min_conf_val = float(min_conf) if min_conf is not None else None
    except Exception:
        min_conf_val = None
    rows = search(q, limit=limit, timeout_s=timeout_s)
    best: dict | None = None
    best_s = 0.0
    for it in rows:
        if not isinstance(it, dict):
            continue
        attrs = it.get("attributes") if isinstance(it.get("attributes"), dict) else {}
        _t, s = _pick_best_title(attrs, q)
        if best is None or s > best_s:
            best = it
            best_s = float(s)
    if min_conf_val is not None and best is not None and best_s < float(min_conf_val):
        return None
    return best


def fetch_by_id(kitsu_id: str, *, timeout_s: float = 15.0) -> dict | None:
    kid = str(kitsu_id or "").strip()
    if not kid:
        return None
    cache_key = f"kitsu:anime:{kid}"
    try:
        if cache_get_negative(cache_key):
            return None
    except Exception as e:
        _debug_suppressed(e)
    try:
        cached = cache_get(cache_key, ttl=(24 * 60 * 60))
        if isinstance(cached, dict):
            return cached
    except Exception as e:
        _debug_suppressed(e)

    data = _http_get(f"{KITSU_BASE}/anime/{kid}", timeout_s=timeout_s)
    if not isinstance(data, dict) or not data.get("data"):
        try:
            cache_set_negative(cache_key, True, ttl=(24 * 60 * 60))
        except Exception as e:
            _debug_suppressed(e)
        return None
    try:
        cache_set(cache_key, data, ttl=(24 * 60 * 60))
    except Exception as e:
        _debug_suppressed(e)
    return data


def fetch_by_mal_id(mal_id: str, *, timeout_s: float = 15.0) -> dict | None:
    """Resolve a MyAnimeList anime id to the exact Kitsu anime record."""
    mid = str(mal_id or "").strip()
    if not mid:
        return None
    cache_key = f"kitsu:mal:{mid}"
    try:
        if cache_get_negative(cache_key):
            return None
    except Exception as e:
        _debug_suppressed(e)
    try:
        cached = cache_get(cache_key, ttl=(24 * 60 * 60))
        if isinstance(cached, dict):
            return cached
    except Exception as e:
        _debug_suppressed(e)

    data = _http_get(
        f"{KITSU_BASE}/mappings",
        params={
            "filter[externalSite]": "myanimelist/anime",
            "filter[externalId]": mid,
            "include": "item",
            "page[limit]": "5",
        },
        timeout_s=timeout_s,
    )
    if not isinstance(data, dict):
        try:
            cache_set_negative(cache_key, True, ttl=(24 * 60 * 60))
        except Exception as e:
            _debug_suppressed(e)
        return None

    kitsu_id = None
    rows = data.get("data")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            rel = row.get("relationships")
            item = rel.get("item") if isinstance(rel, dict) else None
            item_data = item.get("data") if isinstance(item, dict) else None
            if isinstance(item_data, dict) and str(item_data.get("type") or "").lower() == "anime":
                kitsu_id = str(item_data.get("id") or "").strip() or None
                break

    included = data.get("included")
    if kitsu_id and isinstance(included, list):
        for row in included:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "").lower() == "anime" and str(row.get("id") or "") == str(kitsu_id):
                out = {"data": row}
                try:
                    cache_set(cache_key, out, ttl=(24 * 60 * 60))
                except Exception as e:
                    _debug_suppressed(e)
                return out

    if kitsu_id:
        out = fetch_by_id(kitsu_id, timeout_s=timeout_s)
        if isinstance(out, dict):
            try:
                cache_set(cache_key, out, ttl=(24 * 60 * 60))
            except Exception as e:
                _debug_suppressed(e)
            return out

    try:
        cache_set_negative(cache_key, True, ttl=(24 * 60 * 60))
    except Exception as e:
        _debug_suppressed(e)
    return None


def obtener_episodios(
    kitsu_anime_id: str,
    *,
    limit_total: int | None = None,
    page_size: int = 20,
    timeout_s: float = 15.0,
) -> list[dict]:
    kid = str(kitsu_anime_id or "").strip()
    if not kid:
        return []
    if limit_total is None:
        try:
            limit_total = int(os.environ.get("ARCANEA_KITSU_EPISODES_LIMIT", "400") or 400)
        except Exception:
            limit_total = 400
    limit_total = max(0, int(limit_total))
    cache_key = f"kitsu:episodes:{kid}:{int(limit_total)}"
    try:
        cached = cache_get(cache_key, ttl=(24 * 60 * 60))
        if isinstance(cached, list):
            return cached
    except Exception as e:
        _debug_suppressed(e)

    out: list[dict] = []
    offset = 0
    while True:
        remaining = max(0, int(limit_total) - len(out))
        if remaining <= 0:
            break
        this_page = min(int(page_size), remaining)
        data = _http_get(
            f"{KITSU_BASE}/episodes",
            params={
                "filter[mediaType]": "Anime",
                "filter[mediaId]": str(kid),
                "page[limit]": str(int(this_page)),
                "page[offset]": str(int(offset)),
                "sort": "number",
            },
            timeout_s=timeout_s,
        )
        rows = (data or {}).get("data")
        if not isinstance(rows, list) or not rows:
            break
        out.extend([r for r in rows if isinstance(r, dict)])
        offset += len(rows)
        if len(rows) < this_page:
            break

    try:
        cache_set(cache_key, out, ttl=(24 * 60 * 60))
    except Exception as e:
        _debug_suppressed(e)
    return out
