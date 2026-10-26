from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException

from ...db import models
from .. import runtime
from ...services import media_pipeline as scan_service
from ..deps import get_db, admin_required

router = APIRouter()


def _root_path(value: str) -> str:
    """Normalize a root path.

    Google Drive roots are `gdrive://<folder_id>` pseudo-paths, so they must not be
    passed through `os.path.abspath` (that would turn them into relative paths on
    disk); everything else keeps the previous behavior.
    """
    text = str(value or "").strip()
    if text.startswith("gdrive://"):
        return text
    return os.path.abspath(text)


def _root_kind(path: str) -> str:
    return "gdrive" if str(path).startswith("gdrive://") else "local"


def _root_payload(item) -> dict:
    return {
        "path": item.path,
        "type": item.type,
        "source": item.source,
        "kind": getattr(item, "kind", None) or _root_kind(str(item.path)),
    }


@router.get("/api/roots")
def list_roots(db=Depends(get_db), _: bool = Depends(admin_required)):
    items = db.query(models.MediaRoot).all()
    return {"roots": [_root_payload(i) for i in items]}


@router.get("/api/roots/pending-confirmation")
def list_roots_pending_confirmation(db=Depends(get_db), _: bool = Depends(admin_required)):
    try:
        items = db.query(models.MediaRoot).filter((models.MediaRoot.type == None) | (models.MediaRoot.type == "unknown")).all()  # noqa: E711
        return {
            "roots": [
                {"path": i.path, "type": (i.type or "unknown"), "source": (i.source or "auto")} for i in items
            ]
        }
    except Exception:
        raise HTTPException(status_code=500, detail="failed listing pending roots")


@router.post("/api/roots")
def add_root(payload: dict, db=Depends(get_db), _: bool = Depends(admin_required)):
    path = payload.get("path") if isinstance(payload, dict) else None
    requested_type = payload.get("type") if isinstance(payload, dict) else None
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    ap = _root_path(path)

    def _normalize_type(raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            t = str(raw).strip().lower()
        except Exception:
            return None
        if not t or t in ("auto", "unknown", "unset", "none", "null", "infer", "inferred"):
            return None
        if t in ("tv", "show", "shows"):
            t = "series"
        if t not in ("anime", "movie", "series"):
            return None
        return t

    def _infer_type_from_path(p: str) -> str:
        b = os.path.basename(p).lower()
        LIBRARY_RULES = {
            "anime": ["anime", "animes"],
            "movie": ["pelicula", "peliculas", "movie", "movies", "film", "films"],
            "series": ["serie", "series", "tv", "show", "shows"],
        }
        for k, tokens in LIBRARY_RULES.items():
            for t in tokens:
                if t in b:
                    return k
        return "unknown"

    kind = _root_kind(ap)
    exists = db.query(models.MediaRoot).filter(models.MediaRoot.path == ap).first()
    if exists:
        # If user explicitly provided a type, update existing root to manual.
        explicit = _normalize_type(requested_type)
        if explicit:
            try:
                exists.type = explicit
                exists.source = "manual"
                db.add(exists)
                db.commit()
            except Exception:
                db.rollback()
        return {"roots": [_root_payload(i) for i in db.query(models.MediaRoot).all()]}

    try:
        explicit = _normalize_type(requested_type)
        if explicit:
            mr = models.MediaRoot(path=ap, type=explicit, source="manual", kind=kind)
        else:
            inferred = _infer_type_from_path(ap) if kind == "local" else "unknown"
            mr = models.MediaRoot(path=ap, type=inferred, source="auto", kind=kind)
        db.add(mr)
        db.commit()
    except Exception:
        db.rollback()

    items = db.query(models.MediaRoot).all()
    try:
        runtime.watcher_set_roots_from_db(db)
    except Exception:
        pass
    try:
        scan_service.enqueue_scan_path(ap, pipeline=True)
    except Exception:
        pass
    return {"roots": [_root_payload(i) for i in items]}


@router.delete("/api/roots")
def remove_root(payload: dict, db=Depends(get_db), _: bool = Depends(admin_required)):
    path = payload.get("path") if isinstance(payload, dict) else None
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    ap = _root_path(path)
    try:
        db.query(models.MediaRoot).filter(models.MediaRoot.path == ap).delete()
        db.commit()
    except Exception:
        db.rollback()
    items = db.query(models.MediaRoot).all()
    try:
        runtime.watcher_set_roots_from_db(db)
    except Exception:
        pass
    return {"roots": [_root_payload(i) for i in items]}


@router.patch("/api/roots")
def update_root(payload: dict, db=Depends(get_db), _: bool = Depends(admin_required)):
    path = payload.get("path") if isinstance(payload, dict) else None
    t = payload.get("type") if isinstance(payload, dict) else None
    if not path or not t:
        raise HTTPException(status_code=400, detail="path and type are required")
    ap = _root_path(path)
    try:
        t = str(t).strip().lower()
    except Exception:
        t = None
    if not t:
        raise HTTPException(status_code=400, detail="type is required")
    if t in ("tv", "show", "shows"):
        t = "series"
    if t in ("auto", "unknown", "unset", "none", "null", "infer", "inferred"):
        t = "unknown"
    if t not in ("anime", "movie", "series", "unknown"):
        raise HTTPException(status_code=400, detail="invalid type")
    try:
        mr = db.query(models.MediaRoot).filter(models.MediaRoot.path == ap).first()
        if not mr:
            raise HTTPException(status_code=404, detail="root not found")
        mr.type = str(t).strip().lower()
        mr.source = "manual"
        db.add(mr)
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="failed updating root")
    items = db.query(models.MediaRoot).all()
    try:
        runtime.watcher_set_roots_from_db(db)
    except Exception:
        pass
    return {"roots": [_root_payload(i) for i in items]}


@router.post("/api/roots/pick")
def pick_roots(payload: dict | None = None, _: bool = Depends(admin_required)):
    """Open a native folder selection dialog on the server and return chosen path(s)."""
    try:
        multiple = False
        if isinstance(payload, dict):
            multiple = bool(payload.get("multiple"))
        try:
            import tkinter as _tk
            from tkinter import filedialog as _fd
        except Exception:
            return {"paths": [], "error": "tkinter_unavailable"}

        root = _tk.Tk()
        try:
            root.withdraw()
            # Try to keep the native picker on top of the browser window and avoid a lingering empty Tk window.
            root.attributes("-topmost", True)
            root.update()
            root.focus_force()
        except Exception:
            pass
        paths: list[str] = []
        try:
            if multiple:
                while True:
                    p = _fd.askdirectory(parent=root)
                    if not p:
                        break
                    if p not in paths:
                        paths.append(p)
            else:
                p = _fd.askdirectory(parent=root)
                if p:
                    paths.append(p)
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        return {"paths": paths}
    except Exception as e:
        logging.getLogger(__name__).exception("Failed picking roots")
        return {"paths": [], "error": str(e)}
