"""Focused tests for GET /rooms/{id}/stats and GET /admin/usage-report.

Covers:
- Empty rooms (zero bookings)
- Inclusive range boundaries
- Cancelled bookings excluded
- Multiple organizations isolation
- Immediate cache invalidation
- Concurrent activity
"""
import uuid
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _register_and_login(org_name: str, username: str) -> tuple[str, int]:
    registered = client.post(
        "/auth/register",
        json={"org_name": org_name, "username": username, "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": username, "password": "pw12345"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["access_token"], registered.json()["user_id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_room(token: str, rate: int = 500, name: str = "Room") -> int:
    r = client.post(
        "/rooms",
        json={"name": name, "capacity": 4, "hourly_rate_cents": rate},
        headers=_headers(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------- #
# Room stats tests
# --------------------------------------------------------------------------- #


def test_stats_empty_room():
    org = f"stats-empty-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token)

    stats = client.get(f"/rooms/{room_id}/stats", headers=_headers(admin_token)).json()
    assert stats["total_confirmed_bookings"] == 0
    assert stats["total_revenue_cents"] == 0


def test_stats_after_booking_creation():
    org = f"stats-create-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=300)

    start = _future(48)
    end = _future(50)
    booking = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=_headers(admin_token),
    )
    assert booking.status_code == 201, booking.text

    stats = client.get(f"/rooms/{room_id}/stats", headers=_headers(admin_token)).json()
    assert stats["total_confirmed_bookings"] == 1
    assert stats["total_revenue_cents"] == 600  # 300 * 2h


def test_stats_after_cancellation():
    org = f"stats-cancel-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=400)

    start = _future(72)
    end = _future(73)
    booking = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=_headers(admin_token),
    )
    assert booking.status_code == 201, booking.text

    cancel = client.post(
        f"/bookings/{booking.json()['id']}/cancel",
        headers=_headers(admin_token),
    )
    assert cancel.status_code == 200, cancel.text

    stats = client.get(f"/rooms/{room_id}/stats", headers=_headers(admin_token)).json()
    assert stats["total_confirmed_bookings"] == 0
    assert stats["total_revenue_cents"] == 0


def test_stats_multiple_bookings_summed():
    org = f"stats-sum-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=200)

    for i in range(3):
        start = _future(48 + i * 4)
        end = _future(48 + i * 4 + 1)
        r = client.post(
            "/bookings",
            json={"room_id": room_id, "start_time": start, "end_time": end},
            headers=_headers(admin_token),
        )
        assert r.status_code == 201, r.text

    stats = client.get(f"/rooms/{room_id}/stats", headers=_headers(admin_token)).json()
    assert stats["total_confirmed_bookings"] == 3
    assert stats["total_revenue_cents"] == 600  # 200 * 1h * 3


def test_stats_cross_org_isolation():
    org_a = f"stats-iso-a-{uuid.uuid4().hex}"
    org_b = f"stats-iso-b-{uuid.uuid4().hex}"
    token_a, _ = _register_and_login(org_a, "admin")
    token_b, _ = _register_and_login(org_b, "admin")

    room_a = _make_room(token_a, rate=500)
    _make_room(token_b, rate=300)

    start = _future(48)
    end = _future(49)
    r = client.post(
        "/bookings",
        json={"room_id": room_a, "start_time": start, "end_time": end},
        headers=_headers(token_a),
    )
    assert r.status_code == 201, r.text

    stats_a = client.get(f"/rooms/{room_a}/stats", headers=_headers(token_a)).json()
    assert stats_a["total_confirmed_bookings"] == 1

    r = client.get(f"/rooms/{room_a}/stats", headers=_headers(token_b))
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_stats_concurrent_burst():
    org = f"stats-burst-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=500)

    slots = [_future(30 + i) for i in range(6)]

    def create(slot):
        return client.post(
            "/bookings",
            json={
                "room_id": room_id,
                "start_time": slot,
                "end_time": _future(
                    31 + slots.index(slot)
                ),
            },
            headers=_headers(admin_token),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create, slots))

    successes = sum(1 for r in results if r.status_code == 201)
    assert successes == 6, f"expected 6 successes, got {successes}"

    stats = client.get(f"/rooms/{room_id}/stats", headers=_headers(admin_token)).json()
    assert stats["total_confirmed_bookings"] == 6
    assert stats["total_revenue_cents"] == 6 * 500


# --------------------------------------------------------------------------- #
# Usage-report tests
# --------------------------------------------------------------------------- #


def test_usage_report_empty_rooms():
    org = f"usage-empty-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, name="Empty")

    today = datetime.now(timezone.utc).date().isoformat()
    report = client.get(
        "/admin/usage-report",
        params={"from": today, "to": today},
        headers=_headers(admin_token),
    ).json()

    assert len(report["rooms"]) == 1
    assert report["rooms"][0]["room_id"] == room_id
    assert report["rooms"][0]["confirmed_bookings"] == 0
    assert report["rooms"][0]["revenue_cents"] == 0


def test_usage_report_inclusive_from_boundary():
    org = f"usage-from-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=300)

    s = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=2)).isoformat(),
        },
        headers=_headers(admin_token),
    )

    report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    ).json()

    assert report["rooms"][0]["confirmed_bookings"] == 1
    assert report["rooms"][0]["revenue_cents"] == 600


def test_usage_report_inclusive_to_boundary():
    org = f"usage-to-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=200)

    s = datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(admin_token),
    )

    report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    ).json()

    assert report["rooms"][0]["confirmed_bookings"] == 1, "booking late on `to` date must be included"


def test_usage_report_cancelled_excluded():
    org = f"usage-cancel-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=400)

    s = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    b = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(admin_token),
    )
    assert b.status_code == 201
    client.post(f"/bookings/{b.json()['id']}/cancel", headers=_headers(admin_token))

    report = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    ).json()

    assert report["rooms"][0]["confirmed_bookings"] == 0
    assert report["rooms"][0]["revenue_cents"] == 0


def test_usage_report_multiple_orgs():
    org_a = f"usage-multi-a-{uuid.uuid4().hex}"
    org_b = f"usage-multi-b-{uuid.uuid4().hex}"
    token_a, _ = _register_and_login(org_a, "admin")
    token_b, _ = _register_and_login(org_b, "admin")

    room_a = _make_room(token_a, rate=500, name="A")
    _make_room(token_b, rate=300, name="B")

    s = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    client.post(
        "/bookings",
        json={
            "room_id": room_a,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(token_a),
    )

    report_a = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(token_a),
    ).json()
    assert len(report_a["rooms"]) == 1
    assert report_a["rooms"][0]["confirmed_bookings"] == 1

    report_b = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(token_b),
    ).json()
    assert len(report_b["rooms"]) == 1
    assert report_b["rooms"][0]["confirmed_bookings"] == 0


def test_usage_report_cache_invalidation_on_create():
    org = f"usage-cache-c-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=600)

    s = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    r1 = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    )
    assert r1.json()["rooms"][0]["confirmed_bookings"] == 0

    client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(admin_token),
    )

    r2 = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    )
    assert r2.json()["rooms"][0]["confirmed_bookings"] == 1


def test_usage_report_cache_invalidation_on_cancel():
    org = f"usage-cache-x-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token, rate=600)

    s = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    day = s.date().isoformat()

    b = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": s.isoformat(),
            "end_time": (s + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(admin_token),
    )
    assert b.status_code == 201

    client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    )

    client.post(f"/bookings/{b.json()['id']}/cancel", headers=_headers(admin_token))

    r = client.get(
        "/admin/usage-report",
        params={"from": day, "to": day},
        headers=_headers(admin_token),
    )
    assert r.json()["rooms"][0]["confirmed_bookings"] == 0


def test_usage_report_response_field_names():
    org = f"usage-fields-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    _make_room(admin_token)

    today = datetime.now(timezone.utc).date().isoformat()
    report = client.get(
        "/admin/usage-report",
        params={"from": today, "to": today},
        headers=_headers(admin_token),
    ).json()

    assert "from" in report
    assert "to" in report
    assert "rooms" in report
    room = report["rooms"][0]
    assert "room_id" in room
    assert "room_name" in room
    assert "confirmed_bookings" in room
    assert "revenue_cents" in room
