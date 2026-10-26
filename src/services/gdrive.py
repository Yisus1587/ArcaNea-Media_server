from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except Exception:
    Credentials = None
    Flow = None
    build = None
    HttpError = Exception
    MediaIoBaseDownload = None

from ..core.config import DATA_DIR

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

CREDENTIALS_FILE = DATA_DIR / 'gdrive_credentials.json'
TOKEN_FILE = DATA_DIR / 'gdrive_token.json'


class GoogleDriveService:
    def __init__(self):
        self.service = None
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
            except Exception as e:
                logger.error(f"Error building drive service: {e}")
                self.service = None
    
    def _save_token(self, creds):
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        except Exception as e:
            logger.error(f"Error saving token: {e}")
    
    def is_authenticated(self) -> bool:
        return self.service is not None
    
    def get_auth_url(self, redirect_uri: str) -> Optional[str]:
        if Flow is None or not CREDENTIALS_FILE.exists():
            logger.error("No credentials file found or google-auth-oauthlib not available")
            return None
        try:
            flow = Flow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                scopes=SCOPES,
                redirect_uri=redirect_uri
            )
            auth_url, _ = flow.authorization_url(prompt='consent')
            # Store flow for later
            flow_file = DATA_DIR / 'gdrive_flow.json'
            with open(flow_file, 'w') as f:
                json.dump({
                    'client_config': flow.client_config,
                    'redirect_uri': redirect_uri,
                    'state': flow.state
                }, f)
            return auth_url
        except Exception as e:
            logger.error(f"Error getting auth url: {e}")
            return None
    
    def authenticate(self, code: str, redirect_uri: str) -> bool:
        if Flow is None or build is None:
            return False
        try:
            flow_file = DATA_DIR / 'gdrive_flow.json'
            if not flow_file.exists():
                return False
            with open(flow_file, 'r') as f:
                flow_data = json.load(f)
            
            flow = Flow.from_client_config(
                flow_data['client_config'],
                scopes=SCOPES,
                redirect_uri=redirect_uri,
                state=flow_data.get('state')
            )
            flow.fetch_token(code=code)
            creds = flow.credentials
            self._save_token(creds)
            self.service = build('drive', 'v3', credentials=creds)
            try:
                flow_file.unlink()
            except:
                pass
            return True
        except Exception as e:
            logger.error(f"Error authenticating: {e}")
            return False
    
    def list_folders(self, parent_id: str = 'root') -> List[Dict[str, Any]]:
        if not self.service:
            return []
        try:
            results = self.service.files().list(
                q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                spaces='drive',
                fields='files(id, name, parents)',
                pageSize=100
            ).execute()
            return results.get('files', [])
        except HttpError as e:
            logger.error(f"Error listing folders: {e}")
            return []
    
    def list_files(self, folder_id: str, mime_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if not self.service:
            return []
        try:
            query = f"'{folder_id}' in parents and trashed=false"
            if mime_types:
                mime_query = ' or '.join([f"mimeType='{mt}'" for mt in mime_types])
                query += f" and ({mime_query})"
            else:
                # Only video files
                query += " and mimeType contains 'video/'"
            
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name, mimeType, size, modifiedTime, parents)',
                pageSize=1000
            ).execute()
            return results.get('files', [])
        except HttpError as e:
            logger.error(f"Error listing files: {e}")
            return []
    
    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        if not self.service:
            return None
        try:
            return self.service.files().get(
                fileId=file_id,
                fields='id, name, mimeType, size, webContentLink, downloadUrl'
            ).execute()
        except HttpError as e:
            logger.error(f"Error getting file: {e}")
            return None
    
    def download_file(self, file_id: str, request=None) -> Iterator[bytes]:
        if not self.service:
            return
        try:
            request_obj = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request_obj, chunksize=1024*1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if fh.tell() > 0:
                    fh.seek(0)
                    data = fh.read()
                    fh.seek(0)
                    fh.truncate()
                    yield data
            # Yield remaining
            fh.seek(0)
            data = fh.read()
            if data:
                yield data
        except Exception as e:
            logger.error(f"Error downloading file: {e}")


gdrive_service = GoogleDriveService()
