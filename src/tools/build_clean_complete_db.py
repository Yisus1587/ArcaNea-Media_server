"""Create a clean, complete DB candidate from the current ArcaNea DB.

This is faster than a provider-only rebuild: it snapshots the current DB,
cleans known provider/editorial noise, rescans current roots, then enriches only
missing/repairable items.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DEFAULT_SOURCE = DATA_DIR / "arcanea.db"


EDITORIAL_TAIL_RE = re.compile(
    r"\s*[\[\(\{]?\s*(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)\s*:[^\]\)\}\n]*(?:[\]\)\}])?\s*$",
    re.IGNORECASE,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _snapshot_db(source: Path, output: Path) -> None:
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(output), timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        src.close()
        dst.close()


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").strip()
    except Exception:
        return None
    if not text:
        return text
    blocks: list[str] = []
    for block in re.split(r"\n{2,}", text):
        b = re.sub(r"[ \t]+", " ", block).strip()
        if not b:
            continue
        if re.match(r"^[\[\(\{]?\s*(?:source|sources|note|notes|editorial(?: notes?)?|staff notes?)\b", b, re.I):
            continue
        blocks.append(b)
    text = "\n\n".join(blocks).strip()
    text = EDITORIAL_TAIL_RE.sub("", text).strip()
    if text in {"(", "[", "{"}:
        return ""
    return text


def _clean_metadata_json(raw: str | None) -> tuple[str | None, bool]:
    if not raw:
        return raw, False
    try:
        data = json.loads(raw)
    except Exception:
        return raw, False
    changed = False

    def clean_obj(obj: object) -> None:
        nonlocal changed
        if isinstance(obj, dict):
            for key in ("synopsis", "overview", "description", "summary"):
                if key in obj and isinstance(obj.get(key), str):
                    cleaned = _clean_text(obj.get(key))
                    if cleaned is not None and cleaned != obj.get(key):
                        obj[key] = cleaned
                        changed = True
            eps = obj.get("episodes")
            if isinstance(eps, list):
                for ep in eps:
                    clean_obj(ep)
        elif isinstance(obj, list):
            for item in obj:
                clean_obj(item)

    clean_obj(data)
    if not changed:
        return raw, False
    return json.dumps(data, ensure_ascii=False, sort_keys=True), True


def _clean_existing_data(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    counts = {
        "media_metadata": 0,
        "media_item": 0,
        "season": 0,
        "episode": 0,
        "media_translations": 0,
    }
    try:
        for row in conn.execute("select id, data from media_metadata"):
            new_data, changed = _clean_metadata_json(row["data"])
            if changed:
                conn.execute("update media_metadata set data=? where id=?", (new_data, row["id"]))
                counts["media_metadata"] += 1

        for table, key_col, text_col, counter in (
            ("media_item", "id", "synopsis_localized", "media_item"),
            ("season", "id", "synopsis_localized", "season"),
            ("episode", "id", "synopsis_localized", "episode"),
            ("media_translations", "id", "overview", "media_translations"),
        ):
            try:
                rows = conn.execute(f"select {key_col}, {text_col} from {table}").fetchall()
            except Exception:
                continue
            for row in rows:
                cleaned = _clean_text(row[text_col])
                if cleaned is not None and cleaned != row[text_col]:
                    conn.execute(f"update {table} set {text_col}=? where {key_col}=?", (cleaned, row[key_col]))
                    counts[counter] += 1

        # Fix a known scanner artifact from numeric anime suffixes.
        try:
            cur = conn.execute(
                """
                update season
                set title_en = replace(title_en, 'Ken2', 'Ken 2nd Season')
                where title_en like '%Ken2'
                """
            )
            counts["season"] += int(cur.rowcount or 0)
        except Exception:
            pass

        conn.commit()
        return counts
    finally:
        conn.close()


def _reset_missing_for_retry(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        cur = conn.execute(
            """
            update media_item
            set status='SCANNED', is_identified=0
            where not exists(
                select 1 from media_metadata mm
                where mm.media_item_id = media_item.id
                and mm.provider is not null
                and mm.provider != ''
            )
            and status in ('ERROR', 'NO_MATCH')
            """
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _summary(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        out: dict[str, object] = {}
        for table in (
            "media_item",
            "media_metadata",
            "media_image",
            "series",
            "season",
            "episode",
            "file_record",
            "manual_mappings",
            "manual_overrides",
        ):
            out[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        out["with_metadata"] = int(
            conn.execute(
                "select count(distinct media_item_id) from media_metadata where provider is not null and provider != ''"
            ).fetchone()[0]
        )
        out["missing_metadata"] = int(
            conn.execute(
                """
                select count(*)
                from media_item mi
                where not exists(
                    select 1 from media_metadata mm
                    where mm.media_item_id = mi.id
                    and mm.provider is not null
                    and mm.provider != ''
                )
                """
            ).fetchone()[0]
        )
        out["errors"] = int(conn.execute("select count(*) from media_item where status = 'ERROR'").fetchone()[0])
        out["no_match"] = int(conn.execute("select count(*) from media_item where status = 'NO_MATCH'").fetchone()[0])
        out["integrity"] = conn.execute("pragma integrity_check").fetchone()[0]
        return out
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output", default="")
    parser.add_argument("--replace-output", action="store_true")
    parser.add_argument("--skip-scan", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        print(f"source_not_found={source}", file=sys.stderr)
        return 2

    output = Path(args.output).resolve() if args.output else DATA_DIR / f"arcanea.clean_complete_{datetime.now():%Y%m%d_%H%M%S}.db"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if not args.replace_output:
            print(f"output_exists={output}", file=sys.stderr)
            return 2
        output.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(output) + suffix)
        if p.exists():
            p.unlink()

    print(f"output={output}")
    _snapshot_db(source, output)
    print("snapshot=ok")
    print("cleaned=" + json.dumps(_clean_existing_data(output), ensure_ascii=False, sort_keys=True))

    os.environ["ARCANEA_DB_URL"] = _sqlite_url(output)
    os.environ.setdefault("ARCANEA_EAGER_INIT_DB", "0")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from src.db import init_db
    from src.db.models import Base
    from src.scanner.scanner import Scanner
    from src.services.dispatch_enrichment import dispatch_enrichment_by_roots
    from src.tools.apply_db_migrations import main as apply_migrations

    init_db(Base)
    apply_migrations()

    if not args.skip_scan:
        scanner = Scanner(discovery_only=False)
        print("scan_roots=" + json.dumps([str(r) for r in scanner.roots], ensure_ascii=False))
        for root in [str(r) for r in scanner.roots]:
            print(f"scan_start={root}")
            scanner.scan_path(root)
            print(f"scan_done={root}")

    reset = _reset_missing_for_retry(output)
    print(f"reset_missing_for_retry={reset}")

    if not args.skip_enrich:
        print("enrichment_start=dispatch_by_roots")
        report = dispatch_enrichment_by_roots(tmdb_series_only=False)
        print("enrichment_report=" + json.dumps(report, ensure_ascii=False, default=str))

    # Final cleanup after any newly fetched provider text.
    print("cleaned_final=" + json.dumps(_clean_existing_data(output), ensure_ascii=False, sort_keys=True))
    print("summary=" + json.dumps(_summary(output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
