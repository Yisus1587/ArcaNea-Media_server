from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_

from ..db import SessionLocal
from ..db.models import FileRecord, MediaItem, MediaRoot
from ..services.db_commit import commit_and_report
from ..services.gdrive import gdrive_service
from ..services.scan_state import get_state as get_scan_state
from ..services.scan_state import set_state as set_scan_state
from ..utils.media_exts import get_media_extensions
from ..utils.path_hash import hash_path
from .filesystem_scanner import (
    _clean_title,
    _episode_number_from_filename,
    _media_id_for_path,
    _normalize_root_type,
    _season_number_from_folder,
    _search_titles_for,
    _series_slug,
    sync_series_tree,
)

logger = logging.getLogger(__name__)


def _mtime_from_iso(value: str | None) -> int | None:
    """Convert Drive's `modifiedTime` (RFC 3339) to an epoch integer."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


class GoogleDriveScanner:
    """Scan Google Drive for media, grouping by folder like the local scanner.

    Each Drive folder that directly contains videos becomes one MediaItem named after
    the folder, with Series/Season/Episode rows for `series`/`anime` roots. Without
    this, everything landed in a single "Google Drive Media" entry that enrichment
    could not identify.
    """

    MAX_FOLDERS = 500
    MAX_DEPTH = 6

    def __init__(self, db_session_factory=None):
        self.db_session_factory = db_session_factory or SessionLocal
        self.media_exts = set(get_media_extensions())
        self._load_roots()

    def _load_roots(self) -> None:
        self.roots: list[dict[str, str]] = []
        session = self.db_session_factory()
        try:
            for row in session.query(MediaRoot).all():
                if getattr(row, "kind", "local") == "gdrive" or str(row.path).startswith("gdrive://"):
                    root_type = _normalize_root_type(getattr(row, "type", None)) or "series"
                    self.roots.append({"path": str(row.path), "type": root_type})
        finally:
            session.close()

    def _root(self, path: str) -> dict[str, str]:
        for root in self.roots:
            if root["path"] == path:
                return root
        return {"path": path, "type": "series"}

    def _is_media_file(self, name: str | None, mime_type: str | None = None) -> bool:
        text = str(name or "").strip()
        if not text or text.startswith("."):
            return False
        low = text.lower()
        if any(low.endswith(ext) for ext in self.media_exts):
            return True
        return bool(mime_type and str(mime_type).startswith("video/"))

    def _drive_context(
        self,
        root: dict[str, str],
        folder_name: str,
        parent_name: str,
        folder_id: str,
    ) -> dict[str, object]:
        """Mirror of the local scanner's `_context_for_folder`, without a filesystem."""
        root_type = str(root.get("type") or "series")
        generic_season = _season_number_from_folder(folder_name)

        if root_type == "movie":
            base_title = _clean_title(folder_name)
            season_number = None
            series_key = None
            media_type = "movie"
        else:
            if generic_season is not None and parent_name:
                base_title = _clean_title(parent_name)
                season_number = int(generic_season)
            else:
                base_title = _clean_title(folder_name)
                season_number = 1
            media_type = "series" if root_type in ("series", "anime") else root_type
            # Keyed by root + title, so the season folders of one show share a Series
            # instead of getting one Series row each.
            root_hash = hash_path(str(root.get("path") or ""))[:10]
            prefix = "anime" if root_type == "anime" else "gdrive"
            series_key = f"{prefix}_{root_hash}_{_series_slug(base_title)}"

        title = base_title
        if season_number is not None and season_number > 1 and generic_season is not None:
            title = f"{base_title} - Temporada {season_number}"
        elif season_number == 0:
            title = f"{base_title} - Especiales"

        return {
            "root": str(root.get("path") or ""),
            "root_type": root_type,
            "media_type": media_type,
            "base_title": base_title,
            "title": title,
            "season_number": season_number,
            "series_key": series_key,
        }

    def scan_path(self, path: str) -> None:
        path = str(path or "").strip()
        if not path.startswith("gdrive://"):
            return
        if not gdrive_service.is_authenticated():
            logger.warning("Google Drive not authenticated; skipping scan of %s", path)
            return

        folder_id = path.replace("gdrive://", "").strip("/")
        root = self._root(path)
        try:
            state = get_scan_state()
        except Exception:
            state = {}
        if state.get("status") != "scanning":
            set_scan_state({"status": "scanning", "phase": "scan", "current": path})

        try:
            info = gdrive_service.get_file(folder_id) or {}
            root_name = str(info.get("name") or path)
            self._scan_tree(folder_id, root_name, "", 0, root)
        finally:
            set_scan_state({"status": "idle", "phase": "scan_done", "current": None})

    def _scan_tree(
        self,
        folder_id: str,
        folder_name: str,
        parent_name: str,
        depth: int,
        root: dict[str, str],
    ) -> None:
        queue: list[tuple[str, str, str, int]] = [(folder_id, folder_name, parent_name, depth)]
        seen: set[str] = set()
        visited = 0

        while queue and visited < self.MAX_FOLDERS:
            current_id, current_name, current_parent, current_depth = queue.pop(0)
            if not current_id or current_id in seen:
                continue
            seen.add(current_id)
            visited += 1

            folders, files = gdrive_service.list_folder_contents(current_id)
            video_files = [
                f for f in files if self._is_media_file(f.get("name"), f.get("mimeType"))
            ]
            if video_files:
                self._upsert_folder(current_id, current_name, current_parent, video_files, root)

            if current_depth >= self.MAX_DEPTH:
                continue
            for sub in folders:
                sub_id = str(sub.get("id") or "").strip()
                if not sub_id:
                    continue
                queue.append((sub_id, str(sub.get("name") or sub_id), current_name, current_depth + 1))

        if visited >= self.MAX_FOLDERS:
            logger.warning(
                "Google Drive scan stopped at %s folders (%s); increase MAX_FOLDERS if needed.",
                self.MAX_FOLDERS,
                root.get("path"),
            )

    def _sorted_files(self, media_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(item: dict[str, Any]):
            name = str(item.get("name") or "")
            return (Path(name).stem.lower(), name.lower())

        return sorted(media_files, key=key)

    def _upsert_folder(
        self,
        folder_id: str,
        folder_name: str,
        parent_name: str,
        media_files: list[dict[str, Any]],
        root: dict[str, str],
    ) -> None:
        canonical = f"gdrive://{folder_id}"
        ctx = self._drive_context(root, folder_name, parent_name, folder_id)
        files_sorted = self._sorted_files(media_files)
        if not files_sorted:
            return

        session = self.db_session_factory()
        try:
            if ctx["root_type"] == "movie":
                for drive_file in files_sorted:
                    self._upsert_movie_file(session, drive_file, str(root.get("path") or ""))
                commit_and_report(session, f"scan:{canonical}")
                return

            path_hash = hash_path(canonical)
            media_id = _media_id_for_path(canonical)
            mi = (
                session.query(MediaItem)
                .filter(or_(MediaItem.canonical_path_hash == path_hash, MediaItem.canonical_path == canonical))
                .order_by(MediaItem.id.asc())
                .first()
            )
            created = False
            if mi is None:
                mi = MediaItem(media_id=media_id, canonical_path=canonical, canonical_path_hash=path_hash)
                created = True
            mi.canonical_path = canonical
            mi.canonical_path_hash = path_hash
            mi.media_id = mi.media_id or media_id
            mi.media_root = str(ctx["root"])
            mi.library_type = str(ctx["root_type"])
            mi.media_type = str(ctx["media_type"])
            mi.base_title = str(ctx["base_title"])
            if created or not getattr(mi, "is_identified", False):
                mi.title = str(ctx["title"])
            if not mi.status:
                mi.status = "SCANNED"
            if getattr(mi, "is_identified", None) is None:
                mi.is_identified = False
            try:
                mi.search_titles = json.dumps(
                    _search_titles_for(str(ctx["base_title"]), folder_name), ensure_ascii=False
                )
            except Exception:
                pass
            session.add(mi)
            session.flush()

            file_rows: list[FileRecord] = []
            for idx, drive_file in enumerate(files_sorted, start=1):
                file_id = str(drive_file.get("id") or "").strip()
                if not file_id:
                    continue
                file_path = f"gdrive://{file_id}"
                size = int(drive_file["size"]) if drive_file.get("size") else None
                ep_num = _episode_number_from_filename(
                    str(drive_file.get("name") or file_id),
                    idx,
                    str(ctx.get("base_title") or folder_name),
                )
                fr = session.query(FileRecord).filter(FileRecord.path == file_path).first()
                if fr is None:
                    fr = FileRecord(path=file_path)
                fr.media_item_id = mi.id
                fr.size = size
                fr.mtime = _mtime_from_iso(drive_file.get("modifiedTime"))
                fr.file_index = ep_num
                session.add(fr)
                session.flush()
                file_rows.append(fr)

            sync_series_tree(session, mi, ctx, file_rows)
            commit_and_report(session, f"scan:{canonical}")
        except Exception:
            session.rollback()
            logger.exception("Google Drive scan upsert failed for %s", canonical)
            raise
        finally:
            session.close()

    def _upsert_movie_file(self, session, drive_file: dict[str, Any], media_root: str) -> None:
        file_id = str(drive_file.get("id") or "").strip()
        if not file_id:
            return
        canonical = f"gdrive://{file_id}"
        title = _clean_title(Path(str(drive_file.get("name") or file_id)).stem)
        path_hash = hash_path(canonical)
        media_id = _media_id_for_path(canonical)

        mi = (
            session.query(MediaItem)
            .filter(or_(MediaItem.canonical_path_hash == path_hash, MediaItem.canonical_path == canonical))
            .order_by(MediaItem.id.asc())
            .first()
        )
        created = False
        if mi is None:
            mi = MediaItem(media_id=media_id, canonical_path=canonical, canonical_path_hash=path_hash)
            created = True
        mi.canonical_path = canonical
        mi.canonical_path_hash = path_hash
        mi.media_id = mi.media_id or media_id
        mi.media_root = media_root
        mi.library_type = "movie"
        mi.media_type = "movie"
        mi.base_title = title
        if created or not getattr(mi, "is_identified", False):
            mi.title = title
        if not mi.status:
            mi.status = "SCANNED"
        if getattr(mi, "is_identified", None) is None:
            mi.is_identified = False
        try:
            mi.search_titles = json.dumps(_search_titles_for(title), ensure_ascii=False)
        except Exception:
            pass
        session.add(mi)
        session.flush()

        fr = session.query(FileRecord).filter(FileRecord.path == canonical).first()
        if fr is None:
            fr = FileRecord(path=canonical)
        fr.media_item_id = mi.id
        fr.size = int(drive_file["size"]) if drive_file.get("size") else None
        fr.mtime = _mtime_from_iso(drive_file.get("modifiedTime"))
        fr.file_index = 1
        session.add(fr)

    def scan_roots_sequential(
        self,
        roots: Iterable[str] | None = None,
        *,
        start_enrichment_after_scan: bool = False,
    ) -> None:
        targets = [str(r) for r in roots] if roots is not None else [r["path"] for r in self.roots]
        for root in targets:
            self.scan_path(root)
