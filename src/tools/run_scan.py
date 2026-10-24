"""Run a full scan over configured media_roots and populate the DB.

Usage: python -m src.tools.run_scan
"""
import logging
from src.db import init_db
from src.db.models import Base
from src.scanner.scanner import Scanner
from src.core.config import get
from src.services.dispatch_enrichment import dispatch_enrichment_by_roots

logger = logging.getLogger(__name__)


def run_once():
    logging.basicConfig(level=logging.INFO)
    print('Initializing DB...')
    init_db(Base)
    scanner = Scanner()
    roots = get('media_roots') or []
    print('Scanning roots:', roots)
    for r in roots:
        try:
            scanner.scan_path(r)
        except Exception as e:
            logger.exception('Scan failed for %s: %s', r, e)
    print('Scan complete')
    print('Scan complete. Enrichment is a separate step and is NOT run by this tool.')


if __name__ == '__main__':
    run_once()
