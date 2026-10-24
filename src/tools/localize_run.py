from __future__ import annotations

import argparse
import json
import sys

from .apply_db_migrations import main as apply_db_migrations_main
from devtools.lenguaje.localize_runner import run_localization_once


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run TMDB localization sync (Jikan anchor -> TMDB -> episodes).")
    p.add_argument("--limit-series", type=int, default=None, help="Limit number of series to process")
    p.add_argument("--limit-seasons", type=int, default=None, help="Limit number of seasons to process")
    p.add_argument("--tmdb-id", type=str, default=None, help="Only backfill episodes for a TMDB TV id")
    p.add_argument("--media-item-id", type=int, default=None, help="Only backfill episodes for a media_item id")
    p.add_argument("--series-id", type=int, default=None, help="Only backfill episodes for a series id")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = p.parse_args(argv)

    try:
        apply_db_migrations_main()
    except Exception:
        # best-effort; if migrations fail, the job will likely fail too
        pass

    res = run_localization_once(
        limit_series=args.limit_series,
        limit_seasons=args.limit_seasons,
        manage_state=False,
        tmdb_id=args.tmdb_id,
        media_item_id=args.media_item_id,
        series_id=args.series_id,
    )
    if args.pretty:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
