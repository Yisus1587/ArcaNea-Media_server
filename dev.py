from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _stream(prefix: str, pipe, out) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            out.write(f"[{prefix}] {line}")
            out.flush()
    except Exception:
        # best-effort log streaming
        pass


def _terminate(p: subprocess.Popen, timeout_s: float = 5.0) -> None:
    try:
        if p.poll() is not None:
            return
        p.terminate()
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if p.poll() is not None:
                return
            time.sleep(0.1)
        p.kill()
    except Exception:
        pass


def _wait_port(host: str, port: int, timeout_s: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> int:
    # Find repo root regardless of where this script lives (root vs tools/).
    here = Path(__file__).resolve()
    repo_root = here.parent
    for cand in [here.parent, *here.parents]:
        try:
            if (cand / "src").is_dir() and (cand / "arcanea-media-server").is_dir():
                repo_root = cand
                break
        except Exception:
            continue
    frontend_dir = repo_root / "arcanea-media-server"

    # Prefer venv python if present so uvicorn/fastapi deps are found.
    venv_py = repo_root / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_py) if venv_py.exists() else sys.executable

    env = os.environ.copy()
    # Make imports robust regardless of how the user launches `dev.py` or what PYTHONPATH is set to.
    # Some Windows setups have PYTHONPATH pointing at `<repo>/src`, which changes imports to `api.*`.
    try:
        py_path = env.get("PYTHONPATH", "") or ""
    except Exception:
        py_path = ""
    parts = [p for p in py_path.split(os.pathsep) if p]
    repo_s = str(repo_root)
    src_s = str(repo_root / "src")
    if repo_s not in parts:
        parts.insert(0, repo_s)
    if src_s not in parts:
        parts.insert(1, src_s)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # Default: no startup scan. First-time scan is triggered by Setup Wizard or manual action.
    # Set ARCANEA_STARTUP_SCAN=once|always to override.
    env.setdefault("ARCANEA_STARTUP_SCAN", "0")
    env.setdefault("ARCANEA_STARTUP_ENRICH", "1")
    # Enable watcher by default in dev so new/modified media files are picked up automatically.
    env.setdefault("ARCANEA_WATCH", "1")
    # Some dev environments have broken HTTP(S)_PROXY settings; this bypass prevents provider calls from failing.
    env.setdefault("ARCANEA_DISABLE_PROXY", "1")
    # Force dev DB to live inside the repo to avoid accidentally writing to EXE/AppData locations.
    try:
        db_path = (repo_root / "data" / "arcanea.db").resolve()
        env["ARCANEA_DB_URL"] = f"sqlite:///{db_path.as_posix()}"
    except Exception:
        pass
    # Force dev to use Vite (do not serve bundled frontend)
    env.pop("ARCANEA_SERVE_FRONTEND", None)
    # Avoid auto-opening browsers from Vite
    env.setdefault("BROWSER", "none")

    backend_port = int(env.get("ARCANEA_BACKEND_PORT", "9800") or "9800")
    dev_host = env.get("ARCANEA_DEV_HOST", "0.0.0.0")
    dev_port = int(env.get("ARCANEA_DEV_PORT", "9800") or "9800")
    if dev_port == backend_port:
        # Avoid port conflict. If the user wants the UI on 9800, shift backend to 9801.
        backend_port = backend_port + 1

    backend_cmd = [
        python_exe,
        "-m",
        "uvicorn",
        "src.api.asgi:app",
        "--app-dir",
        str(repo_root),
        "--reload",
        "--host",
        "0.0.0.0",
        "--port",
        str(backend_port),
    ]
    npm_exe = "npm.cmd" if os.name == "nt" else "npm"
    env.setdefault("ARCANEA_DEV_BACKEND", f"http://127.0.0.1:{backend_port}")
    frontend_cmd = [npm_exe, "run", "dev", "--", "--host", dev_host, "--port", str(dev_port), "--strictPort"]

    print("Starting backend:", " ".join(backend_cmd))
    backend = subprocess.Popen(
        backend_cmd,
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Wait for backend so Vite proxy doesn't spam ECONNREFUSED on boot.
    try:
        if _wait_port("127.0.0.1", backend_port, timeout_s=12.0):
            print(f"Backend ready on http://127.0.0.1:{backend_port}")
        else:
            print("Backend not ready yet (continuing).")
    except Exception:
        pass

    def _start_frontend():
        print("Starting frontend:", " ".join(frontend_cmd))
        return subprocess.Popen(
            frontend_cmd,
            cwd=str(frontend_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _lan_ip() -> str | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return None

    frontend = _start_frontend()
    try:
        if dev_host in ("0.0.0.0", "::"):
            ip = _lan_ip()
            if ip:
                print(f"[dev] Mobile/LAN: http://{ip}:{dev_port}/")
    except Exception:
        pass

    threads: list[threading.Thread] = []
    if backend.stdout is not None:
        t = threading.Thread(target=_stream, args=("backend", backend.stdout, sys.stdout), daemon=True)
        t.start()
        threads.append(t)
    def _attach_frontend_stream(proc: subprocess.Popen):
        if proc.stdout is None:
            return
        t = threading.Thread(target=_stream, args=("frontend", proc.stdout, sys.stdout), daemon=True)
        t.start()
        threads.append(t)

    _attach_frontend_stream(frontend)

    def _handle_sigint(sig, frame):
        raise KeyboardInterrupt()

    old_handler = signal.signal(signal.SIGINT, _handle_sigint)

    restart_window_s = 30.0
    max_restarts = 5
    restart_times: list[float] = []

    try:
        while True:
            b = backend.poll()
            f = frontend.poll()
            if b is not None or f is not None:
                # If frontend crashed, try restarting a few times before giving up.
                if f is not None and b is None:
                    now = time.time()
                    restart_times = [t for t in restart_times if now - t < restart_window_s]
                    restart_times.append(now)
                    if len(restart_times) > max_restarts:
                        print("Frontend keeps crashing. Stopping dev.py.")
                        return int(f or 1)
                    print(f"Frontend exited ({f}). Restarting in 1s...")
                    time.sleep(1.0)
                    frontend = _start_frontend()
                    _attach_frontend_stream(frontend)
                    continue
                print("Process exited:", {"backend": b, "frontend": f})
                return int(b or f or 0)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        try:
            signal.signal(signal.SIGINT, old_handler)
        except Exception:
            pass
        _terminate(frontend)
        _terminate(backend)


if __name__ == "__main__":
    raise SystemExit(main())
