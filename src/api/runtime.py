from __future__ import annotations

import logging
import queue as _queue
import threading
import time
from typing import Iterable

from sqlalchemy.orm import Session

try:
    from ..db import SessionLocal, models
except ImportError:  # pragma: no cover
    try:
        from src.db import SessionLocal, models  # type: ignore
    except Exception:
        from db import SessionLocal, models  # type: ignore
from ..services import media_pipeline as scan_service
try:
    from ..services.scan_state import set_state as set_scan_state, get_state as get_scan_state
except ImportError:  # pragma: no cover
    try:
        from src.services.scan_state import set_state as set_scan_state, get_state as get_scan_state  # type: ignore
    except Exception:
        from services.scan_state import set_state as set_scan_state, get_state as get_scan_state  # type: ignore

_watcher_service = None
_watcher_queue: _queue.Queue | None = None
_watcher_worker_thread: threading.Thread | None = None

_watcher_stop = threading.Event()
_watcher_pause = threading.Event()


def watcher_is_paused() -> bool:
    return bool(_watcher_pause.is_set())


def watcher_pause() -> None:
    _watcher_pause.set()


def watcher_resume() -> None:
    _watcher_pause.clear()


def watcher_set_roots(paths: Iterable[str]) -> None:
    global _watcher_service
    try:
        if _watcher_service is not None:
            _watcher_service.set_roots([p for p in paths if p])
    except Exception:
        logging.getLogger(__name__).exception("watcher.set_roots failed")


def watcher_set_roots_from_db(db: Session) -> None:
    try:
        items = db.query(models.MediaRoot).all()
        # Remote roots (gdrive://...) have no filesystem to watch; local polling
        # handles them through the scan pipeline instead.
        watcher_set_roots(
            [
                str(i.path)
                for i in items
                if getattr(i, "path", None)
                and not str(i.path).startswith("gdrive://")
                and getattr(i, "kind", "local") != "gdrive"
            ]
        )
    except Exception:
        logging.getLogger(__name__).exception("watcher_set_roots_from_db failed")


def watcher_start_if_enabled() -> None:
    """Start the filesystem watcher + background worker if ARCANEA_WATCH is enabled."""
    try:
        # Env var override (explicit). If not set, fall back to DB setting `watch_enabled`.
        watch_env = __import__("os").environ.get("ARCANEA_WATCH")
        watch_mode = str(watch_env).strip().lower() if watch_env is not None else ""
    except Exception:
        watch_mode = ""

    if watch_mode:
        enabled = watch_mode in ("1", "true", "yes", "on")
    else:
        # DB fallback: enable watcher when onboarding is complete unless explicitly disabled.
        enabled = True
        try:
            db0 = SessionLocal()
            try:
                row = db0.query(models.Setting).filter(models.Setting.key == "watch_enabled").first()
                if row and getattr(row, "value", None) is not None:
                    import json as _json

                    raw = getattr(row, "value", "")
                    try:
                        v = _json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        v = raw
                    if isinstance(v, bool):
                        enabled = bool(v)
                    elif isinstance(v, str) and v.strip():
                        enabled = v.strip().lower() in ("1", "true", "yes", "on")
            finally:
                db0.close()
        except Exception:
            enabled = True

    if not enabled:
        logging.getLogger(__name__).info("Watcher disabled")
        return

    global _watcher_service, _watcher_queue, _watcher_worker_thread
    if _watcher_worker_thread and _watcher_worker_thread.is_alive():
        return

    try:
        from ..watcher.service import WatchService
    except Exception:
        logging.getLogger(__name__).exception("Watcher requested but WatchService import failed")
        return

    _watcher_stop.clear()
    _watcher_queue = _queue.Queue()
    _watcher_service = WatchService(out_queue=_watcher_queue)
    _watcher_service.start()

    def _watcher_worker() -> None:
        log = logging.getLogger(__name__)
        while not _watcher_stop.is_set():
            while _watcher_pause.is_set() and not _watcher_stop.is_set():
                time.sleep(0.25)
            try:
                item = _watcher_queue.get(timeout=0.5) if _watcher_queue is not None else None
            except Exception:
                continue
            if item is None:
                break
            try:
                if isinstance(item, dict):
                    result = scan_service.handle_watch_event(item)
                    state_path = item.get("path") or item.get("dest_path") or item.get("src_path") or item.get("action")
                else:
                    result = scan_service.handle_watch_event({"action": "changed", "path": str(item)})
                    state_path = str(item)
                try:
                    set_scan_state({"watch": {"path": str(state_path), "result": result, "ts": int(time.time())}})
                except Exception:
                    pass
            except Exception:
                log.exception("Watcher worker failed for %s", item)

    _watcher_worker_thread = threading.Thread(target=_watcher_worker, daemon=True, name="watcher-worker")
    _watcher_worker_thread.start()
    logging.getLogger(__name__).info("Watcher enabled")


def watcher_stop() -> None:
    """Stop watcher gracefully (if enabled)."""
    global _watcher_service, _watcher_queue, _watcher_worker_thread
    try:
        _watcher_stop.set()
        try:
            if _watcher_queue is not None:
                _watcher_queue.put(None)
        except Exception:
            pass
        try:
            if _watcher_service is not None:
                _watcher_service.stop()
        except Exception:
            pass
        _watcher_service = None
        _watcher_queue = None
        _watcher_worker_thread = None
    except Exception:
        logging.getLogger(__name__).exception("Failed stopping watcher")
