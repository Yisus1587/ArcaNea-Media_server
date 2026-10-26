from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from sqlalchemy import or_

from ..core.config import get
from ..db import SessionLocal
from ..db.models import Episode, EpisodeFile, FileRecord, MediaItem, MediaRoot, Season, SeasonItem, Series
from ..services.db_commit import commit_and_report
from ..services.gdrive import gdrive_service
from ..services.local_identity import find_local_identity
from ..services.scan_state import get_state as get_scan_state
from ..services.scan_state import set_state as set_scan_state
from ..utils.media_exts import COMMON_VIDEO_EXTS, get_media_extensions, resolve_media_extensions
from ..utils.path_hash import hash_path
from ..utils.utils import normalize_folder_name_for_search

logger = logging.getLogger(__name__)


class GoogleDriveScanner:
    def __init__(self, db_session_factory=None):
        self.db_session_factory = db_session_factory or SessionLocal
        self.media_exts = set(get_media_extensions())
        self._load_roots()
    
    def _load_roots(self):
        self.roots = []
        session = self.db_session_factory()
        try:
            for row in session.query(MediaRoot).all():
                if getattr(row, 'kind', 'local') == 'gdrive' or str(row.path).startswith('gdrive://'):
                    self.roots.append(row.path)
        finally:
            session.close()
    
    def scan_path(self, path: str):
        if not path.startswith('gdrive://'):
            return
        if not gdrive_service.is_authenticated():
            logger.warning("Google Drive not authenticated")
            return
        
        folder_id = path.replace('gdrive://', '').strip('/')
        self._scan_folder(folder_id, path)
    
    def _scan_folder(self, folder_id: str, root_path: str):
        try:
            st = get_scan_state()
        except Exception:
            st = {}
        if st.get("status") != "scanning":
            set_scan_state({"status": "scanning", "phase": "scan", "current": root_path})
        
        # List video files in this folder
        files = gdrive_service.list_files(folder_id)
        if files:
            self._upsert_folder(folder_id, files, root_path)
        
        # Also scan subfolders recursively? Or just this folder
        # For now, scan this folder's files
        set_scan_state({"status": "idle", "phase": "scan_done", "current": None})
    
    def _upsert_folder(self, folder_id: str, media_files: list, root_path: str):
        canonical_folder = root_path
        session = self.db_session_factory()
        try:
            path_hash = hash_path(canonical_folder)
            media_id = hash_path(canonical_folder)
            mi = (
                session.query(MediaItem)
                .filter(or_(MediaItem.canonical_path_hash == path_hash, MediaItem.canonical_path == canonical_folder))
                .order_by(MediaItem.id.asc())
                .first()
            )
            created = False
            if mi is None:
                mi = MediaItem(media_id=media_id, canonical_path=canonical_folder, canonical_path_hash=path_hash)
                created = True
            mi.canonical_path = canonical_folder
            mi.canonical_path_hash = path_hash
            mi.media_id = mi.media_id or media_id
            mi.media_root = root_path
            mi.library_type = "other"  # Will be determined by type
            mi.media_type = "series"  # Default
            mi.base_title = "Google Drive Media"
            if created or not getattr(mi, "is_identified", False):
                mi.title = "Google Drive Media"
            if not mi.status:
                mi.status = "SCANNED"
            if getattr(mi, "is_identified", None) is None:
                mi.is_identified = False
            session.add(mi)
            session.flush()
            
            for idx, f in enumerate(media_files, start=1):
                file_id = f['id']
                file_path = f"gdrive://{file_id}"
                mtime = None
                size = int(f.get('size', 0)) if f.get('size') else None
                fr = session.query(FileRecord).filter(FileRecord.path == file_path).first()
                if fr is None:
                    fr = FileRecord(path=file_path)
                fr.media_item_id = mi.id
                fr.size = size
                fr.mtime = mtime
                fr.file_index = idx
                session.add(fr)
            
            commit_and_report(session, f"scan:{canonical_folder}")
        except Exception:
            session.rollback()
            logger.exception("Google Drive scan upsert failed for %s", canonical_folder)
            raise
        finally:
            session.close()
    
    def scan_roots_sequential(self, roots: Iterable[str] | None = None, *, start_enrichment_after_scan: bool = False):
        for root in roots or self.roots:
            self.scan_path(str(root))
