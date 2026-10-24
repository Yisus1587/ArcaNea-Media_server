from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import admin_required


router = APIRouter()


def _disabled_response() -> dict:
    return {
        "running": False,
        "disabled": True,
        "detail": "language tooling was moved to devtools/lenguaje and is not part of the production backend flow",
    }


@router.get("/api/localize/status")
def localize_status():
    return _disabled_response()


@router.post("/api/localize/start")
def localize_start(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/localize/stop")
def localize_stop(_: bool = Depends(admin_required)):
    return {"stop_requested": False, **_disabled_response()}


@router.post("/api/localize/translations-only")
def localize_translations_only(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/localize/series/{tmdb_id}/reconcile")
def localize_reconcile_series(tmdb_id: str, _: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/localize/translations-finalize")
def localize_translations_finalize(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/localize/normalize-language")
def localize_normalize_language(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.post("/api/localize/anime-translations")
def localize_anime_translations(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])


@router.get("/api/localize/tmdb-anchor-probe")
def localize_tmdb_anchor_probe(_: bool = Depends(admin_required)):
    raise HTTPException(status_code=410, detail=_disabled_response()["detail"])
