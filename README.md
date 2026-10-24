# ArcaNea — Servidor de medios local (Backend + Frontend)

ArcaNea es una app para gestionar y reproducir una biblioteca multimedia local
(películas, series y anime) con interfaz estilo Netflix:

- **Backend** (`src/`) — Python con FastAPI, SQLAlchemy y SQLite: escaneo del
  sistema de archivos, identificación de contenido, enriquecimiento de
  metadatos (TMDB, Jikan, Kitsu), streaming con soporte `Range`, telemetría,
  recomendaciones y watcher de cambios en disco.
- **Frontend** (`arcanea-media-server/`) — React 19 + Vite 6 + TypeScript +
  Tailwind CSS 4: multi-perfil con PIN, i18n es/en, reproductor, PWA con
  service worker y acceso por QR en la LAN.

**Estado:** en desarrollo (uso local funcional, en evolución).
Documentación interna del estado del proyecto: [`ANALISIS.md`](ANALISIS.md).

---

## Estructura

```
.
├── src/                    # Backend (FastAPI)
│   ├── api/                # app, server, deps y routers/*
│   ├── core/               # config, network, translations
│   ├── db/                 # models (SQLite) y helpers
│   ├── providers/          # tmdb, jikan, kitsu
│   ├── scanner/            # scanner + filesystem_scanner
│   ├── services/           # enrichment, media_pipeline, telemetry, ...
│   ├── tools/              # migraciones DB y scripts de mantenimiento
│   ├── utils/              # rate_limiter, path_hash, media_exts
│   ├── watcher/            # WatchService (watchdog)
│   └── main.py             # loop watcher → scanner
├── arcanea-media-server/   # Frontend (Vite + React)
├── assets/                 # Icono de la app
├── run.py                  # Orquestador: backend + frontend (dev / release / https)
├── dev.py                  # Atajo de desarrollo: uvicorn --reload + vite
├── inspect_db.py           # Inspección ad-hoc de la base de datos
├── requirements.txt        # Dependencias Python (UTF-8)
└── tmdb_gen.json           # Mapeo de géneros TMDB
```

`data/` (base de datos, caché, `app_config.json`) y los certificados TLS se
generan en tiempo de ejecución y **no se versionan**.

---

## Requisitos

- Python 3.11+
- Node.js 18+ con npm
- Opcional: [`mkcert`](https://github.com/FiloSottile/mkcert) para HTTPS en dev

## Instalación

Backend:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Frontend:

```bash
cd arcanea-media-server
npm install
```

## Ejecución

```bash
# Desarrollo: Vite en 0.0.0.0:9800 (UI) + FastAPI en 9801 (API)
python run.py
python dev.py            # equivalente directo

# Modo release: FastAPI sirve el frontend compilado en el puerto 9800
python run.py --release

# Build del frontend antes de release
python run.py --build-frontend     # o: cd arcanea-media-server && npm run build

# HTTPS con certificados mkcert
python run.py --https

# Solo backend / solo frontend
python -m uvicorn src.api.asgi:app --reload --port 9800
cd arcanea-media-server && npm run dev
```

## Variables útiles

| Variable | Descripción |
|---|---|
| `ARCANEA_MEDIA_ROOT` | Ruta inicial sugerida para el escaneo |
| `ARCANEA_SERVE_FRONTEND=1` | Sirve el frontend compilado desde el backend |
| `ARCANEA_STARTUP_SCAN`, `ARCANEA_WATCH` | Escaneo al arrancar / watcher de cambios |
| `ARCANEA_ADMIN_PIN` | PIN de administración (también se puede crear desde la UI) |
| `ARCANEA_API_PORT`, `ARCANEA_DEV_PORT` | Puerto de la API y de Vite en dev |
| `ARCANEA_DATA_DIR`, `ARCANEA_DB_URL`, `ARCANEA_CACHE_DIR` | Ubicación de datos y SQLite |
| `TMDB_API_KEY` / `TMDB_ACCESS_TOKEN` / `TMDB_USE_V4` | Credenciales TMDB (también se guardan desde Ajustes → Credenciales) |

El listado completo vive en `src/core/config.py`.

## Flujo de uso (resumen)

1. Inicia la app y crea el perfil de gestión (PIN requerido).
2. Agrega rutas de biblioteca.
3. Se ejecuta el escaneo; si hay clave TMDB, se enriquece.
4. Usa "Revisar sin match" o "Corregir Match (Manual)" si hay errores.
5. Reproduce contenido desde la UI.

## Seguridad

- Los endpoints de administración (scan, enrich, fs, logs, credenciales,
  backup, datos) exigen el token de admin; sin PIN configurado solo se
  aceptan peticiones desde localhost.
- Para acceso en LAN, inicia el backend con `0.0.0.0` y revisa el firewall.
- No subas a git: `certs/` (incluida la llave privada), `dist/`, `data/`,
  `.env` ni `src.zip`; todo está en `.gitignore`.

## Licencia

MIT — ver [`LICENSE`](LICENSE).
