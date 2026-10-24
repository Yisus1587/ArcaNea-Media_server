import threading
import queue
import time
import os
import logging
from pathlib import Path
from typing import Any, Iterable, Set, Dict, Tuple
from ..core.config import get
from ..services.local_identity import is_identity_sidecar
from ..utils.media_exts import get_media_extensions

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

logger = logging.getLogger(__name__)

class WatchHandler(FileSystemEventHandler):
    def __init__(self, service: 'WatchService'):
        self.service = service

    def on_modified(self, event):
        if event.is_directory:
            self.service.handle_dir_event(event.src_path) # pyright: ignore[reportArgumentType]
        else:
            self.service.handle_fs_event(event.src_path) # pyright: ignore[reportArgumentType]

    def on_created(self, event):
        if event.is_directory:
            self.service.handle_dir_event(event.src_path) # pyright: ignore[reportArgumentType]
        else:
            self.service.handle_fs_event(event.src_path) # pyright: ignore[reportArgumentType]

    def on_moved(self, event):
        self.service.handle_moved_event(
            event.src_path,  # pyright: ignore[reportArgumentType]
            event.dest_path,  # pyright: ignore[reportArgumentType]
            is_directory=bool(event.is_directory),
        )

    def on_deleted(self, event):
        self.service.handle_deleted_event(
            event.src_path,  # pyright: ignore[reportArgumentType]
            is_directory=bool(event.is_directory),
        )


class WatchService:
    """Watcher: vigila rutas y encola paths nuevos/modificados para escaneo.

    Combina eventos en tiempo real (watchdog) con un polling de respaldo.
    """

    def __init__(self, roots: Iterable[str] | None = None, out_queue: queue.Queue | None = None):
        self.roots = [str(Path(r).resolve()) for r in (roots or get('media_roots') or []) if r]
        self.out_queue = out_queue or queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        
        # Polling fallback interval (300s default for safety, but can be configured)
        self.poll_interval = int(get('watch_poll_interval') or 300)
        self._exts = tuple(get_media_extensions())
        
        # Internal state
        self._initialized_roots: Set[str] = set()
        self._known: Dict[str, Tuple[int | None, int | None]] = {}
        
        # Event debouncing
        self._pending_events: Dict[str, Tuple[float, dict[str, Any]]] = {}
        self._event_lock = threading.Lock()
        
        try:
            self.debounce_s = float(get('watch_debounce_seconds') or 5.0)
        except Exception:
            self.debounce_s = 5.0

        # Watchdog observer
        self._observer = None
        if HAS_WATCHDOG:
            try:
                self._observer = Observer()
            except Exception:
                logger.warning("Failed to initialize watchdog observer, falling back to polling.")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
            
        self._stop.clear()
        
        # Start watchdog if available
        if self._observer:
            handler = WatchHandler(self)
            for root in self.roots:
                try:
                    if os.path.exists(root):
                        self._observer.schedule(handler, root, recursive=True)
                        logger.info(f"Watchdog scheduled for: {root}")
                except Exception:
                    logger.exception(f"Failed to schedule watchdog for {root}")
            
            try:
                self._observer.start()
            except Exception:
                logger.exception("Failed to start watchdog observer")
                self._observer = None

        # Start polling/worker thread
        self._thread = threading.Thread(target=self._run, daemon=True, name='watcher')
        self._thread.start()
        # On startup, only build the baseline snapshot. Do not emit scan jobs for
        # the entire library just because the app started.
        try:
            self._do_poll(emit_initial=False)
        except Exception:
            pass
        logger.info(f"Watcher started (Watchdog: {'ENABLED' if self._observer else 'DISABLED'}, Poll: {self.poll_interval}s)")

    def stop(self):
        self._stop.set()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def _is_media_file(self, path: str) -> bool:
        try:
            p = str(path).lower()
            # Ignorar archivos ocultos o temporales
            name = os.path.basename(p)
            if name.startswith('.') or name.startswith('~$') or '@eadir' in p:
                return False
            return p.endswith(self._exts)
        except Exception:
            return False

    def _is_relevant_file(self, path: str) -> bool:
        return self._is_media_file(path) or is_identity_sidecar(path)

    def _enqueue_event(self, key: str, event: dict[str, Any]):
        try:
            with self._event_lock:
                self._pending_events[key] = (time.time(), event)
        except Exception:
            pass

    def _enqueue_folder(self, folder: str):
        self._enqueue_event(f"changed:{folder}", {"action": "changed", "path": folder})

    def _directory_has_media(self, path: str, max_depth: int = 2) -> bool:
        try:
            base = Path(path).resolve()
        except Exception:
            try:
                base = Path(path)
            except Exception:
                return False
        if not base.exists() or not base.is_dir():
            return False
        try:
            base_depth = len(base.parts)
            for dirpath, dirnames, filenames in os.walk(str(base)):
                current = Path(dirpath)
                if len(current.parts) - base_depth >= max_depth:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames if not d.startswith('.') and d.lower() != '@eadir']
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if self._is_media_file(full):
                        return True
        except Exception:
            return False
        return False

    def handle_fs_event(self, path: str):
        """Called by watchdog handler when a file changes."""
        if not self._is_relevant_file(path):
            return
        try:
            folder = str(Path(path).parent.resolve())
            self._enqueue_folder(folder)
        except Exception:
            pass

    def handle_dir_event(self, path: str):
        """Called by watchdog handler when a directory changes."""
        try:
            folder = str(Path(path).resolve())
            if not self._directory_has_media(folder):
                return
            self._enqueue_folder(folder)
        except Exception:
            pass

    def handle_deleted_event(self, path: str, *, is_directory: bool = False):
        """Called by watchdog handler when a media file/folder is deleted."""
        try:
            raw = str(path)
            if not is_directory and not self._is_relevant_file(raw):
                return
            try:
                normalized = str(Path(raw).resolve())
            except Exception:
                normalized = os.path.abspath(raw)
            self._enqueue_event(f"deleted:{normalized}", {"action": "deleted", "path": normalized})
        except Exception:
            pass

    def handle_moved_event(self, src_path: str, dest_path: str, *, is_directory: bool = False):
        """Called by watchdog handler when a media file/folder is moved or renamed."""
        try:
            src_raw = str(src_path)
            dest_raw = str(dest_path)
            if not is_directory and not (self._is_relevant_file(src_raw) or self._is_relevant_file(dest_raw)):
                return
            try:
                src = str(Path(src_raw).resolve())
            except Exception:
                src = os.path.abspath(src_raw)
            try:
                dest = str(Path(dest_raw).resolve())
            except Exception:
                dest = os.path.abspath(dest_raw)
            if is_directory and not self._directory_has_media(dest):
                self._enqueue_event(f"deleted:{src}", {"action": "deleted", "path": src})
                return
            self._enqueue_event(
                f"moved:{src}->{dest}",
                {"action": "moved", "src_path": src, "dest_path": dest, "is_directory": bool(is_directory)},
            )
        except Exception:
            pass

    def _run(self):
        """Background worker: handles polling fallback and event debouncing."""
        last_poll = 0.0
        
        while not self._stop.is_set():
            now = time.time()
            
            # 1. Handle Debounced Events from Watchdog
            with self._event_lock:
                to_emit = []
                for key, (last_ts, event) in list(self._pending_events.items()):
                    if now - last_ts >= self.debounce_s:
                        to_emit.append(event)
                        self._pending_events.pop(key)
                
                for event in to_emit:
                    logger.info("Watcher: Event detected %s", event)
                    self.out_queue.put(event)

            # 2. Polling Fallback (Sanity Check)
            if now - last_poll >= self.poll_interval:
                self._do_poll()
                last_poll = time.time()

            self._stop.wait(1.0) # Check events every second

    def _do_poll(self, *, emit_initial: bool = False):
        """Full tree walk to detect changes missed by watchdog or deletions.

        When emit_initial=True, enqueue folders even on first snapshot so
        newly-added items while the watcher was offline are picked up on startup.
        """
        for root in list(self.roots):
            if self._stop.is_set(): break
            try:
                p = Path(root)
                if not p.exists(): continue

                root_norm = str(p.resolve())
                seen_this_poll: set[str] = set()

                for dirpath, dirnames, filenames in os.walk(root):
                    if self._stop.is_set(): break
                    
                    # Filter out hidden directories
                    dirnames[:] = [d for d in dirnames if not d.startswith('.') and d.lower() != '@eadir']
                    
                    for name in filenames:
                        full = os.path.join(dirpath, name)
                        if not self._is_media_file(full):
                            continue
                            
                        try:
                            norm = str(Path(full).resolve())
                        except Exception:
                            norm = os.path.abspath(full)
                        seen_this_poll.add(norm)

                        try:
                            st = os.stat(norm)
                            mtime = int(st.st_mtime)
                            size = int(st.st_size)
                        except Exception:
                            continue

                        prev = self._known.get(norm)
                        if prev is not None and prev[0] == mtime and prev[1] == size:
                            continue

                        # Initial snapshot
                        if root_norm not in self._initialized_roots:
                            self._known[norm] = (mtime, size)
                            if emit_initial:
                                folder = str(Path(norm).parent.resolve())
                                self.out_queue.put({"action": "changed", "path": folder})
                            continue

                        # Change detected via polling
                        self._known[norm] = (mtime, size)
                        folder = str(Path(norm).parent.resolve())
                        self.out_queue.put({"action": "changed", "path": folder})

                self._initialized_roots.add(root_norm)

                # Detect deletions
                missing = [k for k in self._known.keys() if k.startswith(root_norm) and k not in seen_this_poll]
                if missing:
                    logger.info(f"Watcher: Detected {len(missing)} missing files in {root_norm}")
                    self.out_queue.put({"action": "missing_batch", "paths": list(missing)})
                    for k in missing:
                        self._known.pop(k, None)

            except Exception:
                logger.exception(f"Polling failed for root {root}")

    def add_root(self, path: str):
        path_norm = str(Path(path).resolve())
        if path_norm not in self.roots:
            self.roots.append(path_norm)
            if self._observer:
                try:
                    handler = WatchHandler(self)
                    self._observer.schedule(handler, path_norm, recursive=True)
                except Exception:
                    pass

    def set_roots(self, paths: Iterable[str]):
        new_roots = [str(Path(p).resolve()) for p in paths if p]
        if set(new_roots) == set(self.roots):
            return
            
        self.roots = new_roots
        if self._observer:
            try:
                self._observer.unschedule_all()
                handler = WatchHandler(self)
                for root in self.roots:
                    if os.path.exists(root):
                        self._observer.schedule(handler, root, recursive=True)
            except Exception:
                pass
        self._initialized_roots.clear()
        self._known.clear()
