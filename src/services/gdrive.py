from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except Exception:
    Credentials = None
    Flow = None
    build = None
    HttpError = Exception

from ..core.config import DATA_DIR

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# No client ID or secret is hardcoded in this project: every ArcaNea
# installation is meant to use its own Google Cloud project. The client comes
# either from DATA_DIR/gdrive_credentials.json (uploaded by the admin) or,
# for whoever builds their own APK/binary, from these two environment
# variables, which take precedence over the file.
CLIENT_ID_ENV = 'ARCANEA_GDRIVE_CLIENT_ID'
CLIENT_SECRET_ENV = 'ARCANEA_GDRIVE_CLIENT_SECRET'

CREDENTIALS_FILE = DATA_DIR / 'gdrive_credentials.json'
TOKEN_FILE = DATA_DIR / 'gdrive_token.json'
FLOW_FILE = DATA_DIR / 'gdrive_flow.json'


def _env_client_config() -> Optional[Dict[str, Any]]:
    """Client settings from environment variables, shaped like Google's
    "installed" JSON so it feeds the same OAuth flow as an uploaded file."""
    client_id = (os.environ.get(CLIENT_ID_ENV) or '').strip()
    client_secret = (os.environ.get(CLIENT_SECRET_ENV) or '').strip()
    if not client_id or not client_secret:
        return None
    return {
        'installed': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost'],
        }
    }


class GoogleDriveService:
    def __init__(self):
        self.service = None
        self.creds = None
        if Credentials is None:
            return
        self._load_credentials()
    
    def _load_credentials(self):
        creds = None
        if TOKEN_FILE.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            except Exception as e:
                logger.error(f"Error loading token: {e}")
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    from google.auth.transport.requests import Request
                    creds.refresh(Request())
                    self._save_token(creds)
                except Exception as e:
                    logger.error(f"Error refreshing token: {e}")
                    creds = None
            else:
                creds = None
        
        if creds and creds.valid and build:
            try:
                self.service = build('drive', 'v3', credentials=creds)
                self.creds = creds
            except Exception as e:
                logger.error(f"Error building drive service: {e}")
                self.service = None

    def _ensure_credentials(self):
        """Return usable credentials, refreshing the access token when it expired."""
        creds = self.creds
        if creds is None:
            return None
        try:
            if not creds.valid and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                self._save_token(creds)
        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            return None
        return creds if getattr(creds, "valid", False) else None
    
    def _save_token(self, creds):
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        except Exception as e:
            logger.error(f"Error saving token: {e}")
    
    def is_authenticated(self) -> bool:
        return self.service is not None

    def _client_config(self) -> Optional[Dict[str, Any]]:
        """OAuth client in use (env vars first, then the uploaded file)."""
        env_cfg = _env_client_config()
        if env_cfg:
            return env_cfg
        if not CREDENTIALS_FILE.exists():
            return None
        try:
            with open(CREDENTIALS_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            logger.error("gdrive_credentials.json ilegible")
            return None
        if not isinstance(cfg, dict):
            return None
        if isinstance(cfg.get('installed'), dict) or isinstance(cfg.get('web'), dict):
            return cfg
        return None

    def credentials_present(self) -> bool:
        return self._client_config() is not None

    def client_type(self) -> Optional[str]:
        """'installed', 'web' or None."""
        cfg = self._client_config()
        if not isinstance(cfg, dict):
            return None
        if isinstance(cfg.get('installed'), dict):
            return 'installed'
        if isinstance(cfg.get('web'), dict):
            return 'web'
        return None

    def clear_token(self) -> None:
        """Forget the stored authorization; the next use needs a new consent."""
        for f in (TOKEN_FILE, FLOW_FILE):
            try:
                if f.exists():
                    f.unlink()
            except Exception as e:
                logger.error(f"Error deleting {f}: {e}")
        self.service = None
        self.creds = None

    def get_auth_url(self, redirect_uri: str) -> Optional[str]:
        cfg = self._client_config()
        if Flow is None or cfg is None:
            logger.error("No OAuth client configured or google-auth-oauthlib not available")
            return None
        try:
            flow = Flow.from_client_config(
                cfg,
                scopes=SCOPES,
                redirect_uri=redirect_uri
            )
            # google-auth-oauthlib >= 1.0 has no Flow.state attribute: the
            # state comes back from authorization_url() and must be stored
            # here to be checked on the callback (CSRF protection).
            state = secrets.token_urlsafe(32)
            auth_url, _ = flow.authorization_url(prompt='consent', state=state)
            # Store flow for later
            with open(FLOW_FILE, 'w') as f:
                json.dump({
                    'client_config': flow.client_config,
                    'redirect_uri': redirect_uri,
                    'state': state
                }, f)
            return auth_url
        except Exception as e:
            logger.error(f"Error getting auth url: {e}")
            return None
    
    def authenticate(self, code: str, redirect_uri: str, state: Optional[str] = None) -> bool:
        if Flow is None or build is None:
            return False
        try:
            flow_file = FLOW_FILE
            if not flow_file.exists():
                return False
            with open(flow_file, 'r') as f:
                flow_data = json.load(f)
            stored_state = flow_data.get('state')
            if stored_state and state and state != stored_state:
                logger.error("State OAuth no coincide")
                return False
            
            flow = Flow.from_client_config(
                flow_data['client_config'],
                scopes=SCOPES,
                redirect_uri=redirect_uri,
                state=flow_data.get('state')
            )
            flow.fetch_token(code=code)
            creds = flow.credentials
            self._save_token(creds)
            self.creds = creds
            self.service = build('drive', 'v3', credentials=creds)
            try:
                if flow_file.exists():
                    flow_file.unlink()
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"Error authenticating: {e}")
            return False
    
    def _list_all(self, query: str, fields: str) -> List[Dict[str, Any]]:
        """Run a files.list query following every page token."""
        if not self.service:
            return []
        out: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        try:
            while True:
                results = self.service.files().list(
                    q=query,
                    spaces='drive',
                    fields=f'nextPageToken, {fields}',
                    pageSize=1000,
                    pageToken=page_token,
                ).execute()
                out.extend(results.get('files', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
        except HttpError as e:
            logger.error(f"Error listing drive files: {e}")
        return out

    def list_folders(self, parent_id: str = 'root') -> List[Dict[str, Any]]:
        return self._list_all(
            f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            'files(id, name, parents)',
        )
    
    def list_files(self, folder_id: str, mime_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        query = f"'{folder_id}' in parents and trashed=false"
        if mime_types:
            mime_query = ' or '.join([f"mimeType='{mt}'" for mt in mime_types])
            query += f" and ({mime_query})"
        else:
            query += " and mimeType contains 'video/'"
        return self._list_all(query, 'files(id, name, mimeType, size, modifiedTime, parents)')

    def list_folder_contents(self, folder_id: str = 'root') -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return (subfolders, video files) for a single folder."""
        folders = self.list_folders(folder_id)
        files = self.list_files(folder_id)
        return folders, files
    
    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        if not self.service:
            return None
        try:
            return self.service.files().get(
                fileId=file_id,
                fields='id, name, mimeType, size, modifiedTime, parents, webContentLink, downloadUrl'
            ).execute()
        except HttpError as e:
            logger.error(f"Error getting file: {e}")
            return None
    
    def open_media(self, file_id: str, start: int = 0, end: Optional[int] = None) -> requests.Response:
        """Open the media bytes of a file, honouring `Range`.

        The raw media endpoint is used instead of the API client because it lets the
        requested byte range reach Google and stream back without buffering the
        whole file in memory, which is what seeking during playback needs.
        """
        creds = self._ensure_credentials()
        if creds is None:
            raise RuntimeError("not_authenticated")
        token = getattr(creds, "token", None)
        if not token:
            raise RuntimeError("missing_access_token")

        headers = {"Authorization": f"Bearer {token}"}
        if start or end is not None:
            last = "" if end is None else str(int(end))
            headers["Range"] = f"bytes={int(start)}-{last}"

        resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers,
            stream=True,
            timeout=(10, 120),
        )
        if resp.status_code >= 400:
            body = ""
            try:
                body = resp.text[:300]
            except Exception:
                pass
            resp.close()
            raise RuntimeError(f"drive_http_{resp.status_code}: {body}")
        return resp


gdrive_service = GoogleDriveService()
