from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable

from sqlalchemy import or_

from ..core.config import get
from ..db import SessionLocal
from ..db.models import Episode, EpisodeFile, FileRecord, MediaItem, MediaRoot, Season, SeasonItem, Series
from ..services.db_commit import commit_and_report
from ..services.local_identity import find_local_identity
from ..services.scan_state import get_state as get_scan_state
from ..services.scan_state import set_state as set_scan_state
from ..utils.media_exts import COMMON_VIDEO_EXTS, get_media_extensions, resolve_media_extensions
from ..utils.path_hash import hash_path
from ..utils.utils import normalize_folder_name_for_search

logger = logging.getLogger(__name__)


GENERIC_SEASON_RE = re.compile(
    r"""(?ix)
    ^
    (?:
        (?:temporada|season|temp|part|cour)\s*0*(?P<n1>\d{1,3})(?:st|nd|rd|th|ra|da|ta|ma|na|va|ba|era|ero|er|ª|a|o)?
        |
        s(?:eason)?\s*0*(?P<n2>\d{1,3})
        |
        0*(?P<n3>\d{1,3})(?:st|nd|rd|th|ra|da|ta|ma|na|va|ba|era|ero|er|ª|a|o)?\s*(?:temporada|season|temp|part|cour)
    )
    $
    """
)


SPECIALS_RE = re.compile(r"(?i)^(specials?|especial(?:es)?|ovas?|onas?|extras?)$")


EPISODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})\b"),
    re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,4})\b"),
    re.compile(r"(?i)\b(?:ep|eps|episode|episodio|capitulo|capítulo|cap|chapter|chap)\.?\s*0*(?P<episode>\d{1,4})\b"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])E(?P<episode>\d{1,4})(?:$|[\s._\-\]\)])"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])0*(?P<episode>\d{1,4})(?:$|[\s._\-\]\)])"),
)


NOISE_NUMBER_RE = re.compile(
    r"(?i)\b(?:480|720|1080|2160|4320|4k|8k|x264|x265|h264|h265|hevc|aac|10bit|8bit)\b"
)


SEPARATOR_EPISODE_RE = re.compile(
    r"(?i)(?:^|[\s._\-\+\[\(])(?:e|ep|eps|episode|episodio|capitulo|capítulo|cap|chapter|chap)?\s*0*(?P<episode>\d{1,4})(?=$|[\s._\-\+\]\)]|\s*\[)"
)


EMBEDDED_CAP_RE = re.compile(
    r"(?i)(?:capitulo|capítulo|cap|episode|episodio|ep)\.?\s*0*(?P<episode>\d{1,4})\b"
)


def _normalize_root_type(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in ("anime", "animes"):
        return "anime"
    if raw in ("movie", "movies", "pelicula", "peliculas", "film", "films"):
        return "movie"
    if raw in ("series", "serie", "tv", "show", "shows"):
        return "series"
    return None


def _infer_root_type(path: str) -> str | None:
    s = str(path or "").lower()
    if "anime" in s or "animes" in s:
        return "anime"
    if any(k in s for k in ("peliculas", "pelicula", "movies", "movie", "films", "film", "cine")):
        return "movie"
    if any(k in s for k in ("series", "serie", "tv", "shows", "show")):
        return "series"
    return None


def _clean_title(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    try:
        variants = normalize_folder_name_for_search(text)
        for item in variants:
            item = str(item or "").strip()
            if not item:
                continue
            if item.lower().startswith(("http://", "https://")):
                continue
            if item.isdigit() and item != text:
                continue
            if item:
                return item
    except Exception:
        pass
    text = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)
    text = re.sub(r"[\._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    return text or "Unknown"


def _season_number_from_folder(name: str | None) -> int | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    if SPECIALS_RE.match(raw):
        return 0
    m = GENERIC_SEASON_RE.match(raw)
    if not m:
        return None
    for key in ("n1", "n2", "n3"):
        val = m.groupdict().get(key)
        if val:
            try:
                return int(val)
            except Exception:
                return None
    return None


def _episode_number_from_filename(path: str, fallback: int, title_hint: str | None = None) -> int:
    stem = Path(path).stem
    probe = NOISE_NUMBER_RE.sub(" ", stem)
    if title_hint:
        probe = _strip_title_prefix_for_episode_probe(probe, str(title_hint))
    found = _first_episode_match(SEPARATOR_EPISODE_RE, probe)
    if found is not None:
        return found
    found = _last_episode_match(EMBEDDED_CAP_RE, probe)
    if found is not None:
        return found
    for rx in EPISODE_PATTERNS:
        found = _last_episode_match(rx, probe)
        if found is not None:
            return found
    return int(fallback)


def _last_episode_match(pattern: re.Pattern[str], probe: str) -> int | None:
    best: int | None = None
    for m in pattern.finditer(probe):
        raw = m.groupdict().get("episode")
        if not raw:
            continue
        try:
            n = int(raw)
        except Exception:
            continue
        if 0 <= n <= 9999:
            best = n
    return best


def _first_episode_match(pattern: re.Pattern[str], probe: str) -> int | None:
    for m in pattern.finditer(probe):
        raw = m.groupdict().get("episode")
        if not raw:
            continue
        try:
            n = int(raw)
        except Exception:
            continue
        if 0 <= n <= 9999:
            return n
    return None


def _strip_title_prefix_for_episode_probe(probe: str, title_hint: str) -> str:
    """Remove title-like prefix so season/title numbers do not beat episode numbers.

    Examples:
    - "Ore ... 2 - 01 [BD]" -> " - 01 [BD]"
    - "Title Part 3 - 04" -> " - 04"
    """
    text = str(probe or "")
    hint = str(title_hint or "").strip()
    if not text or not hint:
        return text
    text_norm = _episode_probe_key(text)
    hint_norm = _episode_probe_key(hint)
    # Common anime pattern: "<full title including season number> - 01 [...]".
    for sep in (" - ", " – ", " — ", "_", "+"):
        positions = [m.start() for m in re.finditer(re.escape(sep), text)]
        for idx in positions:
            if idx <= 0:
                continue
            tail = text[idx:]
            if not re.search(r"\d{1,4}", tail):
                continue
            head = text[:idx]
            head_key = _episode_probe_key(head)
            if hint_norm and (hint_norm in head_key or head_key in hint_norm or _title_similarity_tokens(head_key, hint_norm) >= 0.6):
                return tail
    if hint_norm and text_norm.startswith(hint_norm):
        return re.sub(re.escape(hint), "", text, count=1, flags=re.IGNORECASE).strip() or text
    return text


def _episode_probe_key(value: str) -> str:
    s = str(value or "").lower()
    s = NOISE_NUMBER_RE.sub(" ", s)
    s = re.sub(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_similarity_tokens(a: str, b: str) -> float:
    aa = {x for x in str(a or "").split() if len(x) > 1}
    bb = {x for x in str(b or "").split() if len(x) > 1}
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, min(len(aa), len(bb)))


def _media_id_for_path(path: str) -> str:
    return hash_path(path)


def _apply_local_identity(mi: MediaItem, identity: dict[str, str]) -> None:
    if not identity:
        return
    try:
        status = str(getattr(mi, "status", "") or "").strip().upper()
    except Exception:
        status = ""
    if status in ("MANUAL", "OMITTED"):
        return
    mal_id = str(identity.get("mal_id") or "").strip()
    tmdb_id = str(identity.get("tmdb_id") or "").strip()
    kitsu_id = str(identity.get("kitsu_id") or "").strip()
    provider = str(identity.get("provider") or "").strip().lower()
    provider_id = str(identity.get("provider_id") or "").strip()
    if mal_id:
        mi.mal_id = mal_id
        mi.jikan_anchor_mal_id = getattr(mi, "jikan_anchor_mal_id", None) or mal_id
        provider = provider or "jikan"
        provider_id = provider_id or mal_id
    if tmdb_id:
        mi.tmdb_id = tmdb_id
        provider = provider or "tmdb"
        provider_id = provider_id or tmdb_id
    # There is no dedicated kitsu_id column on MediaItem; keep it as provider/provider_id.
    if kitsu_id and not provider_id:
        provider = provider or "kitsu"
        provider_id = kitsu_id
    if provider in ("jikan", "tmdb", "kitsu"):
        mi.provider = provider
    if provider_id:
        mi.provider_id = provider_id
    if mal_id or tmdb_id or kitsu_id:
        mi.is_identified = False
        if status not in ("ENRICHED", "MANUAL"):
            mi.status = "SCANNED"


class Scanner:
    """Filesystem-only scanner.

    Responsibilities:
    - validate roots and root type
    - index media files
    - create local MediaItem/FileRecord rows
    - create local Series/Season/Episode structure from folder layout + episode numbers

    It does not call providers, does not decide external metadata, and does not use
    episode names as metadata. File names are only parsed for episode numbering.
    """

    def __init__(
        self,
        db_session_factory=None,
        media_extensions: Iterable[str] | None = None,
        *,
        discovery_only: bool = False,
    ):
        self.db_session_factory = db_session_factory or SessionLocal
        self._common_video_exts = set(COMMON_VIDEO_EXTS)
        self.media_exts = set(resolve_media_extensions(media_extensions)) if media_extensions is not None else set(get_media_extensions())
        self.discovery_only = bool(discovery_only)
        self.roots: list[Path] = []
        self._root_types: dict[str, str] = {}
        self._load_roots()

    def _load_roots(self) -> None:
        seen: set[str] = set()
        for raw in get("media_roots") or []:
            try:
                path = raw.get("path") if isinstance(raw, dict) else str(raw).split("|", 1)[0]
                root_type = raw.get("type") if isinstance(raw, dict) else (str(raw).split("|", 1)[1] if "|" in str(raw) else None)
                rp = str(Path(str(path)).resolve())
            except Exception:
                continue
            if not rp or rp in seen:
                continue
            seen.add(rp)
            self.roots.append(Path(rp))
            rt = _normalize_root_type(root_type) or _infer_root_type(rp)
            if rt:
                self._root_types[rp] = rt

        session = self.db_session_factory()
        try:
            for row in session.query(MediaRoot).all():
                raw_path = str(getattr(row, "path", "") or "").strip()
                if not raw_path:
                    continue
                try:
                    rp = str(Path(raw_path).resolve())
                except Exception:
                    rp = raw_path
                if rp not in seen:
                    seen.add(rp)
                    self.roots.append(Path(rp))
                rt = _normalize_root_type(getattr(row, "type", None)) or _infer_root_type(rp)
                if rt:
                    self._root_types[rp] = rt
        finally:
            session.close()

    def is_media_file(self, path: str) -> bool:
        try:
            p = Path(path)
            return p.is_file() and p.suffix.lower() in self.media_exts
        except Exception:
            return False

    def _best_matching_root(self, path: str) -> str | None:
        try:
            p = str(Path(path).resolve()).rstrip("\\/").lower()
        except Exception:
            p = str(path or "").rstrip("\\/").lower()
        best: str | None = None
        best_len = -1
        for root in self.roots:
            try:
                r = str(Path(root).resolve()).rstrip("\\/")
            except Exception:
                r = str(root).rstrip("\\/")
            rc = r.lower()
            if p == rc or p.startswith(rc + "\\") or p.startswith(rc + "/"):
                if len(rc) > best_len:
                    best = r
                    best_len = len(rc)
        return best

    def _contained_roots_for(self, path: Path) -> list[Path]:
        try:
            p = str(path.resolve()).rstrip("\\/").lower()
        except Exception:
            p = str(path).rstrip("\\/").lower()
        out: list[Path] = []
        for root in self.roots:
            try:
                r = str(Path(root).resolve()).rstrip("\\/")
            except Exception:
                r = str(root).rstrip("\\/")
            rc = r.lower()
            if rc == p or rc.startswith(p + "\\") or rc.startswith(p + "/"):
                out.append(Path(r))
        return sorted(out, key=lambda x: str(x).lower())

    def _is_under_roots(self, path: Path) -> bool:
        try:
            return self._best_matching_root(str(path.resolve())) is not None
        except Exception:
            return False

    def _root_type(self, root: str | None) -> str:
        if not root:
            return "series"
        return self._root_types.get(root) or _infer_root_type(root) or "series"

    def _iter_media_files_in_folder(self, folder: str) -> list[str]:
        out: list[str] = []
        try:
            for name in os.listdir(folder):
                full = os.path.join(folder, name)
                if self.is_media_file(full):
                    try:
                        out.append(str(Path(full).resolve()))
                    except Exception:
                        out.append(os.path.abspath(full))
        except Exception:
            return []
        return sorted(out, key=lambda p: (Path(p).stem.lower(), p.lower()))

    def scan_path(self, path: str):
        p = Path(str(path))
        try:
            p = p.resolve()
        except Exception:
            pass
        if not self._is_under_roots(p):
            for root in self._contained_roots_for(p):
                self.scan_path(str(root))
            return
        try:
            st = get_scan_state()
        except Exception:
            st = {}
        if st.get("status") != "scanning":
            set_scan_state({"status": "scanning", "phase": "scan", "current": str(p)})
        if p.is_file():
            if self.is_media_file(str(p)):
                self._upsert_file(str(p))
            return
        if not p.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(str(p)):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() != "@eadir"]
            files = [
                os.path.join(dirpath, name)
                for name in filenames
                if Path(name).suffix.lower() in self.media_exts
            ]
            if files:
                set_scan_state({"current": str(dirpath)})
                self._upsert_folder(str(dirpath), files)

    def _upsert_file(self, fullpath: str):
        folder = str(Path(fullpath).parent)
        self._upsert_folder(folder, [fullpath])

    def _context_for_folder(self, folder: str) -> dict[str, object] | None:
        root = self._best_matching_root(folder)
        if not root:
            return None
        root_type = self._root_type(root)
        try:
            rel_parts = Path(folder).resolve().relative_to(Path(root).resolve()).parts
        except Exception:
            rel_parts = (Path(folder).name,)
        folder_name = Path(folder).name
        parent_name = Path(folder).parent.name
        generic_season = _season_number_from_folder(folder_name)

        if root_type == "movie":
            base_title = _clean_title(folder_name)
            season_number = None
            series_key = None
            media_type = "movie"
        else:
            if generic_season is not None and len(rel_parts) >= 2:
                base_title = _clean_title(parent_name)
                season_number = int(generic_season)
            else:
                base_title = _clean_title(folder_name)
                season_number = 1
            media_type = "series" if root_type in ("series", "anime") else root_type
            if root_type == "anime":
                series_key = f"anime_{hash_path(folder)[:12]}"
            else:
                root_hash = hash_path(root)[:10]
                series_key = f"local_{root_hash}_{_series_slug(base_title)}"

        title = base_title
        if season_number is not None and season_number > 1 and generic_season is not None:
            title = f"{base_title} - Temporada {season_number}"
        elif season_number == 0:
            title = f"{base_title} - Especiales"
        return {
            "root": root,
            "root_type": root_type,
            "media_type": media_type,
            "base_title": base_title,
            "title": title,
            "season_number": season_number,
            "series_key": series_key,
        }

    def _upsert_folder(
        self,
        folder: str,
        media_files: list[str],
        *,
        skip_grouping: bool = False,
        title_seed_override: str | None = None,
        media_type_override: str | None = None,
        canonical_path_override: str | None = None,
        discovery_only_override: bool | None = None,
        **_ignored,
    ):
        canonical_folder = str(Path(canonical_path_override or folder).resolve())
        ctx = self._context_for_folder(canonical_folder)
        if not ctx:
            return
        all_files = self._iter_media_files_in_folder(canonical_folder) or [str(Path(x).resolve()) for x in media_files]
        if not all_files:
            return
        session = self.db_session_factory()
        try:
            if ctx["root_type"] == "movie":
                for full in all_files:
                    self._upsert_movie_file(session, full, str(ctx["root"]))
                commit_and_report(session, f"scan:{canonical_folder}")
                return

            path_hash = hash_path(canonical_folder)
            media_id = _media_id_for_path(canonical_folder)
            mi = (
                session.query(MediaItem)
                .filter(or_(MediaItem.canonical_path_hash == path_hash, MediaItem.canonical_path == canonical_folder))
                .order_by(MediaItem.id.asc())
                .first()
            )
            created = False
            if mi is None:
                mi = MediaItem(media_id=media_id, canonical_path=canonical_folder, canonical_path_hash=path_hash)
                created = True
            mi.canonical_path = canonical_folder
            mi.canonical_path_hash = path_hash
            mi.media_id = mi.media_id or media_id
            mi.media_root = str(ctx["root"])
            mi.library_type = str(ctx["root_type"])
            mi.media_type = str(media_type_override or ctx["media_type"])
            mi.base_title = str(title_seed_override or ctx["base_title"])
            if created or not getattr(mi, "is_identified", False):
                mi.title = str(title_seed_override or ctx["title"])
            if not mi.status:
                mi.status = "SCANNED"
            if getattr(mi, "is_identified", None) is None:
                mi.is_identified = False
            _apply_local_identity(mi, find_local_identity(canonical_folder))
            try:
                mi.search_titles = json.dumps(_search_titles_for(mi.base_title, Path(canonical_folder).name), ensure_ascii=False)
            except Exception:
                pass
            session.add(mi)
            session.flush()

            file_rows: list[FileRecord] = []
            for idx, full in enumerate(all_files, start=1):
                try:
                    norm = str(Path(full).resolve())
                except Exception:
                    norm = os.path.abspath(full)
                mtime, size = _stat(norm)
                ep_num = _episode_number_from_filename(
                    norm,
                    idx,
                    str(ctx.get("base_title") or Path(canonical_folder).name),
                )
                fr = session.query(FileRecord).filter(FileRecord.path == norm).first()
                if fr is None:
                    fr = FileRecord(path=norm)
                fr.media_item_id = mi.id
                fr.size = size
                fr.mtime = mtime
                fr.file_index = ep_num
                session.add(fr)
                session.flush()
                file_rows.append(fr)

            if not skip_grouping and ctx["root_type"] != "movie":
                self._sync_series_tree(session, mi, ctx, file_rows)

            commit_and_report(session, f"scan:{canonical_folder}")
        except Exception:
            session.rollback()
            logger.exception("Filesystem scan upsert failed for %s", canonical_folder)
            raise
        finally:
            session.close()

    def _upsert_movie_file(self, session, fullpath: str, media_root: str) -> None:
        try:
            canonical_path = str(Path(fullpath).resolve())
        except Exception:
            canonical_path = os.path.abspath(fullpath)
        path_hash = hash_path(canonical_path)
        title_seed = Path(canonical_path).stem
        try:
            parent = Path(canonical_path).parent
            root_path = Path(media_root).resolve()
            parent_path = parent.resolve()
            stem_clean = _clean_title(title_seed)
            if parent_path != root_path and _looks_like_generic_movie_stem(stem_clean):
                title_seed = parent.name
        except Exception:
            pass
        title = _clean_title(title_seed)
        media_id = _media_id_for_path(canonical_path)
        mi = (
            session.query(MediaItem)
            .filter(or_(MediaItem.canonical_path_hash == path_hash, MediaItem.canonical_path == canonical_path))
            .order_by(MediaItem.id.asc())
            .first()
        )
        created = False
        if mi is None:
            mi = MediaItem(media_id=media_id, canonical_path=canonical_path, canonical_path_hash=path_hash)
            created = True
        mi.canonical_path = canonical_path
        mi.canonical_path_hash = path_hash
        mi.media_id = mi.media_id or media_id
        mi.media_root = media_root
        mi.library_type = "movie"
        mi.media_type = "movie"
        mi.base_title = title
        if created or not getattr(mi, "is_identified", False):
            mi.title = title
        if not mi.status:
            mi.status = "SCANNED"
        if getattr(mi, "is_identified", None) is None:
            mi.is_identified = False
        _apply_local_identity(mi, find_local_identity(canonical_path))
        _clear_duplicate_movie_tmdb_identity(session, mi)
        try:
            search_sources = [title, Path(canonical_path).stem]
            try:
                parent = Path(canonical_path).parent.resolve()
                root_path = Path(media_root).resolve()
                if parent != root_path:
                    search_sources.append(Path(canonical_path).parent.name)
            except Exception:
                pass
            mi.search_titles = json.dumps(_search_titles_for(*search_sources), ensure_ascii=False)
        except Exception:
            pass
        session.add(mi)
        session.flush()

        mtime, size = _stat(canonical_path)
        fr = session.query(FileRecord).filter(FileRecord.path == canonical_path).first()
        if fr is None:
            fr = FileRecord(path=canonical_path)
        fr.media_item_id = mi.id
        fr.size = size
        fr.mtime = mtime
        fr.file_index = 1
        session.add(fr)

    def _sync_series_tree(self, session, mi: MediaItem, ctx: dict[str, object], files: list[FileRecord]) -> None:
        series_key = str(ctx.get("series_key") or "")
        if not series_key:
            return
        series = session.query(Series).filter(Series.provider_id == series_key).first()
        if series is None:
            series = Series(
                title=str(ctx["base_title"]),
                title_en=str(ctx["base_title"]),
                provider_id=series_key,
            )
            session.add(series)
            session.flush()
        season_number = int(ctx.get("season_number") if ctx.get("season_number") is not None else 1)
        si = session.query(SeasonItem).filter(SeasonItem.media_item_id == mi.id).first()
        season = session.query(Season).filter(Season.id == si.season_id).first() if si else None
        if season is None:
            season = (
                session.query(Season)
                .filter(Season.series_id == series.id, Season.season_number == season_number)
                .first()
            )
            if season is not None:
                taken = session.query(SeasonItem.id).filter(SeasonItem.season_id == season.id).first()
                if taken:
                    season = None
        if season is None:
            season = Season(series_id=series.id, season_number=season_number)
            session.add(season)
            session.flush()
        else:
            season.series_id = series.id
            season.season_number = season_number
            session.add(season)
        if si is None:
            si = SeasonItem(season_id=season.id, media_item_id=mi.id, media_id=mi.media_id)
        else:
            si.season_id = season.id
            si.media_id = mi.media_id
        session.add(si)
        session.flush()

        for fr in files:
            ep_num = int(fr.file_index or 0)
            ep = (
                session.query(Episode)
                .filter(Episode.season_id == season.id, Episode.episode_number == ep_num)
                .first()
            )
            if ep is None:
                ep = Episode(
                    season_id=season.id,
                    episode_number=ep_num,
                    title=f"Episodio {ep_num}",
                    title_en=f"Episode {ep_num}",
                    title_localized=f"Episodio {ep_num}",
                )
                session.add(ep)
                session.flush()
            ef = session.query(EpisodeFile).filter(EpisodeFile.file_record_id == fr.id).first()
            if ef is None:
                ef = EpisodeFile(episode_id=ep.id, file_record_id=fr.id)
            else:
                ef.episode_id = ep.id
            session.add(ef)

    def normalize_pending_items(self, limit: int | None = None) -> int:
        return 0

    def scan_roots_sequential(self, roots: Iterable[str] | None = None, *, start_enrichment_after_scan: bool = False):
        for root in roots or [str(r) for r in self.roots]:
            self.scan_path(str(root))


def _series_slug(title: str) -> str:
    s = str(title or "").strip().lower()
    s = re.sub(r"[^a-z0-9áéíóúñü]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "unknown"


def _looks_like_generic_movie_stem(value: str | None) -> bool:
    try:
        text = str(value or "").strip()
    except Exception:
        return True
    if not text:
        return True
    if re.fullmatch(r"\d{1,3}", text):
        return True
    if re.fullmatch(r"(?i)(?:cd|disc|disk|part|parte)\s*\d{1,3}", text):
        return True
    if re.fullmatch(r"(?i)(?:movie|video|pelicula|película)", text):
        return True
    return False


def _search_titles_for(*values: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            variants = normalize_folder_name_for_search(value)
        except Exception:
            variants = [value]
        for variant in variants:
            text = str(variant or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def _stat(path: str) -> tuple[int | None, int | None]:
    try:
        st = os.stat(path)
        return int(st.st_mtime), int(st.st_size)
    except Exception:
        return None, None


def _clear_duplicate_movie_tmdb_identity(session, mi: MediaItem) -> None:
    """Keep filesystem scans from failing on stale duplicate movie TMDB IDs.

    The scanner does not validate external identities. If an old enrichment mapped
    multiple movie files to the same tmdb_id, updating the local file row can trip a
    unique index. Clear only the conflicting external identity on this movie row and
    let enrichment/manual mapping resolve it later.
    """
    try:
        tmdb_id = str(getattr(mi, "tmdb_id", "") or "").strip()
    except Exception:
        tmdb_id = ""
    if not tmdb_id:
        return
    try:
        other = (
            session.query(MediaItem.id)
            .filter(MediaItem.tmdb_id == tmdb_id, MediaItem.id != getattr(mi, "id", None))
            .first()
        )
    except Exception:
        other = None
    if not other:
        return
    try:
        mi.tmdb_id = None
        if str(getattr(mi, "provider", "") or "").strip().lower() == "tmdb":
            mi.provider = None
            mi.provider_id = None
        mi.is_identified = False
        if str(getattr(mi, "status", "") or "").strip().upper() not in ("MANUAL", "OMITTED"):
            mi.status = "SCANNED"
    except Exception:
        pass
