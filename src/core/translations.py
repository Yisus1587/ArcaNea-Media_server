from __future__ import annotations

import datetime
from typing import Iterable

from .config import normalize_metadata_language
from ..db import models


def normalize_translation_language(lang: str | None) -> str:
    """Normalize language tags to translation keys stored in DB.

    Stored keys are short but may include region when it matters for fallbacks, e.g.:
    - es-MX, es-ES, es
    - pt-BR, pt
    - en
    """
    s = str(lang or "").strip()
    if not s:
        return "en"
    s = s.replace("_", "-")
    low = s.lower()
    if low.startswith("en"):
        return "en"
    if low.startswith("es"):
        # keep es-XX when present (e.g. es-MX, es-ES)
        parts = s.split("-", 1)
        if len(parts) == 2 and len(parts[0]) == 2:
            return f"{parts[0].lower()}-{parts[1].upper()}"
        return "es"
    # keep casing xx-YY
    parts = s.split("-", 1)
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) in (2, 3):
        return f"{parts[0].lower()}-{parts[1].upper()}"
    if len(s) == 2:
        return s.lower()
    return s


_SPANISH_FALLBACKS = ["es-ES", "es-AR", "es-CL", "es-CO", "es-PE", "es-VE", "es"]


def build_language_fallback_chain(preferred: str | None) -> list[str]:
    """Return language preference chain for UI fields.

    Rules:
    - For Spanish: es-MX > es-ES > es > en
    - For other region languages: xx-YY > xx > en
    - Always ends with en
    """
    p0 = normalize_translation_language(preferred)
    out: list[str] = []
    if p0:
        out.append(p0)

    low = p0.lower()
    if low.startswith("es-"):
        for loc in _SPANISH_FALLBACKS:
            if loc not in out:
                out.append(loc)
    else:
        # add base language
        base = p0.split("-", 1)[0].lower() if p0 else ""
        if base and base not in out and base != "en":
            out.append(base)

    if "en" not in out:
        out.append("en")
    # de-dupe preserving order
    seen = set()
    final: list[str] = []
    for x in out:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        final.append(x)
    return final


def _source_rank(source: str | None) -> int:
    s = str(source or "").strip().lower()
    # Prefer manual overrides, then TMDB localization, then Jikan identity.
    if s == "manual":
        return 0
    if s == "tmdb":
        return 1
    if s == "jikan":
        return 2
    return 9


def upsert_translation(
    db,
    *,
    path_id: int,
    language: str,
    source: str,
    title: str | None = None,
    overview: str | None = None,
) -> models.MediaTranslation:
    lang_key = normalize_translation_language(language)
    src = str(source or "").strip().lower() or "manual"
    t = (title or "").strip() or None
    ov = (overview or "").strip() or None

    row = None
    # Avoid duplicate inserts when autoflush is disabled by checking pending instances first.
    try:
        for obj in list(getattr(db, "new", []) or []):
            if not isinstance(obj, models.MediaTranslation):
                continue
            try:
                if int(getattr(obj, "path_id")) != int(path_id):
                    continue
                if normalize_translation_language(getattr(obj, "language", None)) != lang_key:
                    continue
                if str(getattr(obj, "source", "") or "").strip().lower() != src:
                    continue
                row = obj
                break
            except Exception:
                continue
    except Exception:
        row = None
    if row is None:
        try:
            for obj in list(getattr(db, "dirty", []) or []):
                if not isinstance(obj, models.MediaTranslation):
                    continue
                try:
                    if int(getattr(obj, "path_id")) != int(path_id):
                        continue
                    if normalize_translation_language(getattr(obj, "language", None)) != lang_key:
                        continue
                    if str(getattr(obj, "source", "") or "").strip().lower() != src:
                        continue
                    row = obj
                    break
                except Exception:
                    continue
        except Exception:
            row = None
    if row is None:
        row = (
            db.query(models.MediaTranslation)
            .filter(models.MediaTranslation.path_id == int(path_id))
            .filter(models.MediaTranslation.language == lang_key)
            .filter(models.MediaTranslation.source == src)
            .first()
        )
    if not row:
        row = models.MediaTranslation(path_id=int(path_id), language=lang_key, source=src)

    # Only overwrite with meaningful values; keep existing non-empty content.
    if t:
        row.title = t
    if ov:
        row.overview = ov
    try:
        row.updated_at = datetime.datetime.utcnow()
    except Exception:
        pass

    db.add(row)
    return row


def resolve_translation_fields(
    rows: Iterable[models.MediaTranslation],
    *,
    preferred_langs: list[str],
    allow_jikan: bool = True,
) -> dict[str, str | None]:
    """Resolve title/overview using language chain + per-source ranking.

    Field-level fallback:
    - title: first non-empty across language chain
    - overview: first non-empty across language chain
    """
    by_lang: dict[str, list[models.MediaTranslation]] = {}
    for r in rows:
        try:
            lk = normalize_translation_language(getattr(r, "language", None))
        except Exception:
            continue
        if not allow_jikan:
            try:
                if str(getattr(r, "source", "") or "").strip().lower() == "jikan":
                    continue
            except Exception:
                continue
        by_lang.setdefault(lk, []).append(r)

    def pick(lang_key: str) -> models.MediaTranslation | None:
        candidates = by_lang.get(lang_key) or []
        if not candidates:
            return None
        try:
            candidates = sorted(candidates, key=lambda x: _source_rank(getattr(x, "source", None)))
        except Exception:
            pass
        return candidates[0] if candidates else None

    title: str | None = None
    overview: str | None = None
    for lang_key in preferred_langs:
        r = pick(lang_key)
        if not r:
            continue
        if title is None:
            try:
                t = (getattr(r, "title", None) or "").strip()
            except Exception:
                t = ""
            if t:
                title = t
        if overview is None:
            try:
                ov = (getattr(r, "overview", None) or "").strip()
            except Exception:
                ov = ""
            if ov:
                overview = ov
        if title is not None and overview is not None:
            break

    return {"title": title, "overview": overview}


def preferred_lang_chain_from_config(get_fn) -> list[str]:
    """Build preferred language chain from runtime config/settings."""
    try:
        raw = get_fn("metadata_language") or ""
    except Exception:
        raw = ""
    try:
        # normalize_metadata_language converts es-419 -> es-MX, etc.
        raw = normalize_metadata_language(raw)
    except Exception:
        pass
    return build_language_fallback_chain(raw)
