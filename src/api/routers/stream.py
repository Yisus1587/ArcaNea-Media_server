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


def _parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse a single `bytes=start-end` range, clamped to the file size."""
    if not range_header or file_size <= 0:
        return None
    try:
        units, rng = range_header.split("=", 1)
        if units.strip() != "bytes":
            return None
        rstart, rend = rng.split("-", 1)
        start = int(rstart) if rstart.strip() else 0
        end = int(rend) if rend.strip() else file_size - 1
        if start < 0 or start > end or start >= file_size:
            return None
        return start, min(end, file_size - 1)
    except Exception:
        return None


def _iter_upstream(resp, chunk_size: int = 1024 * 1024):
    """Yield bytes from a streaming HTTP response and always release it."""
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        try:
            resp.close()
        except Exception:
            pass


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
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"drive metadata failed: {e}")
        if not file_info:
            raise HTTPException(status_code=404, detail="file not found")

        file_size = int(file_info.get('size') or 0)
        content_type = file_info.get('mimeType') or 'application/octet-stream'
        requested = _parse_range_header(request.headers.get("range"), file_size)

        try:
            upstream = gdrive_service.open_media(
                file_id,
                start=requested[0] if requested else 0,
                end=requested[1] if requested else None,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"drive stream failed: {e}")

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
        }
        if upstream.status_code == 206:
            content_range = upstream.headers.get("Content-Range")
            if content_range:
                headers["Content-Range"] = content_range
            content_length = upstream.headers.get("Content-Length")
            if content_length:
                headers["Content-Length"] = content_length
            return StreamingResponse(_iter_upstream(upstream), status_code=206, headers=headers)

        # Google ignored the range: report the real size and serve from the start.
        if file_size:
            headers["Content-Length"] = str(file_size)
        return StreamingResponse(_iter_upstream(upstream), headers=headers)
    
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
    parsed = _parse_range_header(range_header, file_size)
    if parsed:
        start, end = parsed
    else:
        start, end = 0, file_size - 1

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
