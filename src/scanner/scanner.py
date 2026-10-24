"""Filesystem scanner public entrypoint.

The scanner layer is intentionally local-only. It does not call metadata
providers and does not enrich titles, posters, synopsis or external IDs.
"""

from .filesystem_scanner import Scanner

__all__ = ["Scanner"]
