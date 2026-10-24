from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, BigInteger, Boolean, Index, Float
import datetime

Base = declarative_base()


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class MediaItem(Base):
    __tablename__ = 'media_item'
    id = Column(Integer, primary_key=True)
    # stable, reproducible identifier for the media item (UUIDv5 or similar)
    media_id = Column(String, nullable=True, unique=True)
    # canonical filesystem folder containing this item
    canonical_path = Column(String, nullable=True)
    # hash of canonical_path for fast lookup and uniqueness
    canonical_path_hash = Column(String, nullable=True)
    # normalized base title derived from folder name
    base_title = Column(String, nullable=True)
    title = Column(String, nullable=True)
    # Anchor title (English) used for deterministic matching and fallback.
    title_en = Column(String, nullable=True)
    # Single active localized title (language determined by settings.target_lang / metadata_language).
    title_localized = Column(String, nullable=True)
    # Localized synopsis/overview (if available).
    synopsis_localized = Column(Text, nullable=True)
    media_type = Column(String, nullable=True)  # movie, series, episode
    provider = Column(String, nullable=True)
    provider_id = Column(String, nullable=True)
    # MAL id (MyAnimeList) when detected from folder or metadata
    mal_id = Column(String, nullable=True)
    # Jikan anime type (TV, OVA, ONA, Special, Movie)
    jikan_type = Column(String, nullable=True)
    # Anchor MAL id for grouping (OVA/ONA/Special -> Season 0 under same series)
    jikan_anchor_mal_id = Column(String, nullable=True)
    # Local poster path cached on disk (relative to project or absolute)
    poster_path = Column(String, nullable=True)
    # status: SCANNED | ENRICHED | ERROR
    status = Column(String, nullable=True, default='SCANNED')
    # library_type represents the logical library this item belongs to (anime, movie, tv)
    library_type = Column(String, nullable=True)
    # which media_root this item belongs to (path prefix)
    media_root = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    # additional quick-access fields populated from metadata
    is_animated = Column(Boolean, nullable=True, default=None)
    origin = Column(String, nullable=True)  # e.g., 'manga', 'novel', 'original', 'live-action'
    release_year = Column(Integer, nullable=True)
    runtime = Column(Integer, nullable=True)
    cast = Column(Text, nullable=True)
    # TMDB-specific quick fields
    tmdb_id = Column(String, nullable=True)
    backdrop_path = Column(String, nullable=True)
    rating = Column(Float, nullable=True)
    # Content rating / certification (e.g., G, PG, TV-Y, TV-G, TV-Y7)
    content_rating = Column(String, nullable=True)
    genres = Column(Text, nullable=True)
    # Additional titles/aliases extracted from folder names to improve matching
    search_titles = Column(Text, nullable=True)
    # Identification flag: True when provider/manual mapping succeeded, False when local-only.
    is_identified = Column(Boolean, nullable=True, default=False)


class Series(Base):
    __tablename__ = 'series'
    id = Column(Integer, primary_key=True)
    # Back-compat display title (prefer localized, else English).
    title = Column(String, nullable=False, index=True)
    title_en = Column(String, nullable=True, index=True)
    title_localized = Column(String, nullable=True, index=True)
    # legacy field kept for backward compatibility; not used as primary exposure source
    provider_id = Column(String, nullable=True)
    mal_id = Column(String, nullable=True, index=True)
    tmdb_id = Column(String, nullable=True, index=True)
    # Jikan anime type (TV, OVA, ONA, Special, Movie) for primary series
    jikan_type = Column(String, nullable=True)
    # When TMDB cannot be resolved from the Jikan English anchor, mark as no-match to avoid infinite retries.
    tmdb_no_match = Column(Boolean, nullable=True, default=None)
    tmdb_no_match_reason = Column(String, nullable=True)
    year = Column(Integer, nullable=True)
    main_poster = Column(String, nullable=True)


class Season(Base):
    __tablename__ = 'season'
    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, ForeignKey('series.id', ondelete='CASCADE'))
    season_number = Column(Integer, nullable=True)
    title_en = Column(String, nullable=True)
    title_localized = Column(String, nullable=True)
    synopsis_localized = Column(Text, nullable=True)
    poster_path = Column(String, nullable=True)
    backdrop_path = Column(String, nullable=True)
    tmdb_season_number = Column(Integer, nullable=True)
    tmdb_episode_start = Column(Integer, nullable=True)
    tmdb_episode_end = Column(Integer, nullable=True)
    air_date_start = Column(String, nullable=True)
    air_date_end = Column(String, nullable=True)
    split_cour = Column(Boolean, nullable=True, default=None)


class Episode(Base):
    __tablename__ = 'episode'
    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey('season.id', ondelete='CASCADE'))
    episode_number = Column(Integer, nullable=True)
    title = Column(String, nullable=True)
    title_en = Column(String, nullable=True)
    title_localized = Column(String, nullable=True)
    synopsis_localized = Column(Text, nullable=True)
    still_path = Column(String, nullable=True)
    air_date = Column(String, nullable=True)
    episode_type = Column(String, nullable=True)


class SeasonItem(Base):
    """Bridge table: maps a scanned MediaItem (folder) to its logical Season row.

    Rule: 1 media_item => 1 season (by default).
    """
    __tablename__ = 'season_item'
    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey('season.id', ondelete='CASCADE'), nullable=False, unique=True)
    media_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=False, unique=True)
    media_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class EpisodeFile(Base):
    """Bridge table: links normalized Episode rows to filesystem FileRecord entries."""
    __tablename__ = 'episode_file'
    id = Column(Integer, primary_key=True)
    episode_id = Column(Integer, ForeignKey('episode.id', ondelete='CASCADE'), nullable=False)
    file_record_id = Column(Integer, ForeignKey('file_record.id', ondelete='CASCADE'), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FileRecord(Base):
    __tablename__ = 'file_record'
    id = Column(Integer, primary_key=True)
    media_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=True)
    path = Column(String, unique=True, nullable=False)
    size = Column(BigInteger, nullable=True)
    mtime = Column(BigInteger, nullable=True)
    # logical order/index within the media item (episodes order)
    file_index = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MediaMetadata(Base):
    __tablename__ = 'media_metadata'
    id = Column(Integer, primary_key=True)
    media_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=False)
    provider = Column(String, nullable=True)
    provider_id = Column(String, nullable=True)
    data = Column(Text, nullable=True)  # JSON blob with normalized metadata
    # versioning and checksum help make enrichment idempotent and auditable
    version = Column(Integer, nullable=True, default=1)
    checksum = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MediaImage(Base):
    __tablename__ = 'media_image'
    id = Column(Integer, primary_key=True)
    media_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=False)
    source = Column(String, nullable=True)  # e.g. 'jikan', 'tmdb', 'local'
    source_url = Column(String, nullable=True)
    local_path = Column(String, nullable=True)  # where image is cached locally
    priority = Column(Integer, nullable=True, default=100)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MediaRelation(Base):
    __tablename__ = 'media_relation'
    id = Column(Integer, primary_key=True)
    from_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=False)
    to_item_id = Column(Integer, ForeignKey('media_item.id', ondelete='CASCADE'), nullable=True)
    relation_type = Column(String, nullable=False)  # prequel | sequel
    provider = Column(String, nullable=True)  # jikan
    external_id = Column(String, nullable=True)  # e.g. MAL id of target
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MediaRoot(Base):
    __tablename__ = 'media_root'
    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True, nullable=False)
    # `type` stores the inferred library type (anime/movie/tv/unknown)
    type = Column(String, nullable=True)
    # `source` indicates how the type was set: 'auto' or 'manual'
    source = Column(String, nullable=True, default='auto')
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# Indexes for fast lookup and uniqueness enforcement (created when DB supports it)
Index('ix_mediaitem_canonical_path_hash', MediaItem.canonical_path_hash, unique=False)
Index('ix_mediaitem_media_id', MediaItem.media_id, unique=False)


class MediaTranslation(Base):
    __tablename__ = "media_translations"
    id = Column(Integer, primary_key=True)
    # Link translations to the logical media item (folder identity).
    # This matches how the UI consumes series/movie-level titles/overviews.
    path_id = Column(Integer, ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    language = Column(String, nullable=False)  # e.g. es-MX, es-ES, es, en
    title = Column(String, nullable=True)
    overview = Column(Text, nullable=True)
    source = Column(String, nullable=False)  # tmdb | jikan | manual
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


Index("ix_media_translations_path_lang", MediaTranslation.path_id, MediaTranslation.language, unique=False)
Index("ux_media_translations_path_lang_source", MediaTranslation.path_id, MediaTranslation.language, MediaTranslation.source, unique=True)
Index('ix_mediametadata_item_checksum', MediaMetadata.media_item_id, MediaMetadata.checksum, unique=False)
Index('ix_mediarelation_from', MediaRelation.from_item_id, MediaRelation.relation_type, unique=False)
Index('ix_mediarelation_external', MediaRelation.external_id, MediaRelation.relation_type, unique=False)
Index('ix_file_record_item_index', FileRecord.media_item_id, FileRecord.file_index, unique=False)
Index('ix_episode_season_number', Episode.season_id, Episode.episode_number, unique=False)
Index('ix_season_series_number', Season.series_id, Season.season_number, unique=False)
Index('ux_series_provider_id', Series.provider_id, unique=True)


class ManualMapping(Base):
    __tablename__ = "manual_mappings"
    id = Column(Integer, primary_key=True)
    media_item_id = Column(Integer, ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False, unique=True)
    tmdb_id = Column(String, nullable=True)
    media_type = Column(String, nullable=True)  # tv | movie
    season_number = Column(Integer, nullable=True)
    poster_url = Column(String, nullable=True)
    backdrop_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class ManualOverride(Base):
    __tablename__ = "manual_overrides"
    id = Column(Integer, primary_key=True)
    media_item_id = Column(Integer, ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    language = Column(String, nullable=False)
    title = Column(String, nullable=True)
    overview = Column(Text, nullable=True)
    genres = Column(Text, nullable=True)  # JSON list
    episode_overrides = Column(Text, nullable=True)  # JSON map/list
    source = Column(String, nullable=False, default="manual")
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


Index("ix_manual_mappings_item", ManualMapping.media_item_id, unique=True)
Index("ix_manual_overrides_item_lang", ManualOverride.media_item_id, ManualOverride.language, unique=False)


class UserList(Base):
    __tablename__ = "user_list"
    id = Column(Integer, primary_key=True)
    profile_id = Column(String, nullable=False)
    media_item_id = Column(Integer, ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


Index("ux_user_list_profile_item", UserList.profile_id, UserList.media_item_id, unique=True)


class PlayHistory(Base):
    __tablename__ = "play_history"
    id = Column(Integer, primary_key=True)
    profile_id = Column(String, nullable=True)
    media_item_id = Column(Integer, ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    play_count = Column(Integer, nullable=True, default=0)
    last_played = Column(DateTime, nullable=True)
    watched = Column(Boolean, nullable=True, default=False)
    last_position = Column(Float, nullable=True)  # seconds
    last_duration = Column(Float, nullable=True)  # seconds
    last_path = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


Index("ux_play_history_profile_item", PlayHistory.profile_id, PlayHistory.media_item_id, unique=True)
Index("ix_play_history_last_played", PlayHistory.last_played, unique=False)
