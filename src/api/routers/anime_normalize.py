from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import admin_required


router = APIRouter()


def _disabled_response() -> dict:
    return {
        "running": False,
        "disabled": True,
        "detail": "anime normalization tooling was moved to devtools/lenguaje and is not part of the production backend flow",
    }


@router.get("/api/anime-normalize/status")
def anime_normalize_status():
    return _disabled_response()


@router.post("/api/anime-normalize/start")
def anime_normalize_start(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/anime-normalize/stop")
def anime_normalize_stop(_: bool = Depends(admin_required)):
    return {"stop_requested": False, **_disabled_response()}
