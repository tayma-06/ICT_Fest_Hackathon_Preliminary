"""In-memory response caches for read-heavy reporting endpoints.

Usage reports and per-room availability are relatively expensive to compute and
are read far more often than the underlying data changes, so results are cached
and invalidated when the data they depend on is modified.
"""
import threading

_report_cache: dict[tuple, dict] = {}
_availability_cache: dict[tuple, dict] = {}
_report_generations: dict[int, int] = {}
_availability_generations: dict[tuple, int] = {}
_cache_lock = threading.Lock()


def get_report(org_id: int, frm: str, to: str):
    with _cache_lock:
        return _report_cache.get((org_id, frm, to))


def report_generation(org_id: int) -> int:
    with _cache_lock:
        return _report_generations.get(org_id, 0)


def set_report(org_id: int, frm: str, to: str, value: dict, generation: int) -> None:
    with _cache_lock:
        if _report_generations.get(org_id, 0) == generation:
            _report_cache[(org_id, frm, to)] = value


def invalidate_report(org_id: int) -> None:
    with _cache_lock:
        _report_generations[org_id] = _report_generations.get(org_id, 0) + 1
        for key in [k for k in _report_cache if k[0] == org_id]:
            _report_cache.pop(key, None)


def get_availability(room_id: int, date: str):
    with _cache_lock:
        return _availability_cache.get((room_id, date))


def availability_generation(room_id: int, date: str) -> int:
    with _cache_lock:
        return _availability_generations.get((room_id, date), 0)


def set_availability(room_id: int, date: str, value: dict, generation: int) -> None:
    key = (room_id, date)
    with _cache_lock:
        if _availability_generations.get(key, 0) == generation:
            _availability_cache[key] = value


def invalidate_availability(room_id: int, date: str) -> None:
    key = (room_id, date)
    with _cache_lock:
        _availability_generations[key] = _availability_generations.get(key, 0) + 1
        _availability_cache.pop(key, None)
