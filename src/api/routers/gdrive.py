from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...db import models
from ...services.gdrive import gdrive_service
from ..deps import get_db, admin_required

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/gdrive/auth/url")
def get_auth_url(request: Request, _: bool = Depends(admin_required)):
    # Build redirect URI
    scheme = request.url.scheme
    host = request.headers.get('host') or request.url.netloc
    redirect_uri = f"{scheme}://{host}/api/gdrive/auth/callback"
    auth_url = gdrive_service.get_auth_url(redirect_uri)
    if not auth_url:
        raise HTTPException(status_code=500, detail="Failed to generate auth URL")
    return {"auth_url": auth_url}


@router.get("/api/gdrive/auth/callback")
def auth_callback(code: str = None, error: str = None, request: Request = None):
    if error:
        return RedirectResponse(url="/?gdrive_error=" + error)
    if not code:
        raise HTTPException(status_code=400, detail="No code provided")
    scheme = request.url.scheme
    host = request.headers.get('host') or request.url.netloc
    redirect_uri = f"{scheme}://{host}/api/gdrive/auth/callback"
    success = gdrive_service.authenticate(code, redirect_uri)
    if success:
        return RedirectResponse(url="/?gdrive_success=true")
    return RedirectResponse(url="/?gdrive_error=auth_failed")


@router.get("/api/gdrive/status")
def auth_status(_: bool = Depends(admin_required)):
    return {"authenticated": gdrive_service.is_authenticated()}


@router.get("/api/gdrive/folders")
def list_folders(parent_id: str = 'root', _: bool = Depends(admin_required)):
    if not gdrive_service.is_authenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")
    folders = gdrive_service.list_folders(parent_id)
    return {"folders": folders}


@router.post("/api/gdrive/roots")
def add_gdrive_root(payload: dict, db=Depends(get_db), _: bool = Depends(admin_required)):
    if not gdrive_service.is_authenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    folder_id = payload.get("folder_id")
    folder_name = payload.get("folder_name") or folder_id
    library_type = payload.get("type") or "unknown"
    
    if not folder_id:
        raise HTTPException(status_code=400, detail="folder_id is required")
    
    # Store as gdrive://folder_id
    root_path = f"gdrive://{folder_id}"
    
    exists = db.query(models.MediaRoot).filter(models.MediaRoot.path == root_path).first()
    if exists:
        return {"roots": [{"path": i.path, "type": i.type, "source": i.source, "kind": getattr(i, 'kind', 'gdrive')} for i in db.query(models.MediaRoot).all()]}
    
    try:
        from .. import runtime
        mr = models.MediaRoot(path=root_path, type=library_type if library_type in ('anime', 'movie', 'series') else None, source='manual', kind='gdrive')
        db.add(mr)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    items = db.query(models.MediaRoot).all()
    try:
        from ...services import media_pipeline as scan_service
        scan_service.enqueue_scan_path(root_path, pipeline=True)
    except Exception:
        pass
    
    return {"roots": [{"path": i.path, "type": i.type, "source": i.source, "kind": getattr(i, 'kind', 'gdrive')} for i in items]}
