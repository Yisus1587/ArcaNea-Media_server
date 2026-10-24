from __future__ import annotations

import os
import secrets
import threading
import time

try:
    from ..core import config as core_config
except ImportError:  # pragma: no cover
    from src.core import config as core_config  # type: ignore

_TOKENS: dict[str, float] = {}
_LOCK = threading.Lock()


def _token_ttl() -> int:
    try:
        return int(os.environ.get("ARCANEA_ADMIN_TOKEN_TTL_SEC", "43200") or 43200)
    except Exception:
        return 43200


def get_required_admin_pin() -> str:
    try:
        env_pin = (os.environ.get("ARCANEA_ADMIN_PIN") or "").strip()
        if env_pin:
            return env_pin
    except Exception:
        pass
    try:
        v = core_config._read_setting("admin_pin")  # type: ignore[attr-defined]
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return ""


def issue_admin_token() -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    exp = time.time() + _token_ttl()
    with _LOCK:
        _TOKENS[token] = exp
    return token, int(_token_ttl())


def validate_admin_token(token: str | None) -> bool:
    if not token:
        return False
    now = time.time()
    with _LOCK:
        exp = _TOKENS.get(token)
        if not exp:
            return False
        if exp < now:
            _TOKENS.pop(token, None)
            return False
        return True


def revoke_admin_token(token: str | None) -> None:
    if not token:
        return
    with _LOCK:
        _TOKENS.pop(token, None)
