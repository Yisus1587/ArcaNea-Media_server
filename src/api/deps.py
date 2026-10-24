from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session
from fastapi import Header, HTTPException, Request
import ipaddress

try:
    # Preferred package layout: `src.*`
    from ..db import SessionLocal
except ImportError:  # pragma: no cover
    # Fallback layouts:
    # - when running with PYTHONPATH pointing at `<repo>/src`, modules may import as `api.*` and `db.*`
    # - when tools run files directly, absolute imports may be required
    try:
        from src.db import SessionLocal  # type: ignore
    except Exception:
        from db import SessionLocal  # type: ignore
import os
from .admin_auth import validate_admin_token, get_required_admin_pin


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_admin_token(
    x_arcanea_admin_token: str | None = None,
    admin_token: str | None = None,
) -> str:
    return (x_arcanea_admin_token or admin_token or "").strip()


def has_valid_admin_token(
    x_arcanea_admin_token: str | None = None,
    admin_token: str | None = None,
) -> bool:
    return validate_admin_token(_extract_admin_token(x_arcanea_admin_token, admin_token))


def is_loopback_request(request: Request | None) -> bool:
    try:
        host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    except Exception:
        host = ""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except Exception:
        return host.lower() in ("localhost",)


def admin_required(
    request: Request,
    x_arcanea_manager: str | None = Header(default=None, alias="X-Arcanea-Manager"),
    x_arcanea_role: str | None = Header(default=None, alias="X-Arcanea-Role"),
    x_arcanea_admin_token: str | None = Header(default=None, alias="X-Arcanea-Admin-Token"),
    admin_token: str | None = None,
):
    role = (x_arcanea_role or "").strip().lower()
    mgr = (x_arcanea_manager or "").strip().lower()
    pin_required = get_required_admin_pin()
    if pin_required:
        if not has_valid_admin_token(x_arcanea_admin_token, admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
        return True
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="forbidden")
    if role == "admin" or mgr in ("1", "true", "yes"):
        return True
    raise HTTPException(status_code=403, detail="forbidden")


def admin_or_localhost(
    request: Request,
    x_arcanea_admin_token: str | None = Header(default=None, alias="X-Arcanea-Admin-Token"),
    admin_token: str | None = None,
):
    if is_loopback_request(request):
        return True
    if has_valid_admin_token(x_arcanea_admin_token, admin_token):
        return True
    raise HTTPException(status_code=403, detail="forbidden")
