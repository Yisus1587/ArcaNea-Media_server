from __future__ import annotations

from sqlalchemy.orm import Session
from ..db.models import MediaItem
from ..utils.path_hash import hash_path
from typing import Optional, Tuple, Dict, Any


def get_or_create_media_item(session: Session, canonical_path: str, defaults: Optional[Dict[str, Any]] = None) -> Tuple[MediaItem, bool]:
    """Get or create a MediaItem by canonical_path (or its hash).

    Returns tuple (instance, created).
    The function prefers exact `canonical_path` match, then `canonical_path_hash`.
    If found, updates fields from `defaults` when provided (non-destructive).
    """
    if defaults is None:
        defaults = {}
    cp = str(canonical_path or "")
    created = False
    mi = None
    try:
        mi = session.query(MediaItem).filter(MediaItem.canonical_path == cp).first()
    except Exception:
        mi = None
    if not mi:
        try:
            h = hash_path(cp)
        except Exception:
            h = None
        if h:
            try:
                mi = session.query(MediaItem).filter(MediaItem.canonical_path_hash == h).first()
            except Exception:
                mi = None

    if mi:
        # update with any provided defaults (non-destructive)
        for k, v in (defaults or {}).items():
            try:
                if getattr(mi, k, None) is None and v is not None:
                    setattr(mi, k, v)
            except Exception:
                continue
        try:
            session.add(mi)
        except Exception:
            pass
        return mi, False

    # create new
    try:
        mi = MediaItem(canonical_path=cp, **{k: v for k, v in (defaults or {}).items() if k != 'canonical_path'})
        try:
            h = hash_path(cp)
            if h:
                mi.canonical_path_hash = h
        except Exception:
            pass
        session.add(mi)
        session.flush()
        created = True
    except Exception:
        # Best-effort: try to rollback outer transaction caller may handle
        try:
            session.rollback()
        except Exception:
            pass
        raise
    return mi, created
