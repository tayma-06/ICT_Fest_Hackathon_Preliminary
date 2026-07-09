import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_admin(org_prefix: str = "conflict") -> dict:
    org = _unique(org_prefix)
    registered = client.post(
        "/auth/register",
        json={"org_name": org, "username": "admin", "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"org_name": org, "username": "admin", "password": "pw12345"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {
        "org": org,
        "token": logged_in.json()["access_token"],
        "user_id": registered.json()["user_id"],
        "org_id": registered.json()["org_id"],
    }


def _make_room(token: str, name: str | None = None) -> int:
    response = client.post(
        "/rooms",
        json={
            "name": name or _unique("Room"),
            "capacity": 4,
            "hourly_rate_cents": 600,
        },
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _future(hours: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )


def _book(token: str, room_id: int, start: datetime, end: datetime):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
        headers=_headers(token),
    )


def _book_ok(token: str, room_id: int, start: datetime, end: datetime) -> dict:
    response = _book(token, room_id, start, end)
    assert response.status_code == 201, response.text
    return response.json()


def _assert_conflict(response):
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ROOM_CONFLICT"


def test_all_overlap_shapes_conflict_for_same_room():
    admin = _new_admin()
    base = _future(90)
    cases = [
        (base, base + timedelta(hours=2), base, base + timedelta(hours=2)),
        (base, base + timedelta(hours=4), base + timedelta(hours=1), base + timedelta(hours=2)),
        (base + timedelta(hours=1), base + timedelta(hours=2), base, base + timedelta(hours=4)),
        (base + timedelta(hours=1), base + timedelta(hours=3), base, base + timedelta(hours=2)),
        (base, base + timedelta(hours=2), base + timedelta(hours=1), base + timedelta(hours=3)),
    ]

    for existing_start, existing_end, new_start, new_end in cases:
        room_id = _make_room(admin["token"])
        _book_ok(admin["token"], room_id, existing_start, existing_end)
        _assert_conflict(_book(admin["token"], room_id, new_start, new_end))


def test_back_to_back_bookings_are_allowed():
    admin = _new_admin()
    room_id = _make_room(admin["token"])
    start = _future(120)
    _book_ok(admin["token"], room_id, start, start + timedelta(hours=2))

    ending_at_existing_start = _book(
        admin["token"],
        room_id,
        start - timedelta(hours=1),
        start,
    )
    starting_at_existing_end = _book(
        admin["token"],
        room_id,
        start + timedelta(hours=2),
        start + timedelta(hours=3),
    )

    assert ending_at_existing_start.status_code == 201, ending_at_existing_start.text
    assert starting_at_existing_end.status_code == 201, starting_at_existing_end.text


def test_cancelled_different_room_and_different_org_bookings_do_not_conflict():
    admin_a = _new_admin("conflict-a")
    admin_b = _new_admin("conflict-b")
    room_a = _make_room(admin_a["token"], "A")
    room_a_2 = _make_room(admin_a["token"], "A2")
    room_b = _make_room(admin_b["token"], "B")
    start = _future(140)
    end = start + timedelta(hours=1)

    cancelled = _book_ok(admin_a["token"], room_a, start, end)
    cancel_response = client.post(
        f"/bookings/{cancelled['id']}/cancel",
        headers=_headers(admin_a["token"]),
    )
    assert cancel_response.status_code == 200, cancel_response.text
    assert _book(admin_a["token"], room_a, start, end).status_code == 201

    same_time_other_room = _book(admin_a["token"], room_a_2, start, end)
    same_time_other_org = _book(admin_b["token"], room_b, start, end)
    assert same_time_other_room.status_code == 201, same_time_other_room.text
    assert same_time_other_org.status_code == 201, same_time_other_org.text


def test_concurrent_same_room_same_interval_allows_at_most_one_success():
    admin = _new_admin()
    room_id = _make_room(admin["token"])
    start = _future(160)
    end = start + timedelta(hours=1)

    def attempt(_):
        return _book(admin["token"], room_id, start, end)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(8)))

    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 7, statuses
    for response in responses:
        if response.status_code == 409:
            assert response.json()["code"] == "ROOM_CONFLICT"
