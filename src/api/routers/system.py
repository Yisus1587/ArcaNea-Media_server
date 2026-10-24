from __future__ import annotations

import io
import logging
import os
import socket
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy import text

from ...core import config as core_config
from ...db import SessionLocal
from ...services.enrichment_runner import (
    is_paused as is_enrich_paused,
    set_paused as set_enrich_paused,
)
from .. import runtime
from ..deps import admin_or_localhost, admin_required

try:
    from ...providers import provider_jikan as jikan_provider
except Exception:
    jikan_provider = None

try:
    from ...providers import provider_kitsu as kitsu_provider
except Exception:
    kitsu_provider = None

router = APIRouter()


def _runtime_root() -> Path:
    try:
        return Path(core_config.DATA_DIR).resolve().parent
    except Exception:
        return Path.cwd().resolve()


def _ca_path() -> Path:
    return (_runtime_root() / "certs" / "rootCA.pem").resolve()


def _is_private_ip(ip: str) -> bool:
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return False
        a, b, _c, _d = parts
        if a == 10:
            return True
        if a == 192 and b == 168:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        return False
    except Exception:
        return False


def _collect_hosts() -> list[str]:
    hosts: list[str] = ["localhost", "127.0.0.1"]
    try:
        hn = socket.gethostname()
        if hn and hn not in hosts:
            hosts.append(hn)
    except Exception:
        pass

    ips: set[str] = set()
    try:
        try:
            _hn, _aliases, addrs = socket.gethostbyname_ex(socket.gethostname())
            for a in addrs or []:
                ips.add(a)
        except Exception:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ips.add(s.getsockname()[0])
            finally:
                s.close()
        except Exception:
            pass
    except Exception:
        pass

    for ip in sorted(ips):
        if _is_private_ip(ip) and ip not in hosts:
            hosts.append(ip)

    return hosts


def _pick_preferred_host(hosts: list[str]) -> str | None:
    for h in hosts:
        if _is_private_ip(h):
            return h
    for h in hosts:
        if h and h not in ("localhost", "127.0.0.1"):
            return h
    return hosts[0] if hosts else None


@router.get("/api/system/status")
def system_status():
    return {
        "watcher_paused": runtime.watcher_is_paused(),
        "enrich_paused": bool(is_enrich_paused()),
    }


@router.post("/api/system/pause")
def system_pause(_: bool = Depends(admin_or_localhost)):
    """Pause watcher + enrichment (used by system tray)."""
    try:
        runtime.watcher_pause()
    except Exception:
        pass
    try:
        set_enrich_paused(True)
    except Exception:
        pass
    return system_status()


@router.post("/api/system/resume")
def system_resume(_: bool = Depends(admin_or_localhost)):
    try:
        runtime.watcher_resume()
    except Exception:
        pass
    try:
        set_enrich_paused(False)
    except Exception:
        pass
    return system_status()


@router.get("/api/health")
def healthcheck():
    """Lightweight health endpoint for UI/tray diagnostics."""
    try:
        db_ok = True
        db_detail = None
        try:
            db = SessionLocal()
            try:
                db.execute(text("SELECT 1"))
            finally:
                db.close()
        except Exception as e:
            db_ok = False
            db_detail = str(e)

        data_dir = str(core_config.DATA_DIR)
        runtime_root = str(Path(core_config.DATA_DIR).resolve().parent)
        try:
            anime_provider = core_config.get("metadata_anime_provider")  # type: ignore[attr-defined]
        except Exception:
            anime_provider = None
        try:
            movies_provider = core_config.get("metadata_movies_provider")  # type: ignore[attr-defined]
        except Exception:
            movies_provider = None
        try:
            jikan_health = jikan_provider.healthcheck() if jikan_provider is not None and hasattr(jikan_provider, "healthcheck") else None
        except Exception as e:
            jikan_health = {"ok": False, "detail": str(e)}
        try:
            kitsu_health = kitsu_provider.healthcheck() if kitsu_provider is not None and hasattr(kitsu_provider, "healthcheck") else None
        except Exception as e:
            kitsu_health = {"ok": False, "detail": str(e)}
        return {
            "ok": bool(db_ok),
            "db_ok": bool(db_ok),
            "db_detail": db_detail,
            "data_dir": data_dir,
            "runtime_root": runtime_root,
            "watcher_paused": runtime.watcher_is_paused(),
            "enrich_paused": bool(is_enrich_paused()),
            "provider_config": {
                "anime": anime_provider,
                "movies": movies_provider,
            },
            "providers": {
                "jikan": jikan_health,
                "kitsu": kitsu_health,
            },
            "ts": int(time.time()),
        }
    except Exception as e:
        return {"ok": False, "detail": str(e), "ts": int(time.time())}


@router.get("/api/system/lan-info")
def lan_info(request: Request):
    hosts = _collect_hosts()
    preferred = _pick_preferred_host(hosts)
    try:
        port = int(request.url.port or 0) or int(os.environ.get("ARCANEA_API_PORT", "9800"))
    except Exception:
        port = 9800
    download_url = f"http://{preferred}:{port}/api/system/download-ca" if preferred else None
    return {
        "hosts": hosts,
        "preferred_host": preferred,
        "port": port,
        "download_url": download_url,
    }


@router.get("/api/system/download-ca")
def download_ca():
    p = _ca_path()
    if not p.exists():
        raise HTTPException(status_code=404, detail="root_ca_not_found")
    return FileResponse(
        str(p),
        media_type="application/x-x509-ca-cert",
        filename="arcanea-rootCA.pem",
    )


def _default_log_path() -> Path:
    try:
        rr = Path(core_config.DATA_DIR).resolve().parent
    except Exception:
        rr = Path.cwd().resolve()
    return (rr / "logs" / "arcanea.log").resolve()


@router.get("/api/logs/info")
def logs_info(_: bool = Depends(admin_required)):
    p = _default_log_path()
    return {"path": str(p), "exists": bool(p.exists()), "size": int(p.stat().st_size) if p.exists() else 0}


@router.get("/api/logs/tail")
def logs_tail(lines: int = 200, _: bool = Depends(admin_required)):
    """Return the last N lines of the main log file (best-effort)."""
    try:
        n = int(lines) if lines is not None else 200
        if n <= 0:
            n = 200
        n = min(n, 2000)
    except Exception:
        n = 200

    p = _default_log_path()
    if not p.exists():
        return {"path": str(p), "lines": [], "error": "not_found"}

    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n + 5:
                step = block if size >= block else size
                size -= step
                fh.seek(size, os.SEEK_SET)
                data = fh.read(step) + data
                if size == 0:
                    break
        text_out = data.decode("utf-8", errors="replace")
        parts = [ln for ln in text_out.splitlines() if ln is not None]
        out_lines = parts[-n:]
        return {"path": str(p), "lines": out_lines}
    except Exception as e:
        logging.getLogger(__name__).exception("Failed tailing log")
        return {"path": str(p), "lines": [], "error": str(e)}


def _sqlite_path_from_url(url: str) -> Path | None:
    try:
        if not isinstance(url, str):
            return None
        u = url.strip()
        if not u.lower().startswith("sqlite:///"):
            return None
        raw = u[len("sqlite:///") :]
        return Path(raw).resolve()
    except Exception:
        return None


@router.get("/api/backup/export")
def export_backup(_: bool = Depends(admin_required)):
    """Download a ZIP backup (db + app_config)."""
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            try:
                cfg = Path(core_config.DATA_DIR).resolve() / "app_config.json"
                if cfg.exists():
                    z.write(str(cfg), arcname="data/app_config.json")
            except Exception:
                pass
            try:
                db_url = core_config.config.get("db_url") or ""
                dbp = _sqlite_path_from_url(str(db_url))
                if dbp and dbp.exists():
                    z.write(str(dbp), arcname="db/arcanea.db")
            except Exception:
                pass
        buf.seek(0)
        ts = time.strftime("%Y%m%d-%H%M%S")
        fn = f"arcanea-backup-{ts}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename=\"{fn}\"'},
        )
    except Exception as e:
        logging.getLogger(__name__).exception("Failed exporting backup")
        raise HTTPException(status_code=500, detail=str(e))
