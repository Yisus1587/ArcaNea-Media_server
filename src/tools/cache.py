import os
import json
import time
import threading
import logging

# File-level lock for cache and other file operations
file_lock = threading.Lock()

# Cache configuration (file-backed TTL cache)
# Allow overriding the cache directory via config (so it can be selectable in UI).
BASE_DIR = os.path.dirname(__file__)
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
try:
    from src.core.config import config as _config
    _cfg_cache_dir = _config.get('cache_dir')
    if _cfg_cache_dir:
        CACHE_DIR = _cfg_cache_dir
except Exception:
    pass
CACHE_FILE = os.path.join(CACHE_DIR, "meta_cache.json")
CACHE_TTL = 60 * 60  # 1 hour
# Negative-cache TTL (default 24h) to avoid repeated "not found" lookups.
NEGATIVE_CACHE_TTL = 60 * 60 * 24
try:
    _neg_ttl = _config.get("negative_cache_ttl")
    if _neg_ttl:
        NEGATIVE_CACHE_TTL = int(_neg_ttl)
except Exception:
    pass


def _ensure_cache():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if not os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "w", encoding="utf-8") as cf:
                json.dump({}, cf)
    except Exception as e:
        logging.warning("No se pudo inicializar cache: %s", e)


def cache_get(key, ttl: int | None = None, allow_negative: bool = False):
    try:
        _ensure_cache()
        with open(CACHE_FILE, "r", encoding="utf-8") as cf:
            data = json.load(cf)
        item = data.get(key)
        if not item:
            return None
        if item.get("negative") and not allow_negative:
            return None
        ts = item.get("ts", 0)
        ttl_val = item.get("ttl", None)
        if ttl_val is None:
            ttl_val = CACHE_TTL if ttl is None else ttl
        if time.time() - ts > float(ttl_val or CACHE_TTL):
            return None
        return item.get("value")
    except Exception as e:
        logging.debug("Cache read error: %s", e)
        return None


def cache_set(key, value, ttl: int | None = None, negative: bool = False):
    try:
        _ensure_cache()
        with file_lock:
            with open(CACHE_FILE, "r", encoding="utf-8") as cf:
                try:
                    data = json.load(cf)
                except Exception:
                    data = {}
            item = {"ts": time.time(), "value": value}
            if ttl is not None:
                item["ttl"] = int(ttl)
            if negative:
                item["negative"] = True
            data[key] = item
            with open(CACHE_FILE, "w", encoding="utf-8") as cf:
                json.dump(data, cf, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.debug("Cache write error: %s", e)


def cache_get_negative(key):
    try:
        _ensure_cache()
        with open(CACHE_FILE, "r", encoding="utf-8") as cf:
            data = json.load(cf)
        item = data.get(key)
        if not item or not item.get("negative"):
            return None
        ts = item.get("ts", 0)
        ttl_val = item.get("ttl", NEGATIVE_CACHE_TTL) or NEGATIVE_CACHE_TTL
        if time.time() - ts > float(ttl_val):
            return None
        return item.get("value")
    except Exception:
        return None


def cache_set_negative(key, value=True, ttl: int | None = None):
    try:
        cache_set(key, value, ttl=ttl or NEGATIVE_CACHE_TTL, negative=True)
    except Exception:
        return None
