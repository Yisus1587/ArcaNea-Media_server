import logging
import time
from sqlalchemy.exc import IntegrityError, OperationalError

logger = logging.getLogger(__name__)

try:
    from . import telemetry
except Exception:
    try:
        from src.services import telemetry  # type: ignore
    except Exception:
        telemetry = None
try:
    from .merge_duplicates import attempt_merge_duplicates
except Exception:
    try:
        from src.services.merge_duplicates import attempt_merge_duplicates  # type: ignore
    except Exception:
        attempt_merge_duplicates = None


def safe_commit(session, context: str | None = None) -> bool:
    """Commit with rollback and emitting telemetry on failure. Returns True on success."""
    try:
        session.commit()
        return True
    except OperationalError as e:
        # Retry on SQLite write locks (database is locked)
        msg = str(e)
        if "database is locked" in msg.lower():
            delays = [0.2, 0.5, 1.0, 2.0]
            for d in delays:
                try:
                    session.rollback()
                except Exception:
                    pass
                time.sleep(d)
                try:
                    session.commit()
                    return True
                except OperationalError as e2:
                    if "database is locked" not in str(e2).lower():
                        e = e2
                        break
                except Exception as e2:
                    e = e2
                    break
        try:
            logger.exception("DB commit failed %s: %s", context or "<unknown>", e)
        except Exception:
            pass
        try:
            session.rollback()
        except Exception:
            pass
        try:
            if telemetry is not None:
                try:
                    telemetry.emit_log('error', f"DB commit error: {e}")
                except Exception:
                    pass
                try:
                    telemetry._emit('SCAN_ERROR', {'context': str(context), 'error': str(e)})
                except Exception:
                    pass
        except Exception:
            pass
        return False
    except IntegrityError as e:
        try:
            logger.exception("DB IntegrityError committing %s: %s", context or "<unknown>", e)
        except Exception:
            pass
        try:
            session.rollback()
        except Exception:
            pass
        try:
            if telemetry is not None:
                try:
                    telemetry.emit_log('error', f"DB IntegrityError during commit: {e}")
                except Exception:
                    pass
                try:
                    telemetry._emit('duplicate.error', {'error': str(e), 'context': str(context)})
                except Exception:
                    pass
        except Exception:
            pass
        # Attempt best-effort duplicate resolution and retry once.
        merged = 0
        try:
            if attempt_merge_duplicates is not None:
                try:
                    merged = attempt_merge_duplicates(session) or 0
                except Exception:
                    merged = 0
        except Exception:
            merged = 0
        if merged:
            try:
                session.commit()
                return True
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                return False
        return False
    except Exception as e:
        try:
            logger.exception("DB commit failed %s: %s", context or "<unknown>", e)
        except Exception:
            pass
        try:
            session.rollback()
        except Exception:
            pass
        try:
            if telemetry is not None:
                try:
                    telemetry.emit_log('error', f"DB commit error: {e}")
                except Exception:
                    pass
                try:
                    telemetry._emit('SCAN_ERROR', {'context': str(context), 'error': str(e)})
                except Exception:
                    pass
        except Exception:
            pass
        return False


def commit_and_report(session, context: str | None = None) -> bool:
    """Call safe_commit and emit/report a concise telemetry/log entry on failure.
    Always returns True/False and never raises.
    """
    ok = safe_commit(session, context)
    if not ok:
        try:
            msg = f"DB commit failed for context={context}"
            try:
                logger.error(msg)
            except Exception:
                pass
            if telemetry is not None:
                try:
                    telemetry.emit_log('error', msg)
                except Exception:
                    pass
                try:
                    telemetry._emit('SCAN_ERROR', {'context': str(context), 'error': 'commit_failed'})
                except Exception:
                    pass
        except Exception:
            pass
    return ok
