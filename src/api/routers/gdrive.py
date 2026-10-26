from __future__ import annotations

import ipaddress
import json
import logging
import os
import stat
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...db import models
from ...services.gdrive import CREDENTIALS_FILE, gdrive_service
from ..deps import get_db, admin_required

router = APIRouter()
logger = logging.getLogger(__name__)

CALLBACK_PATH = '/api/gdrive/auth/callback'

_PAGE_STYLE = (
    '<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e5e7eb;'
    'display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}'
    'main{max-width:28rem;padding:2rem;text-align:center}h1{font-size:1.25rem}'
    '</style>'
)


def _host_port(request: Request) -> tuple[str, int]:
    """(hostname, port) of the host the browser is talking to."""
    host_header = request.headers.get('host') or request.url.netloc
    parsed = urlparse('//' + host_header)
    hostname = parsed.hostname or 'localhost'
    port = parsed.port or (443 if request.url.scheme == 'https' else 80)
    return hostname, port


def _build_redirect_uri(request: Request) -> str:
    """Redirect URI to send to Google, adapted to the OAuth client type.

    Google only accepts loopback redirects on localhost (not on a bare IP) for
    "web" clients, while "installed" desktop clients accept 127.0.0.1 or a LAN
    IP with a port. The browser must be able to reach this URI, so the port is
    always the one the caller is using.
    """
    hostname, port = _host_port(request)
    client_type = gdrive_service.client_type()
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    if client_type == 'web' or (is_ip and client_type != 'installed'):
        hostname = 'localhost'
    return f'http://{hostname}:{port}{CALLBACK_PATH}'


@router.get("/api/gdrive/auth/url")
def get_auth_url(request: Request, _: bool = Depends(admin_required)):
    if not gdrive_service.credentials_present():
        raise HTTPException(status_code=400, detail="No Google OAuth client configured")
    redirect_uri = _build_redirect_uri(request)
    auth_url = gdrive_service.get_auth_url(redirect_uri)
    if not auth_url:
        raise HTTPException(status_code=500, detail="Failed to generate auth URL")
    return {
        "auth_url": auth_url,
        "redirect_uri": redirect_uri,
        "client_type": gdrive_service.client_type(),
    }


@router.get("/api/gdrive/auth/callback")
def auth_callback(request: Request, code: str = None, error: str = None, state: str = None):
    # This page is opened by Google in the browser that did the login, which
    # may be a laptop while the UI lives somewhere else (an APK WebView, for
    # instance). A relative redirect would break there, so the answer is a
    # self-contained page that just tells the user to go back to ArcaNea.
    if error:
        return HTMLResponse(
            "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Google Drive</title>{_PAGE_STYLE}</head><body><main>"
            "<h1>No se pudo conectar Google Drive</h1>"
            f"<p>Google devolvió: <code>{error}</code></p>"
            "<p>Vuelve a ArcaNea e inténtalo de nuevo.</p></main></body></html>"
        )
    if not code:
        raise HTTPException(status_code=400, detail="No code provided")
    redirect_uri = _build_redirect_uri(request)
    success = gdrive_service.authenticate(code, redirect_uri, state=state)
    if success:
        body = (
            "<h1>Listo, ya puedes volver a ArcaNea</h1>"
            "<p>Google Drive quedó conectado. Cierra esta pestaña y vuelve a la "
            "app; si sigue en verde, pulsa «Ya conecté».</p>"
        )
    else:
        body = (
            "<h1>No se pudo guardar la autorización</h1>"
            "<p>Vuelve a ArcaNea, comprueba que el JSON del cliente sea correcto "
            "e inténtalo otra vez.</p>"
        )
    return HTMLResponse(
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Google Drive</title>{_PAGE_STYLE}</head>"
        f"<body><main>{body}</main></body></html>"
    )


@router.get("/api/gdrive/status")
def auth_status(_: bool = Depends(admin_required)):
    return {
        "authenticated": gdrive_service.is_authenticated(),
        "credentials_present": gdrive_service.credentials_present(),
        "client_type": gdrive_service.client_type(),
    }


@router.post("/api/gdrive/credentials")
def upload_credentials(payload: dict, _: bool = Depends(admin_required)):
    """Store the OAuth client JSON downloaded from Google Cloud Console.

    The body is either the Google JSON itself ({installed: {...}} / {web:
    {...}}) or {"client_json": {...}}. It is never echoed back nor logged: it
    carries the client secret.
    """
    client_json = payload.get('client_json') if isinstance(payload.get('client_json'), dict) else payload
    if not isinstance(client_json, dict):
        raise HTTPException(status_code=400, detail="client_json must be a JSON object")
    section = client_json.get('installed') if isinstance(client_json.get('installed'), dict) else client_json.get('web')
    if not isinstance(section, dict):
        raise HTTPException(status_code=400, detail="The JSON must contain an \"installed\" or \"web\" section")
    if not section.get('client_id') or not section.get('client_secret'):
        raise HTTPException(status_code=400, detail="The client JSON is missing client_id or client_secret")

    try:
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CREDENTIALS_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(client_json, f)
        os.replace(tmp, CREDENTIALS_FILE)
        os.chmod(CREDENTIALS_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600: it holds a secret
    except Exception as e:
        logger.error("Error saving gdrive credentials: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Could not save the client JSON")

    # Any previous token belongs to another client: forget it.
    gdrive_service.clear_token()
    return {"ok": True, "client_type": gdrive_service.client_type()}


@router.post("/api/gdrive/disconnect")
def disconnect(_: bool = Depends(admin_required)):
    gdrive_service.clear_token()
    return {
        "ok": True,
        "authenticated": gdrive_service.is_authenticated(),
        "credentials_present": gdrive_service.credentials_present(),
        "client_type": gdrive_service.client_type(),
    }


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
