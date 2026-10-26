from __future__ import annotations

import logging
import os
import platform
import shutil
import traceback
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..deps import admin_required
from ...core.config import IS_ANDROID

router = APIRouter()

# Android has no drive letters and no `/mnt` mounts: shared storage is
# `/storage/emulated/0` (internal) and each SD card shows up as `/storage/<UUID>`,
# while Termux exposes the same place as `~/storage/shared`. A path can exist but
# still be unreadable without the storage permission, so it is reported with
# zeroes instead of being dropped.
ANDROID_INTERNAL_STORAGE = "/storage/emulated/0"
ANDROID_STORAGE_SKIP = {"emulated", "self", "sdcard0", "enc_emulated", "run"}


def _android_drive_candidates() -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    try:
        if os.path.isdir(ANDROID_INTERNAL_STORAGE):
            candidates.append((ANDROID_INTERNAL_STORAGE, "Almacenamiento interno"))
    except Exception:
        pass

    try:
        for name in sorted(os.listdir("/storage")):
            if name in ANDROID_STORAGE_SKIP:
                continue
            path = os.path.join("/storage", name)
            try:
                if os.path.isdir(path):
                    candidates.append((path, f"SD ({name})"))
            except Exception:
                continue
    except Exception:
        pass

    try:
        termux_shared = Path.home() / "storage" / "shared"
        if termux_shared.is_dir():
            candidates.append((str(termux_shared), "Termux (shared)"))
    except Exception:
        pass

    return candidates


def _desktop_placeholder_drives() -> list[dict]:
    """Fallback used on desktop when drive listing fails; Android gets an empty list."""
    return [
        {
            "path": "C:\\",
            "label": "Windows (C:)",
            "totalSpace": 256 * 1024 * 1024 * 1024,
            "freeSpace": 150 * 1024 * 1024 * 1024,
            "usedSpace": 106 * 1024 * 1024 * 1024,
            "percentage": 41.4,
        },
        {
            "path": "D:\\",
            "label": "Data (D:)",
            "totalSpace": 1024 * 1024 * 1024 * 1024,
            "freeSpace": 800 * 1024 * 1024 * 1024,
            "usedSpace": 224 * 1024 * 1024 * 1024,
            "percentage": 21.9,
        },
    ]


@router.get("/api/drives")
def list_drives(_: bool = Depends(admin_required)):
    drives = []

    try:
        system = platform.system()

        if IS_ANDROID:
            for path, label in _android_drive_candidates():
                try:
                    usage = shutil.disk_usage(path)
                    drives.append(
                        {
                            "path": path,
                            "label": label,
                            "totalSpace": usage.total,
                            "freeSpace": usage.free,
                            "usedSpace": usage.used,
                            "percentage": (usage.used / usage.total) * 100 if usage.total > 0 else 0,
                        }
                    )
                except Exception:
                    drives.append(
                        {
                            "path": path,
                            "label": label,
                            "totalSpace": 0,
                            "freeSpace": 0,
                            "usedSpace": 0,
                            "percentage": 0,
                        }
                    )

        elif system == "Windows":
            import string

            for drive_letter in string.ascii_uppercase:
                path = f"{drive_letter}:\\"
                if os.path.exists(path):
                    try:
                        usage = shutil.disk_usage(path)
                        label = f"Local Disk ({drive_letter}:)"
                        try:
                            import ctypes

                            kernel32 = ctypes.windll.kernel32
                            volume_name_buffer = ctypes.create_unicode_buffer(1024)
                            file_system_buffer = ctypes.create_unicode_buffer(1024)
                            if kernel32.GetVolumeInformationW(
                                ctypes.c_wchar_p(path),
                                volume_name_buffer,
                                ctypes.sizeof(volume_name_buffer),
                                None,
                                None,
                                None,
                                file_system_buffer,
                                ctypes.sizeof(file_system_buffer),
                            ):
                                if volume_name_buffer.value:
                                    label = volume_name_buffer.value
                        except Exception:
                            pass

                        drives.append(
                            {
                                "path": path,
                                "label": label,
                                "totalSpace": usage.total,
                                "freeSpace": usage.free,
                                "usedSpace": usage.used,
                                "percentage": (usage.used / usage.total) * 100 if usage.total > 0 else 0,
                            }
                        )
                    except Exception as e:
                        print(f"Error obteniendo info de {path}: {e}")
                        drives.append(
                            {
                                "path": path,
                                "label": f"Drive {drive_letter}",
                                "totalSpace": 0,
                                "freeSpace": 0,
                                "usedSpace": 0,
                                "percentage": 0,
                            }
                        )

        elif system in ("Linux", "Darwin"):
            try:
                import psutil  # type: ignore

                partitions = psutil.disk_partitions(all=False)
                for partition in partitions:
                    try:
                        if os.path.exists(partition.mountpoint):
                            usage = shutil.disk_usage(partition.mountpoint)
                            label = partition.device
                            if partition.device.startswith("/dev/"):
                                label = partition.device[5:]

                            drives.append(
                                {
                                    "path": partition.mountpoint,
                                    "label": label,
                                    "totalSpace": usage.total,
                                    "freeSpace": usage.free,
                                    "usedSpace": usage.used,
                                    "percentage": (usage.used / usage.total) * 100 if usage.total > 0 else 0,
                                    "fstype": partition.fstype,
                                }
                            )
                    except Exception as e:
                        print(f"Error procesando {partition.mountpoint}: {e}")
            except ImportError:
                candidates = ["/"] + (
                    [f"/mnt/{d}" for d in os.listdir("/mnt") if os.path.isdir(f"/mnt/{d}")]
                    if os.path.exists("/mnt")
                    else []
                )
                candidates += (
                    [f"/media/{d}" for d in os.listdir("/media") if os.path.isdir(f"/media/{d}")]
                    if os.path.exists("/media")
                    else []
                )

                for path in candidates:
                    try:
                        usage = shutil.disk_usage(path)
                        drives.append(
                            {
                                "path": path,
                                "label": os.path.basename(path) or path,
                                "totalSpace": usage.total,
                                "freeSpace": usage.free,
                                "usedSpace": usage.used,
                                "percentage": (usage.used / usage.total) * 100 if usage.total > 0 else 0,
                            }
                        )
                    except Exception:
                        pass

        drives.sort(key=lambda x: x["path"])
    except Exception as e:
        print(f"Error general en list_drives: {e}")
        print(traceback.format_exc())
        # Android has no drive letters: an empty list is better than fake C:/D: units.
        drives = [] if IS_ANDROID else _desktop_placeholder_drives()

    return {"drives": drives}


@router.get("/api/fs/list")
def fs_list(path: str | None = None, limit: int = 200, _: bool = Depends(admin_required)):
    try:
        limit = int(limit) if limit is not None else 200
        if limit <= 0:
            limit = 200
        limit = min(limit, 2000)
    except Exception:
        limit = 200

    try:
        if not path:
            try:
                drives = list_drives().get("drives", [])
                entries = []
                for d in drives:
                    p = (d.get("path") or "").strip()
                    if not p:
                        continue
                    entries.append({"name": d.get("label") or p, "path": p, "type": "drive"})
                entries.sort(key=lambda e: (e.get("type") != "drive", str(e.get("name") or "").lower()))
                return {"path": None, "parent": None, "entries": entries}
            except Exception:
                return {"path": None, "parent": None, "entries": []}

        p = Path(path).resolve()
        if not p.exists():
            return JSONResponse({"path": str(p), "parent": None, "entries": [], "error": "not_found"}, status_code=404)

        parent = str(p.parent) if p.parent and p.parent.exists() else None
        entries: list[dict] = []
        try:
            with os.scandir(str(p)) as it:
                for ent in it:
                    try:
                        if not ent.is_dir(follow_symlinks=False):
                            continue
                        name = ent.name
                        if name in (".", ".."):
                            continue
                        entries.append({"name": name, "path": str(Path(ent.path)), "type": "dir"})
                        if len(entries) >= limit:
                            break
                    except Exception:
                        continue
        except PermissionError:
            return JSONResponse(
                {"path": str(p), "parent": parent, "entries": [], "error": "permission_denied"}, status_code=403
            )

        entries.sort(key=lambda e: (e.get("type") != "drive", str(e.get("name") or "").lower()))
        return {"path": str(p), "parent": parent, "entries": entries}
    except Exception as e:
        logging.getLogger(__name__).exception("fs_list failed")
        return JSONResponse({"path": path, "parent": None, "entries": [], "error": str(e)}, status_code=500)
