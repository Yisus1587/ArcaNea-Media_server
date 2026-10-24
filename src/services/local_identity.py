from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SIDECAR_NAMES = (
    "id_mal.json",
    "id_mal.txt",
    "mal_id.json",
    "mal_id.txt",
    "mal.json",
    "mal.txt",
    "tmdb_id.json",
    "tmdb_id.txt",
    "tmdb.json",
    "kitsu_id.json",
    "kitsu_id.txt",
    "kitsu.json",
    "provider_id.json",
    "provider_id.txt",
    "id.json",
    "id.txt",
)

SIDECAR_NAME_SET = {name.lower() for name in SIDECAR_NAMES}


def is_identity_sidecar(path: str | Path) -> bool:
    try:
        name = Path(path).name.lower()
    except Exception:
        return False
    if name in SIDECAR_NAME_SET:
        return True
    return bool(re.search(r"(?i)\.(?:id_mal|mal_id|tmdb_id|kitsu_id|provider_id|id)\.(?:json|txt)$", name))


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _digits(value: Any) -> str | None:
    text = _first_text(value)
    if not text:
        return None
    match = re.search(r"(\d{1,12})", text)
    return match.group(1) if match else text


def _provider_from_text(text: str | None) -> str | None:
    low = str(text or "").strip().lower()
    if "myanimelist.net" in low or "mal" in low or "jikan" in low:
        return "jikan"
    if "themoviedb.org" in low or "tmdb" in low:
        return "tmdb"
    if "kitsu" in low:
        return "kitsu"
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except UnicodeDecodeError:
        with path.open("r", encoding="latin-1") as fh:
            data = json.load(fh)
    if isinstance(data, dict):
        return data
    return {"id": data}


def _identity_from_json(path: Path, data: dict[str, Any]) -> dict[str, str]:
    provider = _provider_from_text(data.get("provider") or data.get("source") or data.get("url") or path.name)
    mal_id = _digits(
        data.get("id_mal")
        or data.get("mal_id")
        or data.get("mal")
        or data.get("myanimelist_id")
        or (data.get("id") if provider == "jikan" or "mal" in path.name.lower() else None)
    )
    tmdb_id = _digits(
        data.get("tmdb_id")
        or data.get("tmdb")
        or data.get("themoviedb_id")
        or (data.get("id") if provider == "tmdb" or "tmdb" in path.name.lower() else None)
    )
    kitsu_id = _digits(
        data.get("kitsu_id")
        or data.get("kitsu")
        or (data.get("id") if provider == "kitsu" or "kitsu" in path.name.lower() else None)
    )

    # Backward compatible generic id.json in anime folders usually means MAL.
    if not any((mal_id, tmdb_id, kitsu_id)) and path.name.lower() in ("id.json", "id.txt"):
        generic = _digits(data.get("id") or data.get("provider_id"))
        if generic:
            if provider == "tmdb":
                tmdb_id = generic
            elif provider == "kitsu":
                kitsu_id = generic
            else:
                provider = "jikan"
                mal_id = generic

    out: dict[str, str] = {}
    if mal_id:
        out["mal_id"] = str(mal_id)
        out.setdefault("provider", "jikan")
        out.setdefault("provider_id", str(mal_id))
    if tmdb_id:
        out["tmdb_id"] = str(tmdb_id)
        out.setdefault("provider", "tmdb")
        out.setdefault("provider_id", str(tmdb_id))
    if kitsu_id:
        out["kitsu_id"] = str(kitsu_id)
        out.setdefault("provider", "kitsu")
        out.setdefault("provider_id", str(kitsu_id))
    if provider and "provider" not in out:
        out["provider"] = provider
    if out:
        out["source_path"] = str(path)
    return out


def _identity_from_text(path: Path, text: str) -> dict[str, str]:
    provider = _provider_from_text(text) or _provider_from_text(path.name)
    ident = _digits(text)
    if not ident:
        return {}
    low = path.name.lower()
    if provider == "tmdb" or "tmdb" in low:
        return {"tmdb_id": ident, "provider": "tmdb", "provider_id": ident, "source_path": str(path)}
    if provider == "kitsu" or "kitsu" in low:
        return {"kitsu_id": ident, "provider": "kitsu", "provider_id": ident, "source_path": str(path)}
    return {"mal_id": ident, "provider": "jikan", "provider_id": ident, "source_path": str(path)}


def read_identity_file(path: str | Path) -> dict[str, str]:
    p = Path(path)
    try:
        if not p.is_file():
            return {}
        if p.suffix.lower() == ".json":
            return _identity_from_json(p, _read_json(p))
        text = p.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="latin-1").strip()
        except Exception:
            return {}
    except Exception:
        return {}
    return _identity_from_text(p, text)


def find_local_identity(path: str | Path) -> dict[str, str]:
    """Find provider ID sidecars near a folder/file without calling the network."""
    p = Path(path)
    candidates: list[Path] = []
    try:
        base = p if p.is_dir() else p.parent
    except Exception:
        base = p.parent
    for name in SIDECAR_NAMES:
        candidates.append(base / name)
    if p.is_file():
        stem = p.stem
        for suffix in (".json", ".txt"):
            for token in ("id_mal", "mal_id", "tmdb_id", "kitsu_id", "id"):
                candidates.append(base / f"{stem}.{token}{suffix}")
                candidates.append(base / f"{stem}.{suffix.lstrip('.')}")

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        ident = read_identity_file(candidate)
        if ident:
            return ident
    return {}
