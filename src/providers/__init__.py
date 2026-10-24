"""Providers package for metadata adapters.

This package exposes provider modules such as `provider_jikan`.
"""

__all__ = ["provider_jikan", "provider_tmdb", "provider_kitsu"]

from . import provider_jikan  # make provider importable as src.providers.provider_jikan
from . import provider_tmdb  # make provider importable as src.providers.provider_tmdb
from . import provider_kitsu  # make provider importable as src.providers.provider_kitsu
