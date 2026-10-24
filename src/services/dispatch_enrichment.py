"""Dispatch enrichment after a filesystem scan.

This module processes pending `MediaItem` rows grouped by configured `media_roots` in
the order they are declared. For each root, it finds pending items (no MediaMetadata)
under that root and calls `enrich_one` for each item so provider preference follows
the media_root mapping.
"""
import logging
import os
from typing import Optional
from pathlib import Path

from ..db import SessionLocal, session_scope
from ..db.models import MediaItem, MediaMetadata, MediaRoot
from ..core.config import get
from .enrichment_runner import enrich_one_serialized
from .enrichment_pipeline import enrich_items_decoupled
from ..scanner.scanner import Scanner
from .scan_state import set_state as set_scan_state, get_state as get_scan_state
from .db_commit import commit_and_report
try:
    from . import telemetry
except Exception:
    try:
        from src.services import telemetry  # type: ignore
    except Exception:
        telemetry = None

logger = logging.getLogger(__name__)


def dispatch_enrichment_by_roots(limit_per_root: Optional[int] = None, tmdb_series_only: bool = False) -> dict:
    """Dispatch enrichment for pending items grouped by configured media_roots.

    Returns a report dict with processed counts by root.
    """
    try:
        st_scan = get_scan_state()
        if (st_scan.get("status") or "").lower() == "scanning":
            logger.info("Dispatch enrichment blocked: scan in progress")
            return {"ok": False, "detail": "scan_in_progress"}
    except Exception:
        pass
    report = {'stages': [], 'total_processed': 0}
    with session_scope() as session:
        # Prefer explicit MediaRoot rows (persisted by scanner/onboarding) since those
        # represent the source of truth for scanning. Fall back to core config roots
        # only when DB has none (legacy).
        try:
            persisted = session.query(MediaRoot).all()
        except Exception:
            persisted = []
        roots_cfg = get('media_roots') or []

        def _classify_root(bname: str) -> str:
            b = bname.lower()
            if any(k in b for k in ('anime', 'animes')):
                return 'anime'
            if any(k in b for k in ('pelicula', 'peliculas', 'movie', 'movies', 'film')):
                return 'movie'
            if any(k in b for k in ('serie', 'series', 'tv', 'shows')):
                return 'tv'
            return 'other'

        root_entries = []
        if persisted:
            for pr in persisted:
                rp0 = getattr(pr, "path", None)
                if not rp0:
                    continue
                try:
                    root_path = str(Path(str(rp0)).resolve())
                except Exception:
                    root_path = str(rp0)
                rt = (getattr(pr, "type", None) or "").strip().lower() or None
                if rt not in ("anime", "movie", "tv"):
                    rt = _classify_root(Path(root_path).name)
                root_entries.append({'root': root_path, 'class': rt})
        else:
            for r in roots_cfg:
                try:
                    root_path = str(Path(r).resolve())
                except Exception:
                    root_path = str(r)
                rt = _classify_root(Path(root_path).name)
                root_entries.append({'root': root_path, 'class': rt})

        # Stage 1: run the configured anime provider first only for roots classified as 'anime'
        try:
            set_scan_state({"phase": "anime"})
        except Exception:
            pass
        stage_report = {'stage': 'anime_first', 'per_root': []}
        for entry in root_entries:
            if entry['class'] != 'anime':
                continue
            root_path = entry['root']
            # find pending items under this root (no MediaMetadata with provider)
            subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
            q = session.query(MediaItem).filter(~MediaItem.id.in_(subq)).filter((MediaItem.status == None) | ((MediaItem.status != 'ERROR') & (MediaItem.status != 'NO_MATCH')))
            like_base = str(root_path).rstrip("\\/")
            like_pattern = f"{like_base}{os.sep}%"
            q = q.filter(MediaItem.canonical_path.like(like_pattern))
            if limit_per_root:
                q = q.limit(limit_per_root)
            pending = q.all()
            pending_ids: list[int] = []
            for mi in pending:
                mid_val = getattr(mi, 'id', None)
                try:
                    pending_ids.append(int(mid_val))
                except Exception:
                    logger.warning('Skipping media_item with non-int id=%s', mid_val)
            # Don't force 'series' for anime roots; let enrichment decide (movie vs series)
            # based on files count and metadata. Downloads are parallel; DB persistence is serialized.
            batch = enrich_items_decoupled(pending_ids, providers_override=None, force_media_type=None)
            processed = int(batch.get("persisted") or 0)
            stage_report['per_root'].append({'root': root_path, 'class': entry['class'], 'pending': len(pending), 'processed': processed})
            report['total_processed'] += processed
        report['stages'].append(stage_report)

        # Stage 2: run TMDB for tv roots (optionally only series), movie/other as best-effort
        try:
            set_scan_state({"phase": "tmdb"})
        except Exception:
            pass
        stage_report = {'stage': 'tmdb_last', 'per_root': []}
        for entry in root_entries:
            if tmdb_series_only:
                if entry['class'] not in ('tv', 'series'):
                    continue
            elif entry['class'] not in ('movie', 'tv', 'other'):
                continue
            root_path = entry['root']
            subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
            # TMDB stage should also repair items that were previously enriched with the wrong provider
            # (e.g. Jikan match in a TV root) by ensuring a TMDB identity is present.
            q = (
                session.query(MediaItem)
                .filter((MediaItem.status == None) | ((MediaItem.status != 'ERROR') & (MediaItem.status != 'NO_MATCH')))
                .filter(
                    (~MediaItem.id.in_(subq))
                    | (MediaItem.tmdb_id == None)  # noqa: E711
                    | (MediaItem.tmdb_id == "")
                )
            )
            like_pattern = f"{root_path}%"
            q = q.filter(MediaItem.canonical_path.like(like_pattern))
            if limit_per_root:
                q = q.limit(limit_per_root)
            pending = q.all()
            pending_ids = []
            for mi in pending:
                mid_val = getattr(mi, 'id', None)
                try:
                    pending_ids.append(int(mid_val))
                except Exception:
                    logger.warning('Skipping media_item with non-int id=%s', mid_val)
            # choose media_type based on root class: 'movie' -> movie, 'tv' -> series, 'other' -> autodetect
            fm = 'movie' if entry['class'] == 'movie' else ('series' if entry['class'] == 'tv' else None)
            batch = enrich_items_decoupled(pending_ids, providers_override=['tmdb'], force_media_type=fm)
            processed = int(batch.get("persisted") or 0)
            stage_report['per_root'].append({'root': root_path, 'class': entry['class'], 'pending': len(pending), 'processed': processed})
            report['total_processed'] += processed
        report['stages'].append(stage_report)
        try:
            set_scan_state({"phase": "idle"})
        except Exception:
            pass

        return report


def scan_and_enrich_by_roots_sequential(limit_per_root: Optional[int] = None) -> dict:
    """For each configured media_root: run a scan (route-by-route), then run enrichment
    sequentially for that same root (Jikan first when classified as 'anime', then TMDB).

    Emits telemetry task events per-root so the frontend can show progress for the active root.
    """
    report = {"roots": [], "total_processed": 0}
    with session_scope() as session:
        try:
            persisted = session.query(MediaRoot).all()
        except Exception:
            persisted = []
        roots_cfg = get('media_roots') or []

        def _classify_root(bname: str) -> str:
            b = bname.lower()
            if any(k in b for k in ('anime', 'animes')):
                return 'anime'
            if any(k in b for k in ('pelicula', 'peliculas', 'movie', 'movies', 'film')):
                return 'movie'
            if any(k in b for k in ('serie', 'series', 'tv', 'shows')):
                return 'tv'
            return 'other'

        root_entries = []
        if persisted:
            for pr in persisted:
                rp0 = getattr(pr, "path", None)
                if not rp0:
                    continue
                try:
                    root_path = str(Path(str(rp0)).resolve())
                except Exception:
                    root_path = str(rp0)
                rt = (getattr(pr, "type", None) or "").strip().lower() or None
                if rt not in ("anime", "movie", "tv"):
                    rt = _classify_root(Path(root_path).name)
                root_entries.append({'root': root_path, 'class': rt})
        else:
            for r in roots_cfg:
                try:
                    root_path = str(Path(r).resolve())
                except Exception:
                    root_path = str(r)
                rt = _classify_root(Path(root_path).name)
                root_entries.append({'root': root_path, 'class': rt})

        for entry in root_entries:
            root_path = entry['root']
            root_class = entry.get('class')
            task_id = f"root:{root_path}"
            # telemetry context
            tokens = None
            try:
                if telemetry is not None:
                    tokens = telemetry.set_task_context(task_id, 'root')
                    telemetry.task_queued(task_id, 'root', root=root_path, class_=root_class)
                    telemetry.task_start(task_id, 'root', root=root_path, class_=root_class)
            except Exception:
                tokens = None

            # ensure media_root row exists
            try:
                mr = session.query(MediaRoot).filter(MediaRoot.path == root_path).first()
                if not mr:
                    mr = MediaRoot(path=root_path, type=root_class, source='auto')
                    session.add(mr)
                    commit_and_report(session, f"dispatch_enrich:media_root:{root_path}")
            except Exception:
                session.rollback()

            # 1) Scan this root (route-by-route)
            try:
                scanner = Scanner()
                if telemetry is not None:
                    telemetry._emit('ROOT_SCAN_START', {'root': root_path})
                scanner.scan_path(root_path)
                if telemetry is not None:
                    telemetry._emit('ROOT_SCAN_END', {'root': root_path})
            except Exception as e:
                try:
                    logger.exception('Error scanning root %s: %s', root_path, e)
                except Exception:
                    pass
                try:
                    if telemetry is not None:
                        telemetry._emit('SCAN_ERROR', {'root': root_path, 'error': str(e)})
                except Exception:
                    pass

            # 2) Enrichment for this root: Jikan first if anime, then TMDB
            processed_root = 0
            try:
                # Anime provider pass for anime roots
                if root_class == 'anime':
                    root_base = str(root_path).rstrip("\\/")
                    subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
                    q = (
                        session.query(MediaItem)
                        .filter(~MediaItem.id.in_(subq))
                        .filter((MediaItem.status == None) | ((MediaItem.status != 'ERROR') & (MediaItem.status != 'NO_MATCH')))
                        .filter(MediaItem.canonical_path.like(f"{root_base}{os.sep}%"))
                    )
                    if limit_per_root:
                        q = q.limit(limit_per_root)
                    pending = q.all()
                    for mi in pending:
                        try:
                            mid_val = getattr(mi, 'id', None)
                            try:
                                mid_int = int(mid_val) if mid_val is not None else None
                            except Exception:
                                mid_int = None
                            if mid_int is None:
                                continue
                            ok = enrich_one_serialized(mid_int, providers_override=None, force_media_type=None)
                            if ok:
                                processed_root += 1
                                report['total_processed'] += 1
                        except Exception:
                            logger.exception('Failed enriching media_item %s during anime stage', getattr(mi, 'id', None))

                # TMDB pass for all classes (repair or fill tmdb_id)
                root_base = str(root_path).rstrip("\\/")
                subq = session.query(MediaMetadata.media_item_id).filter(MediaMetadata.provider != None).filter(MediaMetadata.provider != '')
                q = (
                    session.query(MediaItem)
                    .filter((MediaItem.status == None) | ((MediaItem.status != 'ERROR') & (MediaItem.status != 'NO_MATCH')))
                    .filter(
                        (~MediaItem.id.in_(subq))
                        | (MediaItem.tmdb_id == None)  # noqa: E711
                        | (MediaItem.tmdb_id == "")
                    )
                    .filter(MediaItem.canonical_path.like(f"{root_base}{os.sep}%"))
                )
                if limit_per_root:
                    q = q.limit(limit_per_root)
                pending = q.all()
                for mi in pending:
                    try:
                        mid_val = getattr(mi, 'id', None)
                        try:
                            mid_int = int(mid_val) if mid_val is not None else None
                        except Exception:
                            mid_int = None
                        if mid_int is None:
                            continue
                        fm = 'movie' if root_class == 'movie' else ('series' if root_class == 'tv' else None)
                        ok = enrich_one_serialized(mid_int, providers_override=['tmdb'], force_media_type=fm)
                        if ok:
                            processed_root += 1
                            report['total_processed'] += 1
                    except Exception:
                        logger.exception('Failed enriching media_item %s during tmdb stage', getattr(mi, 'id', None))

            except Exception:
                logger.exception('Error during enrichment for root %s', root_path)

            report['roots'].append({'root': root_path, 'class': root_class, 'processed': processed_root})

            try:
                if telemetry is not None:
                    telemetry.task_end(task_id, 'ok', root=root_path, processed=processed_root)
            except Exception:
                pass
            try:
                if tokens and telemetry is not None:
                    telemetry.reset_task_context(tokens)
            except Exception:
                pass

        return report


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print('Dispatching enrichment by configured media_roots...')
    r = dispatch_enrichment_by_roots()
    print('Dispatch report:', r)
