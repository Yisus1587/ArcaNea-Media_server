from __future__ import annotations

import datetime
import re
from typing import Iterable

from sqlalchemy import or_

from ..db import models

# Tunable weights
WEIGHT_GENRE = 0.55
WEIGHT_RATING = 0.30
WEIGHT_RECENCY = 0.15
WATCHED_PENALTY = 0.1
SEED_LIMIT = 10
OUT_LIMIT = 20


def _safe_non_negative_float(v: float | int | str | None) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
    except Exception:
        return None
    if not (n >= 0):
        return None
    return n


def extract_episode_from_path(path: str | None) -> tuple[int | None, int | None]:
    s = str(path or "").strip()
    if not s:
        return (None, None)
    season: int | None = None
    episode: int | None = None
    try:
        m = re.search(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})", s)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            return (season, episode)
        m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", s, flags=re.IGNORECASE)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            return (season, episode)
        m = re.search(r"(?:temporada|season)[\s._-]*(\d{1,2})", s, flags=re.IGNORECASE)
        if m:
            season = int(m.group(1))
    except Exception:
        season = None
    try:
        m = re.search(r"(?:episodio|episode|ep|cap(?:itulo)?)\s*[\._:-]?\s*(\d{1,3})", s, flags=re.IGNORECASE)
        if m:
            episode = int(m.group(1))
    except Exception:
        episode = None
    return (season, episode)


def build_progress_snapshot(
    *,
    position: float | None,
    duration: float | None,
    path: str | None,
    watched: bool,
) -> dict:
    pos = _safe_non_negative_float(position) or 0.0
    dur = _safe_non_negative_float(duration) or 0.0
    progress = 0.0
    try:
        if dur > 0 and pos >= 0:
            progress = max(0.0, min(1.0, pos / dur))
    except Exception:
        progress = 0.0
    remaining = max(0.0, dur - pos) if dur > 0 else 0.0
    season_number, episode_number = extract_episode_from_path(path)
    return {
        "position": pos,
        "duration": dur,
        "path": path,
        "progress": progress,
        "watched": bool(watched),
        "position_minutes": round(pos / 60.0, 2),
        "duration_minutes": round(dur / 60.0, 2),
        "remaining_seconds": round(remaining, 2),
        "remaining_minutes": round(remaining / 60.0, 2),
        "season_number": season_number,
        "episode_number": episode_number,
    }


def _parse_genres(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip().lower() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [g.strip().lower() for g in raw.split(",") if g.strip()]
    return []


def _recency_score(created_at: datetime.datetime | None) -> float:
    if not created_at:
        return 0.0
    now = datetime.datetime.utcnow()
    try:
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    except Exception:
        return 0.0
    # 0..1, with a 30-day soft half-life
    return 1.0 / (1.0 + (age_days / 30.0))


def _rating_score(rating: float | None) -> float:
    if rating is None:
        return 0.0
    try:
        r = float(rating)
    except Exception:
        return 0.0
    # normalize 0..10 (tmdb/jikan). if 0..100, scale down.
    if r > 10:
        r = r / 10.0
    return max(0.0, min(1.0, r / 10.0))


def _genre_score(item_genres: list[str], seed_weights: dict[str, int]) -> float:
    if not item_genres or not seed_weights:
        return 0.0
    total = sum(seed_weights.values()) or 1
    score = 0.0
    for g in item_genres:
        score += seed_weights.get(g, 0)
    return score / total


def _candidate_limit(limit: int | None) -> int:
    try:
        lim = max(1, int(limit or OUT_LIMIT))
    except Exception:
        lim = OUT_LIMIT
    return max(120, min(600, lim * 25))


def get_recommendations(db, *, profile_id: str | None = None, limit: int = OUT_LIMIT) -> list[models.MediaItem]:
    # Pull last played items
    ph_q = db.query(models.PlayHistory).filter(models.PlayHistory.media_item_id != None)
    if profile_id:
        ph_q = ph_q.filter(models.PlayHistory.profile_id == profile_id)
    ph = (
        ph_q.order_by(models.PlayHistory.last_played.desc(), models.PlayHistory.updated_at.desc())
        .limit(SEED_LIMIT)
        .all()
    )
    seed_ids = [int(x.media_item_id) for x in ph if getattr(x, "media_item_id", None) is not None]

    seed_weights: dict[str, int] = {}
    if seed_ids:
        seeds = db.query(models.MediaItem).filter(models.MediaItem.id.in_(seed_ids)).all()
        for s in seeds:
            for g in _parse_genres(getattr(s, "genres", None)):
                seed_weights[g] = seed_weights.get(g, 0) + 1

    watched_ids: set[int] = set()
    try:
        watched_q = db.query(models.PlayHistory.media_item_id, models.PlayHistory.watched)
        if profile_id:
            watched_q = watched_q.filter(models.PlayHistory.profile_id == profile_id)
        watched_rows = watched_q.all()
        for media_item_id, watched in watched_rows:
            if media_item_id is None or not watched:
                continue
            try:
                watched_ids.add(int(media_item_id))
            except Exception:
                continue
    except Exception:
        watched_ids = set()

    candidate_limit = _candidate_limit(limit)
    items: list[models.MediaItem] = []
    seen_ids: set[int] = set()

    def _append_candidates(rows: list[models.MediaItem]) -> None:
        for row in rows:
            try:
                row_id = int(getattr(row, "id", 0) or 0)
            except Exception:
                row_id = 0
            if not row_id or row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            items.append(row)

    try:
        base_q = db.query(models.MediaItem).order_by(
            models.MediaItem.rating.desc(),
            models.MediaItem.created_at.desc(),
            models.MediaItem.id.desc(),
        )
        top_genres = [g for g, _w in sorted(seed_weights.items(), key=lambda kv: kv[1], reverse=True)[:6] if g]
        if top_genres:
            genre_filters = [models.MediaItem.genres.ilike(f"%{genre}%") for genre in top_genres]
            _append_candidates(base_q.filter(or_(*genre_filters)).limit(candidate_limit).all())
        if len(items) < min(candidate_limit, max(40, int(limit or OUT_LIMIT) * 6)):
            _append_candidates(base_q.limit(candidate_limit).all())
    except Exception:
        items = []

    scored: list[tuple[float, models.MediaItem]] = []
    for it in items:
        genres = _parse_genres(getattr(it, "genres", None))
        g_score = _genre_score(genres, seed_weights)
        r_score = _rating_score(getattr(it, "rating", None))
        c_score = _recency_score(getattr(it, "created_at", None))
        score = (WEIGHT_GENRE * g_score) + (WEIGHT_RATING * r_score) + (WEIGHT_RECENCY * c_score)

        try:
            item_id = int(getattr(it, "id", 0) or 0)
        except Exception:
            item_id = 0
        if item_id and item_id in watched_ids:
            score *= WATCHED_PENALTY

        scored.append((score, it))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[: max(1, int(limit or OUT_LIMIT))]]


def get_in_progress(
    db,
    *,
    profile_id: str | None = None,
    limit: int = 20,
) -> list[tuple[models.MediaItem, models.PlayHistory, float]]:
    q = (
        db.query(models.PlayHistory, models.MediaItem)
        .join(models.MediaItem, models.MediaItem.id == models.PlayHistory.media_item_id)
        .filter(models.PlayHistory.last_position.is_not(None))
        .filter(models.PlayHistory.last_duration.is_not(None))
        .filter(models.PlayHistory.last_duration > 0)
        .order_by(models.PlayHistory.updated_at.desc(), models.PlayHistory.last_played.desc())
    )
    if profile_id:
        q = q.filter(models.PlayHistory.profile_id == profile_id)
    rows = q.limit(max(5, int(limit or 20)) * 3).all()
    out: list[tuple[models.MediaItem, models.PlayHistory, float]] = []
    for ph, it in rows:
        try:
            if getattr(ph, "watched", False):
                continue
            pos = float(ph.last_position or 0)
            dur = float(ph.last_duration or 0)
        except Exception:
            continue
        if dur <= 0:
            continue
        ratio = pos / dur
        if ratio >= 0.95:
            continue
        if pos < 5:
            continue
        out.append((it, ph, ratio))
        if len(out) >= max(1, int(limit or 20)):
            break
    return out


def record_play(db, *, media_item_id: int, profile_id: str | None = None) -> dict:
    now = datetime.datetime.utcnow()
    row_q = db.query(models.PlayHistory).filter(models.PlayHistory.media_item_id == media_item_id)
    if profile_id:
        row_q = row_q.filter(models.PlayHistory.profile_id == profile_id)
    row = row_q.first()
    if not row:
        row = models.PlayHistory(media_item_id=media_item_id, profile_id=profile_id)
        row.play_count = 0
    row.play_count = int(row.play_count or 0) + 1
    row.last_played = now
    row.updated_at = now
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return {"ok": False, "detail": "db_commit_failed"}
    return {"ok": True, "play_count": row.play_count, "watched": bool(row.watched)}


def record_progress(
    db,
    *,
    media_item_id: int,
    profile_id: str | None = None,
    position: float | None = None,
    duration: float | None = None,
    path: str | None = None,
) -> dict:
    now = datetime.datetime.utcnow()
    row_q = db.query(models.PlayHistory).filter(models.PlayHistory.media_item_id == media_item_id)
    if profile_id:
        row_q = row_q.filter(models.PlayHistory.profile_id == profile_id)
    row = row_q.first()
    if not row:
        row = models.PlayHistory(media_item_id=media_item_id, profile_id=profile_id)
        row.play_count = 0
    if position is not None:
        pos = _safe_non_negative_float(position)
        if pos is not None:
            row.last_position = pos
    if duration is not None:
        dur = _safe_non_negative_float(duration)
        if dur is not None and dur > 0:
            row.last_duration = dur
    if path is not None:
        p = str(path).strip()
        if p:
            row.last_path = p
    row.last_played = now
    row.updated_at = now
    try:
        if row.last_duration and row.last_position is not None and float(row.last_duration) > 0:
            ratio = float(row.last_position) / float(row.last_duration)
            if ratio >= 0.92:
                row.watched = True
    except Exception:
        pass
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return {"ok": False, "detail": "db_commit_failed"}
    return {
        "ok": True,
        **build_progress_snapshot(
            position=row.last_position,
            duration=row.last_duration,
            path=row.last_path,
            watched=bool(row.watched),
        ),
    }


def get_progress(db, *, media_item_id: int, profile_id: str | None = None) -> dict:
    row_q = db.query(models.PlayHistory).filter(models.PlayHistory.media_item_id == media_item_id)
    if profile_id:
        row_q = row_q.filter(models.PlayHistory.profile_id == profile_id)
    row = row_q.first()
    if not row:
        return {"ok": True, **build_progress_snapshot(position=0, duration=0, path=None, watched=False)}
    return {
        "ok": True,
        **build_progress_snapshot(
            position=row.last_position,
            duration=row.last_duration,
            path=row.last_path,
            watched=bool(row.watched),
        ),
    }
