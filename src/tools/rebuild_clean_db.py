"""Build a clean ArcaNea SQLite DB from the current media roots.

This intentionally writes to a new DB file by default. It copies only the
configuration required to run providers/roots, scans the filesystem, then runs
the normal enrichment dispatcher against the new DB.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DEFAULT_SOURCE = DATA_DIR / "arcanea.db"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _copy_table_rows(source: Path, dest: Path, table: str) -> int:
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        src.row_factory = sqlite3.Row
        src_cols = [r["name"] for r in src.execute(f"pragma table_info({table})")]
        dst_cols = [r[1] for r in dst.execute(f"pragma table_info({table})")]
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            return 0
        rows = src.execute(f"select {', '.join(cols)} from {table}").fetchall()
        if not rows:
            return 0
        placeholders = ", ".join(["?"] * len(cols))
        dst.executemany(
            f"insert or replace into {table} ({', '.join(cols)}) values ({placeholders})",
            [[row[c] for c in cols] for row in rows],
        )
        dst.commit()
        return len(rows)
    finally:
        src.close()
        dst.close()


def _copy_manual_rows_by_path(source: Path, dest: Path) -> dict[str, int]:
    """Copy manual mapping/override rows by matching old/new canonical_path."""
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        src.row_factory = sqlite3.Row
        dst.row_factory = sqlite3.Row
        old_paths = {
            str(r["canonical_path"] or ""): int(r["id"])
            for r in src.execute("select id, canonical_path from media_item")
            if r["canonical_path"]
        }
        new_paths = {
            str(r["canonical_path"] or ""): int(r["id"])
            for r in dst.execute("select id, canonical_path from media_item")
            if r["canonical_path"]
        }
        id_map = {old_id: new_paths[path] for path, old_id in old_paths.items() if path in new_paths}
        copied = {"manual_mappings": 0, "manual_overrides": 0}

        if id_map:
            mm_cols = [r[1] for r in dst.execute("pragma table_info(manual_mappings)")]
            src_cols = [r[1] for r in src.execute("pragma table_info(manual_mappings)")]
            cols = [c for c in src_cols if c in mm_cols and c != "id"]
            rows = src.execute("select * from manual_mappings").fetchall()
            for row in rows:
                old_mid = int(row["media_item_id"])
                new_mid = id_map.get(old_mid)
                if not new_mid:
                    continue
                vals = []
                for col in cols:
                    vals.append(new_mid if col == "media_item_id" else row[col])
                placeholders = ", ".join(["?"] * len(cols))
                dst.execute(
                    f"insert or replace into manual_mappings ({', '.join(cols)}) values ({placeholders})",
                    vals,
                )
                copied["manual_mappings"] += 1

            mo_cols = [r[1] for r in dst.execute("pragma table_info(manual_overrides)")]
            src_cols = [r[1] for r in src.execute("pragma table_info(manual_overrides)")]
            cols = [c for c in src_cols if c in mo_cols and c != "id"]
            rows = src.execute("select * from manual_overrides").fetchall()
            for row in rows:
                old_mid = int(row["media_item_id"])
                new_mid = id_map.get(old_mid)
                if not new_mid:
                    continue
                vals = []
                for col in cols:
                    vals.append(new_mid if col == "media_item_id" else row[col])
                placeholders = ", ".join(["?"] * len(cols))
                dst.execute(
                    f"insert into manual_overrides ({', '.join(cols)}) values ({placeholders})",
                    vals,
                )
                copied["manual_overrides"] += 1
            dst.commit()
        return copied
    finally:
        src.close()
        dst.close()


def _summary(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(str(db_path))
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
            try:
                out[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            except Exception:
                out[table] = None
        out["with_metadata"] = int(
            conn.execute(
                "select count(distinct media_item_id) from media_metadata where provider is not null and provider != ''"
            ).fetchone()[0]
        )
        out["pending_runnable"] = int(
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
                and (
                    mi.status is null
                    or mi.status not in ('ERROR', 'NO_MATCH', 'MANUAL', 'OMITTED')
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
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Current/source DB")
    parser.add_argument("--output", default="", help="Output clean DB path")
    parser.add_argument("--replace-output", action="store_true", help="Overwrite output if it exists")
    parser.add_argument("--skip-enrich", action="store_true", help="Only initialize and scan")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        print(f"source_not_found={source}", file=sys.stderr)
        return 2

    if args.output:
        output = Path(args.output).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = (DATA_DIR / f"arcanea.clean_{stamp}.db").resolve()
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

    os.environ["ARCANEA_DB_URL"] = _sqlite_url(output)
    os.environ.setdefault("ARCANEA_EAGER_INIT_DB", "0")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from src.db import init_db
    from src.db.models import Base
    from src.scanner.scanner import Scanner
    from src.services.dispatch_enrichment import dispatch_enrichment_by_roots
    from src.tools.apply_db_migrations import main as apply_migrations

    print(f"output={output}")
    init_db(Base)
    apply_migrations()

    copied_settings = _copy_table_rows(source, output, "settings")
    copied_roots = _copy_table_rows(source, output, "media_root")
    print(f"copied_settings={copied_settings}")
    print(f"copied_media_roots={copied_roots}")

    scanner = Scanner(discovery_only=False)
    roots = [str(r) for r in scanner.roots]
    print("scan_roots=" + json.dumps(roots, ensure_ascii=False))
    for root in roots:
        print(f"scan_start={root}")
        scanner.scan_path(root)
        print(f"scan_done={root}")

    manual_copied = _copy_manual_rows_by_path(source, output)
    print("manual_copied=" + json.dumps(manual_copied, ensure_ascii=False))

    if not args.skip_enrich:
        print("enrichment_start=dispatch_by_roots")
        report = dispatch_enrichment_by_roots(tmdb_series_only=False)
        print("enrichment_report=" + json.dumps(report, ensure_ascii=False, default=str))
    else:
        print("enrichment_skipped=true")

    print("summary=" + json.dumps(_summary(output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
