from __future__ import annotations

import hashlib
import os
import re

from ..core.config import IS_ANDROID


# Android exposes the same shared storage under several names; /sdcard and
# /storage/self/primary are symlinks to /storage/emulated/0, and Termux reaches it
# through ~/storage/shared. Hashing them separately would create duplicate media
# entries for the very same folder.
_ANDROID_SHARED_ALIASES = ("/sdcard", "/storage/self/primary")
_ANDROID_SHARED_CANONICAL = "/storage/emulated/0"
_TERMUX_SHARED = "/data/data/com.termux/files/home/storage/shared"


def _canonicalize_android_path(s: str) -> str:
    for alias in _ANDROID_SHARED_ALIASES:
        if s == alias:
            return _ANDROID_SHARED_CANONICAL
        if s.startswith(alias + "/"):
            return _ANDROID_SHARED_CANONICAL + s[len(alias):]
    if s == _TERMUX_SHARED:
        return _ANDROID_SHARED_CANONICAL
    if s.startswith(_TERMUX_SHARED + "/"):
        return _ANDROID_SHARED_CANONICAL + s[len(_TERMUX_SHARED):]
    return s


def normalize_path_for_hash(path: str | None) -> str:
    if not path:
        return ""
    try:
        s = str(path).strip()
    except Exception:
        return ""
    if not s:
        return ""

    try:
        s = os.path.normpath(s)
    except Exception:
        pass

    s = s.replace("\\", "/")

    prefix = ""
    if s.startswith("//"):
        prefix = "//"
        s = s[2:]
    s = re.sub(r"/+", "/", s)
    s = prefix + s

    try:
        if IS_ANDROID:
            s = _canonicalize_android_path(s)
    except Exception:
        pass

    try:
        if os.name == "nt" or re.match(r"^[A-Za-z]:/", s):
            s = s.lower()
    except Exception:
        pass

    return s


def hash_path(path: str | None) -> str:
    norm = normalize_path_for_hash(path)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()
