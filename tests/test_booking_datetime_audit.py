import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _new_admin_room(rate: int = 375) -> tuple[str, int]:
    org = _unique("datetime")
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
    token = logged_in.json()["access_token"]
    room = client.post(
        "/rooms",
        json={"name": "Datetime Room", "capacity": 4, "hourly_rate_cents": rate},
        headers=_headers(token),
    )
    assert room.status_code == 201, room.text
    return token, room.json()["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _future(hours: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )


def _post_booking(token: str, room_id: int, start: str, end: str):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=_headers(token),
    )


def _assert_invalid(response):
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_naive_z_and_offset_inputs_are_stored_and_returned_as_utc():
    token, room_id = _new_admin_room(rate=425)

    naive_start = _future(50)
    naive_response = _post_booking(
        token,
        room_id,
        naive_start.replace(tzinfo=None).isoformat(),
        (naive_start + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
    )
    assert naive_response.status_code == 201, naive_response.text
    naive_body = naive_response.json()
    assert datetime.fromisoformat(naive_body["start_time"]) == naive_start
    assert datetime.fromisoformat(naive_body["end_time"]) == naive_start + timedelta(hours=1)
    assert datetime.fromisoformat(naive_body["created_at"]).utcoffset() == timedelta(0)
    assert naive_body["price_cents"] == 425

    z_start = _future(54)
    z_response = _post_booking(
        token,
        room_id,
        z_start.isoformat().replace("+00:00", "Z"),
        (z_start + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    )
    assert z_response.status_code == 201, z_response.text
    z_body = z_response.json()
    assert datetime.fromisoformat(z_body["start_time"]) == z_start
    assert datetime.fromisoformat(z_body["end_time"]) == z_start + timedelta(hours=2)
    assert z_body["price_cents"] == 850

    positive_offset_start = _future(58)
    plus_6 = timezone(timedelta(hours=6))
    positive_response = _post_booking(
        token,
        room_id,
        positive_offset_start.astimezone(plus_6).isoformat(),
        (positive_offset_start + timedelta(hours=3)).astimezone(plus_6).isoformat(),
    )
    assert positive_response.status_code == 201, positive_response.text
    positive_body = positive_response.json()
    assert datetime.fromisoformat(positive_body["start_time"]) == positive_offset_start
    assert datetime.fromisoformat(positive_body["end_time"]) == positive_offset_start + timedelta(hours=3)
    assert positive_body["price_cents"] == 1275

    negative_offset_start = _future(64)
    minus_5 = timezone(timedelta(hours=-5))
    negative_response = _post_booking(
        token,
        room_id,
        negative_offset_start.astimezone(minus_5).isoformat(),
        (negative_offset_start + timedelta(hours=1)).astimezone(minus_5).isoformat(),
    )
    assert negative_response.status_code == 201, negative_response.text
    negative_body = negative_response.json()
    assert datetime.fromisoformat(negative_body["start_time"]) == negative_offset_start
    assert datetime.fromisoformat(negative_body["end_time"]) == negative_offset_start + timedelta(hours=1)
    assert negative_body["price_cents"] == 425


def test_invalid_booking_windows_return_invalid_booking_window():
    token, room_id = _new_admin_room()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    future = _future(80)

    cases = [
        (now - timedelta(hours=1), now),
        (now, now + timedelta(hours=1)),
        (future, future),
        (future, future - timedelta(hours=1)),
        (future, future + timedelta(minutes=90)),
        (future, future + timedelta(hours=9)),
    ]

    for start, end in cases:
        response = _post_booking(token, room_id, start.isoformat(), end.isoformat())
        _assert_invalid(response)


def test_one_hour_and_eight_hour_bookings_price_correctly():
    token, room_id = _new_admin_room(rate=123)
    one_hour_start = _future(90)
    eight_hour_start = _future(100)

    one_hour = _post_booking(
        token,
        room_id,
        one_hour_start.isoformat(),
        (one_hour_start + timedelta(hours=1)).isoformat(),
    )
    assert one_hour.status_code == 201, one_hour.text
    assert one_hour.json()["price_cents"] == 123

    eight_hour = _post_booking(
        token,
        room_id,
        eight_hour_start.isoformat(),
        (eight_hour_start + timedelta(hours=8)).isoformat(),
    )
    assert eight_hour.status_code == 201, eight_hour.text
    assert eight_hour.json()["price_cents"] == 984
