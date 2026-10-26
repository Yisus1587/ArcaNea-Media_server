from __future__ import annotations

import io
import mimetypes
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core.config import get as get_config
from ...db import SessionLocal, models
try:
    from ...services.gdrive import gdrive_service
except Exception:
    gdrive_service = None

router = APIRouter()

def _normcase_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return os.path.normcase(str(path))


def _is_under_root(path: str, root: str) -> bool:
    try:
        p = _normcase_path(path)
        r = _normcase_path(root).rstrip("\\/")
        if not r:
            return False
        return p == r or p.startswith(r + os.sep)
    except Exception:
        return False


def _is_under_roots(path: str) -> bool:
    try:
        roots = get_config("media_roots") or []
    except Exception:
        roots = []
    for r in roots:
        if r and _is_under_root(path, str(r)):
            return True
    return False


def _is_known_media_file(path: str) -> bool:
    db = SessionLocal()
    try:
        row = db.query(models.FileRecord).filter(models.FileRecord.path == path).first()
        if not row and os.name == "nt":
            try:
                row = db.query(models.FileRecord).filter(models.FileRecord.path.ilike(path)).first()
            except Exception:
                row = None
        return bool(row)
    finally:
        db.close()


@router.get("/api/stream")
def stream_file(path: str, request: Request):
    """Stream a local file with support for Range requests."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    
    # Handle Google Drive files
    if str(path).startswith('gdrive://'):
        if not gdrive_service or not gdrive_service.is_authenticated():
            raise HTTPException(status_code=401, detail="Google Drive not authenticated")
        file_id = str(path).replace('gdrive://', '').strip()
        try:
            file_info = gdrive_service.get_file(file_id)
            if not file_info:
                raise HTTPException(status_code=404, detail="file not found")
            file_size = int(file_info.get('size', 0)) if file_info.get('size') else 0
            content_type = file_info.get('mimeType') or 'application/octet-stream'
            
            def iter_gdrive():
                try:
                    for chunk in gdrive_service.download_file(file_id):
                        yield chunk
                except Exception as e:
                    raise HTTPException(status_code=500, detail=str(e))
            
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Type": content_type,
            }
            if file_size:
                headers["Content-Length"] = str(file_size)
            return StreamingResponse(iter_gdrive(), headers=headers)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    try:
        ap = str(os.path.abspath(path))
    except Exception:
        ap = path

    # Only allow known media files (scanned into DB).
    try:
        if not _is_known_media_file(ap):
            raise HTTPException(status_code=404, detail="file not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="failed to verify file")

    # Ensure requested file stays inside configured media roots when available.
    try:
        roots = get_config("media_roots") or []
    except Exception:
        roots = []
    if roots:
        try:
            if not _is_under_roots(ap):
                raise HTTPException(status_code=403, detail="path not allowed")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="path validation failed")

    if not os.path.exists(ap) or not os.path.isfile(ap):
        raise HTTPException(status_code=404, detail="file not found")

    file_size = os.path.getsize(ap)
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1

    if range_header:
        try:
            units, rng = range_header.split("=")
            if units.strip() != "bytes":
                raise ValueError("Only bytes range supported")
            rstart, rend = rng.split("-")
            if rstart:
                start = int(rstart)
            if rend:
                end = int(rend)
            if start > end or end >= file_size:
                raise ValueError("Invalid range")
        except Exception:
            start = 0
            end = file_size - 1

    chunk_size = 1024 * 1024

    def iter_file(path, start, end, chunk_size=chunk_size):
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                data = f.read(read_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    content_type, _ = mimetypes.guess_type(ap)
    content_type = content_type or "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Content-Length": str(end - start + 1),
    }

    if range_header and (start != 0 or end != file_size - 1):
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return StreamingResponse(iter_file(ap, start, end), status_code=206, headers=headers)

    return StreamingResponse(iter_file(ap, start, end), headers=headers)
