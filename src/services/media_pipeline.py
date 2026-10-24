from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_

from ..db import SessionLocal
from ..db import models
from ..scanner.scanner import Scanner
from ..utils.path_hash import hash_path
from . import telemetry
from .dispatch_enrichment import dispatch_enrichment_by_roots
from .enrich_state import set_state as set_enrich_state
from .local_identity import find_local_identity
from .scan_state import get_state as get_scan_state
from .scan_state import set_state as set_scan_state

logger = logging.getLogger(__name__)

_queue: queue.Queue[tuple[str, str | None, bool, str] | None] | None = None
_worker_thread: threading.Thread | None = None
_scan_lock = threading.Lock()
_queue_lock = threading.Lock()
_pending: set[str] = set()
_last_enqueued: dict[str, float] = {}
_task_counter = 0
_run_enrichment_when_drained = False
_enrich_thread: threading.Thread | None = None
_enrich_lock = threading.Lock()

try:
    _DEBOUNCE_SEC = float(os.environ.get("ARCANEA_SCAN_ENQUEUE_DEBOUNCE_SEC", "5") or 5)
except Exception:
    _DEBOUNCE_SEC = 5.0


def _task_id(kind: str) -> str:
    global _task_counter
    with _queue_lock:
        _task_counter += 1
        n = _task_counter
    return f"scan:{kind}:{int(time.time())}:{n}"


def _root_rows() -> list[models.MediaRoot]:
    db = SessionLocal()
    try:
        return list(db.query(models.MediaRoot).order_by(models.MediaRoot.id.asc()).all())
    finally:
        db.close()


def _root_paths() -> list[str]:
    paths: list[str] = []
    for row in _root_rows():
        raw = str(getattr(row, "path", "") or "").strip()
        if not raw:
            continue
        try:
            paths.append(str(Path(raw).resolve()))
        except Exception:
            paths.append(raw)
    return paths


def _classify_root(path: str, root_type: str | None = None) -> str:
    rt = str(root_type or "").strip().lower()
    if rt in ("anime", "movie", "tv", "series"):
        return "tv" if rt == "series" else rt
    name = Path(str(path or "")).name.lower()
    if any(k in name for k in ("anime", "animes")):
        return "anime"
    if any(k in name for k in ("pelicula", "peliculas", "movie", "movies", "film")):
        return "movie"
    if any(k in name for k in ("serie", "series", "tv", "shows", "show")):
        return "tv"
    return "other"


def _is_under_configured_root(path: Path) -> bool:
    try:
        p = str(path.resolve()).rstrip("\\/").lower()
    except Exception:
        p = str(path).rstrip("\\/").lower()
    for root in _root_paths():
        r = str(root).rstrip("\\/").lower()
        if p == r or p.startswith(r + os.sep.lower()) or p.startswith(r + "\\") or p.startswith(r + "/"):
            return True
    return False


def _best_root_for(path: str) -> str | None:
    try:
        p = str(Path(path).resolve())
    except Exception:
        p = str(path)
    best: str | None = None
    best_len = -1
    p_cmp = p.rstrip("\\/").lower()
    for root in _root_paths():
        r_cmp = str(root).rstrip("\\/").lower()
        if p_cmp == r_cmp or p_cmp.startswith(r_cmp + "\\") or p_cmp.startswith(r_cmp + "/"):
            if len(r_cmp) > best_len:
                best = root
                best_len = len(r_cmp)
    return best


def _contained_roots_for(path: str) -> list[str]:
    try:
        p_cmp = str(Path(path).resolve()).rstrip("\\/").lower()
    except Exception:
        p_cmp = str(path or "").rstrip("\\/").lower()
    out: list[str] = []
    for root in _root_paths():
        r_cmp = str(root).rstrip("\\/").lower()
        if r_cmp == p_cmp or r_cmp.startswith(p_cmp + "\\") or r_cmp.startswith(p_cmp + "/"):
            out.append(root)
    return sorted(set(out), key=lambda x: x.lower())


def _media_exts(scanner: Scanner) -> tuple[str, ...]:
    return tuple(sorted({str(e).lower() for e in getattr(scanner, "media_exts", set()) if e}))


def _is_media_file(path: str, exts: tuple[str, ...]) -> bool:
    try:
        name = os.path.basename(path)
        if name.startswith(".") or name.startswith("~$") or "@eadir" in path.lower():
            return False
        return path.lower().endswith(exts)
    except Exception:
        return False


def _iter_media_files(path: Path, exts: tuple[str, ...]) -> Iterable[str]:
    if path.is_file():
        full = str(path.resolve())
        if _is_media_file(full, exts):
            yield full
        return
    for dirpath, dirnames, filenames in os.walk(str(path)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() != "@eadir"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not _is_media_file(full, exts):
                continue
            try:
                yield str(Path(full).resolve())
            except Exception:
                yield os.path.abspath(full)


def _file_stat(path: str) -> tuple[int | None, int | None]:
    try:
        st = os.stat(path)
        return int(st.st_mtime), int(st.st_size)
    except Exception:
        return None, None


def _existing_file_records(prefix: str, *, expect_file_item_path: bool = False) -> dict[str, tuple[int | None, int | None, int | None, int | None, bool]]:
    db = SessionLocal()
    try:
        prefix_base = prefix.rstrip("\\/")
        like_prefix = f"{prefix_base}{os.sep}%"
        rows = (
            db.query(
                models.FileRecord.path,
                models.FileRecord.mtime,
                models.FileRecord.size,
                models.FileRecord.id,
                models.FileRecord.media_item_id,
                models.MediaItem.canonical_path,
            )
            .outerjoin(models.MediaItem, models.MediaItem.id == models.FileRecord.media_item_id)
            .filter(or_(models.FileRecord.path == prefix, models.FileRecord.path.like(like_prefix)))
            .yield_per(5000)
        )
        out: dict[str, tuple[int | None, int | None, int | None, int | None, bool]] = {}
        for path, mtime, size, file_id, media_item_id, media_canonical_path in rows:
            path_s = str(path)
            stale_binding = False
            if expect_file_item_path:
                stale_binding = (media_item_id is None) or (str(media_canonical_path or "") != path_s)
            out[path_s] = (
                int(mtime) if mtime is not None else None,
                int(size) if size is not None else None,
                int(file_id) if file_id is not None else None,
                int(media_item_id) if media_item_id is not None else None,
                bool(stale_binding),
            )
        return out
    finally:
        db.close()


def _delete_missing_records(stale: list[tuple[int | None, int | None]]) -> int:
    if not stale:
        return 0
    db = SessionLocal()
    removed = 0
    try:
        file_ids = [fid for fid, _mid in stale if fid is not None]
        media_ids = {mid for _fid, mid in stale if mid is not None}
        episode_ids: set[int] = set()
        if file_ids:
            for i in range(0, len(file_ids), 500):
                chunk = file_ids[i : i + 500]
                try:
                    for (episode_id,) in db.query(models.EpisodeFile.episode_id).filter(
                        models.EpisodeFile.file_record_id.in_(chunk)
                    ).all():
                        if episode_id is not None:
                            episode_ids.add(int(episode_id))
                    db.query(models.EpisodeFile).filter(models.EpisodeFile.file_record_id.in_(chunk)).delete(
                        synchronize_session=False
                    )
                except Exception:
                    pass
                removed += (
                    db.query(models.FileRecord)
                    .filter(models.FileRecord.id.in_(chunk))
                    .delete(synchronize_session=False)
                )
        # Missing episodes stay missing. We only remove Episode rows that lost all files;
        # we never renumber remaining files to close gaps.
        if episode_ids:
            orphan_episode_ids: list[int] = []
            for episode_id in sorted(episode_ids):
                still_linked = db.query(models.EpisodeFile.id).filter(models.EpisodeFile.episode_id == episode_id).first()
                if not still_linked:
                    orphan_episode_ids.append(int(episode_id))
            for i in range(0, len(orphan_episode_ids), 500):
                chunk = orphan_episode_ids[i : i + 500]
                db.query(models.Episode).filter(models.Episode.id.in_(chunk)).delete(synchronize_session=False)
        orphan_ids: list[int] = []
        for mid in media_ids:
            if mid is None:
                continue
            remaining = db.query(models.FileRecord.id).filter(models.FileRecord.media_item_id == mid).first()
            if not remaining:
                orphan_ids.append(int(mid))
        for i in range(0, len(orphan_ids), 200):
            chunk = orphan_ids[i : i + 200]
            season_ids = [
                int(r[0])
                for r in db.query(models.SeasonItem.season_id).filter(models.SeasonItem.media_item_id.in_(chunk)).all()
                if r and r[0] is not None
            ]
            if season_ids:
                season_episode_ids = [
                    int(r[0])
                    for r in db.query(models.Episode.id).filter(models.Episode.season_id.in_(season_ids)).all()
                    if r and r[0] is not None
                ]
                for j in range(0, len(season_episode_ids), 500):
                    ep_chunk = season_episode_ids[j : j + 500]
                    db.query(models.EpisodeFile).filter(models.EpisodeFile.episode_id.in_(ep_chunk)).delete(
                        synchronize_session=False
                    )
                    db.query(models.Episode).filter(models.Episode.id.in_(ep_chunk)).delete(synchronize_session=False)
                for j in range(0, len(season_ids), 500):
                    season_chunk = season_ids[j : j + 500]
                    db.query(models.SeasonItem).filter(models.SeasonItem.season_id.in_(season_chunk)).delete(
                        synchronize_session=False
                    )
                    db.query(models.Season).filter(models.Season.id.in_(season_chunk)).delete(synchronize_session=False)
            db.query(models.MediaMetadata).filter(models.MediaMetadata.media_item_id.in_(chunk)).delete(
                synchronize_session=False
            )
            db.query(models.MediaImage).filter(models.MediaImage.media_item_id.in_(chunk)).delete(
                synchronize_session=False
            )
            try:
                db.query(models.MediaRelation).filter(
                    (models.MediaRelation.from_item_id.in_(chunk)) | (models.MediaRelation.to_item_id.in_(chunk))
                ).delete(synchronize_session=False)
            except Exception:
                pass
            try:
                db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            try:
                db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            try:
                db.query(models.ManualOverride).filter(models.ManualOverride.media_item_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            db.query(models.MediaItem).filter(models.MediaItem.id.in_(chunk)).delete(synchronize_session=False)
        db.commit()
        return int(removed)
    except Exception:
        db.rollback()
        logger.exception("Failed deleting stale file records")
        return 0
    finally:
        db.close()


def _delete_orphan_media_items(prefix: str) -> int:
    db = SessionLocal()
    deleted = 0
    try:
        prefix_base = str(prefix).rstrip("\\/")
        like_prefix = f"{prefix_base}{os.sep}%"
        rows = (
            db.query(models.MediaItem.id)
            .filter(or_(models.MediaItem.canonical_path == prefix_base, models.MediaItem.canonical_path.like(like_prefix)))
            .filter(~models.MediaItem.id.in_(db.query(models.FileRecord.media_item_id).filter(models.FileRecord.media_item_id != None)))  # noqa: E711
            .all()
        )
        ids = [int(r[0]) for r in rows if r and r[0] is not None]
        if not ids:
            return 0
        season_ids = [
            int(r[0])
            for r in db.query(models.SeasonItem.season_id).filter(models.SeasonItem.media_item_id.in_(ids)).all()
            if r and r[0] is not None
        ]
        episode_ids: list[int] = []
        if season_ids:
            episode_ids = [
                int(r[0])
                for r in db.query(models.Episode.id).filter(models.Episode.season_id.in_(season_ids)).all()
                if r and r[0] is not None
            ]
        for i in range(0, len(episode_ids), 500):
            chunk = episode_ids[i : i + 500]
            db.query(models.EpisodeFile).filter(models.EpisodeFile.episode_id.in_(chunk)).delete(synchronize_session=False)
            db.query(models.Episode).filter(models.Episode.id.in_(chunk)).delete(synchronize_session=False)
        for i in range(0, len(season_ids), 500):
            chunk = season_ids[i : i + 500]
            db.query(models.SeasonItem).filter(models.SeasonItem.season_id.in_(chunk)).delete(synchronize_session=False)
            db.query(models.Season).filter(models.Season.id.in_(chunk)).delete(synchronize_session=False)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            db.query(models.MediaMetadata).filter(models.MediaMetadata.media_item_id.in_(chunk)).delete(synchronize_session=False)
            db.query(models.MediaImage).filter(models.MediaImage.media_item_id.in_(chunk)).delete(synchronize_session=False)
            try:
                db.query(models.MediaRelation).filter(
                    (models.MediaRelation.from_item_id.in_(chunk)) | (models.MediaRelation.to_item_id.in_(chunk))
                ).delete(synchronize_session=False)
            except Exception:
                pass
            try:
                db.query(models.MediaTranslation).filter(models.MediaTranslation.path_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            try:
                db.query(models.ManualMapping).filter(models.ManualMapping.media_item_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            try:
                db.query(models.ManualOverride).filter(models.ManualOverride.media_item_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            except Exception:
                pass
            deleted += db.query(models.MediaItem).filter(models.MediaItem.id.in_(chunk)).delete(synchronize_session=False)
        db.commit()
        return int(deleted)
    except Exception:
        db.rollback()
        logger.exception("Failed deleting orphan media items under %s", prefix)
        return 0
    finally:
        db.close()


def _delete_orphan_episodes(prefix: str) -> int:
    db = SessionLocal()
    deleted = 0
    try:
        prefix_base = str(prefix).rstrip("\\/")
        like_prefix = f"{prefix_base}{os.sep}%"
        rows = (
            db.query(models.Episode.id)
            .join(models.Season, models.Season.id == models.Episode.season_id)
            .join(models.SeasonItem, models.SeasonItem.season_id == models.Season.id)
            .join(models.MediaItem, models.MediaItem.id == models.SeasonItem.media_item_id)
            .filter(or_(models.MediaItem.canonical_path == prefix_base, models.MediaItem.canonical_path.like(like_prefix)))
            .filter(~models.Episode.id.in_(db.query(models.EpisodeFile.episode_id)))
            .all()
        )
        ids = [int(r[0]) for r in rows if r and r[0] is not None]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            deleted += db.query(models.Episode).filter(models.Episode.id.in_(chunk)).delete(synchronize_session=False)
        db.commit()
        return int(deleted)
    except Exception:
        db.rollback()
        logger.exception("Failed deleting orphan episodes under %s", prefix)
        return 0
    finally:
        db.close()


def _normalize_event_path(path: str) -> str:
    try:
        return str(Path(str(path)).resolve())
    except Exception:
        return os.path.abspath(str(path))


def _cleanup_missing_paths(paths: Iterable[str]) -> dict:
    normalized_paths = [_normalize_event_path(p) for p in paths if str(p or "").strip()]
    if not normalized_paths:
        return {"status": "empty", "removed": 0, "removed_items": 0, "removed_episodes": 0}

    stale: list[tuple[int | None, int | None]] = []
    prefixes: set[str] = set()
    skipped: list[str] = []

    db = SessionLocal()
    try:
        for raw in normalized_paths:
            p = Path(raw)
            if not _is_under_configured_root(p):
                contained = _contained_roots_for(raw)
                if not contained:
                    skipped.append(raw)
                    continue
                prefixes.update(contained)
                continue

            prefix_base = raw.rstrip("\\/")
            like_prefix = f"{prefix_base}{os.sep}%"
            rows = (
                db.query(models.FileRecord.id, models.FileRecord.media_item_id)
                .filter(or_(models.FileRecord.path == prefix_base, models.FileRecord.path.like(like_prefix)))
                .all()
            )
            stale.extend(
                (
                    int(file_id) if file_id is not None else None,
                    int(media_item_id) if media_item_id is not None else None,
                )
                for file_id, media_item_id in rows
            )
            prefixes.add(prefix_base)
    finally:
        db.close()

    removed = _delete_missing_records(stale)
    removed_items = 0
    removed_episodes = 0
    for prefix in sorted(prefixes, key=len, reverse=True):
        removed_items += _delete_orphan_media_items(prefix)
        removed_episodes += _delete_orphan_episodes(prefix)
    if removed or removed_items or removed_episodes:
        _update_titles_total()
        set_scan_state(
            {
                "removed_files": removed,
                "removed_items": removed_items,
                "removed_episodes": removed_episodes,
                "phase": "scan_done",
            }
        )
    return {
        "status": "ok",
        "removed": int(removed),
        "removed_items": int(removed_items),
        "removed_episodes": int(removed_episodes),
        "skipped": skipped,
    }


def _scan_target_for_watch_path(path: str, *, is_directory: bool | None = None) -> str:
    normalized = _normalize_event_path(path)
    p = Path(normalized)
    if is_directory is True:
        return normalized
    if p.exists() and p.is_file():
        return str(p.parent.resolve())
    if p.exists() and p.is_dir():
        return normalized
    scanner = Scanner(discovery_only=False)
    if _is_media_file(normalized, _media_exts(scanner)):
        return str(p.parent)
    return normalized


def handle_watch_event(event: dict[str, Any]) -> dict:
    action = str(event.get("action") or "changed").strip().lower()
    if action in ("missing_batch", "deleted"):
        paths = event.get("paths") if action == "missing_batch" else [event.get("path")]
        if not isinstance(paths, list):
            paths = []
        return _cleanup_missing_paths([str(p) for p in paths if p])

    if action == "moved":
        src = event.get("src_path") or event.get("from") or event.get("old_path")
        dest = event.get("dest_path") or event.get("to") or event.get("path")
        cleanup = _cleanup_missing_paths([str(src)]) if src else {"status": "empty"}
        queued = {"status": "empty"}
        if dest:
            queued = enqueue_scan_path(
                _scan_target_for_watch_path(str(dest), is_directory=bool(event.get("is_directory"))),
                pipeline=True,
            )
        return {"status": "ok", "action": "moved", "cleanup": cleanup, "queued": queued}

    path = event.get("path") or event.get("dest_path")
    if not path:
        return {"status": "bad_request", "detail": "path_required"}
    return enqueue_scan_path(
        _scan_target_for_watch_path(str(path), is_directory=bool(event.get("is_directory")) if "is_directory" in event else None),
        pipeline=True,
    )


def _update_titles_total() -> None:
    db = SessionLocal()
    try:
        set_scan_state({"titles_total": int(db.query(models.MediaItem).count())})
    except Exception:
        pass
    finally:
        db.close()


def _scan_path_incremental(path: str, *, task_id: str | None = None) -> dict:
    scanner = Scanner(discovery_only=False)
    exts = _media_exts(scanner)
    p = Path(str(path)).resolve()
    if not _is_under_configured_root(p):
        contained_roots = _contained_roots_for(str(p))
        if not contained_roots:
            return {"status": "not_under_roots", "detail": str(p)}
        changed_total = 0
        removed_total = 0
        results = []
        for root_path in contained_roots:
            res = _scan_path_incremental(root_path, task_id=task_id)
            results.append(res)
            try:
                changed_total += int(res.get("changed") or 0)
                removed_total += int(res.get("removed") or 0)
            except Exception:
                pass
        return {
            "status": "ok",
            "path": str(p),
            "expanded_roots": contained_roots,
            "changed": changed_total,
            "removed": removed_total,
            "results": results,
        }

    root = _best_root_for(str(p)) or str(p)
    prefix = str(p)
    root_kind = _classify_root(root)
    existing = _existing_file_records(prefix, expect_file_item_path=(root_kind == "movie"))
    files = list(_iter_media_files(p, exts))
    seen: set[str] = set()
    changed_by_folder: dict[str, list[str]] = defaultdict(list)
    files_by_folder: dict[str, list[str]] = defaultdict(list)

    force_full = str(os.environ.get("ARCANEA_SCAN_MODE", "")).strip().lower() in ("full", "all", "force")

    for full in files:
        seen.add(full)
        folder_key = str(Path(full).parent)
        files_by_folder[folder_key].append(full)
        mtime, size = _file_stat(full)
        prev = existing.get(full)
        stale_binding = bool(prev[4]) if prev is not None and len(prev) > 4 else False
        if (not force_full) and prev is not None and (not stale_binding) and prev[0] == mtime and prev[1] == size:
            continue
        changed_by_folder[folder_key].append(full)

    # Sidecar identities (id_mal.json, tmdb_id.txt, etc.) are local hints, not media files.
    # Re-apply folders that contain them even when video mtimes did not change.
    for folder, folder_files in files_by_folder.items():
        if folder in changed_by_folder:
            continue
        try:
            if find_local_identity(folder):
                changed_by_folder[folder].extend(folder_files)
        except Exception:
            pass

    stale = [(info[2], info[3]) for file_path, info in existing.items() if file_path not in seen]

    total = sum(len(v) for v in changed_by_folder.values()) + len(stale)
    payload = {
        "status": "scanning",
        "phase": "scan",
        "total": int(total),
        "processed": 0,
        "current": str(p),
        "started_at": int(time.time()),
        "finished_at": None,
        "task_id": task_id,
        "task_kind": "path",
        "media_root": root,
    }
    set_scan_state(payload)

    processed = 0
    try:
        for folder, changed_files in changed_by_folder.items():
            set_scan_state({"current": folder})
            scanner._upsert_folder(folder, changed_files)
            processed += len(changed_files)
            set_scan_state({"processed": processed})
        removed = _delete_missing_records(stale)
        orphan_items = _delete_orphan_media_items(prefix)
        orphan_episodes = _delete_orphan_episodes(prefix)
        if stale:
            processed += len(stale)
            set_scan_state({"processed": processed, "removed_files": removed})
        if orphan_items:
            set_scan_state({"removed_items": orphan_items})
        if orphan_episodes:
            set_scan_state({"removed_episodes": orphan_episodes})
        set_scan_state(
            {
                "status": "idle",
                "phase": "scan_done",
                "current": None,
                "processed": processed,
                "finished_at": int(time.time()),
                "task_id": None,
                "task_kind": None,
            }
        )
        _update_titles_total()
        return {
            "status": "ok",
            "path": str(p),
            "changed": sum(len(v) for v in changed_by_folder.values()),
            "removed": len(stale),
            "removed_items": orphan_items,
            "removed_episodes": orphan_episodes,
        }
    except Exception as exc:
        logger.exception("scan failed for %s", p)
        set_scan_state({"status": "idle", "phase": "scan_done", "current": None, "finished_at": int(time.time())})
        return {"status": "error", "detail": str(exc), "path": str(p)}


def _has_enrichment_work() -> bool:
    db = SessionLocal()
    try:
        roots = list(db.query(models.MediaRoot).order_by(models.MediaRoot.id.asc()).all())
        if not roots:
            return False
        metadata_subq = (
            db.query(models.MediaMetadata.media_item_id)
            .filter(models.MediaMetadata.provider != None)  # noqa: E711
            .filter(models.MediaMetadata.provider != "")
        )
        runnable_status = (
            (models.MediaItem.status == None)  # noqa: E711
            | (
                (models.MediaItem.status != "ERROR")
                & (models.MediaItem.status != "NO_MATCH")
                & (models.MediaItem.status != "MANUAL")
                & (models.MediaItem.status != "OMITTED")
            )
        )
        for root in roots:
            root_path = str(getattr(root, "path", "") or "").strip().rstrip("\\/")
            if not root_path:
                continue
            root_class = _classify_root(root_path, getattr(root, "type", None))
            like_pattern = f"{root_path}{os.sep}%"
            base_q = (
                db.query(models.MediaItem.id)
                .filter(runnable_status)
                .filter(or_(models.MediaItem.canonical_path == root_path, models.MediaItem.canonical_path.like(like_pattern)))
            )
            if root_class == "anime":
                row = base_q.filter(~models.MediaItem.id.in_(metadata_subq)).first()
            else:
                row = (
                    base_q.filter(
                        (~models.MediaItem.id.in_(metadata_subq))
                        | (models.MediaItem.tmdb_id == None)  # noqa: E711
                        | (models.MediaItem.tmdb_id == "")
                    )
                    .first()
                )
            if row is not None:
                return True
        return False
    except Exception:
        logger.exception("Failed checking enrichment work")
        return True
    finally:
        db.close()


def start_enrichment_job() -> bool:
    global _enrich_thread
    try:
        st = get_scan_state()
        if st.get("status") in ("scanning", "queued"):
            return False
        if str(st.get("phase") or "").lower() in ("scan", "normalize"):
            return False
    except Exception:
        pass
    with _queue_lock:
        if _pending:
            return False
    if not _has_enrichment_work():
        return False
    with _enrich_lock:
        if _enrich_thread is not None and _enrich_thread.is_alive():
            return False

        def _worker() -> None:
            try:
                set_scan_state({"phase": "enrich"})
                set_enrich_state({"running": True, "current_step": "dispatch", "current_id": None, "current_title": None})
                report = dispatch_enrichment_by_roots(tmdb_series_only=False)
                logger.info("media pipeline enrichment report: %s", report)
            except Exception:
                logger.exception("media pipeline enrichment failed")
            finally:
                try:
                    set_enrich_state({"running": False, "current_step": "idle", "current_id": None, "current_title": None})
                    set_scan_state({"phase": "idle"})
                except Exception:
                    pass

        _enrich_thread = threading.Thread(target=_worker, daemon=True, name="media-pipeline-enrich")
        _enrich_thread.start()
        return True


def _ensure_worker() -> None:
    global _queue, _worker_thread, _run_enrichment_when_drained
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _queue = queue.Queue()

    def _worker() -> None:
        global _run_enrichment_when_drained
        while True:
            try:
                item = _queue.get(timeout=0.5) if _queue is not None else None
            except queue.Empty:
                continue
            if item is None:
                break
            kind, path, pipeline, task_id = item
            pending_key = "__ALL__" if kind == "all" else str(path or "")
            try:
                with _queue_lock:
                    _pending.discard(pending_key)
                    queued = len(_pending)
                set_scan_state({"queued": queued, "task_id": task_id, "task_kind": kind})
            except Exception:
                pass
            try:
                telemetry.task_start(task_id, kind, path=path)
            except Exception:
                pass
            ok = True
            try:
                if kind == "all":
                    scan_all_roots(task_id=task_id, start_enrichment=False)
                else:
                    scan_single_path(str(path or ""), task_id=task_id)
            except Exception:
                ok = False
                logger.exception("Queued scan failed kind=%s path=%s", kind, path)
            finally:
                try:
                    telemetry.task_end(task_id, "ok" if ok else "error")
                except Exception:
                    pass
                with _queue_lock:
                    should_enrich = bool(_run_enrichment_when_drained and not _pending)
                    if should_enrich:
                        _run_enrichment_when_drained = False
                if should_enrich or bool(pipeline and not _pending):
                    started = start_enrichment_job()
                    if not started:
                        set_scan_state({"phase": "idle"})
                elif not _pending:
                    set_scan_state({"status": "idle", "queued": 0, "phase": "idle"})

    _worker_thread = threading.Thread(target=_worker, daemon=True, name="media-pipeline-scan")
    _worker_thread.start()


def enqueue_scan_path(path: str, pipeline: bool = True) -> dict:
    global _run_enrichment_when_drained
    if not path:
        return {"status": "bad_request", "detail": "path_required"}
    try:
        p = str(Path(str(path)).resolve())
    except Exception:
        p = str(path)
    now = time.time()
    with _queue_lock:
        last = _last_enqueued.get(p)
        if last is not None and (now - last) < _DEBOUNCE_SEC:
            return {"status": "debounced", "path": p, "cooldown": _DEBOUNCE_SEC}
        _last_enqueued[p] = now
        if p in _pending:
            return {"status": "queued", "path": p, "deduped": True}
        _pending.add(p)
        if pipeline:
            _run_enrichment_when_drained = True
        queued = len(_pending)
    _ensure_worker()
    task_id = _task_id("path")
    try:
        telemetry.task_queued(task_id, "path", path=p)
    except Exception:
        pass
    if _queue is None:
        with _queue_lock:
            _pending.discard(p)
        return {"status": "error", "detail": "queue_unavailable", "path": p}
    _queue.put(("path", p, bool(pipeline), task_id))
    set_scan_state({"status": "queued", "queued": queued})
    return {"status": "queued", "path": p, "task_id": task_id}


def enqueue_scan_all_roots() -> dict:
    global _run_enrichment_when_drained
    paths = _root_paths()
    if not paths:
        return {"status": "empty", "detail": "no_media_roots"}
    _ensure_worker()
    with _queue_lock:
        if "__ALL__" in _pending:
            return {"status": "queued", "queued": len(_pending), "deduped": True}
        _pending.add("__ALL__")
        _run_enrichment_when_drained = True
        queued = len(_pending)
    task_id = _task_id("all")
    try:
        telemetry.task_queued(task_id, "all")
    except Exception:
        pass
    if _queue is None:
        with _queue_lock:
            _pending.discard("__ALL__")
        return {"status": "error", "detail": "queue_unavailable"}
    _queue.put(("all", None, True, task_id))
    set_scan_state({"status": "queued", "queued": queued})
    return {"status": "queued", "queued": queued, "added": len(paths), "task_id": task_id}


def clear_scan_queue() -> dict:
    cleared = 0
    try:
        if _queue is not None:
            while True:
                try:
                    _queue.get_nowait()
                    cleared += 1
                except queue.Empty:
                    break
    except Exception:
        pass
    with _queue_lock:
        _pending.clear()
    set_scan_state({"queued": 0})
    return {"ok": True, "cleared": cleared}


def clear_pending_path(path: str) -> bool:
    try:
        p = str(Path(str(path)).resolve())
    except Exception:
        p = str(path)
    with _queue_lock:
        existed = p in _pending
        _pending.discard(p)
        queued = len(_pending)
    if existed:
        set_scan_state({"queued": queued})
    return existed


def scan_single_path(path: str, task_id: str | None = None) -> dict:
    if not path:
        return {"status": "bad_request", "detail": "path_required"}
    with _scan_lock:
        return _scan_path_incremental(path, task_id=task_id)


def scan_path_with_pipeline(path: str, task_id: str | None = None) -> dict:
    result = scan_single_path(path, task_id=task_id)
    if isinstance(result, dict) and result.get("status") == "ok":
        start_enrichment_job()
    return result


def scan_all_roots(task_id: str | None = None, *, start_enrichment: bool = True) -> None:
    roots = _root_paths()
    with _scan_lock:
        total_changed = 0
        total_removed = 0
        set_scan_state(
            {
                "status": "scanning",
                "phase": "scan",
                "total": 0,
                "processed": 0,
                "current": None,
                "started_at": int(time.time()),
                "finished_at": None,
                "task_id": task_id,
                "task_kind": "all",
            }
        )
        for root in roots:
            result = _scan_path_incremental(root, task_id=task_id)
            try:
                total_changed += int(result.get("changed") or 0)
                total_removed += int(result.get("removed") or 0)
            except Exception:
                pass
        set_scan_state(
            {
                "status": "idle",
                "phase": "scan_done",
                "current": None,
                "finished_at": int(time.time()),
                "task_id": None,
                "task_kind": None,
                "changed_files": total_changed,
                "removed_files": total_removed,
            }
        )
        _update_titles_total()
    if start_enrichment:
        start_enrichment_job()
