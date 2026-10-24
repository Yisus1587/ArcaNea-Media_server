"""Enhanced classifier that uses Jikan's source field for precise anime detection."""

from typing import Optional, Dict, Any


def classify_with_source_check(jikan_data: Dict[str, Any]) -> Optional[str]:
    """
    Classify media based on Jikan's specific metadata, especially 'source' field.
    
    Returns: 'anime_series', 'anime_movie', 'tv_series', 'movie_animated', 'movie_live', or None
    """
    if not jikan_data:
        return None
    
    # Check Jikan's source field first (most reliable indicator)
    source = (jikan_data.get('source') or '').lower()
    media_type = (jikan_data.get('type') or '').lower()
    
    # List of source values that indicate anime adaptation
    MANGA_SOURCES = ['manga', 'web manga', '4-koma manga', 'digital manga']
    NOVEL_SOURCES = ['light novel', 'novel', 'web novel', 'visual novel']
    GAME_SOURCES = ['game', 'visual novel', 'card game']
    ORIGINAL_SOURCES = ['original', 'music', 'radio', 'book']
    
    all_anime_sources = MANGA_SOURCES + NOVEL_SOURCES + GAME_SOURCES + ORIGINAL_SOURCES
    
    # Determine if it's anime based on source
    is_anime_adaptation = any(source_str in source for source_str in all_anime_sources)
    
    # Check media type
    is_tv_series = 'tv' in media_type or 'series' in media_type
    is_movie = 'movie' in media_type or 'film' in media_type
    
    # Classification logic
    if is_anime_adaptation:
        if is_tv_series:
            return 'anime_series'
        elif is_movie:
            return 'anime_movie'
        else:
            # Default to series for anime adaptations
            return 'anime_series'
    
    # If not anime adaptation, check other indicators
    genres = jikan_data.get('genres', [])
    genre_names = []
    for g in genres:
        if isinstance(g, dict):
            genre_names.append(g.get('name', '').lower())
        else:
            genre_names.append(str(g).lower())
    
    # Check for animation genre
    is_animation = any('animation' in g for g in genre_names) or any('anime' in g for g in genre_names)
    
    if is_animation:
        if is_tv_series:
            return 'tv_series'  # Animated TV series (Western animation)
        elif is_movie:
            return 'movie_animated'
    
    # Default fallback based on media type
    if is_tv_series:
        return 'tv_series'
    elif is_movie:
        return 'movie_live'
    
    return None


def enhanced_classify_metadata(normalized: dict) -> Optional[str]:
    """
    Main classification function that works with normalized metadata.
    Prioritizes Jikan's source field when available.
    """
    if not normalized:
        return None
    
    provider = (normalized.get('provider') or '').lower()
    raw_data = normalized.get('raw') or {}
    
    # If it's from Jikan, use the enhanced classifier
    if provider == 'jikan':
        return classify_with_source_check(raw_data)
    
    # For TMDB or other providers, use existing logic
    media_type = (normalized.get('media_type') or '').lower()
    genres = normalized.get('genres') or []
    
    # Check for animation in genres
    genre_strings = [str(g).lower() for g in genres]
    is_animated = any('animation' in g for g in genre_strings) or any('anime' in g for g in genre_strings)
    
    if is_animated:
        if media_type in ['tv', 'series']:
            return 'tv_series'
        elif media_type == 'movie':
            return 'movie_animated'
    
    # Fallback to media type
    if media_type in ['tv', 'series']:
        return 'tv_series'
    elif media_type == 'movie':
        return 'movie_live'
    
    return None

def classify_metadata(normalized: dict) -> Optional[str]:
    if not normalized:
        return None
    prov = (normalized.get('provider') or '').lower()
    genres = normalized.get('genres') or []
    media_type = (normalized.get('media_type') or '').lower() if isinstance(normalized.get('media_type'), str) else None
    raw = normalized.get('raw') or {}

    # Jikan providers => anime
    if prov == 'jikan':
        raw_type = (raw.get('type') or '').lower() if isinstance(raw.get('type'), str) else None
        raw_source = (raw.get('source') or '').lower() if isinstance(raw.get('source'), str) else None
        if raw_type and ('tv' in raw_type or 'series' in raw_type):
            return 'anime_series'
        if raw_type and 'movie' in raw_type:
            return 'anime_movie'
        if raw_source and any(k in raw_source for k in ('manga', 'light', 'novel')):
            return 'anime_series'
        return 'anime_series'

    # TMDB or others: animation genre -> animated
    if any('animation' in (g or '').lower() for g in genres):
        if media_type == 'tv' or media_type == 'series':
            return 'tv_series'
        return 'movie_animated'

    # live-action detection via cast/credits
    try:
        rawobj = raw or {}
        cast = None
        if isinstance(rawobj, dict):
            cast = rawobj.get('credits', {}).get('cast') if isinstance(rawobj.get('credits'), dict) else rawobj.get('cast')
        if cast:
            return 'movie_live' if media_type == 'movie' or not media_type else media_type
    except Exception:
        pass

    # fallback to media_type if present
    if media_type == 'series' or media_type == 'tv':
        return 'tv_series'
    if media_type == 'movie':
        return 'movie_live'
    return None
