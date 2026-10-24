"""Apply minimal DB migrations to add new columns and tables required by updated models.

This script does:
- backup `data/arcanea.db` -> `data/arcanea.db.bak.TIMESTAMP`
- inspect `media_item` columns and add missing columns with `ALTER TABLE ... ADD COLUMN` for simple types
- create `media_image` table if not exists
- prints actions taken

Safe to run multiple times.
"""
from __future__ import annotations
import sqlite3
import shutil
import time
import os
import re
from pathlib import Path

try:
    from ..core.config import DATA_DIR, sqlite_path_from_url  # type: ignore
except Exception:  # pragma: no cover - ejecución fuera del paquete (imports planos)
    DATA_DIR = Path('data')
    sqlite_path_from_url = None  # type: ignore[assignment]

def _resolve_sqlite_db_path() -> Path | None:
    """Resolve the *actual* SQLite file path the app is using.

    Important: the runtime DB may be overridden via `ARCANEA_DB_URL`. Migrations must
    target that same file, otherwise SQLAlchemy models can drift from schema and crash.
    The URL -> path conversion lives in `core.config.sqlite_path_from_url`, so this tool
    and the application always agree (Linux/Windows path differences included).
    """
    try:
        db_url = os.environ.get("ARCANEA_DB_URL", "") or ""
    except Exception:
        db_url = ""
    if not db_url:
        try:
            from ..core.config import get as _get  # type: ignore

            db_url = str(_get("db_url") or "")
        except Exception:
            db_url = ""
    if sqlite_path_from_url is None:
        return None
    return sqlite_path_from_url(db_url)


DB_PATH = _resolve_sqlite_db_path() or (Path(DATA_DIR) / "arcanea.db")
BACKUP_DIR = DB_PATH.parent

ALTER_STATEMENTS = [
    ("mal_id", "TEXT"),
    ("jikan_type", "TEXT"),
    ("jikan_anchor_mal_id", "TEXT"),
    ("poster_path", "TEXT"),
    ("is_animated", "INTEGER"),
    ("origin", "TEXT"),
    ("release_year", "INTEGER"),
    ("runtime", "INTEGER"),
    ("cast", "TEXT"),
    ("tmdb_id", "TEXT"),
    ("backdrop_path", "TEXT"),
    ("rating", "REAL"),
    ("content_rating", "TEXT"),
    ("genres", "TEXT"),
    ("search_titles", "TEXT"),
    ("is_identified", "INTEGER"),
    # Phase 2: anchor + localized fields (single active localized language)
    ("title_en", "TEXT"),
    ("title_localized", "TEXT"),
    ("synopsis_localized", "TEXT"),
]

MEDIA_IMAGE_CREATE = '''
CREATE TABLE IF NOT EXISTS media_image (
    id INTEGER PRIMARY KEY,
    media_item_id INTEGER NOT NULL,
    source TEXT,
    source_url TEXT,
    local_path TEXT,
    priority INTEGER,
    created_at DATETIME
);
'''

SETTINGS_CREATE = '''
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME
);
'''

SERIES_CREATE = '''
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    provider_id TEXT,
    title_en TEXT,
    title_localized TEXT,
    mal_id TEXT,
    tmdb_id TEXT,
    jikan_type TEXT,
    tmdb_no_match INTEGER,
    tmdb_no_match_reason TEXT,
    year INTEGER,
    main_poster TEXT
);
'''

SEASON_CREATE = '''
CREATE TABLE IF NOT EXISTS season (
    id INTEGER PRIMARY KEY,
    series_id INTEGER,
    season_number INTEGER,
    title_en TEXT,
    title_localized TEXT,
    synopsis_localized TEXT,
    poster_path TEXT,
    backdrop_path TEXT,
    split_cour INTEGER,
    FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
);
'''

EPISODE_CREATE = '''
CREATE TABLE IF NOT EXISTS episode (
    id INTEGER PRIMARY KEY,
    season_id INTEGER,
    episode_number INTEGER,
    title TEXT,
    title_en TEXT,
    title_localized TEXT,
    synopsis_localized TEXT,
    still_path TEXT,
    FOREIGN KEY(season_id) REFERENCES season(id) ON DELETE CASCADE
);
'''

SEASON_ITEM_CREATE = '''
CREATE TABLE IF NOT EXISTS season_item (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL UNIQUE,
    media_item_id INTEGER NOT NULL UNIQUE,
    media_id TEXT,
    created_at DATETIME,
    FOREIGN KEY(season_id) REFERENCES season(id) ON DELETE CASCADE,
    FOREIGN KEY(media_item_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

EPISODE_FILE_CREATE = '''
CREATE TABLE IF NOT EXISTS episode_file (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL,
    file_record_id INTEGER NOT NULL UNIQUE,
    created_at DATETIME,
    FOREIGN KEY(episode_id) REFERENCES episode(id) ON DELETE CASCADE,
    FOREIGN KEY(file_record_id) REFERENCES file_record(id) ON DELETE CASCADE
);
'''

MEDIA_TRANSLATIONS_CREATE = '''
CREATE TABLE IF NOT EXISTS media_translations (
    id INTEGER PRIMARY KEY,
    path_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    title TEXT,
    overview TEXT,
    source TEXT NOT NULL,
    updated_at DATETIME,
    FOREIGN KEY(path_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

MANUAL_MAPPINGS_CREATE = '''
CREATE TABLE IF NOT EXISTS manual_mappings (
    id INTEGER PRIMARY KEY,
    media_item_id INTEGER NOT NULL UNIQUE,
    tmdb_id TEXT,
    media_type TEXT,
    season_number INTEGER,
    poster_url TEXT,
    backdrop_url TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    FOREIGN KEY(media_item_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

MANUAL_OVERRIDES_CREATE = '''
CREATE TABLE IF NOT EXISTS manual_overrides (
    id INTEGER PRIMARY KEY,
    media_item_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    title TEXT,
    overview TEXT,
    genres TEXT,
    episode_overrides TEXT,
    source TEXT NOT NULL,
    updated_at DATETIME,
    FOREIGN KEY(media_item_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

USER_LIST_CREATE = '''
CREATE TABLE IF NOT EXISTS user_list (
    id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL,
    media_item_id INTEGER NOT NULL,
    created_at DATETIME,
    FOREIGN KEY(media_item_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

PLAY_HISTORY_CREATE = '''
CREATE TABLE IF NOT EXISTS play_history (
    id INTEGER PRIMARY KEY,
    profile_id TEXT,
    media_item_id INTEGER NOT NULL,
    play_count INTEGER,
    last_played DATETIME,
    watched INTEGER,
    last_position REAL,
    last_duration REAL,
    last_path TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    FOREIGN KEY(media_item_id) REFERENCES media_item(id) ON DELETE CASCADE
);
'''

PLAY_HISTORY_ALTERS = [
    ("last_position", "REAL"),
    ("last_duration", "REAL"),
    ("last_path", "TEXT"),
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS ix_settings_updated ON settings(updated_at);",
    "CREATE INDEX IF NOT EXISTS ix_file_record_item_index ON file_record(media_item_id, file_index);",
    "CREATE INDEX IF NOT EXISTS ix_season_series_number ON season(series_id, season_number);",
    "CREATE INDEX IF NOT EXISTS ix_episode_season_number ON episode(season_id, episode_number);",
    "CREATE INDEX IF NOT EXISTS ix_episode_file_episode ON episode_file(episode_id);",
    "CREATE INDEX IF NOT EXISTS ix_season_item_media ON season_item(media_item_id);",
    "CREATE INDEX IF NOT EXISTS ix_season_item_media_id ON season_item(media_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_series_provider_id ON series(provider_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_series_tmdb_id ON series(tmdb_id);",
    "CREATE INDEX IF NOT EXISTS ix_series_tmdb_no_match ON series(tmdb_no_match);",
    "CREATE INDEX IF NOT EXISTS ix_media_translations_path_lang ON media_translations(path_id, language);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_media_translations_path_lang_source ON media_translations(path_id, language, source);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_manual_mappings_item ON manual_mappings(media_item_id);",
    "CREATE INDEX IF NOT EXISTS ix_manual_overrides_item_lang ON manual_overrides(media_item_id, language);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_list_profile_item ON user_list(profile_id, media_item_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_play_history_profile_item ON play_history(profile_id, media_item_id);",
    "CREATE INDEX IF NOT EXISTS ix_play_history_last_played ON play_history(last_played);",
    "DROP INDEX IF EXISTS ux_media_item_tmdb_movie;",
    "CREATE INDEX IF NOT EXISTS ix_media_item_tmdb_movie ON media_item(tmdb_id) WHERE tmdb_id IS NOT NULL AND tmdb_id <> '' AND (media_type = 'movie' OR media_type = 'film');",
]


def backup_db(db_path: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    # create a single backup file (overwrite previous) for safety
    dest = BACKUP_DIR / f"{db_path.name}.bak"
    shutil.copy2(db_path, dest)
    return dest


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info('{table}')")
    cols = {row[1] for row in cur.fetchall()}  # name is at index 1
    return cols


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
            (index_name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _index_name_from_stmt(stmt: str) -> str | None:
    try:
        # e.g. "CREATE INDEX IF NOT EXISTS ix_name ON table(col);"
        s = " ".join((stmt or "").strip().split())
        parts = s.split()
        if len(parts) < 6:
            return None
        # find "exists" then name
        for i, tok in enumerate(parts):
            if tok.lower() == "exists" and (i + 1) < len(parts):
                return parts[i + 1].strip().strip(";")
        # fallback: CREATE INDEX <name>
        if parts[0].lower() == "create" and parts[1].lower() == "index":
            return parts[2].strip().strip(";")
    except Exception:
        return None
    return None


def _dedupe_series_by_tmdb(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT tmdb_id, MIN(id) as keep_id, COUNT(*) as cnt
            FROM series
            WHERE tmdb_id IS NOT NULL AND tmdb_id <> ''
            GROUP BY tmdb_id
            HAVING cnt > 1
            """
        ).fetchall()
    except Exception:
        rows = []
    for tmdb_id, keep_id, _cnt in rows:
        try:
            # Move seasons to the kept series id
            conn.execute(
                "UPDATE season SET series_id=? WHERE series_id IN (SELECT id FROM series WHERE tmdb_id=? AND id<>?)",
                (int(keep_id), str(tmdb_id), int(keep_id)),
            )
            # Delete duplicate series rows
            conn.execute(
                "DELETE FROM series WHERE tmdb_id=? AND id<>?",
                (str(tmdb_id), int(keep_id)),
            )
        except Exception:
            continue


def _dedupe_movie_items_by_tmdb(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT tmdb_id, MIN(id) as keep_id, COUNT(*) as cnt
            FROM media_item
            WHERE tmdb_id IS NOT NULL AND tmdb_id <> '' AND (media_type='movie' OR media_type='film')
            GROUP BY tmdb_id
            HAVING cnt > 1
            """
        ).fetchall()
    except Exception:
        rows = []

    for tmdb_id, keep_id, _cnt in rows:
        try:
            dup_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM media_item WHERE tmdb_id=? AND id<>? AND (media_type='movie' OR media_type='film')",
                    (str(tmdb_id), int(keep_id)),
                ).fetchall()
            ]
        except Exception:
            dup_ids = []
        if not dup_ids:
            continue
        # Repoint foreign keys to keep_id
        for tbl, col in (
            ("file_record", "media_item_id"),
            ("media_metadata", "media_item_id"),
            ("media_image", "media_item_id"),
            ("media_translations", "path_id"),
            ("manual_mappings", "media_item_id"),
            ("manual_overrides", "media_item_id"),
            ("user_list", "media_item_id"),
            ("play_history", "media_item_id"),
            ("season_item", "media_item_id"),
        ):
            try:
                conn.execute(
                    f"UPDATE {tbl} SET {col}=? WHERE {col} IN ({','.join(['?'] * len(dup_ids))})",
                    [int(keep_id), *[int(x) for x in dup_ids]],
                )
            except Exception:
                pass
        # Repoint media_relation edges
        try:
            conn.execute(
                f"UPDATE media_relation SET from_item_id=? WHERE from_item_id IN ({','.join(['?'] * len(dup_ids))})",
                [int(keep_id), *[int(x) for x in dup_ids]],
            )
        except Exception:
            pass
        try:
            conn.execute(
                f"UPDATE media_relation SET to_item_id=? WHERE to_item_id IN ({','.join(['?'] * len(dup_ids))})",
                [int(keep_id), *[int(x) for x in dup_ids]],
            )
        except Exception:
            pass
        # Drop duplicate media_item rows
        try:
            conn.execute(
                f"DELETE FROM media_item WHERE id IN ({','.join(['?'] * len(dup_ids))})",
                [int(x) for x in dup_ids],
            )
        except Exception:
            pass


def main():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Nothing to migrate.")
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        try:
            conn.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        def ensure_columns(table: str, statements: list[tuple[str, str]]):
            cols = get_columns(conn, table)
            print(f"Existing {table} columns: {sorted(cols)}")
            for name, sqltype in statements:
                if name in cols:
                    print(f"Column {table}.{name} already exists; skipping")
                    continue
                stmt = f"ALTER TABLE {table} ADD COLUMN {name} {sqltype};"
                print(f"Applying: {stmt}")
                conn.execute(stmt)

        # Determine whether migrations are needed before doing an expensive backup copy.
        missing_schema = False
        missing_indexes = False
        try:
            mi_cols = get_columns(conn, 'media_item')
            if any(name not in mi_cols for (name, _t) in ALTER_STATEMENTS):
                missing_schema = True
        except Exception:
            missing_schema = True
        for t in ("media_image", "media_translations", "manual_mappings", "manual_overrides", "user_list", "play_history", "settings", "series", "season", "episode", "season_item", "episode_file"):
            if not table_exists(conn, t):
                missing_schema = True
                break
        # normalized tables may exist but miss phase-2 columns
        if table_exists(conn, "series"):
            sc = get_columns(conn, "series")
            if any(n not in sc for n in ("title_en", "title_localized", "mal_id", "tmdb_id", "tmdb_no_match", "tmdb_no_match_reason", "year", "main_poster")):
                missing_schema = True
        if table_exists(conn, "season"):
            sc = get_columns(conn, "season")
            if any(
                n not in sc
                for n in (
                    "title_en",
                    "title_localized",
                    "synopsis_localized",
                    "poster_path",
                    "tmdb_season_number",
                    "tmdb_episode_start",
                    "tmdb_episode_end",
                    "air_date_start",
                    "air_date_end",
                )
            ):
                missing_schema = True
        if table_exists(conn, "episode"):
            ec = get_columns(conn, "episode")
            if any(n not in ec for n in ("title_en", "title_localized", "synopsis_localized", "still_path", "air_date", "episode_type")):
                missing_schema = True
        if table_exists(conn, "play_history"):
            phc = get_columns(conn, "play_history")
            if any(n not in phc for n, _t in PLAY_HISTORY_ALTERS):
                missing_schema = True

        for stmt in INDEX_STATEMENTS:
            nm = _index_name_from_stmt(stmt)
            if nm and not index_exists(conn, nm):
                missing_indexes = True
                break

        if not missing_schema and not missing_indexes:
            print("No migrations needed.")
            return

        bak = None
        if missing_schema:
            print(f"Backing up {DB_PATH} (single backup at {BACKUP_DIR / 'arcanea.db.bak'})...")
            bak = backup_db(DB_PATH)
            print(f"Backup created: {bak}")

        ensure_columns('media_item', ALTER_STATEMENTS)
        ensure_columns('play_history', PLAY_HISTORY_ALTERS)
        # create media_image table
        print("Ensuring media_image table exists")
        conn.execute(MEDIA_IMAGE_CREATE)
        # translations table for localized UI fields
        print("Ensuring media_translations table exists")
        conn.execute(MEDIA_TRANSLATIONS_CREATE)
        print("Ensuring manual_mappings table exists")
        conn.execute(MANUAL_MAPPINGS_CREATE)
        print("Ensuring manual_overrides table exists")
        conn.execute(MANUAL_OVERRIDES_CREATE)
        print("Ensuring play_history table exists")
        conn.execute(PLAY_HISTORY_CREATE)
        print("Ensuring user_list table exists")
        conn.execute(USER_LIST_CREATE)
        # settings table (single source of truth for app config)
        print("Ensuring settings table exists")
        conn.execute(SETTINGS_CREATE)
        # normalized tables for series/season/episode linkage
        print("Ensuring series/season/episode normalized tables exist")
        conn.execute(SERIES_CREATE)
        conn.execute(SEASON_CREATE)
        conn.execute(EPISODE_CREATE)
        conn.execute(SEASON_ITEM_CREATE)
        conn.execute(EPISODE_FILE_CREATE)
        # Phase 2: add missing columns to normalized tables when they already exist.
        ensure_columns('series', [
            ('title_en', 'TEXT'),
            ('title_localized', 'TEXT'),
            ('mal_id', 'TEXT'),
            ('tmdb_id', 'TEXT'),
            ('jikan_type', 'TEXT'),
            ('tmdb_no_match', 'INTEGER'),
            ('tmdb_no_match_reason', 'TEXT'),
            ('year', 'INTEGER'),
            ('main_poster', 'TEXT'),
        ])
        ensure_columns('season', [
            ('title_en', 'TEXT'),
            ('title_localized', 'TEXT'),
            ('synopsis_localized', 'TEXT'),
            ('poster_path', 'TEXT'),
            ('backdrop_path', 'TEXT'),
            ('tmdb_season_number', 'INTEGER'),
            ('tmdb_episode_start', 'INTEGER'),
            ('tmdb_episode_end', 'INTEGER'),
            ('air_date_start', 'TEXT'),
            ('air_date_end', 'TEXT'),
            ('split_cour', 'INTEGER'),
        ])
        ensure_columns('season_item', [
            ('media_id', 'TEXT'),
        ])
        ensure_columns('episode', [
            ('title_en', 'TEXT'),
            ('title_localized', 'TEXT'),
            ('synopsis_localized', 'TEXT'),
            ('still_path', 'TEXT'),
            ('air_date', 'TEXT'),
            ('episode_type', 'TEXT'),
        ])
        # Dedupe TMDB ids before creating unique indexes.
        try:
            _dedupe_series_by_tmdb(conn)
        except Exception:
            pass
        try:
            _dedupe_movie_items_by_tmdb(conn)
        except Exception:
            pass
        for stmt in INDEX_STATEMENTS:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        conn.commit()
        print("Migrations applied successfully.")
        # remove backup now that migration completed
        try:
            if bak and bak.exists():
                bak.unlink()
                print(f"Removed temporary backup: {bak}")
        except Exception:
            pass
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('Migration failed:', e)
        raise
