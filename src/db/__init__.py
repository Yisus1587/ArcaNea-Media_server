"""Database package: SQLAlchemy engine, session factory and init helper.

Exports:
- `engine` : SQLAlchemy engine
- `SessionLocal` : scoped session factory
- `init_db(Base)` : helper to create tables
- `models` : convenience import of models module
"""
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
import logging
import os
from ..core.config import get, DATA_DIR

logger = logging.getLogger(__name__)

# Build DB URL from config (defaults handled in core.config)
_default_db_path = (DATA_DIR / "arcanea.db").resolve()
_default_db_url = f"sqlite:///{_default_db_path.as_posix()}"
DB_URL = get('db_url') or os.environ.get('ARCANEA_DB_URL') or _default_db_url

# Create engine and session factory
_sqlite_connect_args = {"check_same_thread": False, "timeout": 30} if DB_URL.startswith('sqlite') else {}
engine = create_engine(DB_URL, connect_args=_sqlite_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def session_scope(*, commit: bool = False):
    """Context-managed DB session with optional commit and rollback on error."""
    session = SessionLocal()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        raise
    finally:
        session.close()

def _sqlite_normalize_journal_mode(v: str | None) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    allowed = {'delete', 'truncate', 'persist', 'memory', 'wal', 'off'}
    return s if s in allowed else None


def _sqlite_normalize_synchronous(v: str | None) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    allowed = {'off', 'normal', 'full', 'extra', '0', '1', '2', '3'}
    return s if s in allowed else None


if DB_URL.startswith('sqlite'):
    _jm = _sqlite_normalize_journal_mode(os.environ.get('ARCANEA_SQLITE_JOURNAL_MODE')) or 'wal'
    _sync = _sqlite_normalize_synchronous(os.environ.get('ARCANEA_SQLITE_SYNCHRONOUS'))

    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[no-redef]
        try:
            cur = dbapi_connection.cursor()
            try:
                # Enforce FK constraints across normalized tables.
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                if _jm:
                    cur.execute(f"PRAGMA journal_mode={_jm}")
                if _sync:
                    cur.execute(f"PRAGMA synchronous={_sync}")
            finally:
                cur.close()
        except Exception:
            # Avoid breaking app startup if PRAGMA is rejected for some reason.
            pass


def init_db(Base):
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        logger.exception('Failed creating DB schema')


# Expose models module for convenience imports
try:
    from . import models  # type: ignore
except Exception:
    models = None
