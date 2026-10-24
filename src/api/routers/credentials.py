from __future__ import annotations

import json
import logging
import os
import threading

from fastapi import APIRouter, HTTPException, Depends

from ...core import config as core_config
from ...services.dispatch_enrichment import dispatch_enrichment_by_roots
from ...services.enrichment_runner import start_enrichment_job
from ..services.credentials import check_tmdb_connectivity
from ..deps import admin_required

router = APIRouter()


def _ensure_settings_table_exists() -> None:
    """Best-effort schema creation for `settings` so we can persist credentials."""
    try:
        import sqlite3
        from pathlib import Path

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
            db_path = Path(core_config.DATA_DIR).resolve() / "arcanea.db"

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
        return


def _persist_tmdb_credentials_to_settings(tmdb_key, tmdb_token, tmdb_use_v4) -> None:
    """Persist credentials into SQLite settings (single source of truth)."""
    try:
        from datetime import datetime

        from ...db import SessionLocal, models

        _ensure_settings_table_exists()
        db = SessionLocal()
        try:
            def _upsert(key: str, value_obj) -> None:
                raw = json.dumps(value_obj, ensure_ascii=False)
                row = db.query(models.Setting).filter(models.Setting.key == key).first()
                if row:
                    row.value = raw
                    try:
                        row.updated_at = datetime.utcnow()
                    except Exception:
                        pass
                    db.add(row)
                else:
                    db.add(models.Setting(key=key, value=raw))

            if isinstance(tmdb_key, str) and tmdb_key.strip():
                _upsert("tmdb_api_key", tmdb_key.strip())
            if isinstance(tmdb_token, str) and tmdb_token.strip():
                _upsert("tmdb_access_token", tmdb_token.strip())
            if tmdb_use_v4 is not None:
                _upsert("tmdb_use_v4", bool(tmdb_use_v4))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception:
        return


def _run_tmdb_repair_background() -> None:
    """Repair TV/Movie roots that were previously enriched with Jikan due to missing TMDB creds."""
    try:
        def _worker():
            try:
                logging.getLogger(__name__).info("Starting TMDB repair dispatch after credentials save")
                report = dispatch_enrichment_by_roots()
                logging.getLogger(__name__).info("TMDB repair dispatch finished: %s", report)
            except Exception:
                logging.getLogger(__name__).exception("TMDB repair dispatch failed")

        threading.Thread(target=_worker, daemon=True, name="tmdb-repair-dispatch").start()
    except Exception:
        return


@router.post("/api/credentials")
def save_credentials(payload: dict, _: bool = Depends(admin_required)):
    """Save provider credentials (tmdb key/token) from frontend into app_config.json and runtime config.

    Payload example: {"tmdb_api_key": "xxx", "tmdb_access_token": "yyy", "tmdb_use_v4": true}
    """
    try:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        tmdb_key = payload.get("tmdb_api_key")
        tmdb_token = payload.get("tmdb_access_token")
        tmdb_use_v4 = payload.get("tmdb_use_v4")

        if isinstance(tmdb_token, str):
            tt = tmdb_token.strip()
            if tt.lower().startswith("bearer "):
                tmdb_token = tt[7:].strip()

        try:
            if isinstance(tmdb_key, str):
                core_config.config["tmdb_api_key"] = tmdb_key
                os.environ["TMDB_API_KEY"] = tmdb_key
            if isinstance(tmdb_token, str):
                core_config.config["tmdb_access_token"] = tmdb_token
                os.environ["TMDB_ACCESS_TOKEN"] = tmdb_token
            if tmdb_use_v4 is not None:
                core_config.config["tmdb_use_v4"] = bool(tmdb_use_v4)
                os.environ["TMDB_USE_V4"] = "1" if bool(tmdb_use_v4) else "0"
        except Exception:
            logging.getLogger(__name__).exception("Failed updating runtime env for tmdb credentials")

        cfg_path = os.path.join(core_config.DATA_DIR, "app_config.json")
        acfg: dict = {}
        try:
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    acfg = json.load(fh) or {}
        except Exception:
            acfg = {}

        if tmdb_key:
            acfg.setdefault("metadata", {})["tmdb_api_key"] = tmdb_key
        if tmdb_token:
            acfg.setdefault("metadata", {})["tmdb_access_token"] = tmdb_token
        if tmdb_use_v4 is not None:
            acfg.setdefault("metadata", {})["tmdb_use_v4"] = bool(tmdb_use_v4)

        try:
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(acfg, fh, indent=2)
            try:
                os.chmod(cfg_path, 0o600)
            except Exception:
                pass
        except Exception:
            logging.getLogger(__name__).exception("Failed persisting tmdb credentials to app_config")

        # Persist to SQLite settings too so runtime jobs see TMDB as configured after restart.
        try:
            _persist_tmdb_credentials_to_settings(tmdb_key, tmdb_token, tmdb_use_v4)
        except Exception:
            logging.getLogger(__name__).debug("Failed persisting tmdb credentials to settings", exc_info=True)

        try:
            res = check_tmdb_connectivity()
            tmdb_ok = bool(res.get("ok"))
            detail = res.get("detail")
            enrichment_started = False
            if tmdb_ok:
                try:
                    enrichment_started = bool(start_enrichment_job())
                except Exception:
                    enrichment_started = False
                logging.getLogger(__name__).info("TMDB credentials valid. enrichment_started=%s", enrichment_started)
                try:
                    _run_tmdb_repair_background()
                except Exception:
                    pass
            return {
                "saved": True,
                "tmdb_ok": tmdb_ok,
                "detail": detail,
                "enrichment_started": enrichment_started,
            }
        except Exception:
            return {"saved": True, "tmdb_ok": False, "detail": "failed_check"}
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger(__name__).exception("Failed saving credentials")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/credentials")
def get_credentials_status(_: bool = Depends(admin_required)):
    """Return whether TMDB credentials are configured (without returning secrets)."""
    try:
        tmdb_key = core_config.config.get("tmdb_api_key") or os.environ.get("TMDB_API_KEY")
        tmdb_token = core_config.config.get("tmdb_access_token") or os.environ.get("TMDB_ACCESS_TOKEN")
        tmdb_ok = bool((tmdb_key and str(tmdb_key).strip()) or (tmdb_token and str(tmdb_token).strip()))
        return {"tmdb_configured": bool(tmdb_ok)}
    except Exception:
        return {"tmdb_configured": False}


@router.get("/api/credentials/check")
def check_credentials_connectivity(_: bool = Depends(admin_required)):
    return check_tmdb_connectivity()


@router.post("/api/credentials/check")
def check_credentials_payload(payload: dict, _: bool = Depends(admin_required)):
    """Validate supplied TMDB credentials without persisting them."""
    try:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        key = payload.get("tmdb_api_key")
        token = payload.get("tmdb_access_token")
        use_v4 = payload.get("tmdb_use_v4")

        if isinstance(token, str):
            tt = token.strip()
            if tt.lower().startswith("bearer "):
                token = tt[7:].strip()

        old_key = core_config.config.get("tmdb_api_key")
        old_token = core_config.config.get("tmdb_access_token")
        old_use_v4 = core_config.config.get("tmdb_use_v4")
        try:
            if isinstance(key, str):
                core_config.config["tmdb_api_key"] = key
                os.environ["TMDB_API_KEY"] = key
            if isinstance(token, str):
                core_config.config["tmdb_access_token"] = token
                os.environ["TMDB_ACCESS_TOKEN"] = token
            if use_v4 is not None:
                core_config.config["tmdb_use_v4"] = bool(use_v4)

            return check_tmdb_connectivity()
        finally:
            try:
                core_config.config["tmdb_api_key"] = old_key
                core_config.config["tmdb_access_token"] = old_token
                core_config.config["tmdb_use_v4"] = old_use_v4

                if old_key is None:
                    os.environ.pop("TMDB_API_KEY", None)
                else:
                    os.environ["TMDB_API_KEY"] = str(old_key)

                if old_token is None:
                    os.environ.pop("TMDB_ACCESS_TOKEN", None)
                else:
                    os.environ["TMDB_ACCESS_TOKEN"] = str(old_token)
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger(__name__).exception("Failed validating supplied credentials")
        raise HTTPException(status_code=500, detail=str(e))
