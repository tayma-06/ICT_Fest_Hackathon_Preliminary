import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_room(token: str, name: str, rate: int = 500) -> dict:
    response = client.post(
        "/rooms",
        json={"name": name, "capacity": 4, "hourly_rate_cents": rate},
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _future(hours: int) -> datetime:
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


def test_cross_org_resources_are_hidden_and_org_scoped_results_do_not_leak():
    org_a = _unique("tenant-a")
    org_b = _unique("tenant-b")
    a_admin = _register_login(org_a, "admin")
    a_member = _register_login(org_a, "member")
    b_admin = _register_login(org_b, "admin")
    b_member = _register_login(org_b, "member")

    a_room = _make_room(a_admin["token"], "A Room", rate=700)
    b_room = _make_room(b_admin["token"], "B Room", rate=900)
    a_start = _future(72)
    b_start = _future(74)
    a_booking = _book_ok(a_member["token"], a_room["id"], a_start, hours=2)
    b_booking = _book_ok(b_member["token"], b_room["id"], b_start, hours=1)

    a_rooms = client.get("/rooms", headers=_headers(a_member["token"]))
    b_rooms = client.get("/rooms", headers=_headers(b_member["token"]))
    assert {room["id"] for room in a_rooms.json()} == {a_room["id"]}
    assert {room["id"] for room in b_rooms.json()} == {b_room["id"]}

    for path, code in (
        (f"/rooms/{a_room['id']}/availability", "ROOM_NOT_FOUND"),
        (f"/rooms/{a_room['id']}/stats", "ROOM_NOT_FOUND"),
    ):
        response = client.get(
            path,
            params={"date": a_start.date().isoformat()},
            headers=_headers(b_admin["token"]),
        )
        assert response.status_code == 404
        assert response.json()["code"] == code

    cross_org_booking = _book(
        b_member["token"],
        a_room["id"],
        b_start + timedelta(hours=2),
        hours=1,
    )
    assert cross_org_booking.status_code == 404
    assert cross_org_booking.json()["code"] == "ROOM_NOT_FOUND"

    for method, path in (
        ("get", f"/bookings/{a_booking['id']}"),
        ("post", f"/bookings/{a_booking['id']}/cancel"),
    ):
        response = getattr(client, method)(path, headers=_headers(b_admin["token"]))
        assert response.status_code == 404
        assert response.json()["code"] == "BOOKING_NOT_FOUND"

    b_listing = client.get("/bookings", headers=_headers(b_member["token"])).json()
    assert b_listing["total"] == 1
    assert [item["id"] for item in b_listing["items"]] == [b_booking["id"]]

    # Prime both org report caches with the same range and make sure cache keys
    # include org_id, not only date range.
    day = a_start.date().isoformat()
    a_report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(a_admin["token"]),
    )
    b_report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(b_admin["token"]),
    )
    assert a_report.status_code == 200
    assert b_report.status_code == 200
    assert {room["room_id"] for room in a_report.json()["rooms"]} == {a_room["id"]}
    assert {room["room_id"] for room in b_report.json()["rooms"]} == {b_room["id"]}

    a_export = client.get("/admin/export", headers=_headers(a_admin["token"]))
    b_export = client.get("/admin/export", headers=_headers(b_admin["token"]))
    assert a_export.status_code == 200
    assert b_export.status_code == 200
    a_rows = list(csv.DictReader(io.StringIO(a_export.text)))
    b_rows = list(csv.DictReader(io.StringIO(b_export.text)))
    assert {int(row["id"]) for row in a_rows} == {a_booking["id"]}
    assert {int(row["id"]) for row in b_rows} == {b_booking["id"]}

    cross_export = client.get(
        "/admin/export",
        params={"room_id": a_room["id"]},
        headers=_headers(b_admin["token"]),
    )
    assert cross_export.status_code == 404
    assert cross_export.json()["code"] == "ROOM_NOT_FOUND"


def test_members_cannot_use_admin_endpoints():
    org = _unique("tenant-member")
    admin = _register_login(org, "admin")
    member = _register_login(org, "member")
    room = _make_room(admin["token"], "Only Room")
    day = _future(80).date().isoformat()

    create_room = client.post(
        "/rooms",
        json={"name": "Forbidden", "capacity": 2, "hourly_rate_cents": 100},
        headers=_headers(member["token"]),
    )
    report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(member["token"]),
    )
    export = client.get("/admin/export", headers=_headers(member["token"]))

    for response in (create_room, report, export):
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    availability = client.get(
        f"/rooms/{room['id']}/availability",
        params={"date": day},
        headers=_headers(member["token"]),
    )
    stats = client.get(f"/rooms/{room['id']}/stats", headers=_headers(member["token"]))
    assert availability.status_code == 200
    assert stats.status_code == 200
