from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Header, Depends

from ...db import SessionLocal, models
from ..admin_auth import issue_admin_token, revoke_admin_token, get_required_admin_pin
from ..deps import admin_required
from .app_config import _ensure_settings_table_exists

router = APIRouter()


@router.get("/api/admin/status")
def admin_status():
    required = get_required_admin_pin()
    return {"pin_required": bool(required)}


@router.post("/api/admin/login")
def admin_login(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    pin = str(payload.get("pin") or "").strip()
    if not pin or len(pin) < 4:
        raise HTTPException(status_code=400, detail="invalid pin")
    required = get_required_admin_pin()
    if not required:
        # First-run bootstrap: persist pin and accept login.
        try:
            _ensure_settings_table_exists()
        except Exception:
            pass
        db = SessionLocal()
        try:
            raw = json.dumps(pin, ensure_ascii=False)
            row = db.query(models.Setting).filter(models.Setting.key == "admin_pin").first()
            if row:
                if not getattr(row, "value", None):
                    row.value = raw
                    try:
                        row.updated_at = datetime.utcnow()
                    except Exception:
                        pass
                    db.add(row)
            else:
                db.add(models.Setting(key="admin_pin", value=raw))
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail="failed saving pin")
        finally:
            db.close()
    else:
        if pin != required:
            raise HTTPException(status_code=403, detail="invalid pin")
    token, ttl = issue_admin_token()
    return {"ok": True, "token": token, "expires_in": ttl}


@router.post("/api/admin/logout")
def admin_logout(
    x_arcanea_admin_token: str | None = Header(default=None, alias="X-Arcanea-Admin-Token"),
    admin_token: str | None = None,
    _: bool = Depends(admin_required),
):
    token = (x_arcanea_admin_token or admin_token or "").strip()
    revoke_admin_token(token)
    return {"ok": True}


@router.post("/api/admin/pin")
def admin_set_pin(payload: dict, _: bool = Depends(admin_required)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    pin = str(payload.get("pin") or "").strip()
    if not pin or len(pin) < 4:
        raise HTTPException(status_code=400, detail="invalid pin")
    # If env pin is set, it overrides DB on restart.
    try:
        env_pin = (os.environ.get("ARCANEA_ADMIN_PIN") or "").strip()
        if env_pin:
            # Allow runtime override but warn caller.
            os.environ["ARCANEA_ADMIN_PIN"] = pin
    except Exception:
        env_pin = ""

    try:
        _ensure_settings_table_exists()
    except Exception:
        pass

    db = SessionLocal()
    try:
        raw = json.dumps(pin, ensure_ascii=False)
        row = db.query(models.Setting).filter(models.Setting.key == "admin_pin").first()
        if row:
            row.value = raw
            try:
                row.updated_at = datetime.utcnow()
            except Exception:
                pass
            db.add(row)
        else:
            db.add(models.Setting(key="admin_pin", value=raw))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="failed updating pin")
    finally:
        db.close()

    return {
        "ok": True,
        "env_override": bool(env_pin),
        "detail": "pin updated",
    }
