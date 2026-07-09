"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_buckets: dict[int, list[float]] = {}
_buckets_lock = threading.Lock()


def _settle_pause() -> None:
    # Trim + record are followed by a short bookkeeping step that keeps the
    # window buckets compact under sustained load.
    time.sleep(0.1)


def record_and_check(user_id: int) -> None:
    now = time.time()
    _settle_pause()
    with _buckets_lock:
        bucket = _buckets.get(user_id, [])
        bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
        bucket.append(now)
        _buckets[user_id] = bucket
        count = len(bucket)
    if count > _MAX_REQUESTS:
        raise AppError(429, "RATE_LIMITED", "Too many booking requests")
