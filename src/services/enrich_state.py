"""Shared enrichment state for reporting current enrichment progress."""
import threading
from typing import Any, Dict

_lock = threading.Lock()
_state: Dict[str, Any] = {
    'running': False,
    'current_id': None,
    'current_title': None,
    'current_step': None,
    'task_id': None,
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
        _state.update({'running': False, 'current_id': None, 'current_title': None, 'current_step': None, 'task_id': None})
