from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import queue
import threading
import time
from pathlib import Path
import os
from collections import deque
from typing import Any, Deque, Dict, Iterable, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore

_task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("telemetry_task_id", default=None)
_task_kind_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("telemetry_task_kind", default=None)

_sub_lock = threading.Lock()
_subscribers: set[queue.Queue] = set()

_tasks_lock = threading.Lock()
_tasks: Dict[str, Dict[str, Any]] = {}

_recent_logs: Deque[Dict[str, Any]] = deque(maxlen=200)

_MAX_TASKS = 200
_DEFAULT_QUEUE_MAX = 200

_handler_installed = False

_telemetry_log_path: Path | None = None
def _get_telemetry_log_path() -> Path | None:
    global _telemetry_log_path
    if _telemetry_log_path is not None:
        return _telemetry_log_path
    try:
        base = Path(__file__).resolve().parents[2]
    except Exception:
        base = Path(".")
    try:
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _telemetry_log_path = log_dir / "telemetry.log"
        return _telemetry_log_path
    except Exception:
        return None


def _now_ts() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


def _memory_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        proc = psutil.Process()
        rss = proc.memory_info().rss
        return round(float(rss) / (1024.0 * 1024.0), 2)
    except Exception:
        return None


def get_task_context() -> tuple[Optional[str], Optional[str]]:
    try:
        return _task_id_var.get(), _task_kind_var.get()
    except Exception:
        return None, None


def set_task_context(task_id: Optional[str], task_kind: Optional[str] = None):
    token_id = _task_id_var.set(task_id)
    token_kind = _task_kind_var.set(task_kind)
    return token_id, token_kind


def reset_task_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    try:
        _task_id_var.reset(tokens[0])
    except Exception:
        pass
    try:
        _task_kind_var.reset(tokens[1])
    except Exception:
        pass


@contextlib.contextmanager
def task_context(task_id: Optional[str], task_kind: Optional[str] = None):
    token_id = _task_id_var.set(task_id)
    token_kind = _task_kind_var.set(task_kind)
    try:
        yield
    finally:
        try:
            _task_id_var.reset(token_id)
        except Exception:
            pass
        try:
            _task_kind_var.reset(token_kind)
        except Exception:
            pass


def _touch_task(task_id: str, task_kind: Optional[str] = None) -> Dict[str, Any]:
    now = _now_ts()
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t:
            t = {
                "task_id": task_id,
                "task_kind": task_kind,
                "status": "queued",
                "phases": {},
                "started_at": None,
                "finished_at": None,
                "last_updated": now,
            }
            _tasks[task_id] = t
        if task_kind and not t.get("task_kind"):
            t["task_kind"] = task_kind
        t["last_updated"] = now
        _trim_tasks_locked()
        return t


def _trim_tasks_locked() -> None:
    if len(_tasks) <= _MAX_TASKS:
        return
    # Drop oldest finished tasks first
    items = sorted(_tasks.items(), key=lambda kv: kv[1].get("last_updated") or 0)
    for task_id, task in items:
        if len(_tasks) <= _MAX_TASKS:
            break
        if task.get("status") in ("done", "error", "no_match"):
            _tasks.pop(task_id, None)
    # If still too many, drop oldest regardless
    if len(_tasks) > _MAX_TASKS:
        items = sorted(_tasks.items(), key=lambda kv: kv[1].get("last_updated") or 0)
        for task_id, _task in items:
            if len(_tasks) <= _MAX_TASKS:
                break
            _tasks.pop(task_id, None)


def subscribe(maxsize: int = _DEFAULT_QUEUE_MAX) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=maxsize)
    with _sub_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        _subscribers.discard(q)


def _publish(event: Dict[str, Any]) -> None:
    if not isinstance(event, dict):
        return
    with _sub_lock:
        dead: list[queue.Queue] = []
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass
            except Exception:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


def _emit(event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = _now_ts()
    task_id, task_kind = get_task_context()
    ev: Dict[str, Any] = {"type": event_type, "ts": now}
    if payload:
        ev.update(payload)
    if ev.get("task_id") is None and task_id:
        ev["task_id"] = task_id
    if ev.get("task_kind") is None and task_kind:
        ev["task_kind"] = task_kind
    try:
        log_path = _get_telemetry_log_path()
        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _publish(ev)
    return ev


def task_queued(task_id: str, task_kind: Optional[str] = None, **extra: Any) -> None:
    if not task_id:
        return
    _touch_task(task_id, task_kind)
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t:
            t["status"] = "queued"
            t["queued_at"] = _now_ts()
            t["last_updated"] = _now_ts()
    payload = {"task_id": task_id, "task_kind": task_kind, "status": "queued"}
    payload.update(extra)
    _emit("task.queued", payload)


def task_start(task_id: str, task_kind: Optional[str] = None, **extra: Any) -> None:
    if not task_id:
        return
    now = _now_ts()
    _touch_task(task_id, task_kind)
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t:
            t["status"] = "running"
            t["started_at"] = t.get("started_at") or now
            t["last_updated"] = now
    payload = {"task_id": task_id, "task_kind": task_kind, "status": "running"}
    payload.update(extra)
    mem = _memory_mb()
    if mem is not None:
        payload["memory_mb"] = mem
    _emit("task.start", payload)


def task_end(task_id: str, status: str = "ok", **extra: Any) -> None:
    if not task_id:
        return
    now = _now_ts()
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t:
            t["status"] = "done" if status == "ok" else status
            t["finished_at"] = now
            t["last_updated"] = now
    payload = {"task_id": task_id, "status": status}
    payload.update(extra)
    mem = _memory_mb()
    if mem is not None:
        payload["memory_mb"] = mem
    _emit("task.end", payload)


def task_phase(phase: str, status: str, task_id: Optional[str] = None, task_kind: Optional[str] = None, duration_ms: Optional[float] = None) -> None:
    if not phase:
        return
    if not task_id:
        task_id, task_kind = get_task_context()
    if not task_id:
        return
    now = _now_ts()
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t:
            t = {
                "task_id": task_id,
                "task_kind": task_kind,
                "status": "running",
                "phases": {},
                "started_at": None,
                "finished_at": None,
                "last_updated": now,
            }
            _tasks[task_id] = t
        phases = t.setdefault("phases", {})
        ph = phases.get(phase) or {}
        if status == "enter":
            ph["status"] = "running"
            ph["started_at"] = now
            ph["finished_at"] = None
        elif status in ("exit", "done"):
            ph["status"] = "done"
            ph["finished_at"] = now
            if duration_ms is not None:
                ph["duration_ms"] = float(duration_ms)
                prev = float(ph.get("total_ms") or 0.0)
                ph["total_ms"] = prev + float(duration_ms)
        elif status == "skip":
            ph["status"] = "skipped"
            ph["finished_at"] = now
        else:
            ph["status"] = status
        phases[phase] = ph
        t["phases"] = phases
        t["last_updated"] = now
        _trim_tasks_locked()
    payload = {
        "task_id": task_id,
        "task_kind": task_kind,
        "phase": phase,
        "status": status,
    }
    if duration_ms is not None:
        payload["duration_ms"] = float(duration_ms)
    mem = _memory_mb()
    if mem is not None:
        payload["memory_mb"] = mem
    _emit("task.phase", payload)


@contextlib.contextmanager
def phase(phase_name: str, task_id: Optional[str] = None, task_kind: Optional[str] = None):
    if not task_id:
        task_id, task_kind = get_task_context()
    if not task_id:
        yield
        return
    t0 = time.perf_counter()
    task_phase(phase_name, "enter", task_id=task_id, task_kind=task_kind)
    try:
        yield
    finally:
        dt = (time.perf_counter() - t0) * 1000.0
        task_phase(phase_name, "exit", task_id=task_id, task_kind=task_kind, duration_ms=dt)


def emit_api_timing(service: str, endpoint: str, duration_ms: float, status_code: Optional[int] = None, error: Optional[str] = None) -> None:
    payload = {
        "service": service,
        "endpoint": endpoint,
        "duration_ms": float(duration_ms),
    }
    if status_code is not None:
        payload["status_code"] = int(status_code)
    if error:
        payload["error"] = str(error)
    _emit("api.timing", payload)


def emit_log(level: str, message: str, logger_name: Optional[str] = None) -> None:
    payload = {
        "level": level,
        "message": message,
        "logger": logger_name,
    }
    ev = _emit("log", payload)
    try:
        _recent_logs.append(ev)
    except Exception:
        pass


def snapshot() -> Dict[str, Any]:
    with _tasks_lock:
        tasks = list(_tasks.values())
    tasks_sorted = sorted(tasks, key=lambda t: t.get("last_updated") or 0, reverse=True)
    return {"tasks": tasks_sorted, "ts": _now_ts(), "logs": list(_recent_logs)}


def format_sse(event_type: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n"


class _TelemetryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        try:
            emit_log(record.levelname, msg, record.name)
        except Exception:
            return


def install_logging_handler() -> None:
    global _handler_installed
    if _handler_installed:
        return
    root = logging.getLogger()
    handler = _TelemetryLogHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    _handler_installed = True
