from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
import logging
import urllib.request


def _is_frozen() -> bool:
    try:
        return bool(getattr(sys, "frozen", False))
    except Exception:
        return False


def _bundle_root() -> Path | None:
    if not _is_frozen():
        return None
    try:
        mp = getattr(sys, "_MEIPASS", None)
    except Exception:
        mp = None
    try:
        return Path(str(mp)).resolve() if mp else None
    except Exception:
        return None


def _stream(prefix: str, pipe, out) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            out.write(f"[{prefix}] {line}")
            out.flush()
    except Exception:
        pass


def _terminate(p: subprocess.Popen, timeout_s: float = 6.0) -> None:
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


def _repo_root() -> Path:
    br = _bundle_root()
    if br is not None:
        return br
    here = Path(__file__).resolve()
    for cand in [here.parent, *here.parents]:
        try:
            if (cand / "src").is_dir() and (cand / "arcanea-media-server").is_dir():
                return cand
        except Exception:
            continue
    return here.parent


def _writable_root(repo_root: Path) -> Path:
    """Where we can write runtime files (data, certs) in all modes."""
    if _is_frozen():
        # Prefer a persistent per-user location so users don't accidentally delete db/cache.
        try:
            base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
            if base:
                p = (Path(base) / "ArcaNea").resolve()
                p.mkdir(parents=True, exist_ok=True)
                return p
        except Exception:
            pass
        # Fallback: next to the exe (may be read-only if installed under Program Files).
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            return Path.cwd().resolve()
    return repo_root


def _bootstrap_persistent_data(persist_root: Path, repo_root: Path) -> None:
    """Best-effort copy of initial config into %APPDATA%/ArcaNea on first run."""
    try:
        data_dir = (persist_root / "data").resolve()
        db_dir = (persist_root / "db").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_dir.mkdir(parents=True, exist_ok=True)

        # Copy app_config.json if missing (prefer sibling ./data next to exe; then repo data/)
        dst_cfg = data_dir / "app_config.json"
        if not dst_cfg.exists():
            candidates: list[Path] = []
            try:
                exe_dir = Path(sys.executable).resolve().parent
                # Preferred: shipped seed config (installer drops this next to the exe)
                candidates.append(exe_dir / "seed" / "app_config.json")
                # Back-compat: older builds copied seed into ./data
                candidates.append(exe_dir / "data" / "app_config.json")
            except Exception:
                pass
            candidates.append(repo_root / "data" / "app_config.json")
            for src in candidates:
                try:
                    if src.is_file():
                        dst_cfg.write_bytes(src.read_bytes())
                        break
                except Exception:
                    continue

        # Migrate existing db if present next to the exe and not yet in AppData
        dst_db = db_dir / "arcanea.db"
        if not dst_db.exists():
            try:
                exe_dir = Path(sys.executable).resolve().parent
                old_db = exe_dir / "data" / "arcanea.db"
                if old_db.is_file():
                    dst_db.write_bytes(old_db.read_bytes())
            except Exception:
                pass
    except Exception:
        # best-effort
        return


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


def _collect_hosts(extra_hosts: list[str] | None = None) -> list[str]:
    hosts: list[str] = ["localhost", "127.0.0.1"]
    try:
        hn = socket.gethostname()
        if hn and hn not in hosts:
            hosts.append(hn)
    except Exception:
        pass

    # Best-effort LAN IP discovery
    ips: set[str] = set()
    try:
        # Method 1: hostname resolution
        try:
            _hn, _aliases, addrs = socket.gethostbyname_ex(socket.gethostname())
            for a in addrs or []:
                ips.add(a)
        except Exception:
            pass

        # Method 2: default route trick (no packets sent)
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

    for h in (extra_hosts or []):
        hh = (h or "").strip()
        if hh and hh not in hosts:
            hosts.append(hh)

    return hosts


def _ensure_mkcert_certs(repo_root: Path, hosts: list[str]) -> tuple[Path, Path] | None:
    # In frozen/onefile mode, repo_root is inside _MEIPASS (read-only). Write certs next to the exe.
    cert_dir = (_writable_root(repo_root) / "certs") if _is_frozen() else (repo_root / "arcanea-media-server" / "certs")
    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_path = cert_dir / "arcanea-cert.pem"
    key_path = cert_dir / "arcanea-key.pem"

    mkcert_exe = "mkcert.exe" if os.name == "nt" else "mkcert"

    def _copy_root_ca() -> None:
        try:
            dest_dir = (_writable_root(repo_root) / "certs").resolve()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / "rootCA.pem"
            if dst.exists():
                return
            res = subprocess.run([mkcert_exe, "-CAROOT"], check=True, capture_output=True, text=True)
            caroot = (res.stdout or res.stderr or "").strip()
            if not caroot:
                return
            root_pem = Path(caroot) / "rootCA.pem"
            if root_pem.is_file():
                dst.write_bytes(root_pem.read_bytes())
        except Exception:
            pass

    if cert_path.exists() and key_path.exists():
        _copy_root_ca()
        return cert_path, key_path

    try:
        subprocess.run([mkcert_exe, "-version"], check=True, capture_output=True, text=True)
    except Exception:
        print("[run] mkcert no está instalado/en PATH. Instálalo y vuelve a correr `python run.py --https`.")
        return None

    # Install local CA (may require admin depending on environment)
    try:
        subprocess.run([mkcert_exe, "-install"], check=True)
    except Exception as e:
        print(f"[run] mkcert -install falló (puede requerir permisos). Continuo igual. Detalle: {e}")

    cmd = [
        mkcert_exe,
        "-cert-file",
        str(cert_path),
        "-key-file",
        str(key_path),
        *hosts,
    ]
    print("[run] Generando certs HTTPS con mkcert...")
    print("[run] ", " ".join(cmd))
    subprocess.run(cmd, check=True)
    _copy_root_ca()
    return cert_path, key_path


def main() -> int:
    # If user double-clicks the EXE, default to the desired behavior.
    try:
        if _is_frozen() and len(sys.argv) <= 1:
            sys.argv.extend(["--release", "--open"])
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="ArcaNea: prender y listo (backend+frontend+PWA HTTPS opcional)")
    ap.add_argument("--release", action="store_true", help="Modo release: NO usa npm. FastAPI sirve el frontend compilado (dist).")
    ap.add_argument("--build-frontend", action="store_true", help="(Opcional) Ejecuta `npm run build` antes de levantar (requiere npm).")
    ap.add_argument("--https", action="store_true", help="Genera/usa certificados mkcert y levanta con HTTPS")
    ap.add_argument("--open", action="store_true", help="Abre el navegador automáticamente")
    ap.add_argument("--tray", action="store_true", help="Habilita system tray (Windows). En modo EXE se activa por defecto.")
    ap.add_argument("--no-tray", action="store_true", help="Deshabilita system tray (solo aplica en modo EXE).")
    ap.add_argument("--host", default="0.0.0.0", help="Host para Vite (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9800, help="Puerto para Vite (default: 9800)")
    ap.add_argument("--api-port", type=int, default=None, help="Puerto para FastAPI (default: 9800 release / 9801 dev)")
    ap.add_argument("--lan-url", default="", help="URL a abrir (ej: https://192.168.1.70:9800). Si vacío, usa localhost.")
    ap.add_argument("--extra-host", action="append", default=[], help="Hosts extra para el certificado (puede repetirse)")
    args = ap.parse_args()
    if args.api_port is None:
        args.api_port = 9800 if args.release else 9801
    if not args.release and int(args.port) == int(args.api_port):
        args.api_port = int(args.api_port) + 1

    repo_root = _repo_root()
    frontend_dir = repo_root / "arcanea-media-server"
    dist_dir = (frontend_dir / "dist").resolve()
    writable_root = _writable_root(repo_root)
    stop_event = threading.Event()

    env = os.environ.copy()
    # Default: no startup scan. First-time scan is triggered by Setup Wizard or manual action.
    # Set ARCANEA_STARTUP_SCAN=once|always to override.
    env.setdefault("ARCANEA_STARTUP_SCAN", "0")
    env.setdefault("ARCANEA_STARTUP_ENRICH", "1")
    env.setdefault("ARCANEA_WATCH", "1")
    env.setdefault("ARCANEA_DISABLE_PROXY", "1")
    try:
        env.setdefault("ARCANEA_API_PORT", str(int(args.api_port)))
    except Exception:
        env.setdefault("ARCANEA_API_PORT", "9800")
    if not args.release:
        env.setdefault("ARCANEA_DEV_BACKEND", f"http://127.0.0.1:{int(args.api_port)}")

    # File logging (helps when running as EXE and console is hidden/closed)
    log_path: Path | None = None
    try:
        log_dir = (writable_root / "logs").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "arcanea.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(str(log_path), encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        )
        logging.getLogger(__name__).info("ArcaNea start (frozen=%s release=%s)", _is_frozen(), bool(args.release))
    except Exception:
        pass

    # Persistent per-user data (db/cache/config) under %APPDATA%/ArcaNea in frozen mode.
    try:
        persist_root = writable_root
        _bootstrap_persistent_data(persist_root, repo_root)
        data_dir = (persist_root / "data").resolve()
        db_dir = (persist_root / "db").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_dir.mkdir(parents=True, exist_ok=True)

        # Bundle root stays as base dir for read-only packaged resources (dist, tmdb_gen.json, etc.)
        env.setdefault("ARCANEA_BASE_DIR", str(repo_root))
        env.setdefault("ARCANEA_DATA_DIR", str(data_dir))
        env.setdefault("ARCANEA_CACHE_DIR", str((data_dir / "cache").resolve()))
        env.setdefault("ARCANEA_DB_URL", f"sqlite:///{(db_dir / 'arcanea.db').resolve()}")
    except Exception:
        pass

    # Prefer venv python if present so uvicorn/fastapi deps are found.
    venv_py = repo_root / ".venv" / "Scripts" / "python.exe"
    # NOTE: In frozen mode, sys.executable is the EXE itself, not python.
    python_exe = str(venv_py) if venv_py.exists() else sys.executable

    cert_pair: tuple[Path, Path] | None = None
    if args.https:
        hosts = _collect_hosts(args.extra_host)
        try:
            cert_pair = _ensure_mkcert_certs(repo_root, hosts)
        except Exception as e:
            print(f"[run] No se pudieron generar certs con mkcert: {e}")
            print("[run] Continuo sin HTTPS (puedes reintentar luego).")

    if args.build_frontend:
        npm_exe = "npm.cmd" if os.name == "nt" else "npm"
        print("[run] Building frontend:", f"{npm_exe} run build")
        subprocess.run([npm_exe, "run", "build"], check=True, cwd=str(frontend_dir), env=env)

    # In release mode, FastAPI serves the built frontend so we don't depend on npm at runtime.
    if args.release:
        env.setdefault("ARCANEA_SERVE_FRONTEND", "1")
        env.setdefault("ARCANEA_FRONTEND_DIST", str(dist_dir))
        if not dist_dir.is_dir():
            msg = (
                f"dist no existe:\n{dist_dir}\n\n"
                "Corre primero:\n"
                "  cd arcanea-media-server && npm run build\n\n"
                "o usa:\n"
                "  python run.py --release --build-frontend\n"
            )
            print(f"[run] ERROR: {msg}")
            # In onefile mode, the console may close immediately; show a MessageBox to avoid confusion.
            try:
                if _is_frozen() and os.name == "nt":
                    import ctypes  # type: ignore
                    ctypes.windll.user32.MessageBoxW(0, msg, "ArcaNea - Falta dist", 0x10)
            except Exception:
                pass
            return 2

    # Determine TLS certs (only when HTTPS is explicitly requested)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    if args.https:
        if args.release:
            try:
                cdir = (writable_root / "certs").resolve()
                c = cdir / "arcanea-cert.pem"
                k = cdir / "arcanea-key.pem"
                if c.is_file() and k.is_file():
                    tls_cert, tls_key = c, k
            except Exception:
                tls_cert, tls_key = None, None
        if cert_pair:
            tls_cert, tls_key = cert_pair

    def _wait_port(host: str, port: int, timeout_s: float = 12.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except Exception:
                time.sleep(0.25)
        return False

    backend: subprocess.Popen | None = None
    backend_thread: threading.Thread | None = None
    backend_failed: list[str] = []
    server_ref: dict[str, object | None] = {'server': None}

    if _is_frozen():
        # In EXE mode, run uvicorn in-process (spawning `sys.executable -m uvicorn` won't work).
        os.environ.update(env)

        def _run_backend() -> None:
            try:
                import uvicorn  # type: ignore
                from src.api.asgi import app as api_app  # noqa: F401

                cfg = uvicorn.Config(
                    api_app,
                    host="0.0.0.0",
                    port=int(args.api_port),
                    reload=False,
                    ssl_certfile=str(tls_cert) if tls_cert else None,
                    ssl_keyfile=str(tls_key) if tls_key else None,
                    log_level="info",
                    # In frozen/windowed builds, uvicorn's default dictConfig can fail if some
                    # logging classes aren't discoverable. We rely on our own logging.basicConfig.
                    log_config=None,
                )
                server = uvicorn.Server(cfg)
                server_ref['server'] = server
                server.run()
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                backend_failed.append(msg)
                logging.getLogger(__name__).exception("Backend in-process failed")
                try:
                    if os.name == "nt":
                        import ctypes  # type: ignore
                        ctypes.windll.user32.MessageBoxW(0, msg, "ArcaNea - Backend error", 0x10)
                except Exception:
                    pass

        logging.getLogger(__name__).info("Starting backend in-process on port %s", args.api_port)
        backend_thread = threading.Thread(target=_run_backend, daemon=True, name="backend")
        backend_thread.start()
    else:
        backend_cmd = [
            python_exe,
            "-m",
            "uvicorn",
            "src.api.asgi:app",
            *(["--reload"] if not args.release else []),
            "--host",
            "0.0.0.0",
            "--port",
            str(int(args.api_port)),
        ]
        if args.release and tls_cert and tls_key:
            backend_cmd += ["--ssl-certfile", str(tls_cert), "--ssl-keyfile", str(tls_key)]

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

    frontend: subprocess.Popen | None = None
    if not args.release:
        npm_exe = "npm.cmd" if os.name == "nt" else "npm"
        frontend_cmd = [
            npm_exe,
            "run",
            "dev",
            "--",
            "--host",
            str(args.host),
            "--port",
            str(int(args.port)),
        ]
        print("Starting frontend:", " ".join(frontend_cmd))
        frontend = subprocess.Popen(
            frontend_cmd,
            cwd=str(frontend_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    if backend is not None and backend.stdout is not None:
        threading.Thread(target=_stream, args=("backend", backend.stdout, sys.stdout), daemon=True).start()
    if frontend is not None and frontend.stdout is not None:
        threading.Thread(target=_stream, args=("frontend", frontend.stdout, sys.stdout), daemon=True).start()

    def _handle_sigint(sig, frame):
        raise KeyboardInterrupt()

    old_handler = signal.signal(signal.SIGINT, _handle_sigint)

    def _http_post(url: str, timeout_s: float = 2.5) -> bool:
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                status = getattr(resp, "status", 200)
                try:
                    status = int(status)
                except Exception:
                    status = 200
                return 200 <= status < 300
        except Exception:
            return False

    def _start_tray(base_url: str) -> None:
        """Run a pystray loop (best-effort)."""
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
        except Exception:
            logging.getLogger(__name__).warning("System tray no disponible (instala pystray + pillow).")
            return

        def _make_icon() -> "Image.Image":
            candidates: list[Path] = []
            try:
                candidates.append((repo_root / "assets" / "arcanea.ico").resolve())
            except Exception:
                pass
            try:
                candidates.append((Path(sys.executable).resolve().parent / "assets" / "arcanea.ico").resolve())
            except Exception:
                pass
            try:
                candidates.append((Path(sys.executable).resolve().parent / "arcanea.ico").resolve())
            except Exception:
                pass
            for cand in candidates:
                try:
                    if cand.is_file():
                        return Image.open(str(cand)).convert("RGBA")
                except Exception:
                    continue
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.polygon([(32, 6), (58, 58), (48, 58), (41, 42), (23, 42), (16, 58), (6, 58)], outline=(99, 102, 241, 255), width=5)
            d.line([(23, 42), (41, 42)], fill=(99, 102, 241, 255), width=5)
            d.ellipse((8, 50, 18, 60), outline=(56, 189, 248, 255), width=4)
            d.ellipse((46, 50, 56, 60), outline=(168, 85, 247, 255), width=4)
            return img

        def _open(_icon, _item):
            try:
                webbrowser.open(base_url + "/")
            except Exception:
                pass

        def _view_logs(_icon, _item):
            try:
                target_log = log_path if log_path and log_path.exists() else ((writable_root / "logs" / "arcanea.log").resolve())
                if os.name == "nt":
                    ps_exe = "powershell.exe"
                    safe_path = str(target_log).replace("'", "''")
                    cmd = (
                        "$Host.UI.RawUI.WindowTitle = 'ArcaNea - Logs'; "
                        f"if (Test-Path '{safe_path}') {{ "
                        f"Get-Content -Path '{safe_path}' -Tail 200 -Wait "
                        "} else { "
                        f"Write-Host 'Log no encontrado:' '{safe_path}'; "
                        "Write-Host 'Esperando a que se cree el archivo...'; "
                        "while ($true) { Start-Sleep -Seconds 2; if (Test-Path '" + safe_path + "') { "
                        "Get-Content -Path '" + safe_path + "' -Tail 200 -Wait; break } } }"
                    )
                    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                    subprocess.Popen(
                        [ps_exe, "-NoExit", "-Command", cmd],
                        cwd=str(writable_root),
                        creationflags=creationflags,
                    )
                else:
                    os.startfile(str(target_log))  # type: ignore[attr-defined]
            except Exception:
                pass

        def _exit(_icon, _item):
            try:
                stop_event.set()
            except Exception:
                pass
            try:
                _icon.stop()
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Abrir", _open, default=True),
            pystray.MenuItem("Ver registros", _view_logs),
            pystray.MenuItem("Salir", _exit),
        )
        icon = pystray.Icon("ArcaNea", _make_icon(), "ArcaNea", menu)
        try:
            icon.run()
        except Exception:
            logging.getLogger(__name__).exception("Tray crashed")

    try:
        # Start tray by default in EXE mode (unless explicitly disabled)
        use_tray = bool(args.tray or (_is_frozen() and not args.no_tray))
        if use_tray and os.name == "nt":
            scheme = "https" if (tls_cert and tls_key) else "http"
            base_url = f"{scheme}://127.0.0.1:{int(args.api_port)}" if args.release else f"{scheme}://127.0.0.1:{int(args.port)}"
            threading.Thread(target=_start_tray, args=(base_url,), daemon=True, name="tray").start()

        if args.open:
            # If TLS is active (either requested or found existing certs), prefer https in auto-open.
            scheme = "https" if (tls_cert and tls_key) else ("http" if not args.https else "https")
            if args.release:
                url = args.lan_url.strip() or f"{scheme}://localhost:{int(args.api_port)}/"
            else:
                url = args.lan_url.strip() or f"{scheme}://localhost:{int(args.port)}/"
            # give dev servers a moment
            if args.release:
                _wait_port("127.0.0.1", int(args.api_port), timeout_s=12.0)
            else:
                time.sleep(2.0)
            try:
                webbrowser.open(url)
                print(f"[run] Abriendo: {url}")
            except Exception:
                pass

        while True:
            if stop_event.is_set():
                try:
                    srv = server_ref.get('server')
                    if srv is not None and hasattr(srv, 'should_exit'):
                        setattr(srv, 'should_exit', True)
                except Exception:
                    pass
                print("Stopping (tray)...")
                return 0
            b = backend.poll() if backend is not None else None
            f = frontend.poll() if frontend is not None else None
            if backend is not None and b is not None:
                print("Process exited:", {"backend": b, "frontend": f})
                return int(b or f or 0)
            if backend is None and backend_thread is not None and not backend_thread.is_alive():
                print("Backend thread exited:", backend_failed[0] if backend_failed else "unknown")
                return 1
            if frontend is not None and f is not None:
                print("Frontend process exited:", f)
                return int(f or 0)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        try:
            signal.signal(signal.SIGINT, old_handler)
        except Exception:
            pass
        if frontend is not None:
            _terminate(frontend)
        if backend is not None:
            _terminate(backend)


if __name__ == "__main__":
    raise SystemExit(main())
