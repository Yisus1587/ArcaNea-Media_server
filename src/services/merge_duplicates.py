"""Best-effort duplicates merger invoked when IntegrityError occurs.

This module implements safe, conservative merges for a small set of common
uniqueness constraints observed in the app: duplicated `MediaItem.media_id`
and duplicated `Series.provider_id`/`Series.tmdb_id` states. The functions are
best-effort and defensive: they run inside try/except, perform minimal updates
to re-link FK references, and delete orphan duplicate rows when safe.

They are used by `db_commit.safe_commit` as a last-ditch attempt to resolve
unique-constraint conflicts so the scanner/enrichment can proceed.
"""
from typing import List
import logging

logger = logging.getLogger(__name__)

try:
    from ..db import SessionLocal
    from ..db.models import MediaItem, FileRecord, MediaMetadata, MediaImage, MediaRelation, SeasonItem, ManualMapping, ManualOverride, UserList, PlayHistory, Series, Season
except Exception:
    SessionLocal = None
    MediaItem = None


def _merge_mediaitems_by_media_id(session) -> int:
    """Find duplicate MediaItem.media_id and merge them. Returns number of merges."""
    if SessionLocal is None or MediaItem is None:
        return 0
    merged = 0
    try:
        rows = (
            session.query(MediaItem.media_id)
            .filter(MediaItem.media_id != None)
            .group_by(MediaItem.media_id)
            .having(func.count(MediaItem.id) > 1)
            .all()
        )
    except Exception:
        return 0
    try:
        mids = [r[0] for r in rows if r and r[0]]
    except Exception:
        mids = []
    for mid in mids[:20]:  # limit work
        try:
            dup_rows = session.query(MediaItem).filter(MediaItem.media_id == mid).order_by(MediaItem.id.asc()).all()
            if len(dup_rows) < 2:
                continue
            keep = dup_rows[0]
            drops = dup_rows[1:]
            keep_id = getattr(keep, 'id', None)
            if keep_id is None:
                continue
            drop_ids = [getattr(d, 'id', None) for d in drops if getattr(d, 'id', None) is not None]
            if not drop_ids:
                continue
            # move FileRecord
            try:
                session.query(FileRecord).filter(FileRecord.media_item_id.in_(drop_ids)).update({FileRecord.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            # move media metadata/images/translations/relations
            try:
                session.query(MediaMetadata).filter(MediaMetadata.media_item_id.in_(drop_ids)).update({MediaMetadata.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(MediaImage).filter(MediaImage.media_item_id.in_(drop_ids)).update({MediaImage.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(MediaRelation).filter(MediaRelation.from_item_id.in_(drop_ids)).update({MediaRelation.from_item_id: keep_id}, synchronize_session=False)
                session.query(MediaRelation).filter(MediaRelation.to_item_id.in_(drop_ids)).update({MediaRelation.to_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(SeasonItem).filter(SeasonItem.media_item_id.in_(drop_ids)).update({SeasonItem.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            # other references
            try:
                session.query(ManualMapping).filter(ManualMapping.media_item_id.in_(drop_ids)).update({ManualMapping.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(ManualOverride).filter(ManualOverride.media_item_id.in_(drop_ids)).update({ManualOverride.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(UserList).filter(UserList.media_item_id.in_(drop_ids)).update({UserList.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            try:
                session.query(PlayHistory).filter(PlayHistory.media_item_id.in_(drop_ids)).update({PlayHistory.media_item_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            # delete duplicates
            for did in drop_ids:
                try:
                    session.query(MediaItem).filter(MediaItem.id == did).delete(synchronize_session=False)
                except Exception:
                    pass
            try:
                session.commit()
                merged += 1
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                continue
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            continue
    return merged


def _merge_series_by_provider_id(session) -> int:
    if SessionLocal is None or Series is None:
        return 0
    merged = 0
    try:
        rows = (
            session.query(Series.provider_id)
            .filter(Series.provider_id != None)
            .group_by(Series.provider_id)
            .having(func.count(Series.id) > 1)
            .all()
        )
    except Exception:
        return 0
    try:
        pids = [r[0] for r in rows if r and r[0]]
    except Exception:
        pids = []
    for pid in pids[:20]:
        try:
            rows = session.query(Series).filter(Series.provider_id == pid).order_by(Series.id.asc()).all()
            if len(rows) < 2:
                continue
            keep = rows[0]
            drops = rows[1:]
            keep_id = getattr(keep, 'id', None)
            if keep_id is None:
                continue
            drop_ids = [getattr(d, 'id', None) for d in drops if getattr(d, 'id', None) is not None]
            if not drop_ids:
                continue
            # move seasons
            try:
                session.query(Season).filter(Season.series_id.in_(drop_ids)).update({Season.series_id: keep_id}, synchronize_session=False)
            except Exception:
                pass
            # delete duplicate series without seasons
            try:
                for did in drop_ids:
                    left = session.query(Season).filter(Season.series_id == did).count()
                    if left == 0:
                        session.query(Series).filter(Series.id == did).delete(synchronize_session=False)
                session.commit()
                merged += 1
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                continue
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            continue
    return merged


def attempt_merge_duplicates(session) -> int:
    """Run merge passes and return number of merge operations applied."""
    total = 0
    try:
        # defer heavy imports
        from sqlalchemy import func
    except Exception:
        return 0
    try:
        total += _merge_mediaitems_by_media_id(session)
    except Exception:
        pass
    try:
        total += _merge_series_by_provider_id(session)
    except Exception:
        pass
    return total
