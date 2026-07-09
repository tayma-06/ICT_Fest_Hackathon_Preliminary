"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so the stats
endpoint can serve them without re-aggregating the whole booking table.
"""
import threading
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Booking

_stats: dict[int, dict] = {}
_stats_lock = threading.Lock()


def _aggregate_pause() -> None:
    time.sleep(0.1)


def record_create(room_id: int, price_cents: int) -> None:
    _aggregate_pause()
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        count, revenue = current["count"], current["revenue"]
        _stats[room_id] = {"count": count + 1, "revenue": revenue + price_cents}


def record_cancel(room_id: int, price_cents: int) -> None:
    _aggregate_pause()
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        count, revenue = current["count"], current["revenue"]
        _stats[room_id] = {
            "count": max(0, count - 1),
            "revenue": max(0, revenue - price_cents),
        }


def get(room_id: int) -> dict:
    return _stats.get(room_id, {"count": 0, "revenue": 0})


def get_live(db: Session, room_id: int) -> dict:
    count, revenue = (
        db.query(func.count(Booking.id), func.coalesce(func.sum(Booking.price_cents), 0))
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .one()
    )
    return {"count": int(count), "revenue": int(revenue or 0)}
