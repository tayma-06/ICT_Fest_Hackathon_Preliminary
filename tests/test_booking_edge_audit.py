import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.errors import AppError
from app.main import app
from app.models import Booking, RefundLog, Room, User
from app.routers import bookings
from app.services import ratelimit, reference


client = TestClient(app)


class FakeClock:
    def __init__(self, value: float):
        self.value = value

    def now(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_login(org: str, username: str) -> dict:
    registered = client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {
        "token": logged_in.json()["access_token"],
        "user_id": registered.json()["user_id"],
        "org_id": registered.json()["org_id"],
        "role": registered.json()["role"],
    }


def _make_room(token: str, rate: int = 500, name: str | None = None) -> dict:
    response = client.post(
        "/rooms",
        json={
            "name": name or f"Room {uuid.uuid4().hex}",
            "capacity": 4,
            "hourly_rate_cents": rate,
        },
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post_booking(token: str, room_id: int, start: datetime, end: datetime):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
        headers=_headers(token),
    )


def _seed_booking(
    room_id: int,
    user_id: int,
    start: datetime,
    end: datetime,
    price_cents: int = 500,
    status: str = "confirmed",
    reference_code: str | None = None,
) -> int:
    db = SessionLocal()
    try:
        booking = Booking(
            room_id=room_id,
            user_id=user_id,
            start_time=start.replace(tzinfo=None),
            end_time=end.replace(tzinfo=None),
            status=status,
            reference_code=reference_code or f"CW-{uuid.uuid4().int % 10**18:018d}",
            price_cents=price_cents,
            created_at=datetime.utcnow(),
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)
        return booking.id
    finally:
        db.close()


def _unused_reference_pair() -> tuple[str, str]:
    db = SessionLocal()
    try:
        max_seen = 0
        for (code,) in db.query(Booking.reference_code).all():
            if isinstance(code, str) and code.startswith("CW-") and code[3:].isdigit():
                max_seen = max(max_seen, int(code[3:]))
        return f"CW-{max_seen + 1:06d}", f"CW-{max_seen + 2:06d}"
    finally:
        db.close()


def _refund_logs(booking_id: int) -> list[RefundLog]:
    db = SessionLocal()
    try:
        return (
            db.query(RefundLog)
            .filter(RefundLog.booking_id == booking_id)
            .order_by(RefundLog.id.asc())
            .all()
        )
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clear_rate_limit_state(monkeypatch):
    with ratelimit._buckets_lock:
        ratelimit._buckets.clear()
    monkeypatch.setattr(ratelimit, "_settle_pause", lambda: None)


def test_rate_limit_uses_rolling_window_and_removes_only_expired_entries(monkeypatch):
    clock = FakeClock(59.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock.now)
    monkeypatch.setattr(ratelimit.time, "time", clock.now)

    for _ in range(20):
        ratelimit.record_and_check(user_id=101)

    clock.set(60.1)
    with pytest.raises(AppError) as limited:
        ratelimit.record_and_check(user_id=101)
    assert limited.value.status_code == 429
    assert limited.value.code == "RATE_LIMITED"
    assert len(ratelimit._buckets[101]) == 21

    clock.set(119.0)
    ratelimit.record_and_check(user_id=101)
    assert len(ratelimit._buckets[101]) == 2


def test_rate_limit_survives_concurrent_requests(monkeypatch):
    clock = FakeClock(500.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock.now)
    monkeypatch.setattr(ratelimit.time, "time", clock.now)

    def attempt(_):
        try:
            ratelimit.record_and_check(user_id=202)
            return "ok"
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(attempt, range(25)))

    assert results.count("ok") == 20
    assert results.count("RATE_LIMITED") == 5


def test_rate_limit_uses_monotonic_clock_when_wall_clock_moves_backwards(monkeypatch):
    wall_clock = FakeClock(1_000.0)
    monotonic_clock = FakeClock(100.0)
    monkeypatch.setattr(ratelimit.time, "time", wall_clock.now)
    monkeypatch.setattr(ratelimit.time, "monotonic", monotonic_clock.now)

    for _ in range(20):
        ratelimit.record_and_check(user_id=303)

    wall_clock.set(900.0)
    monotonic_clock.set(161.0)

    ratelimit.record_and_check(user_id=303)
    assert len(ratelimit._buckets[303]) == 1


def test_post_bookings_rate_limit_is_per_user_and_counts_validation_failures(monkeypatch):
    clock = FakeClock(10_000.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock.now)
    monkeypatch.setattr(ratelimit.time, "time", clock.now)

    org = _unique("rate-users")
    admin = _register_login(org, "admin")
    same_org_member = _register_login(org, "member")
    other_org_admin = _register_login(_unique("rate-other"), "admin")
    room = _make_room(admin["token"])
    past = datetime.now(timezone.utc) - timedelta(hours=2)

    for _ in range(20):
        response = _post_booking(admin["token"], room["id"], past, past + timedelta(hours=1))
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_BOOKING_WINDOW"

    limited = _post_booking(admin["token"], room["id"], past, past + timedelta(hours=1))
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"

    same_org = _post_booking(same_org_member["token"], room["id"], past, past + timedelta(hours=1))
    other_org = _post_booking(other_org_admin["token"], room["id"], past, past + timedelta(hours=1))
    assert same_org.status_code == 400
    assert other_org.status_code == 400


def test_post_bookings_rate_limit_counts_conflicts_and_quota_failures(monkeypatch):
    clock = FakeClock(20_000.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock.now)
    monkeypatch.setattr(ratelimit.time, "time", clock.now)

    org = _unique("rate-failures")
    admin = _register_login(org, "admin")
    member = _register_login(org, "member")
    conflict_room = _make_room(admin["token"], name="Conflict Room")
    quota_room = _make_room(admin["token"], name="Quota Room")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now + timedelta(hours=72)

    _seed_booking(conflict_room["id"], member["user_id"], start, start + timedelta(hours=1))
    for _ in range(20):
        response = _post_booking(member["token"], conflict_room["id"], start, start + timedelta(hours=1))
        assert response.status_code == 409
        assert response.json()["code"] == "ROOM_CONFLICT"
    assert _post_booking(member["token"], conflict_room["id"], start, start + timedelta(hours=1)).status_code == 429

    with ratelimit._buckets_lock:
        ratelimit._buckets.clear()

    quota_starts = [now + timedelta(hours=2 + i) for i in range(3)]
    for slot in quota_starts:
        _seed_booking(quota_room["id"], member["user_id"], slot, slot + timedelta(hours=1))
    target = now + timedelta(hours=8)
    for _ in range(20):
        response = _post_booking(member["token"], quota_room["id"], target, target + timedelta(hours=1))
        assert response.status_code == 409
        assert response.json()["code"] == "QUOTA_EXCEEDED"
    limited = _post_booking(member["token"], quota_room["id"], target, target + timedelta(hours=1))
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"


def test_cancellation_permissions_and_cross_org_ids_return_not_found():
    org = _unique("cancel-perms")
    admin = _register_login(org, "admin")
    owner = _register_login(org, "owner")
    other_member = _register_login(org, "other")
    outsider = _register_login(_unique("cancel-outsider"), "admin")
    room = _make_room(admin["token"])
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=80)
    member_booking_id = _seed_booking(room["id"], owner["user_id"], start, start + timedelta(hours=1))

    response = client.post(
        f"/bookings/{member_booking_id}/cancel",
        headers=_headers(other_member["token"]),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "BOOKING_NOT_FOUND"

    response = client.post(
        f"/bookings/{member_booking_id}/cancel",
        headers=_headers(outsider["token"]),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "BOOKING_NOT_FOUND"

    response = client.post(
        f"/bookings/{member_booking_id}/cancel",
        headers=_headers(admin["token"]),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_refund_boundaries_stored_price_rounding_and_single_log(monkeypatch):
    fixed_now = datetime(2030, 1, 1, 12, 0, 0)

    class FixedDateTime:
        @classmethod
        def utcnow(cls):
            return fixed_now

    monkeypatch.setattr(bookings, "datetime", FixedDateTime)

    user = _register_login(_unique("refund-boundary"), "admin")
    room = _make_room(user["token"], rate=9999)
    cases = [
        (fixed_now + timedelta(hours=48), 101, 100, 101),
        (fixed_now + timedelta(hours=24), 101, 50, 51),
        (fixed_now + timedelta(hours=24) - timedelta(microseconds=1), 101, 0, 0),
    ]

    for start, stored_price, expected_percent, expected_amount in cases:
        booking_id = _seed_booking(
            room["id"],
            user["user_id"],
            start,
            start + timedelta(hours=1),
            price_cents=stored_price,
        )

        response = client.post(f"/bookings/{booking_id}/cancel", headers=_headers(user["token"]))
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"id", "status", "refund_percent", "refund_amount_cents"}
        assert response.json()["refund_percent"] == expected_percent
        assert response.json()["refund_amount_cents"] == expected_amount

        logs = _refund_logs(booking_id)
        assert len(logs) == 1
        assert logs[0].amount_cents == expected_amount


def test_already_cancelled_booking_returns_conflict_without_second_refund():
    user = _register_login(_unique("already-cancelled"), "admin")
    room = _make_room(user["token"])
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=72)
    booking_id = _seed_booking(room["id"], user["user_id"], start, start + timedelta(hours=1))

    first = client.post(f"/bookings/{booking_id}/cancel", headers=_headers(user["token"]))
    second = client.post(f"/bookings/{booking_id}/cancel", headers=_headers(user["token"]))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "ALREADY_CANCELLED"
    assert len(_refund_logs(booking_id)) == 1


def test_concurrent_cancellation_one_success_one_refund_no_hangs():
    user = _register_login(_unique("cancel-race"), "admin")
    room = _make_room(user["token"], rate=333)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=72)
    booking_id = _seed_booking(room["id"], user["user_id"], start, start + timedelta(hours=1), price_cents=333)
    errors = []

    def attempt(_):
        try:
            return client.post(f"/bookings/{booking_id}/cancel", headers=_headers(user["token"]))
        except Exception as exc:  # pragma: no cover - converted to assertion detail below
            errors.append(exc)
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt, i) for i in range(8)]
        results = [future.result(timeout=15) for future in futures]

    assert errors == []
    status_codes = [response.status_code for response in results]
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 7
    assert all(
        response.json().get("code") == "ALREADY_CANCELLED"
        for response in results
        if response.status_code == 409
    )

    detail = client.get(f"/bookings/{booking_id}", headers=_headers(user["token"]))
    assert detail.status_code == 200
    assert detail.json()["status"] == "cancelled"
    logs = _refund_logs(booking_id)
    winner = next(response for response in results if response.status_code == 200)
    assert len(logs) == 1
    assert winner.json()["refund_amount_cents"] == logs[0].amount_cents


def test_reference_code_collision_is_retried_safely(monkeypatch):
    no_raise_client = TestClient(app, raise_server_exceptions=False)
    user = _register_login(_unique("ref-collision"), "admin")
    room = _make_room(user["token"])
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=96)
    existing_code, retry_code = _unused_reference_pair()
    _seed_booking(
        room["id"],
        user["user_id"],
        start + timedelta(days=10),
        start + timedelta(days=10, hours=1),
        reference_code=existing_code,
    )
    codes = iter([existing_code, retry_code])
    issued = []

    def forced_reference(_db):
        code = next(codes)
        issued.append(code)
        return code

    monkeypatch.setattr(reference, "next_reference_code", forced_reference)

    response = no_raise_client.post(
        "/bookings",
        json={
            "room_id": room["id"],
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(user["token"]),
    )

    assert response.status_code == 201, response.text
    assert issued == [existing_code, retry_code]
    assert response.json()["reference_code"] == retry_code
    assert response.json()["reference_code"].startswith("CW-")


def test_reference_codes_stay_unique_under_concurrent_creation():
    user = _register_login(_unique("ref-race"), "admin")
    rooms = [_make_room(user["token"], name=f"Race {i}") for i in range(8)]
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=120)
    start_barrier = threading.Barrier(len(rooms))

    def attempt(room):
        start_barrier.wait(timeout=10)
        return _post_booking(user["token"], room["id"], start, start + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=len(rooms)) as pool:
        results = list(pool.map(attempt, rooms))

    assert all(response.status_code == 201 for response in results)
    codes = [response.json()["reference_code"] for response in results]
    assert len(codes) == len(set(codes))
    assert all(code.startswith("CW-") and code[3:].isdigit() for code in codes)
