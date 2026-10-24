# Análisis de ArcaNea Media Server

> Fecha: 2026-08-17 · Proyecto personal con ~3 meses de desarrollo · Sin control de versiones detectado

## 📋 Resumen general

**ArcaNea** es un servidor de medios self-hosted estilo Plex/Jellyfin. Tiene dos piezas:

- **Frontend** (`arcanea-media-server/`) — React 19 + TypeScript + Vite 6 + Tailwind CSS v4. PWA con service worker, multi-perfil, selector de idioma es/en, lazy loading.
- **Backend** (`src/`) — FastAPI + SQLAlchemy + SQLite. Escáner de archivos, enriquecimiento de metadatos (TMDB / Jikan / Kitsu), watcher de cambios, streaming, telemetría y recomendaciones.

**Propósito:** escanear carpetas locales de películas/series/anime, identificarlas contra proveedores de metadatos, enriquecerlas y servirlas por HTTP/HTTPS con una UI tipo Netflix.

**Tipo:** app web full-stack (SPA + API REST/WebSocket/SSE), con opción de empaquetarse como ejecutable de escritorio (PyInstaller + system tray en Windows).

---

## 🏗️ Estructura de carpetas

```
.
├── arcanea-media-server/   # Frontend React (Vite)
│   ├── components/         # app, auth, layout, media, player, settings, setup, system, ui
│   ├── hooks/              # useMediaLibrary, usePlayerState, useSettingsState, etc.
│   ├── i18n/               # i18n.tsx (es/en, es-419, es-MX)
│   ├── services/api.ts     # Cliente HTTP/WS/SSE del backend (1298 líneas)
│   ├── public/             # fonts, icons, images, manifest, sw.js
│   ├── dist/               # Build compilado (commiteado)
│   ├── certs/              # Certificados HTTPS (llave privada incluida)
│   └── .env.local          # Secretos locales
├── src/                    # Backend Python (FastAPI)
│   ├── api/                # app, server, deps, routers/*, services/*
│   ├── core/               # config, network, translations
│   ├── db/                 # models.py (17 tablas), helpers
│   ├── providers/          # provider_jikan, provider_kitsu, provider_tmdb
│   ├── scanner/            # filesystem_scanner, scanner
│   ├── services/           # enrichment, media_pipeline, recommendations, telemetry...
│   ├── tools/              # migraciones DB, cache, scripts de mantenimiento
│   ├── utils/              # rate_limiter, path_hash, media_exts
│   ├── watcher/            # WatchService (watchdog)
│   └── main.py             # loop watcher→scanner
├── data/                   # arcanea.db (+ WAL/SHM/backup), app_config.json, cache
├── assets/                 # arcanea.ico
├── run.py                  # Orquestador: levanta backend+frontend, HTTPS, tray
├── dev.py                  # Script de desarrollo
├── inspect_db.py           # Inspección ad-hoc de la DB
├── requirements.txt        # Dependencias Python (⚠️ en UTF-16)
└── tmdb_gen.json           # Mapeo de géneros TMDB
```

---

## 📦 Dependencias principales

**Frontend** (`package.json`):
- `react` / `react-dom` ^19.2.4
- `lucide-react` (iconos), `qrcode` (QR para acceso LAN)
- Vite 6, TypeScript 5.8, Tailwind 4, PostCSS

**Backend** (`requirements.txt`):
- `fastapi` 0.128, `uvicorn` 0.40, `starlette` 0.50
- `SQLAlchemy` 2.0, `sqlite3` (stdlib)
- `httpx`, `requests` (clientes HTTP a proveedores)
- `watchdog` / `watchfiles` (watcher de filesystem)
- `websockets` 15 (status en tiempo real)
- `tenacity` (retries), `pystray` + `pyinstaller` (empaquetado EXE + tray)
- `google-genai` + `google-auth` (integración Gemini)
- `Pillow`, `PyYAML`, `python-dotenv`, `psutil`, `rich`

---

## 🧩 Arquitectura

**Flujo de vida de un media item:**
1. `WatchService` (watchdog) detecta cambios en las carpetas raíz → encola rutas.
2. `Scanner` (`filesystem_scanner`) clasifica película/serie/anime, agrupa por carpeta, normaliza títulos.
3. Los items quedan en `media_item` (status `SCANNED`).
4. `EnrichmentRunner`/`enrichment_pipeline` los consulta contra proveedores (TMDB para movies, Jikan/Kitsu para anime) y escribe `media_metadata`, imágenes, traducciones y relaciones.
5. La API (`routers/media.py`, `stream.py`) expone items, posters y streaming.
6. El frontend consume `/api` vía `services/api.ts`, con status por WebSocket y telemetría por SSE.

**Patrones detectados:**
- Provider abstraction (Jikan/Kitsu/TMDB detrás de una misma interfaz).
- Bridge tables normalizadas (`season_item`, `episode_file`).
- idempotencia en enriquecimiento vía `checksum` + `version`.
- "Prender y listo": `run.py` orquesta dev/release, HTTPS con mkcert y tray sin tocar npm manualmente.
- Lazy loading y code splitting en el frontend.
- Multi-perfil con PIN, modo manager/admin.

---

## ✅ Puntos fuertes

1. **Separación clara backend/frontend** y módulos backend bien organizados por responsabilidad.
2. **Modelo de datos normalizado** (17 tablas): series→seasons→episodes→files con bridges, traducciones, historial, imágenes y relaciones. Nada que envidiarle a un media server comercial.
3. **Providers bien abstraídos** con fallback (Jikan → TMDB strict matching) y normalización de estado "aireando".
4. **i18n de verdad**: soporte es/en con matices `es-419` → `es-MX`, y UI locale separado del locale de metadatos del proveedor.
5. **Defensividad alta**: try/except por todos lados, datos mock si el backend está caído, self-healing del layout.
6. **DX de "doble clic y listo"**: `run.py` maneja dev vs release, genera certs, abre navegador, soporta EXE con tray.
7. **PWA**: service worker, manifest, iconos maskables, fuentes locales (offline).
8. **Lazy loading** de los componentes pesados (player y detail modal).

---

## ⚠️ Debilidades y sugerencias

### 🔴 Críticas (hazlas ya)

1. **No hay git ni `.gitignore`.** Tres meses sin control de versiones es el mayor riesgo del proyecto. Un solo `rm` accidental y pierdes todo.
   - *Sugerencia:* `git init`, agrega `.gitignore` (abajo te dejo lo que debe excluir) y haz el primer commit YA.

2. **Fuga de `GEMINI_API_KEY` al bundle del frontend.** En `vite.config.ts`:
   ```ts
   define: {
     'process.env.API_KEY': JSON.stringify(env.GEMINI_API_KEY),
     'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY)
   }
   ```
   Si la variable está seteada, la llave queda incrustada en el JS público (`dist/assets/*.js`) y cualquier navegador la ve.
   - *Sugerencia:* quita ambos `define` del frontend. Gemini debe llamarse solo desde el backend, nunca exponer la llave al cliente.

3. **`index.tsx` borra todo el almacenamiento en cada carga** (localStorage, sessionStorage, cookies, caches y service workers) salvo que la URL traiga `?keep_storage=1`:
   ```ts
   if (shouldClearStorage) { localStorage.clear(); sessionStorage.clear(); ... }
   ```
   Consecuencias:
   - El admin token (`arcanea_admin_token`) se pierde en cada reload → el modo admin se cae.
   - La PWA nunca persiste (registra el SW solo con `keep_storage=1`).
   - La preferencia de idioma se resetea cada vez.
   - *Sugerencia:* esto parece leftover de desarrollo para evitar caches stale. Inviértelo: solo limpiar cuando exista un flag explícito (ej. `?clear_storage=1`), no al revés.

4. **Datos sensibles/runtime sueltos en el proyecto:**
   - `data/arcanea.db` + `-shm` + `-wal` + backup → tu base de datos real.
   - `data/app_config.json` → rutas, perfiles y (potencialmente) PINs.
   - `certs/arcanea-key.pem` → llave privada HTTPS.
   - `arcanea-media-server/.env.local` → secretos.
   - `arcanea-media-server/dist/`, `node_modules/`, `src/__pycache__/`, `src/tools/.cache/`.
   - *Sugerencia:* todo lo anterior va al `.gitignore`. La llave privada de certs regenérala (mkcert) o no la versiones.

### 🟠 Deuda técnica

5. **`src/services/enrichment.py` tiene ~10,951 líneas.** Es un archivo dios que concentra casi toda la lógica de enriquecimiento. Imposible de mantener/testear.
   - *Sugerencia:* partir en módulos por proveedor/fase (fetch, normalizar, persistir, relacionar). Ya tienes `enrichment_pipeline.py` y `enrichment_runner.py`; el grueso debería vivir ahí, no en un solo archivo.

6. **`requirements.txt` está en UTF-16 LE con BOM.** `pip` puede fallar o dar problemas de parseo.
   - *Sugerencia:* convertirlo a UTF-8:
     ```bash
     iconv -f UTF-16 -t UTF-8 requirements.txt > req_utf8.txt && mv req_utf8.txt requirements.txt
     ```

7. **Sin tests.** No hay suite de pruebas ni en backend ni en frontend. Con tanta lógica de matching/enriquecimiento, esto es frágil.
   - *Sugerencia:* al menos tests unitarios para `normalizeRoots`, `mapBackendItem`, clasificación del scanner y resolución de "aireando".

8. **Sin README.** `metadata.json` describe el frontend, pero no hay documentación de cómo correr, configurar TMDB keys, o qué hace cada cosa.
   - *Sugerencia:* README con: requisitos, `python run.py --dev/--release`, cómo setear `TMDB_API_KEY`, estructura, y el estado del proyecto.

9. **Archivos frontend demasiado grandes:** `services/api.ts` (1,298 líneas), `i18n/i18n.tsx` (1,117), `App.tsx` (766 con mucha lógica de boot). Backend: `routers/media.py` (2,135), `services/manual_mapping.py` (1,392), `media_pipeline.py` (960).

10. **`App.tsx` sincroniza config en un `useEffect` con `eslint-disable`** y dependencias incompletas. Riesgo de loops o estado stale. La lógica de arranque (`handleBootComplete`) está toda en el componente raíz.

### 🟡 Menores

11. **`MOCK_MEDIA` tiene `duration` inconsistente** (ej. `Interstellar: 10140` que se interpreta como 169 horas). El tipo dice "minutos" pero los valores no cuadran. Dato legacy.
12. **`config.py` duplica lógica de carga de config** (dotenv, app_config.json, overrides de scanner keywords) de forma muy inline y repetida. Convendría una sola función `load_app_config()`.
13. **`vite.config.ts` default tiene `devPort` y `backendPort` ambos en 9800.** `run.py` lo corrige (sube API a 9801 en dev), pero `npm run dev` directo provocaría que el proxy `/api` apunte a sí mismo.
14. **Scripts sueltos en la raíz** (`inspect_db.py`, `dev.py`, `tmdb_gen.json`) sin un propósito documentado. `tmdb_gen.json` es un mapeo de géneros que podría ir en `src/core` o generarse.

---

## 📝 Notas

- **No se pudo leer** `arcanea-media-server/.env.local` (protegido por el entorno), ni conviene. Solo confirmo que existe y debe quedar fuera de cualquier commit.
- **`data/app_config.json`** actualmente **no** guarda credenciales TMDB en claro (solo provider/prefs); igual va al `.gitignore` porque puede cambiar.
- La base de datos (`arcanea.db`) se abrió en modo solo-lectura para listar tablas: 17 tablas, esquema sano.
- No hay repositorio git en el workspace, así que no se pudo revisar historial ni diff.

---

## 🗑️ Qué debería ir en `.gitignore` (mínimo)

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
venv/
.venv/

# Node
node_modules/
dist/
.vite/

# Runtime data / secretos
data/
*.db
*.db-shm
*.db-wal
*.db.bak_*
arcanea-media-server/.env
arcanea-media-server/.env.local
.env
.env.local

# Certificados (regenerables con mkcert)
certs/
arcanea-media-server/certs/

# Cache de herramientas
src/tools/.cache/

# OS / editor
.DS_Store
Thumbs.db
.idea/
.vscode/
```

## ✅ Orden sugerido de acción

1. `git init` + `.gitignore` + primer commit (sin datos sensibles).
2. Quitar el `define` de `GEMINI_API_KEY` del `vite.config.ts`.
3. Arreglar `index.tsx` para que no borre storage en cada carga.
4. Convertir `requirements.txt` a UTF-8.
5. Partir `enrichment.py`.
6. Escribir README + primeros tests.
