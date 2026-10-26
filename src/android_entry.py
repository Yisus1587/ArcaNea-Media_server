"""Entry point used by the Android app to run the ArcaNea backend on the phone.

Chaquopy loads this module from Java and calls `start()`; uvicorn runs in a
background thread bound to 127.0.0.1 so the WebView can talk to it. Nothing here is
imported on Windows/Linux: `run.py` and `dev.py` keep using the normal entry points.
"""

from __future__ import annotations

import os
import threading
import time

_server = None
_thread: threading.Thread | None = None
_port: int | None = None
_lock = threading.Lock()

DEFAULT_PORT = 9800


def _prepare_env(data_dir: str) -> None:
    """Point the config at the app sandbox before the backend is imported."""
    os.environ["ARCANEA_PLATFORM"] = "android"
    os.environ["ARCANEA_ANDROID_DATA_DIR"] = data_dir
    os.environ["ARCANEA_DATA_DIR"] = data_dir
    os.environ.setdefault("ARCANEA_CACHE_DIR", os.path.join(data_dir, "cache"))
    os.environ.setdefault("ARCANEA_DB_URL", f"sqlite:///{os.path.join(data_dir, 'arcanea.db')}")
    # The app starts scans explicitly; a filesystem watcher is pointless (and
    # expensive on /sdcard) inside the phone.
    os.environ.setdefault("ARCANEA_WATCH", "0")
    os.environ.setdefault("ARCANEA_STARTUP_SCAN", "0")


def start(port: int = DEFAULT_PORT, data_dir: str | None = None, timeout_s: float = 30.0) -> str:
    """Start uvicorn in a daemon thread.

    Returns `ok:<port>` when the server accepted its sockets, `already_running:<port>`
    when it was already up, or `error:<reason>`.
    """
    global _server, _thread

    global _port

    port = int(port or DEFAULT_PORT)
    with _lock:
        if _thread is not None and _thread.is_alive():
            return f"already_running:{_port or port}"
        if not data_dir:
            return "error:missing_data_dir"

        _prepare_env(str(data_dir))

        try:
            import uvicorn

            from src.api.asgi import app
        except Exception as exc:  # pragma: no cover - only reachable on device
            return f"error:import_failed:{type(exc).__name__}:{exc}"

        try:
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="info",
                log_config=None,
                access_log=False,
                # The app must be stoppable from the notification without waiting
                # on a stuck connection.
                timeout_graceful_shutdown=3,
            )
            _server = uvicorn.Server(config)
            _port = port
            _thread = threading.Thread(target=_server.run, name="arcanea-uvicorn", daemon=True)
            _thread.start()
        except Exception as exc:
            _server = None
            _thread = None
            _port = None
            return f"error:start_failed:{type(exc).__name__}:{exc}"

    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if getattr(_server, "started", False):
            return f"ok:{port}"
        if _thread is None or not _thread.is_alive():
            return "error:thread_died"
        time.sleep(0.1)
    return "error:timeout"


def stop(timeout_s: float = 10.0) -> str:
    """Ask uvicorn to shut down and wait for the thread to finish.

    Graceful shutdown first; if it does not finish in time (a hung request or a
    non-daemon task), force it so the foreground service can be torn down.
    """
    global _server, _thread, _port
    with _lock:
        server = _server
        thread = _thread
        if server is None or thread is None:
            return "not_running"
        server.should_exit = True

    graceful_deadline = time.time() + max(1.0, float(timeout_s) * 0.6)
    while time.time() < graceful_deadline and thread.is_alive():
        time.sleep(0.1)

    if thread.is_alive():
        try:
            server.force_exit = True
        except Exception:
            pass
        hard_deadline = time.time() + max(1.0, float(timeout_s) * 0.4)
        while time.time() < hard_deadline and thread.is_alive():
            time.sleep(0.1)

    stopped = not thread.is_alive()
    with _lock:
        if stopped:
            _server = None
            _thread = None
            _port = None
    return "stopped" if stopped else "timeout"


def is_running() -> bool:
    thread = _thread
    return bool(thread is not None and thread.is_alive() and getattr(_server, "started", False))


def data_dir() -> str:
    return os.environ.get("ARCANEA_DATA_DIR", "")


def python_version() -> str:
    import sys

    return sys.version.split()[0]
