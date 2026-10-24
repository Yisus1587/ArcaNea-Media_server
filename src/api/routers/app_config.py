from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import hashlib
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from ...core import config as core_config
from ...db import SessionLocal, models
from ...services import media_pipeline as scan_service
from ..services.credentials import check_tmdb_connectivity
from ..admin_auth import get_required_admin_pin
from ..deps import has_valid_admin_token, is_loopback_request

router = APIRouter()


def _ensure_settings_table_exists() -> None:
    """Best-effort schema creation for `settings` so app-config can work before full migrations run."""
    try:
        # Resolve the actual sqlite file being used (can be overridden via ARCANEA_DB_URL).
        try:
            db_url = os.environ.get("ARCANEA_DB_URL", "") or ""
        except Exception:
            db_url = ""
        if not db_url:
            try:
                db_url = str(core_config.get("db_url") or "")
            except Exception:
                db_url = ""

        db_url = (db_url or "").strip().split("?", 1)[0]
        db_path: Path | None = None
        if db_url.lower().startswith("sqlite") and ":memory:" not in db_url:
            if db_url.lower().startswith("sqlite:////"):
                db_path = Path(db_url[len("sqlite:////") :])
            elif db_url.lower().startswith("sqlite:///"):
                db_path = Path(db_url[len("sqlite:///") :])
            elif db_url.lower().startswith("sqlite://"):
                db_path = Path(db_url[len("sqlite://") :])

        if db_path is None:
            candidate = Path(core_config.DATA_DIR).resolve() / "arcanea.db"
            db_path = candidate if candidate.exists() else (Path("data") / "arcanea.db")

        # Handle "/C:/" style paths if present.
        try:
            s = str(db_path)
            if len(s) >= 4 and s[0] == "/" and s[2] == ":" and (s[3] == "\\" or s[3] == "/"):
                db_path = Path(s[1:])
        except Exception:
            pass

        try:
            db_path = db_path.resolve() if not db_path.is_absolute() else db_path
        except Exception:
            pass
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_settings_updated ON settings(updated_at);")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Don't break app-config endpoints if schema ensure fails.
        return


def _json_dumps(v):
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return json.dumps(str(v))


def _json_loads(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _load_app_config_from_settings(db) -> dict:
    """Return config dict built from settings rows (top-level keys)."""
    out: dict = {}
    try:
        rows = db.query(models.Setting).all()
        for r in rows:
            k = getattr(r, "key", None)
            if not k:
                continue
            try:
                out[str(k)] = _json_loads(getattr(r, "value", "") or "")
            except Exception:
                out[str(k)] = getattr(r, "value", None)
    except Exception:
        return {}
    return out


def _write_app_config_to_settings(db, payload: dict):
    """Persist each top-level field as settings.key/value (JSON string)."""
    # Some keys are write-once or server-managed; ignore user updates once present.
    WRITE_ONCE_KEYS = {"admin_pin", "telemetry_install_id", "telemetry_token"}
    for k, v in (payload or {}).items():
        if not k:
            continue
        key = str(k)
        if key in WRITE_ONCE_KEYS:
            try:
                existing = db.query(models.Setting).filter(models.Setting.key == key).first()
                if existing and getattr(existing, "value", None):
                    continue
            except Exception:
                pass
        val = _json_dumps(v)
        existing = db.query(models.Setting).filter(models.Setting.key == key).first()
        if existing:
            existing.value = val
            try:
                existing.updated_at = __import__("datetime").datetime.utcnow()
            except Exception:
                pass
            db.add(existing)
        else:
            db.add(models.Setting(key=key, value=val))


def _ensure_telemetry_settings(db) -> tuple[str | None, bool]:
    """Ensure telemetry_install_id and telemetry_token exist if admin_pin is set.

    Returns (telemetry_token, mutated)
    """
    mutated = False
    try:
        pin_row = db.query(models.Setting).filter(models.Setting.key == "admin_pin").first()
        if not pin_row or not getattr(pin_row, "value", None):
            return None, False
        admin_pin = _json_loads(getattr(pin_row, "value", "") or "")
        admin_pin = str(admin_pin or "").strip()
        if not admin_pin:
            return None, False

        iid_row = db.query(models.Setting).filter(models.Setting.key == "telemetry_install_id").first()
        install_id = ""
        if iid_row and getattr(iid_row, "value", None):
            try:
                install_id = str(_json_loads(getattr(iid_row, "value", "") or "") or "").strip()
            except Exception:
                install_id = str(getattr(iid_row, "value", "") or "").strip()
        if not install_id:
            install_id = secrets.token_urlsafe(18)
            if iid_row:
                iid_row.value = _json_dumps(install_id)
                try:
                    iid_row.updated_at = __import__("datetime").datetime.utcnow()
                except Exception:
                    pass
                db.add(iid_row)
            else:
                db.add(models.Setting(key="telemetry_install_id", value=_json_dumps(install_id)))
            mutated = True

        tok_row = db.query(models.Setting).filter(models.Setting.key == "telemetry_token").first()
        token = ""
        if tok_row and getattr(tok_row, "value", None):
            try:
                token = str(_json_loads(getattr(tok_row, "value", "") or "") or "").strip()
            except Exception:
                token = str(getattr(tok_row, "value", "") or "").strip()
        if not token:
            # Deterministic-but-unpredictable: depends on admin_pin + per-install random seed.
            token = hashlib.sha256(f"arcanea-telemetry:{admin_pin}:{install_id}".encode("utf-8")).hexdigest()
            if tok_row:
                tok_row.value = _json_dumps(token)
                try:
                    tok_row.updated_at = __import__("datetime").datetime.utcnow()
                except Exception:
                    pass
                db.add(tok_row)
            else:
                db.add(models.Setting(key="telemetry_token", value=_json_dumps(token)))
            mutated = True

        return token, mutated
    except Exception:
        return None, False


def _normalize_app_config_langs(cfg: dict) -> dict:
    """Normalize config payload language fields (best-effort)."""
    try:
        md = cfg.get("metadata") if isinstance(cfg, dict) else None
        if isinstance(md, dict) and md.get("language"):
            md2 = dict(md)
            md2["language"] = core_config.normalize_metadata_language(md2.get("language"))
            cfg2 = dict(cfg)
            cfg2["metadata"] = md2
            return cfg2
    except Exception:
        pass
    return cfg


def _lazy_migrate_app_config_json_to_settings() -> dict:
    """If settings table is empty but app_config.json exists, import it into settings."""
    _ensure_settings_table_exists()
    cfg_path = os.path.join(core_config.DATA_DIR, "app_config.json")
    if not os.path.exists(cfg_path):
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        return {}

    db = SessionLocal()
    try:
        try:
            any_row = db.query(models.Setting).first()
        except Exception:
            any_row = None
        if any_row:
            return {}
        _write_app_config_to_settings(db, data)
        db.commit()
        return data
    except Exception:
        db.rollback()
        return {}
    finally:
        db.close()


@router.get("/api/app-config")
def get_app_config(
    request: Request,
    x_arcanea_admin_token: str | None = Header(default=None, alias="X-Arcanea-Admin-Token"),
    admin_token: str | None = None,
):
    include_sensitive = bool(
        is_loopback_request(request) or has_valid_admin_token(x_arcanea_admin_token, admin_token)
    )
    # Prefer DB settings (single source of truth).
    try:
        db = SessionLocal()
        try:
            cfg = _load_app_config_from_settings(db)
            if cfg:
                # Ensure server-managed telemetry token exists when admin pin is configured.
                token, mutated = _ensure_telemetry_settings(db)
                if mutated:
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                if token and include_sensitive:
                    cfg["telemetry_token"] = token
                # Never expose admin_pin to the frontend.
                try:
                    cfg.pop("admin_pin", None)
                    cfg.pop("telemetry_install_id", None)
                except Exception:
                    pass
                return _normalize_app_config_langs(cfg)
        finally:
            db.close()
    except Exception:
        pass

    # Lazy migrate from legacy file if needed.
    migrated = _lazy_migrate_app_config_json_to_settings()
    if migrated:
        try:
            if isinstance(migrated, dict):
                migrated = dict(migrated)
                migrated.pop("admin_pin", None)
        except Exception:
            pass
        return _normalize_app_config_langs(migrated)

    # Fallback to legacy file (compat).
    try:
        cfg_path = os.path.join(core_config.DATA_DIR, "app_config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
                try:
                    if isinstance(raw, dict):
                        raw = dict(raw)
                        raw.pop("admin_pin", None)
                except Exception:
                    pass
                return _normalize_app_config_langs(raw)
    except Exception:
        logging.getLogger(__name__).exception("Failed reading app config")
    return {}


@router.post("/api/app-config")
def save_app_config(
    payload: dict,
    request: Request,
    x_arcanea_admin_token: str | None = Header(default=None, alias="X-Arcanea-Admin-Token"),
    admin_token: str | None = None,
):
    pin_required = get_required_admin_pin()
    if pin_required:
        if not has_valid_admin_token(x_arcanea_admin_token, admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
    elif not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        payload = dict(payload or {})
        try:
            md = payload.get("metadata") or {}
            if isinstance(md, dict) and md.get("language"):
                md2 = dict(md)
                md2["language"] = core_config.normalize_metadata_language(md2.get("language"))
                payload["metadata"] = md2
        except Exception:
            pass

        cfg_path = os.path.join(core_config.DATA_DIR, "app_config.json")

        # Persist to DB settings first (source of truth).
        _ensure_settings_table_exists()
        db = SessionLocal()
        try:
            _write_app_config_to_settings(db, payload or {})
            # If admin pin was just set, ensure telemetry tokens exist immediately.
            try:
                _ensure_telemetry_settings(db)
            except Exception:
                pass
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        # Mirror to legacy file for backward compatibility with components that still read it.
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(payload or {}, fh, indent=2)

        roots = (payload or {}).get("media_roots")
        if isinstance(roots, list) and len(roots) > 0:
            db = SessionLocal()
            new_paths: list[str] = []
            try:
                # Optional mapping: { "E:\\Peliculas": "movie", "E:\\Series": "series" }
                root_types_map = {}
                try:
                    rtm = (payload or {}).get("media_root_types")
                    if isinstance(rtm, dict):
                        for k, v in rtm.items():
                            try:
                                ap0 = os.path.abspath(str(k))
                            except Exception:
                                ap0 = str(k)
                            try:
                                root_types_map[ap0] = v
                            except Exception:
                                continue
                except Exception:
                    root_types_map = {}

                def _normalize_root_type(v) -> str | None:
                    if v is None:
                        return None
                    try:
                        s = str(v).strip().lower()
                    except Exception:
                        return None
                    if not s or s in ("auto", "none", "null", "unset", "infer", "inferred"):
                        return None
                    if s in ("tv", "show", "shows"):
                        return "series"
                    if s in ("films", "film"):
                        return "movie"
                    if s not in ("movie", "series", "anime", "mixed"):
                        return None
                    return s

                for entry in roots:
                    try:
                        p = None
                        t = None
                        if isinstance(entry, dict):
                            p = entry.get("path") or entry.get("root") or entry.get("dir")
                            t = entry.get("type") or entry.get("library_type") or None
                        else:
                            p = entry
                            # allow string entries like "E:\\Peliculas|movie"
                            if isinstance(p, str) and "|" in p:
                                try:
                                    p0, t0 = p.split("|", 1)
                                    p = p0
                                    t = t0
                                except Exception:
                                    pass
                        ap = os.path.abspath(str(p))
                        # Prefer explicit mapping (even when it clears type).
                        explicit_has = ap in root_types_map
                        if explicit_has:
                            t = root_types_map.get(ap)
                        t_norm = _normalize_root_type(t)
                        exists = db.query(models.MediaRoot).filter(models.MediaRoot.path == ap).first()
                        if not exists:
                            mr = models.MediaRoot(path=ap, type=t_norm, source="manual")
                            db.add(mr)
                            new_paths.append(ap)
                        else:
                            # Update type when provided (including explicit clearing to auto/None).
                            try:
                                if explicit_has or t_norm:
                                    exists.type = t_norm
                                    db.add(exists)
                            except Exception:
                                pass
                    except Exception:
                        continue
                db.commit()
                try:
                    from .. import runtime

                    runtime.watcher_set_roots_from_db(db)
                except Exception:
                    pass
                if new_paths:
                    for p in new_paths:
                        try:
                            scan_service.enqueue_scan_path(p, pipeline=True)
                        except Exception:
                            pass
            except Exception as e:
                db.rollback()
                logging.getLogger(__name__).error(f"Error saving media roots to DB: {e}")
            finally:
                db.close()

        tmdb_check = {"tmdb_ok": False, "detail": None}
        try:
            md = (payload or {}).get("metadata") or {}
            if md.get("tmdb_api_key") or md.get("tmdb_access_token"):
                try:
                    res = check_tmdb_connectivity()
                    tmdb_check["tmdb_ok"] = bool(res.get("ok"))
                    tmdb_check["detail"] = res.get("detail")
                except Exception:
                    tmdb_check["tmdb_ok"] = False
        except Exception:
            pass

        out = payload or {}
        out.update(tmdb_check)

        try:
            md = (payload or {}).get("metadata") or {}
            if md.get("moviesProvider"):
                mp = str(md.get("moviesProvider")).strip().lower()
                if mp in ("tvdb", "omdb"):
                    logging.getLogger(__name__).warning(
                        "metadata.moviesProvider '%s' not supported; falling back to 'tmdb'", mp
                    )
                    mp = "tmdb"
                core_config.config["metadata_movies_provider"] = mp
                if md.get("animeProvider"):
                    ap = str(md.get("animeProvider")).strip().lower()
                    if ap not in ("jikan", "tmdb", "auto", "anilist", "kitsu"):
                        logging.getLogger(__name__).warning(
                            "metadata.animeProvider '%s' invalid; falling back to 'kitsu'", ap
                        )
                        ap = "kitsu"
                    core_config.config["metadata_anime_provider"] = ap
            if md.get("language"):
                core_config.config["metadata_language"] = core_config.normalize_metadata_language(md.get("language"))
            if md.get("fetchCast") is not None:
                core_config.config["fetch_cast"] = bool(md.get("fetchCast"))
            if md.get("downloadImages") is not None:
                core_config.config["download_images"] = bool(md.get("downloadImages"))
        except Exception:
            logging.getLogger(__name__).exception("Failed updating runtime metadata config")

        return out
    except Exception as e:
        logging.getLogger(__name__).exception("Failed saving app config")
        raise HTTPException(status_code=500, detail=f"Failed saving config: {str(e)}")
