"""Compatibility wrapper for the media scan/enrichment pipeline.

The scan pipeline no longer lives in the API service layer. Keep these exports so
older imports continue to work while endpoints use src.services.media_pipeline.
"""

from ...services.media_pipeline import (  # noqa: F401
    clear_pending_path,
    clear_scan_queue,
    enqueue_scan_all_roots,
    enqueue_scan_path,
    scan_all_roots,
    scan_path_with_pipeline,
    scan_single_path,
    start_enrichment_job,
)
