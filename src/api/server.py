from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import json
import logging
import threading
from pathlib import Path

try:
    from ..db import SessionLocal, init_db
    from ..db import models
    from ..core import config as core_config
    from ..core.config import DATA_DIR
    from ..services.media_pipeline import start_enrichment_job
except ImportError:  # pragma: no cover
    from src.db import SessionLocal, init_db  # type: ignore
    from src.db import models  # type: ignore
    from src.core import config as core_config  # type: ignore
    from src.core.config import DATA_DIR  # type: ignore
    from src.services.media_pipeline import start_enrichment_job  # type: ignore
import os
try:
    from ..tools.apply_db_migrations import main as apply_db_migrations_main
except ImportError:  # pragma: no cover
    try:
        from src.tools.apply_db_migrations import main as apply_db_migrations_main  # type: ignore
    except Exception:
        from tools.apply_db_migrations import main as apply_db_migrations_main  # type: ignore

from . import runtime
from .routers import app_config as app_config_router
from .routers import admin as admin_router
from .routers import credentials as credentials_router
from .routers import enrich as enrich_router
from .routers import system as system_router
from .routers import roots as roots_router
from .routers import scan as scan_router
from .routers import fs as fs_router
from .routers import media as media_router
from .routers import manual_mapping as manual_mapping_router
from .routers import localize as localize_router
from .routers import anime_normalize as anime_normalize_router
from .routers import user_list as user_list_router
from .routers import stream as stream_router
from .routers import telemetry as telemetry_router
from .routers import data_control as data_control_router
from ..services import media_pipeline as scan_service
from ..services import telemetry

# scan state is stored in src.services.scan_state

app = FastAPI(title='ArcaNea API')
telemetry.install_logging_handler()


def _ensure_dev_file_logging() -> None:
    try:
        log_root = Path(DATA_DIR).resolve().parent
    except Exception:
        log_root = Path.cwd().resolve()
    log_dir = log_root / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    log_path = log_dir / "arcanea.log"
    try:
        root_logger = logging.getLogger()
        try:
            if root_logger.level > logging.INFO:
                root_logger.setLevel(logging.INFO)
        except Exception:
            pass
        for h in list(root_logger.handlers):
            try:
                if isinstance(h, logging.FileHandler):
                    if str(getattr(h, "baseFilename", "")) == str(log_path):
                        return
            except Exception:
                continue
        fh = logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)
    except Exception:
        pass

origins = [
    'http://localhost:9800',
    'http://127.0.0.1:9800',
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(app_config_router.router)
app.include_router(admin_router.router)
app.include_router(credentials_router.router)
app.include_router(enrich_router.router)
app.include_router(system_router.router)
app.include_router(roots_router.router)
app.include_router(scan_router.router)
app.include_router(fs_router.router)
app.include_router(media_router.router)
app.include_router(manual_mapping_router.router)
app.include_router(localize_router.router)
app.include_router(anime_normalize_router.router)
app.include_router(user_list_router.router)
app.include_router(stream_router.router)
app.include_router(telemetry_router.router)
app.include_router(data_control_router.router)

# Serve locally cached media assets (posters/backdrops) under a non-conflicting path
try:
    assets_dir = (DATA_DIR / "assets").resolve()
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media-assets", StaticFiles(directory=str(assets_dir)), name="media-assets")
except Exception:
    logging.getLogger(__name__).exception("Failed mounting /media-assets")


@app.on_event('startup')
def on_startup():
    _ensure_dev_file_logging()
    # Ensure DB schema matches current models before any SQLAlchemy queries run.
    # Without this, new columns (e.g. media_item.title_en) can cause OperationalError at runtime.
    try:
        # First create base tables (fresh DB), then apply migrations/ALTERs.
        init_db(models.Base)
        apply_db_migrations_main()
    except Exception:
        logging.getLogger(__name__).exception("DB migrations failed on startup; refusing to start.")
        raise
    # Cleanup: older runs could leave MediaItem.status='ENRICHED' even when only empty metadata rows exist.
    # Revert those to SCANNED so enrichment status is consistent.
    try:
        db_fix = SessionLocal()
        try:
            enriched_subq = db_fix.query(models.MediaMetadata.media_item_id).filter(models.MediaMetadata.provider != None).filter(models.MediaMetadata.provider != '')
            rows = db_fix.query(models.MediaItem).filter(~models.MediaItem.id.in_(enriched_subq)).filter(models.MediaItem.status == 'ENRICHED').all()
            for mi in rows:
                try:
                    mi.status = 'SCANNED'
                    db_fix.add(mi)
                except Exception:
                    continue
            try:
                db_fix.commit()
            except Exception:
                db_fix.rollback()
        finally:
            db_fix.close()
    except Exception:
        logging.getLogger(__name__).debug('Startup cleanup skipped', exc_info=True)

    # Optional: reset NO_MATCH items on startup (helps when provider outages caused false no-match).
    try:
        if str(os.environ.get('ARCANEA_RESET_NO_MATCH_ON_STARTUP', '1')).strip().lower() in ('1', 'true', 'yes'):
            db_nm = SessionLocal()
            try:
                enriched_subq = db_nm.query(models.MediaMetadata.media_item_id).filter(models.MediaMetadata.provider != None).filter(models.MediaMetadata.provider != '')
                rows = (
                    db_nm.query(models.MediaItem)
                    .filter(~models.MediaItem.id.in_(enriched_subq))
                    .filter(models.MediaItem.status == 'NO_MATCH')
                    .all()
                )
                for mi in rows:
                    mi.status = 'SCANNED'
                    db_nm.add(mi)
                try:
                    db_nm.commit()
                except Exception:
                    db_nm.rollback()
            finally:
                db_nm.close()
    except Exception:
        logging.getLogger(__name__).debug('NO_MATCH reset skipped', exc_info=True)
    # seed media_roots into DB if empty using core config defaults
    db = SessionLocal()
    roots_count = 0
    already_scanned = False
    any_root_unscanned = False
    try:
        existing = db.query(models.MediaRoot).count()
        if existing == 0:
            defaults = core_config.config.get('media_roots', [])
            def _infer_type_from_path(p: str) -> str:
                import os
                b = os.path.basename(p).lower()
                LIBRARY_RULES = {
                    'anime': ['anime', 'animes'],
                    'movie': ['pelicula', 'peliculas', 'movie', 'movies', 'film', 'films'],
                    'tv': ['serie', 'series', 'tv', 'show', 'shows'],
                }
                for k, tokens in LIBRARY_RULES.items():
                    for t in tokens:
                        if t in b:
                            return k
                return 'unknown'

            for p in defaults:
                try:
                    inferred = _infer_type_from_path(str(p))
                    mr = models.MediaRoot(path=p, type=inferred, source='auto')
                    db.add(mr)
                except Exception:
                    db.rollback()
            try:
                db.commit()
            except Exception:
                db.rollback()

        # Determine whether a prior scan has already populated the DB.
        # We treat "any FileRecord exists" as "scanned at least once".
        try:
            roots_count = int(db.query(models.MediaRoot).count() or 0)
        except Exception:
            roots_count = 0
        try:
            already_scanned = bool((db.query(models.FileRecord).count() or 0) > 0)
        except Exception:
            already_scanned = False
        try:
            # If any configured root has no FileRecord rows, treat as not fully scanned.
            roots = db.query(models.MediaRoot).all()
            for r in roots:
                try:
                    rp = str(r.path)
                except Exception:
                    rp = None
                if not rp:
                    continue
                try:
                    base = str(rp).rstrip("\\/")
                    like_prefix = f"{base}{os.sep}%"
                    exists = db.query(models.FileRecord.id).filter(models.FileRecord.path.like(like_prefix)).first()
                except Exception:
                    exists = None
                if not exists:
                    any_root_unscanned = True
                    break
        except Exception:
            any_root_unscanned = False
    finally:
        db.close()

    scan_mode = str(os.environ.get('ARCANEA_STARTUP_SCAN', '0')).strip().lower()
    enrich_mode = str(os.environ.get('ARCANEA_STARTUP_ENRICH', '0')).strip().lower()

    # Scan modes:
    # - 0/off/false/no: never scan on startup
    # - once/1/true/yes: scan only if DB looks empty (no FileRecord rows yet)
    # - always: scan on every startup
    startup_scan = False
    if scan_mode in ('always',):
        startup_scan = True
    elif scan_mode in ('once', '1', 'true', 'yes'):
        startup_scan = (not already_scanned) or any_root_unscanned

    startup_enrich = False
    if enrich_mode in ('always', '1', 'true', 'yes'):
        startup_enrich = True
    elif enrich_mode in ('once',):
        # Only start the runner if there are pending items (avoids noisy idle runs).
        startup_enrich = True

    # Start initial background scan of persisted roots (optional; disabled by default).
    if startup_scan and roots_count > 0:
        try:
            t = threading.Thread(target=scan_service.scan_all_roots, daemon=True, name='initial-scan')
            t.start()
        except Exception:
            logging.getLogger(__name__).exception('Failed to start initial scan thread')
    else:
        if roots_count <= 0:
            logging.getLogger(__name__).info('Startup scan skipped (no MediaRoot rows)')
        elif already_scanned and scan_mode in ('once', '1', 'true', 'yes'):
            logging.getLogger(__name__).info('Startup scan skipped (already scanned once)')
        else:
            logging.getLogger(__name__).info('Startup scan disabled (ARCANEA_STARTUP_SCAN=%s)', scan_mode or '0')

    # Start enrichment runner at startup if there are pending items (optional; disabled by default).
    if startup_enrich and not startup_scan:
        try:
            start_enrichment_job()
        except Exception:
            logging.getLogger(__name__).exception('Failed to start initial enrichment runner')
    else:
        logging.getLogger(__name__).info('Startup enrichment disabled (ARCANEA_STARTUP_ENRICH=0)')

    # Optional: start filesystem watcher (default on unless app_config disables it).
    try:
        watch_mode = str(os.environ.get('ARCANEA_WATCH', '')).strip().lower()
        if not watch_mode:
            watch_mode = '1'
            try:
                cfg_path = os.path.join(core_config.DATA_DIR, 'app_config.json')
                if os.path.exists(cfg_path):
                    with open(cfg_path, 'r', encoding='utf-8') as fh:
                        acfg = json.load(fh) or {}
                    md = acfg.get('metadata') or {}
                    if acfg.get('watch_enabled') is False or md.get('watchEnabled') is False:
                        watch_mode = '0'
                    elif acfg.get('watch_enabled') is True or md.get('watchEnabled') is True:
                        watch_mode = '1'
            except Exception:
                pass
        os.environ['ARCANEA_WATCH'] = watch_mode
        if watch_mode in ('1', 'true', 'yes', 'on'):
            runtime.watcher_start_if_enabled()
        else:
            logging.getLogger(__name__).info('Watcher disabled (ARCANEA_WATCH=0)')
    except Exception:
        logging.getLogger(__name__).exception('Failed starting watcher')
    # If TMDB API key is not configured, mark app-config to prompt user on first run
    try:
        tmdb_key = core_config.config.get('tmdb_api_key') or os.environ.get('TMDB_API_KEY')
        cfg_path = os.path.join(core_config.DATA_DIR, 'app_config.json')
        if not tmdb_key:
            # load existing app config, set flag
            acfg = {}
            try:
                if os.path.exists(cfg_path):
                    with open(cfg_path, 'r', encoding='utf-8') as fh:
                        acfg = json.load(fh) or {}
            except Exception:
                acfg = {}
            if not acfg.get('require_tmdb_api_key'):
                acfg['require_tmdb_api_key'] = True
                try:
                    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                    with open(cfg_path, 'w', encoding='utf-8') as fh:
                        json.dump(acfg, fh, indent=2)
                except Exception:
                    logging.getLogger(__name__).exception('Failed writing app_config to request tmdb key')
    except Exception:
        pass


@app.on_event('shutdown')
def on_shutdown():
    try:
        runtime.watcher_stop()
    except Exception:
        logging.getLogger(__name__).exception('Failed stopping watcher')



# --- Release mode: serve built frontend (Vite dist) from FastAPI ---

class _SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback.

    - Serves real files from dist (including PWA assets: sw.js, manifest, icons)
    - Falls back to index.html for client-side routes (no extension)
    - Does NOT interfere with /api, /docs, /openapi.json
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        try:
            if getattr(resp, "status_code", None) != 404:
                return resp
        except Exception:
            return resp

        try:
            req_path = (scope or {}).get("path") or ""
        except Exception:
            req_path = ""

        # Never fallback for API/docs endpoints
        if req_path.startswith("/api") or req_path.startswith("/docs") or req_path.startswith("/openapi.json"):
            return resp

        # If it's a file-like path (has an extension), keep 404
        try:
            base = os.path.basename(req_path)
            if "." in base:
                return resp
        except Exception:
            pass

        # SPA fallback
        try:
            return await super().get_response("index.html", scope)
        except Exception:
            return resp


def _maybe_mount_frontend() -> None:
    """Mount frontend dist at '/' if enabled and available.

    Enable with: ARCANEA_SERVE_FRONTEND=1
    Optional path: ARCANEA_FRONTEND_DIST=<abs path to dist>
    """
    try:
        env_val = os.environ.get("ARCANEA_SERVE_FRONTEND")
    except Exception:
        env_val = None

    try:
        dist_env = os.environ.get("ARCANEA_FRONTEND_DIST", "") or ""
    except Exception:
        dist_env = ""

    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent  # src/api/server.py -> repo root
    default_dist = (repo_root / "arcanea-media-server" / "dist").resolve()

    dist_dir = Path(dist_env).resolve() if dist_env else default_dist
    try:
        dist_exists = dist_dir.is_dir()
    except Exception:
        dist_exists = False

    if env_val is None:
        enabled = dist_exists
    else:
        enabled = str(env_val).strip().lower() in ("1", "true", "yes")

    if not enabled:
        return
    if not dist_exists:
        logging.getLogger(__name__).warning("ARCANEA_SERVE_FRONTEND enabled but dist not found: %s", dist_dir)
        return

    # Mount at the end so API routes match first.
    try:
        app.mount("/", _SPAStaticFiles(directory=str(dist_dir), html=True), name="frontend")
        logging.getLogger(__name__).info("Serving frontend from %s", dist_dir)
    except Exception:
        logging.getLogger(__name__).exception("Failed mounting frontend dist")


_maybe_mount_frontend()
