import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.resolve()))

from src.db import session_scope
from src.db.models import Series, Season, Episode, MediaMetadata, MediaItem

def inspect_db():
    with session_scope() as session:
        series = session.query(Series).filter(Series.title.like('%Rick and Morty%')).first()
        if not series:
            print("No se encontró 'Rick and Morty' en la base de datos.")
            return

        print(f"=== SERIE ===")
        print(f"Título: {series.title}")
        print(f"TMDB ID: {series.tmdb_id}")
        
        # Las temporadas están en la tabla Season
        seasons = session.query(Season).filter(Season.series_id == series.id).order_by(Season.season_number).all()
        print(f"\n=== TEMPORADAS ({len(seasons)}) ===")
        for s in seasons:
            print(f"  Temporada {s.season_number}")
            
            # Los episodios están en Episode
            eps = session.query(Episode).filter(Episode.season_id == s.id).order_by(Episode.episode_number).all()
            print(f"    -> {len(eps)} episodios en DB")
            for ep in eps[:2]: # Mostrar solo los dos primeros
                print(f"      - Ep {ep.episode_number}: '{ep.title}'")

if __name__ == "__main__":
    inspect_db()

