import os
import re
import logging
import queue
import unicodedata
from typing import Optional, List, Any
from pathlib import Path
import posixpath
from urllib.parse import urljoin

from src.core.network import session
from src.core.config import config

# Utilities for filenames, text and images

MOVIE_TITLE_ALIASES = {
    "3 john wick parabelllum": ["John Wick Chapter 3 Parabellum", "John Wick 3 Parabellum"],
    "agente 007 el manana nunca muere": ["Tomorrow Never Dies"],
    "blade 2": ["https://www.themoviedb.org/movie/36586", "Blade II"],
    "blade 3": ["https://www.themoviedb.org/movie/36648", "Blade Trinity"],
    "contactos de cuarto tipo": ["The Fourth Kind"],
    "dragon ball z broly el legendario super saiyajin": ["https://www.themoviedb.org/movie/34433", "Dragon Ball Z Broly The Legendary Super Saiyan"],
    "dragon ball z la batalla mas grande de este mundo esta por comenzar": ["Dragon Ball Z The World's Strongest"],
    "2012": ["https://www.themoviedb.org/movie/14161"],
    "el codigo da vinci 2 angeles y demonios": ["https://www.themoviedb.org/movie/13448", "Angels & Demons"],
    "el codigo da vinci 3 inferno": ["https://www.themoviedb.org/movie/207932", "Inferno 2016"],
    "el final de todo": ["How It Ends 2018"],
    "el juego de ender": ["Ender's Game"],
    "el nino que domo el viento": ["The Boy Who Harnessed the Wind"],
    "estacion zombie 2": ["Train to Busan Peninsula", "Peninsula 2020"],
    "exterminio 1 28 dias despues": ["28 Days Later 2002"],
    "exterminio 2 28 semanas despues": ["28 Weeks Later 2007"],
    "guerra mundial z": ["World War Z"],
    "hombres de negro": ["Men in Black"],
    "hombres de negro 2": ["https://www.themoviedb.org/movie/608", "Men in Black II"],
    "hombres de negro 3": ["https://www.themoviedb.org/movie/41154", "Men in Black 3"],
    "hombres de negro 4": ["https://www.themoviedb.org/movie/479455", "Men in Black International"],
    "jocker 2": ["https://www.themoviedb.org/movie/889737", "Joker Folie a Deux"],
    "iron man": ["https://www.themoviedb.org/movie/1726"],
    "iron man 2": ["https://www.themoviedb.org/movie/10138"],
    "iron man 3": ["https://www.themoviedb.org/movie/68721"],
    "la guerra de manana": ["The Tomorrow War"],
    "las guerreras k pop": ["KPop Demon Hunters"],
    "legion de angeles": ["Legion 2010"],
    "los pinguinos de madagascar": ["https://www.themoviedb.org/movie/270946", "Penguins of Madagascar 2014"],
    "los pinguinos de madascar": ["https://www.themoviedb.org/movie/270946", "Penguins of Madagascar 2014"],
    "los pitufos": ["The Smurfs 2011"],
    "los vengadores infinity wars": ["Avengers Infinity War"],
    "los vengadores endgame": ["https://www.themoviedb.org/movie/299534", "Avengers Endgame"],
    "maquina de guerra": ["War Machine 2017"],
    "piratas del caribe la maldicion de la perla negra": ["Pirates of the Caribbean The Curse of the Black Pearl"],
    "rec 2": ["https://www.themoviedb.org/movie/10664", "REC 2 2009"],
    "robocop": ["https://www.themoviedb.org/movie/5548"],
    "robocop 2014": ["https://www.themoviedb.org/movie/97020"],
    "robocop 2": ["https://www.themoviedb.org/movie/5549"],
    "robocop 3": ["https://www.themoviedb.org/movie/5550"],
    "shrek 2": ["https://www.themoviedb.org/movie/809"],
    "shrek 3": ["https://www.themoviedb.org/movie/810"],
    "shrek 4 para siempre": ["Shrek Forever After"],
    "son como ninos 2": ["Grown Ups 2"],
    "terremoto la falla de san andres": ["San Andreas 2015"],
    "titanes del pacifico 2": ["https://www.themoviedb.org/movie/268896", "Pacific Rim Uprising"],
    "titanes de pacifico": ["Pacific Rim"],
    "halo 4 forward unto dawn": ["https://www.themoviedb.org/tv/56295", "Halo 4 Forward Unto Dawn", "Halo 4: Forward Unto Dawn"],
    "halo the fall of reach": ["https://www.themoviedb.org/tv/77184", "Halo The Fall of Reach", "Halo: The Fall of Reach"],
    "resident evil bienvenidos a raccoon city": ["Resident Evil Welcome to Raccoon City"],
}


def _alias_key(value: str | None) -> str:
    try:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
    except Exception:
        text = str(value or "")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def limpiar_nombre_archivo(nombre: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", nombre)


def dividir_texto(texto: str, max_chars: int = 2048) -> List[str]:
    """Divide texto largo en una lista de trozos de como máximo `max_chars`.

    Siempre devuelve una lista (vacía si `texto` es vacío).
    """
    partes: List[str] = []
    if not texto:
        return partes
    while len(texto) > max_chars:
        corte = texto.rfind(" ", 0, max_chars)
        if corte == -1:
            corte = max_chars
        partes.append(texto[:corte])
        texto = texto[corte:].lstrip()
    if texto:
        partes.append(texto)
    return partes


def limpiar_traduccion(texto: str, ui_queue: Optional[queue.Queue] = None, label: Optional[Any] = None, silent: bool = False) -> str:
    texto_original = texto
    # remove known bad rewrite attributions
    try:
        texto = re.sub(r"\[\s*(?:escrito por mal rewrite|written by mal rewrite)\s*\]", "", texto, flags=re.IGNORECASE)
    except Exception:
        pass
    texto = re.sub(r"(escrito por mal rewrite|written by mal rewrite|mal[_ ]?rewrite)", "", texto, flags=re.IGNORECASE)

    # Remove trailing parenthetical/source attributions such as "(Fuente: MAL)", "(source: myanimelist)", etc.
    try:
        texto = re.sub(r"[\(\[]\s*(?:fuente|source|via|credits|cr[eé]ditos)\b[^\)\]]*[\)\]]\s*$", "", texto, flags=re.IGNORECASE).strip()
    except Exception:
        pass

    # Remove trailing 'Fuente: ...' or 'Source: ...' or '— Fuente: ...' at end of string
    try:
        # Enforce an explicit ':' or '-' before stripping to avoid truncating phrases como "la fuente de poder"
        texto = re.sub(r"(?:\s*[—\-]\s*)?(?:fuente|source|via|credits|cr[eé]ditos)\b\s*[:\-]\s*.+$", "", texto, flags=re.IGNORECASE).strip()
    except Exception:
        pass

    # Final cleanup: strip whitespace then surrounding quotes
    texto = texto.strip()
    # Remove empty trailing brackets left after attribution stripping (e.g. "[]", "()").
    try:
        texto = re.sub(r"(?:\s*\[\s*\]\s*)+$", "", texto).strip()
        texto = re.sub(r"(?:\s*\(\s*\)\s*)+$", "", texto).strip()
    except Exception:
        pass
    texto = texto.strip('"\'')

    if texto != texto_original and not silent:
        aviso = f"[AVISO] Se eliminaron frases de traducción:\n'{texto_original}' → '{texto}'"
        try:
            print(aviso)
        except UnicodeEncodeError:
            safe_aviso = aviso.encode('ascii', errors='replace').decode('ascii', errors='replace')
            print(safe_aviso)
        except Exception:
            logging.debug("No se pudo mostrar aviso de traducción", exc_info=True)
        if ui_queue and label:
            try:
                if hasattr(ui_queue, 'put'):
                    ui_queue.put(("label_text", label, aviso))
                else:
                    logging.debug("ui_queue provided but has no put() method")
            except Exception:
                logging.debug("No se pudo encolar aviso de traducción")
    return texto


def resumir_texto(texto: str, max_len: int = 250) -> str:
    if not texto:
        return ""
    if len(texto) <= max_len:
        return texto
    return texto[:max_len].rsplit(' ', 1)[0] + "…"


def descargar_imagen(url: str, ruta_destino: str) -> bool:
    try:
        response = session.get(url, timeout=10)  #type: ignore
        response.raise_for_status()
        if "image" not in response.headers.get("Content-Type", ""):
            return False
        os.makedirs(os.path.dirname(ruta_destino), exist_ok=True)
        with open(ruta_destino, "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        logging.warning("No se pudo descargar imagen %s: %s", url, e)
        return False


def buscar_imagen_local(carpeta: str) -> Optional[str]:
    """Busca un fichero `1.<ext>` dentro de `carpeta` y devuelve una ruta
    accesible por web en formato POSIX.

    - Si `media_web_prefix` es una URL absoluta (http/https) se usará
      `urllib.parse.urljoin` para crear el enlace completo.
    - En entornos locales (prefijos tipo `/media/`) se construye usando
      `posixpath.join` para asegurar separadores `/` independientemente
      del sistema operativo.
    """
    p = Path(carpeta)
    exts = ("jpg", "jpeg", "png", "gif", "webp")
    for ext in exts:
        candidate = p / f"1.{ext}"
        if candidate.exists():
            web_prefix = config.get('media_web_prefix') or '/media/'
            # Normalize prefix: ensure it has a trailing slash for urljoin semantics
            if isinstance(web_prefix, str) and not web_prefix.endswith('/'):
                web_prefix = web_prefix + '/'

            # If prefix looks like an absolute URL, use urljoin
            lower = web_prefix.lower()
            rel_path = f"{p.name}/1.{ext}"
            if lower.startswith('http://') or lower.startswith('https://'):
                return urljoin(web_prefix, rel_path)
            # Otherwise build a POSIX path (keeps '/'). Ensure single leading '/'
            # so join behaves like a web prefix.
            if not web_prefix.startswith('/'):
                web_prefix = '/' + web_prefix
            return posixpath.join(web_prefix, p.name, f"1.{ext}")
    return None


# Diccionario común de géneros (para traducciones rápidas)
GENRE_MAP = {
    "Action": "Acción", "Adventure": "Aventura", "Comedy": "Comedia", "Drama": "Drama",
    "Romance": "Romance", "Fantasy": "Fantasía", "Horror": "Terror", "Sci-Fi": "Ciencia ficción",
    "Science Fiction": "Ciencia ficción", "Mystery": "Misterio", "Music": "Música", "Sports": "Deportes",
    "Slice of Life": "Recuentos de la vida", "Supernatural": "Sobrenatural", "Ecchi": "Ecchi", "Mecha": "Mecha",
    "Shounen": "Shounen", "Family": "Familia", "Thriller": "Suspense", "Crime": "Crimen",
    "History": "Histórico", "Documentary": "Documental", "Western": "Western", "Biography": "Biografía",
    "Animation": "Animación", "Kids": "Infantil", "Talk": "Talk show", "Reality": "Reality",
    "Sport": "Deporte", "Game-Show": "Concurso",
}


# TMDB genre mapping (load once). This file is expected to be named `tmdb_gen.json` and
# contain a mapping from TMDB genre id (as string) to localized name.
#
# Load order:
# 1) explicit `config['tmdb_gen_path']` (if set)
# 2) repo root `BASE_DIR/tmdb_gen.json` (stable regardless of CWD)
# 3) module dir `src/utils/tmdb_gen.json` (vendored fallback)
# 4) current working directory `./tmdb_gen.json` (legacy)
#
# If absent, the map will be empty.
TMDB_GENRE_MAP = {}
try:
    import json as _json
    # Prefer a user-configurable path from config, fall back to repo root then module dir then cwd
    try:
        from src.core.config import config as _config, BASE_DIR as _BASE_DIR  # type: ignore
        _mapping_path = _config.get('tmdb_gen_path') or str((_BASE_DIR / "tmdb_gen.json").resolve())
    except Exception:
        _mapping_path = os.path.join(os.path.dirname(__file__), "tmdb_gen.json")

    if not os.path.exists(_mapping_path):
        try:
            from src.core.config import BASE_DIR as _BASE_DIR2  # type: ignore
            _mapping_path = str((_BASE_DIR2 / "tmdb_gen.json").resolve())
        except Exception:
            pass
    if not os.path.exists(_mapping_path):
        _mapping_path = os.path.join(os.path.dirname(__file__), "tmdb_gen.json")
    if not os.path.exists(_mapping_path):
        _mapping_path = os.path.join(os.getcwd(), "tmdb_gen.json")
    if os.path.exists(_mapping_path):
        with open(_mapping_path, "r", encoding="utf-8") as _fh:
            TMDB_GENRE_MAP = _json.load(_fh) or {}
except Exception:
    TMDB_GENRE_MAP = {}


def map_tmdb_genres(raw_categoria: str):
    """Convierte una cadena separada por comas que puede contener ids TMDB
    o nombres ya traducidos en una lista de nombres legibles.

    Ejemplos:
      '28,12' -> ['Acción', 'Aventura']
      'Action,Comedy' -> ['Action', 'Comedy'] (sin mapping disponible)
    """
    if not raw_categoria:
        return []
    tokens = [t.strip() for t in raw_categoria.split(",") if t.strip()]
    out = []
    for tag in tokens:
        # if numeric id, try mapping
        if tag.isdigit() and tag in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[tag])
        elif tag.isdigit() and str(int(tag)) in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[str(int(tag))])
        elif tag in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[tag])
        else:
            out.append(tag)
    return out


def normalize_folder_name_for_search(name: str):
    """Normalize a folder name into one or more search-friendly title variants.

    Returns a list of candidate strings ordered from most to least specific.
    The function removes common rip/codec/resolution tokens, bracketed content,
    replaces separators with spaces, and yields variants with/without year.
    """
    if not name:
        return [""]
    s = name
    try:
        s = str(s).translate(str.maketrans({"²": "2", "³": "3", "¹": "1"}))
    except Exception:
        pass
    year_candidates: list[str] = []
    try:
        year_candidates.extend(re.findall(r"\b(?:19|20)\d{2}\b", s))
    except Exception:
        pass
    try:
        for m in re.finditer(r"[\(\[]([^\)\]]+)[\)\]]", s):
            try:
                inner = m.group(1) or ""
            except Exception:
                inner = ""
            if not inner:
                continue
            try:
                year_candidates.extend(re.findall(r"\b(?:19|20)\d{2}\b", inner))
            except Exception:
                pass
    except Exception:
        pass
    # remove file extensions if present
    s = re.sub(r"\.[a-zA-Z0-9]{1,4}$", "", s)
    # replace separators with spaces
    s = re.sub(r'[\._\-]+', ' ', s)
    # Preserve leading title tokens like "[REC]²"; remove other bracket noise.
    try:
        s = re.sub(r"^\[([A-Za-z][A-Za-z0-9 ]{1,24})\]", r"\1 ", s)
    except Exception:
        pass
    # remove bracketed content [..], (..), {..}
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", s)
    # remove common tags like resolutions, codecs, languages, release tags
    tags_rx = r"\b(480p|720p|1080p|2160p|4k|8k|480|720|1080|2160|x264|x265|h264|h265|hevc|bdrip|bluray|bdrip|web[- ]?dl|webdl|webrip|hdrip|hdr|dvdscr|cam|ts|telesync|web|dvd|dvdrip|amzn|hd|online|mp4|mkv|aac|flac|eng|esp|spa|es|en|lat|latino|castellano|espanol|espa\u00f1ol|sub|subbed|dubbed|dub|dual|multi|lat[- ]?eng|capitulo|cap\u00edtulo|cap|chapter|parte|pelicula|pel\u00edcula)\b"
    s = re.sub(tags_rx, '', s, flags=re.IGNORECASE)
    # remove social/channel tags like "@StreamingLatino"
    s = re.sub(r"(?i)\B@\w+\b", " ", s)
    # remove leftover multiple spaces
    s = re.sub(r'\s{2,}', ' ', s).strip()

    candidates = []
    if s:
        candidates.append(s)
    # If name starts with a small leading number (e.g. "1 John Wick"), add variants.
    try:
        m_lead = re.match(r"^\s*(\d{1,2})\s+(.+)$", s)
        if m_lead:
            num = int(m_lead.group(1))
            rest = (m_lead.group(2) or "").strip()
            if rest:
                if rest not in candidates:
                    candidates.append(rest)
                with_num = f"{rest} {num}".strip()
                if with_num and with_num not in candidates:
                    candidates.append(with_num)
    except Exception:
        pass
    # variant without year
    s_no_year = re.sub(r"\b(19|20)\d{2}\b", '', s).strip()
    if s_no_year and s_no_year != s:
        candidates.append(s_no_year)
    # variant with explicit year (if it existed in original text)
    try:
        if year_candidates:
            y = year_candidates[0]
            if y:
                if not re.search(rf"\b{re.escape(y)}\b", s):
                    with_year = f"{s} {y}".strip()
                    if with_year and with_year not in candidates:
                        candidates.append(with_year)
    except Exception:
        pass
    # detect roman numerals used as season suffixes (I, II, III, IV, V, VI...)
    roman_rx = re.compile(r"\b(M{0,3})(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\b", re.IGNORECASE)
    # common short season tokens (also handle "Part 2", "Cour 2", and specials folders)
    season_rx = re.compile(
        r"\bseason\b|\btemp(?:orada)?\b|\btemp\b|\bs\d{1,2}\b|\btemp\s*\d+\b|\bepisodio\b|\bpart\b|\bcour\b|\bespecial(?:es)?\b|\bspecials?\b|\bova(?:s)?\b",
        re.IGNORECASE,
    )
    # also extract explicit numeric season like 'Season 2' or 'Temporada 2' (and Part/Cour)
    season_num_rx = re.compile(
        r"\bseason\s*(\d{1,2})\b|\btemporada\s*(\d{1,2})\b|\btemp(?:\.|\s)*(\d{1,2})\b|\bs(\d{1,2})\b|\bpart\s*(\d{1,2})\b|\bcour\s*(\d{1,2})\b",
        re.IGNORECASE,
    )

    # remove season/episode hints (and part/cour/specials)
    s_seasonless = re.sub(
        r"\bseason\b|\bs\d{1,2}\b|\bep\d+\b|\bepisode\b|\btemp(?:orada)?\b|\btemp\b|\bpart\b|\bcour\b|\bespecial(?:es)?\b|\bspecials?\b|\bova(?:s)?\b",
        '',
        s_no_year,
        flags=re.IGNORECASE,
    ).strip()
    if s_seasonless and s_seasonless not in candidates:
        candidates.append(s_seasonless)

    # If name contains an explicit season number, create variant without it and variant with 'Season N' spelled out
    m = season_num_rx.search(s)
    if m:
        # find the first matching group that's not None
        for g in m.groups():
            if g:
                try:
                    n = int(g)
                    # variant without the season number
                    without = season_num_rx.sub('', s).strip()
                    if without and without not in candidates:
                        candidates.append(without)
                    # variant with english 'Season N' appended (some providers expect this)
                    with_season = f"{without} Season {n}" if without else f"Season {n}"
                    if with_season and with_season not in candidates:
                        candidates.append(with_season)
                except Exception:
                    pass
                break

    # If the folder looks like a specials/OVA collection, add English-friendly variants.
    # This helps providers that expect "Specials" naming while keeping the clean base title too.
    try:
        if re.search(r"(?i)\b(?:especial(?:es)?|specials?|ova(?:s)?)\b", s):
            base_no_special = re.sub(r"(?i)\b(?:especial(?:es)?|specials?|ova(?:s)?)\b", "", s).strip()
            base_no_special = re.sub(r"\s{2,}", " ", base_no_special).strip()
            if base_no_special and base_no_special not in candidates:
                candidates.append(base_no_special)
            if base_no_special:
                v1 = f"{base_no_special} Specials"
                v2 = f"{base_no_special} Special"
                if v1 not in candidates:
                    candidates.append(v1)
                if v2 not in candidates:
                    candidates.append(v2)
    except Exception:
        pass

    # If name ends with a plain trailing number (common season suffix like "... 5"),
    # create variants without it and with "Season N". Avoid treating years as seasons.
    try:
        m_end = re.search(r"\b(?!19\d{2}\b)(?!20\d{2}\b)(\d{1,2})\b\s*$", s)
        if m_end:
            n = int(m_end.group(1))
            without_num = re.sub(r"\s+\d{1,2}\s*$", "", s).strip()
            if without_num and without_num not in candidates:
                candidates.append(without_num)
            with_season = f"{without_num} Season {n}" if without_num else f"Season {n}"
            if with_season and with_season not in candidates:
                candidates.append(with_season)
    except Exception:
        pass

    # detect simple roman numerals (common suffixes like II, III, IV)
    # We look for short roman numerals at the end or near the end
    m2 = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", s, flags=re.IGNORECASE)
    roman_map = {"I":1, "II":2, "III":3, "IV":4, "V":5, "VI":6, "VII":7, "VIII":8, "IX":9, "X":10}
    if m2:
        roman = m2.group(1).upper()
        num = roman_map.get(roman)
        if num:
            without_roman = re.sub(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", '', s, flags=re.IGNORECASE).strip()
            if without_roman and without_roman not in candidates:
                candidates.append(without_roman)
            # variant with 'Season N'
            with_season = f"{without_roman} Season {num}" if without_roman else f"Season {num}"
            if with_season and with_season not in candidates:
                candidates.append(with_season)
    try:
        existing_alias = {str(x).strip().lower() for x in candidates if str(x or "").strip()}
        keys = {_alias_key(s)}
        try:
            no_year_key = _alias_key(re.sub(r"\b(?:19|20)\d{2}\b", " ", s))
            if no_year_key:
                keys.add(no_year_key)
        except Exception:
            pass
        try:
            for y in year_candidates:
                year_key = _alias_key(f"{s} {y}")
                if year_key:
                    keys.add(year_key)
        except Exception:
            pass
        for key in sorted(keys, key=len, reverse=True):
            aliases = MOVIE_TITLE_ALIASES.get(key)
            if not aliases:
                for alias_key, alias_values in sorted(MOVIE_TITLE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
                    if key.startswith(alias_key + " "):
                        aliases = alias_values
                        break
            if not aliases:
                continue
            for alias in aliases:
                alias_s = str(alias or "").strip()
                if alias_s and alias_s.lower() not in existing_alias:
                    alias_l = alias_s.lower()
                    if alias_s.isdigit() or "themoviedb.org/" in alias_l:
                        insert_at = 0
                        while insert_at < len(candidates):
                            existing_c = str(candidates[insert_at] or "").strip()
                            existing_l = existing_c.lower()
                            if not (existing_c.isdigit() or "themoviedb.org/" in existing_l):
                                break
                            insert_at += 1
                        candidates.insert(insert_at, alias_s)
                    else:
                        candidates.append(alias_s)
                    existing_alias.add(alias_l)
    except Exception:
        pass
    # ensure uniqueness and strip
    seen = set()
    out = []
    for c in candidates:
        cc = c.strip()
        if not cc:
            continue
        if cc.lower() in seen:
            continue
        seen.add(cc.lower())
        out.append(cc)
    if not out:
        return [name]
    return out
