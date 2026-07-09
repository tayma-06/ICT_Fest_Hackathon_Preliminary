import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Booking

client = TestClient(app)


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


def _make_room(token: str, name: str | None = None) -> int:
    response = client.post(
        "/rooms",
        json={
            "name": name or _unique("quota-room"),
            "capacity": 4,
            "hourly_rate_cents": 500,
        },
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _future(hours: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )


def _book(token: str, room_id: int, start: datetime, hours: int = 1):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=hours)).isoformat(),
        },
        headers=_headers(token),
    )


def _book_ok(token: str, room_id: int, start: datetime, hours: int = 1) -> dict:
    response = _book(token, room_id, start, hours)
    assert response.status_code == 201, response.text
    return response.json()


def _assert_quota(response):
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUOTA_EXCEEDED"


def _confirmed_qualifying_count(token: str) -> int:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=24)
    response = client.get("/bookings", params={"limit": 100}, headers=_headers(token))
    assert response.status_code == 200, response.text
    return sum(
        1
        for item in response.json()["items"]
        if item["status"] == "confirmed"
        and now < datetime.fromisoformat(item["start_time"]) <= window_end
    )


def _seed_confirmed_booking(user_id: int, room_id: int, start: datetime) -> None:
    db = SessionLocal()
    try:
        stored_start = start.astimezone(timezone.utc).replace(tzinfo=None)
        db.add(
            Booking(
                room_id=room_id,
                user_id=user_id,
                start_time=stored_start,
                end_time=stored_start + timedelta(hours=1),
                status="confirmed",
                reference_code=f"AUD-{uuid.uuid4().hex}",
                price_cents=500,
                created_at=datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def test_quota_counts_start_at_24h_boundary_and_rejects_fourth():
    org = _unique("quota-boundary")
    admin = _register_login(org, "admin")
    member = _register_login(org, "member")
    rooms = [_make_room(admin["token"]) for _ in range(4)]
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    _book_ok(member["token"], rooms[0], base + timedelta(hours=1))
    _book_ok(member["token"], rooms[1], base + timedelta(hours=2))
    _book_ok(member["token"], rooms[2], base + timedelta(hours=24))
    _assert_quota(_book(member["token"], rooms[3], base + timedelta(hours=3)))


def test_after_24h_cancelled_now_and_past_bookings_do_not_count():
    org = _unique("quota-exclusions")
    admin = _register_login(org, "admin")
    member = _register_login(org, "member")
    rooms = [_make_room(admin["token"]) for _ in range(9)]

    now_seed = datetime.now(timezone.utc)
    _seed_confirmed_booking(member["user_id"], rooms[0], now_seed)
    _seed_confirmed_booking(member["user_id"], rooms[1], now_seed - timedelta(hours=2))

    for index, hours in enumerate((25, 26, 27), start=2):
        _book_ok(member["token"], rooms[index], _future(hours))
    assert _book(member["token"], rooms[5], _future(2)).status_code == 201

    first = _book_ok(member["token"], rooms[6], _future(3))
    second = _book_ok(member["token"], rooms[7], _future(4))
    _assert_quota(_book(member["token"], rooms[8], _future(5)))

    cancelled = client.post(
        f"/bookings/{first['id']}/cancel",
        headers=_headers(member["token"]),
    )
    assert cancelled.status_code == 200, cancelled.text
    replacement = _book(member["token"], rooms[8], _future(5))
    assert replacement.status_code == 201, replacement.text
    assert second["status"] == "confirmed"


def test_quota_is_per_member_but_counts_across_all_rooms():
    org = _unique("quota-member")
    admin = _register_login(org, "admin")
    member_a = _register_login(org, "member-a")
    member_b = _register_login(org, "member-b")
    rooms = [_make_room(admin["token"]) for _ in range(7)]

    for index, hours in enumerate((2, 3, 4)):
        _book_ok(member_a["token"], rooms[index], _future(hours))

    member_b_first = _book(member_b["token"], rooms[3], _future(2))
    assert member_b_first.status_code == 201, member_b_first.text

    _book_ok(member_b["token"], rooms[4], _future(3))
    _book_ok(member_b["token"], rooms[5], _future(4))
    _assert_quota(_book(member_b["token"], rooms[6], _future(5)))


def test_concurrent_quota_after_two_existing_bookings_never_exceeds_three():
    org = _unique("quota-race")
    admin = _register_login(org, "admin")
    member = _register_login(org, "member")
    rooms = [_make_room(admin["token"]) for _ in range(8)]

    _book_ok(member["token"], rooms[0], _future(2))
    _book_ok(member["token"], rooms[1], _future(3))

    attempts = [(rooms[index], _future(4 + index)) for index in range(2, 8)]

    def attempt(args):
        room_id, start = args
        return _book(member["token"], room_id, start)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(attempt, attempts))

    statuses = [response.status_code for response in responses]
    assert statuses.count(201) <= 1, statuses
    assert _confirmed_qualifying_count(member["token"]) <= 3
    for response in responses:
        if response.status_code != 201:
            _assert_quota(response)
