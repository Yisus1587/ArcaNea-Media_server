import argparse
import sqlite3
from pathlib import Path


def _best_field(rows, field):
    vals = []
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            vals.append(s)
    if not vals:
        return None
    return max(vals, key=lambda v: len(v))


def _choose_master(rows):
    def title_len(r):
        t = (r.get("title_localized") or r.get("title_en") or r.get("title") or "").strip()
        return len(t)

    # Highest seasons_count, longest title, lowest id
    return max(rows, key=lambda r: (r.get("seasons_count", 0), title_len(r), -int(r.get("id", 0) or 0)))


def main():
    ap = argparse.ArgumentParser(description="Merge duplicate Series rows by tmdb_id.")
    ap.add_argument("--db", default="data/arcanea.db", help="Path to sqlite DB (default: data/arcanea.db)")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without committing")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.execute(
        """
        select tmdb_id
        from series
        where tmdb_id is not null and trim(tmdb_id) != ''
        group by tmdb_id
        having count(*) > 1
        """
    )
    tmdb_ids = [r[0] for r in cur.fetchall()]

    total_groups = 0
    total_merged = 0
    for tmdb_id in tmdb_ids:
        cur.execute(
            """
            select s.id, s.title, s.title_en, s.title_localized, s.provider_id, s.tmdb_id,
                   (select count(*) from season where series_id = s.id) as seasons_count
            from series s
            where s.tmdb_id = ?
            """,
            (tmdb_id,),
        )
        rows = cur.fetchall()
        if len(rows) < 2:
            continue

        total_groups += 1
        items = []
        for r in rows:
            items.append(
                {
                    "id": r[0],
                    "title": r[1],
                    "title_en": r[2],
                    "title_localized": r[3],
                    "provider_id": r[4],
                    "tmdb_id": r[5],
                    "seasons_count": r[6] or 0,
                }
            )

        master = _choose_master(items)
        master_id = master["id"]

        best_title = _best_field(items, "title")
        best_title_en = _best_field(items, "title_en")
        best_title_loc = _best_field(items, "title_localized")
        provider_key = f"tmdb_{tmdb_id}"

        for it in items:
            if it["id"] == master_id:
                continue
            dup_id = it["id"]
            cur.execute("update season set series_id = ? where series_id = ?", (master_id, dup_id))
            cur.execute("delete from series where id = ?", (dup_id,))
            total_merged += 1

        # Update master fields after deleting duplicates to avoid unique conflicts.
        cur.execute("select title, title_en, title_localized, provider_id, tmdb_id from series where id = ?", (master_id,))
        row = cur.fetchone()
        if row:
            cur_title, cur_title_en, cur_title_loc, cur_provider_id, cur_tmdb_id = row
            if best_title and (not cur_title or len(str(cur_title).strip()) < len(best_title)):
                cur.execute("update series set title = ? where id = ?", (best_title, master_id))
            if best_title_en and not (cur_title_en and str(cur_title_en).strip()):
                cur.execute("update series set title_en = ? where id = ?", (best_title_en, master_id))
            if best_title_loc and not (cur_title_loc and str(cur_title_loc).strip()):
                cur.execute("update series set title_localized = ? where id = ?", (best_title_loc, master_id))
            if not (cur_tmdb_id and str(cur_tmdb_id).strip()):
                cur.execute("update series set tmdb_id = ? where id = ?", (str(tmdb_id), master_id))
            if cur_provider_id != provider_key:
                # Only set provider_id if no other row already uses it.
                cur.execute("select id from series where provider_id = ? and id != ?", (provider_key, master_id))
                exists = cur.fetchone()
                if not exists:
                    cur.execute("update series set provider_id = ? where id = ?", (provider_key, master_id))

    if args.dry_run:
        conn.rollback()
        print(f"[dry-run] groups={total_groups} merged_series={total_merged}")
    else:
        conn.commit()
        print(f"[ok] groups={total_groups} merged_series={total_merged}")

    conn.close()


if __name__ == "__main__":
    main()
