from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class _RateState:
    fail_streak: int = 0
    cooldown_until: float = 0.0
    total_requests: int = 0
    total_errors: int = 0
    avg_latency_ms: float = 0.0


class RateLimiter:
    """Simple adaptive rate limiter with exponential backoff + jitter."""

    def __init__(
        self,
        provider: str,
        *,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        jitter_ratio: float = 0.2,
    ) -> None:
        self.provider = str(provider or "provider")
        self.base_backoff = float(base_backoff)
        self.max_backoff = float(max_backoff)
        self.jitter_ratio = float(jitter_ratio)
        self._lock = threading.Lock()
        self._state = _RateState()

    def before_request(self) -> None:
        """Sleep if we're in cooldown."""
        delay = 0.0
        with self._lock:
            now = time.time()
            if self._state.cooldown_until > now:
                delay = max(0.0, self._state.cooldown_until - now)
        if delay > 0:
            time.sleep(delay)

    def record_success(self, latency_ms: float | None = None) -> None:
        with self._lock:
            self._state.fail_streak = 0
            self._state.total_requests += 1
            if latency_ms is not None:
                # Exponential moving average
                if self._state.avg_latency_ms <= 0:
                    self._state.avg_latency_ms = float(latency_ms)
                else:
                    self._state.avg_latency_ms = (self._state.avg_latency_ms * 0.8) + (float(latency_ms) * 0.2)

    def record_failure(self, status_code: int | None = None, exc: Exception | None = None) -> None:
        with self._lock:
            self._state.total_requests += 1
            self._state.total_errors += 1
            self._state.fail_streak += 1
            backoff = min(self.max_backoff, self.base_backoff * (2 ** max(0, self._state.fail_streak - 1)))
            jitter = random.uniform(0, backoff * self.jitter_ratio)
            self._state.cooldown_until = max(self._state.cooldown_until, time.time() + backoff + jitter)
        try:
            if status_code:
                logger.debug("rate_limiter[%s]: failure status=%s, backoff=%.2fs", self.provider, status_code, backoff)
            elif exc:
                logger.debug("rate_limiter[%s]: failure exc=%s, backoff=%.2fs", self.provider, exc, backoff)
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "provider": self.provider,
                "fail_streak": self._state.fail_streak,
                "cooldown_until": self._state.cooldown_until,
                "total_requests": self._state.total_requests,
                "total_errors": self._state.total_errors,
                "avg_latency_ms": self._state.avg_latency_ms,
            }


_limiters: dict[str, RateLimiter] = {}
_global_lock = threading.Lock()


def get_limiter(provider: str) -> RateLimiter:
    key = str(provider or "provider").lower()
    with _global_lock:
        lim = _limiters.get(key)
        if lim is None:
            # Default backoff tuned per provider family
            if key == "jikan":
                lim = RateLimiter(key, base_backoff=2.0, max_backoff=90.0)
            else:
                lim = RateLimiter(key, base_backoff=1.0, max_backoff=60.0)
            _limiters[key] = lim
        return lim
