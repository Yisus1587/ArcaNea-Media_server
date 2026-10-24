import threading
import time
import logging
import os
from typing import Optional

from ..db import SessionLocal, session_scope
from ..db import models
from .enrichment import enrich_one, enrich_one_item
from ..core.config import get
from .enrich_state import set_state, clear_state, get_state as get_enrich_state
from .scan_state import get_state as get_scan_state
from . import enrichment as enrichment_service

from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

_running = False
_run_lock = threading.Lock()
_enrich_call_lock = threading.Lock()
_paused = threading.Event()
_stop = threading.Event()
_attempts: dict[int, int] = {}
try:
    _ENRICH_MAX_RETRIES = int(str(get("enrichment_max_retries") or "1"))
except Exception:
    _ENRICH_MAX_RETRIES = 1

# Max workers for parallel ingestion.
_ENRICH_WORKERS = max(1, int(str(os.environ.get("ARCANEA_ENRICH_WORKERS") or "4")))


def set_paused(paused: bool) -> None:
    try:
        if paused:
            _paused.set()
        else:
            _paused.clear()
        try:
            set_state({'paused': bool(_paused.is_set())})
        except Exception:
            pass
    except Exception:
        return


def is_paused() -> bool:
    try:
        return bool(_paused.is_set())
    except Exception:
        return False


def request_stop() -> None:
    try:
        _stop.set()
        try:
            set_state({'stop_requested': True})
        except Exception:
            pass
    except Exception:
        return


def clear_stop() -> None:
    try:
        _stop.clear()
        try:
            set_state({'stop_requested': False})
        except Exception:
            pass
    except Exception:
        return


def enrich_one_serialized(media_item_id: int, providers_override=None, force_media_type=None) -> bool:
    """Run enrichment for a single item.

    Important: when `providers_override` is provided (manual mapping / admin rematch),
    it must be honored end-to-end by delegating to `enrich_one`, which already supports
    provider overrides. Using `enrich_one_item` directly would ignore the override and
    silently follow global provider config.
    """
    try:
        return bool(
            enrich_one(
                int(media_item_id),
                providers_override=providers_override,
                force_media_type=force_media_type,
            )
        )
    except Exception:
        logger.exception("enrich_one_serialized failed for media_item %s", media_item_id)
        return False
    finally:
        try:
            with _run_lock:
                global _running
                _running = False
        except Exception:
            pass
        try:
            clear_state()
        except Exception:
            pass


def enrich_from_tmdb_id_serialized(
    *,
    media_item_id: int,
    tmdb_id: str,
    media_type: str | None = None,
    season_number: int | None = None,
    language: str | None = None,
    download_assets: bool | None = None,
) -> dict:
    """Run manual TMDB re-match."""
    try:
        return enrichment_service.enrich_from_tmdb_id(
            media_item_id=int(media_item_id),
            tmdb_id=str(tmdb_id),
            media_type=media_type,
            season_number=season_number,
            language=language,
            download_assets=download_assets,
        )
    except Exception as e:
        logger.exception("Manual TMDB re-match failed for media_item %s", media_item_id)
        return {"ok": False, "detail": str(e)}

def backfill_episodes_for_media_item_serialized(media_item_id: int) -> dict:
    """Backfill episode titles for one media item."""
    try:
        return enrichment_service.backfill_tmdb_episode_titles_for_media_item(int(media_item_id), manage_state=True)
    except Exception as e:
        logger.exception("Backfill episodes failed for media_item %s", media_item_id)
        return {"ok": False, "detail": str(e), "seasons_updated": 0, "episodes_updated": 0}


def _fetch_pending_ids(session, limit: Optional[int] = None):
    # Consider an item enriched only if there exists a MediaMetadata row with a non-empty provider
    subq = session.query(models.MediaMetadata.media_item_id).filter(models.MediaMetadata.provider != None).filter(models.MediaMetadata.provider != '')
    q = (
        session.query(models.MediaItem.id)
        .filter(~models.MediaItem.id.in_(subq))
        .filter(
            (models.MediaItem.status == None)  # noqa: E711
            | (
                (models.MediaItem.status != 'ERROR')
                & (models.MediaItem.status != 'NO_MATCH')
                & (models.MediaItem.status != 'MANUAL')
                & (models.MediaItem.status != 'OMITTED')
            )
        )
        .order_by(models.MediaItem.id)
    )
    if limit:
        q = q.limit(limit)
    return [r[0] for r in q.all()]


def _enrichment_worker(batch_size: int, rate_per_min: int):
    """Worker that processes pending media items in batches and respects rate limit.

    - batch_size: how many items to fetch per DB query
    - rate_per_min: approximate provider calls per minute
    """
    global _running
    session = SessionLocal()
    try:
        while True:
            if _stop.is_set():
                logger.info('Enrichment runner: stop requested, exiting')
                break

            # Pause gate
            while _paused.is_set() and not _stop.is_set():
                try:
                    set_state({'running': True, 'paused': True, 'current_step': 'paused'})
                except Exception:
                    pass
                time.sleep(0.25)
            if _stop.is_set():
                logger.info('Enrichment runner: stop requested while paused, exiting')
                break

            ids = _fetch_pending_ids(session, limit=batch_size)
            if not ids:
                logger.info('Enrichment runner: no pending items, exiting')
                break
            delay = 60.0 / max(1, rate_per_min)

            def _task(mid):
                if _stop.is_set():
                    return None
                while _paused.is_set() and not _stop.is_set():
                    time.sleep(0.25)
                if _stop.is_set():
                    return None

                try:
                    ok = bool(enrich_one_serialized(mid))
                    if not ok:
                        # enrich_one_item() returns False for provider/network failures (transient) and
                        # True when the item was handled (including true NO_MATCH cases, which the
                        # enrichment code itself persists as status='NO_MATCH').
                        #
                        # Do NOT mark provider failures as NO_MATCH here; that incorrectly turns
                        # timeouts (e.g. Jikan 504/ReadTimeout) into permanent no-match results.
                        _attempts[mid] = _attempts.get(mid, 0) + 1
                        if _attempts.get(mid, 0) >= _ENRICH_MAX_RETRIES:
                            with session_scope(commit=True) as dbx:
                                mi = dbx.query(models.MediaItem).filter(models.MediaItem.id == mid).first()
                                if mi:
                                    from typing import Any as _Any, cast as _cast
                                    mi_any = _cast(_Any, mi)
                                    # Use ERROR to indicate transient provider failure / retries exceeded.
                                    # This keeps NO_MATCH reserved for true "no metadata match".
                                    mi_any.status = "ERROR"
                                    mi_any.is_identified = False
                                    dbx.add(mi)
                except Exception:
                    logger.exception('Enrichment failed for media_item %s', mid)
                    _attempts[mid] = _attempts.get(mid, 0) + 1
                    if _attempts.get(mid, 0) >= _ENRICH_MAX_RETRIES:
                        with session_scope(commit=True) as dbx:
                            mi = dbx.query(models.MediaItem).filter(models.MediaItem.id == mid).first()
                            if mi:
                                from typing import Any as _Any, cast as _cast
                                mi_any = _cast(_Any, mi)
                                mi_any.status = "ERROR"
                                mi_any.is_identified = False
                                dbx.add(mi)
                return mid

            with ThreadPoolExecutor(max_workers=_ENRICH_WORKERS) as executor:
                futures = []
                for mid in ids:
                    if _stop.is_set():
                        break
                    futures.append(executor.submit(_task, mid))
                    # Stagger starts to respect approximate rate limit
                    time.sleep(delay)
                
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        pass
            if _stop.is_set():
                logger.info('Enrichment runner: stop requested, exiting')
                break
            # small pause between batches
            time.sleep(1)
    finally:
        session.close()
        with _run_lock:
            _running = False
        try:
            _paused.clear()
            _stop.clear()
        except Exception:
            pass
        try:
            clear_state()
        except Exception:
            pass


def start_enrichment_job(batch_size: Optional[int] = None, rate_per_min: Optional[int] = None):
    """Start the enrichment runner in a background thread if not already running.

    Reads defaults from config keys `enrichment_batch_size` and `enrichment_rate_per_min` when not provided.
    """
    global _running
    # Do not start while a scan is running to avoid DB write locks.
    try:
        st_scan = get_scan_state()
        if (st_scan.get("status") or "").lower() == "scanning":
            logger.info("Enrichment start blocked: scan in progress")
            return False
    except Exception:
        pass
    # Preflight: if there is nothing pending, don't start a runner thread. This prevents
    # repeated "idle" runs being triggered by the watcher/scan flow and avoids looping
    # finalization steps indefinitely.
    try:
        s0 = SessionLocal()
        try:
            ids0 = _fetch_pending_ids(s0, limit=1)
        finally:
            s0.close()
        if not ids0:
            return False
    except Exception:
        # If the check fails, fall back to starting (keeps behavior compatible).
        pass
    with _run_lock:
        if _running:
            # detect stale _running flag if shared enrich_state says not running
            try:
                st = get_enrich_state()
                if not st.get('running'):
                    logger.warning('Detected stale _running flag; resetting and starting new runner')
                    _running = False
                else:
                    logger.info('Enrichment runner already running')
                    return False
            except Exception:
                logger.info('Enrichment runner already running')
                return False
        _running = True
    # clear previous stop request if any
    try:
        _stop.clear()
    except Exception:
        pass
    try:
        # reflect runner start in shared enrichment state used by API
        set_state({'running': True, 'paused': bool(_paused.is_set()), 'stop_requested': False, 'current_id': None, 'current_title': None, 'current_step': 'starting'})
    except Exception:
        pass

    if batch_size is None:
        try:
            bs = int(str(get('enrichment_batch_size') or "20"))
        except Exception:
            bs = 20
    else:
        bs = int(batch_size)

    if rate_per_min is None:
        try:
            rpm = int(str(get('enrichment_rate_per_min') or "60"))
        except Exception:
            rpm = 60
    else:
        rpm = int(rate_per_min)

    t = threading.Thread(target=_enrichment_worker, args=(bs, rpm), daemon=True, name='enrichment-runner')
    t.start()
    logger.info('Started enrichment runner (batch=%s rate_per_min=%s)', bs, rpm)
    return True


def is_running():
    with _run_lock:
        return _running
