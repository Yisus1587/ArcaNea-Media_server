"""Shared scan state for reporting current scan progress to the API/frontend."""
import threading
from typing import Any, Dict

_lock = threading.Lock()
_state: Dict[str, Any] = {
    'status': 'idle',
    'phase': 'idle',
    'total': 0,
    'processed': 0,
    'titles_total': 0,
    'titles_processed': 0,
    'queued': 0,
    'current': None,
    'task_id': None,
    'task_kind': None,
    'started_at': None,
    'finished_at': None,
    'last_updated': None,
}


def set_state(updates: Dict[str, Any]):
    from time import time
    with _lock:
        _state.update(updates)
        _state['last_updated'] = int(time())


def get_state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def clear_state():
    with _lock:
        _state.update({'status': 'idle', 'phase': 'idle', 'current': None, 'task_id': None, 'task_kind': None, 'started_at': None, 'finished_at': None})
