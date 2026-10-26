"""Sidecar subtitle discovery and delivery.

MKV/MP4 files usually carry their own subtitle tracks, and the native player
reads those straight out of the container. What it cannot guess is a *sidecar*
file dropped next to the video (`Movie.es.srt`, `Movie.en.forced.srt`, ...),
which is how most libraries ship extra languages.

`/api/stream` is not usable for those: it only serves paths present in
`FileRecord` (i.e. media that the scanner ingested), and subtitles are not
scanned. So this router lists the sidecar files for a video and serves their
bytes, with the same "inside a media root" guard the stream endpoint uses.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .stream import _is_under_roots

router = APIRouter()

# ExoPlayer's SubtitleDecoderFactory understands exactly these text formats.
# (`.sub`/`.idx` are MicroDVD/VobSub and are deliberately excluded: no parser.)
_SUBTITLE_FORMATS: dict[str, tuple[str, str]] = {
    ".srt": ("srt", "application/x-subrip"),
    ".vtt": ("vtt", "text/vtt"),
    ".ass": ("ass", "text/x-ssa"),
    ".ssa": ("ssa", "text/x-ssa"),
}

# ISO 639-1/639-2 plus the Spanish-language tags that show up in the wild.
_LANGUAGE_LABELS: dict[str, str] = {
    "es": "Español",
    "spa": "Español",
    "esp": "Español",
    "lat": "Latino",
    "latino": "Latino",
    "en": "Inglés",
    "eng": "Inglés",
    "pt": "Portugués",
    "por": "Portugués",
    "fr": "Francés",
    "fra": "Francés",
    "fre": "Francés",
    "de": "Alemán",
    "deu": "Alemán",
    "ger": "Alemán",
    "it": "Italiano",
    "ita": "Italiano",
    "ja": "Japonés",
    "jpn": "Japonés",
    "ko": "Coreano",
    "kor": "Coreano",
    "zh": "Chino",
    "zho": "Chino",
    "chi": "Chino",
    "ru": "Ruso",
    "rus": "Ruso",
}

_FLAG_LABELS: dict[str, str] = {
    "forced": "forzado",
    "sdh": "SDH",
    "cc": "CC",
    "hi": "SDH",
}

_MAX_TRACKS = 24
_MAX_SUBTITLE_BYTES = 16 * 1024 * 1024


def _resolve_media_path(raw: str) -> Path:
    """Turn a client path into an absolute path inside a media root."""
    value = str(raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="path is required")
    if value.startswith("gdrive://"):
        # Google Drive ids are not files on disk and have no sidecars.
        raise HTTPException(status_code=400, detail="google drive paths have no sidecar subtitles")
    try:
        candidate = Path(os.path.abspath(value))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    if not _is_under_roots(str(candidate)):
        raise HTTPException(status_code=403, detail="path not allowed")
    return candidate


def _describe_suffix(stem: str, video_stem: str) -> tuple[str, str | None]:
    """Return (label, language) for the part after the video name.

    `Movie.es.srt` -> ("Español", "es"); `Movie.en.forced.srt` ->
    ("Inglés (forzado)", "en"); `Movie.srt` -> ("Predeterminado", None).
    """
    remainder = stem[len(video_stem):]
    tokens = [t for t in re.split(r"[._\-\s()\[\]]+", remainder) if t]
    language: str | None = None
    label_parts: list[str] = []
    for token in tokens:
        low = token.lower()
        if language is None and low in _LANGUAGE_LABELS:
            language = low
            label_parts.append(_LANGUAGE_LABELS[low])
            continue
        if language is None and re.fullmatch(r"[a-z]{2}", low):
            # Unknown two-letter tag: better to show it than to hide the file.
            language = low
            label_parts.append(low.upper())
            continue
        if low in _FLAG_LABELS:
            label_parts.append(_FLAG_LABELS[low])
            continue
        if low.isdigit() and label_parts:
            label_parts.append(f"#{low}")
            continue
    if not label_parts:
        return "Predeterminado", language
    return " ".join(label_parts), language


@router.get("/api/subtitles/tracks")
def list_subtitle_tracks(path: str) -> dict:
    """List the sidecar subtitle files sitting next to a video file."""
    video = _resolve_media_path(path)
    if not video.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    folder = video.parent
    video_stem = video.stem
    lowered = video_stem.lower()
    tracks: list[dict] = []
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read folder: {exc}")

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        fmt = _SUBTITLE_FORMATS.get(entry.suffix.lower())
        if not fmt:
            continue
        name = entry.stem
        low = name.lower()
        if low != lowered and not low.startswith(lowered):
            continue
        remainder = name[len(video_stem):]
        # "Movie2.es.srt" must not match "Movie.mkv", and neither must
        # "Movie (2024) 2.es.srt". A separator is required; a bare space is
        # only accepted when what follows is a known language tag, which is the
        # only spelling that shows up in practice ("Movie - es.srt" is not).
        if remainder and remainder[0] not in "._-[(":
            spaced_tags = [t for t in re.split(r"[._\-\s()\[\]]+", remainder.strip()) if t]
            if not remainder.startswith(" ") or not spaced_tags or spaced_tags[0].lower() not in _LANGUAGE_LABELS:
                continue
        label, language = _describe_suffix(name, video_stem)
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        tracks.append(
            {
                "label": label,
                "language": language,
                "format": fmt[0],
                "mime": fmt[1],
                "path": str(entry),
                "size": size,
                "forced": any(token in low for token in ("forced", " forzado")),
            }
        )

    tracks.sort(key=lambda t: (bool(t.get("forced")), str(t.get("label") or "")))
    return {"path": str(video), "tracks": tracks[:_MAX_TRACKS]}


def _decode_text(raw: bytes) -> str:
    """Decode a subtitle file to text, tolerating latin-1/CP1252 sources.

    Spanish libraries are full of CP1252 `.srt` files; serving them as-is makes
    every accent render as mojibake because ExoPlayer assumes UTF-8.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # CP1252 is a superset of latin-1 for the printable range and is what
    # Windows-produced subtitles use. `errors="replace"` keeps a corrupt byte
    # from breaking the whole track.
    try:
        return raw.decode("cp1252", errors="replace")
    except Exception:
        return raw.decode("latin-1", errors="replace")


@router.get("/api/subtitles/file")
def serve_subtitle_file(path: str, request: Request) -> Response:
    """Return a sidecar subtitle file as UTF-8 text.

    No Range support on purpose: these files are a few kilobytes and the
    encoding conversion would make byte offsets meaningless.
    """
    subtitle = _resolve_media_path(path)
    fmt = _SUBTITLE_FORMATS.get(subtitle.suffix.lower())
    if not fmt:
        raise HTTPException(status_code=400, detail="not a supported subtitle format")
    if not subtitle.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    try:
        size = subtitle.stat().st_size
        if size > _MAX_SUBTITLE_BYTES:
            raise HTTPException(status_code=413, detail="subtitle file too large")
        raw = subtitle.read_bytes()
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read subtitle: {exc}")

    body = _decode_text(raw).encode("utf-8")
    return Response(
        content=body,
        media_type=f"{fmt[1]}; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
