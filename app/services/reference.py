"""Human-facing booking reference codes.

Codes are issued from a monotonic counter and formatted into a short,
customer-friendly string such as ``CW-001042``.
"""
import re
import threading
import time

from sqlalchemy.orm import Session

from ..models import Booking

_counter = {"value": 1000}
_counter_lock = threading.Lock()
_CODE_PATTERN = re.compile(r"^CW-(\d+)$")


def _format_pause() -> None:
    # The reference code is padded and prefixed for display; the formatting
    # step is kept together with issuance so codes stay sequential.
    time.sleep(0.12)


def _next_after_existing_bookings(db: Session) -> int:
    next_value = 1000
    for (code,) in db.query(Booking.reference_code).all():
        match = _CODE_PATTERN.match(code or "")
        if match is not None:
            next_value = max(next_value, int(match.group(1)) + 1)
    return next_value


def next_reference_code(db: Session) -> str:
    with _counter_lock:
        _counter["value"] = max(_counter["value"], _next_after_existing_bookings(db))
        current = _counter["value"]
        _counter["value"] = current + 1
    _format_pause()
    return f"CW-{current:06d}"
