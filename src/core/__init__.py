"""Core configuration/network exports for the backend."""

from __future__ import annotations

import importlib

config = importlib.import_module(".config", __name__)
network = importlib.import_module(".network", __name__)

BASE_DIR = config.BASE_DIR
DATA_DIR = config.DATA_DIR
get = config.get
normalize_metadata_language = config.normalize_metadata_language
set_temp_override = config.set_temp_override

__all__ = [
    "config",
    "get",
    "BASE_DIR",
    "DATA_DIR",
    "normalize_metadata_language",
    "set_temp_override",
    "network",
]
