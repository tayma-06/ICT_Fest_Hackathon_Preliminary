import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from app import cache
from app.auth import hash_password
from app.config import JWT_ALGORITHM, JWT_SECRET
from app.database import SessionLocal
from app.main import app
from app.models import Booking, Organization, Room, User

client = TestClient(app)

CSV_HEADER = [
    "id",
    "reference_code",
    "room_id",
    "user_id",
    "start_time",
    "end_time",
    "status",
    "price_cents",
]


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _register_and_login(org_name: str, username: str) -> tuple[str, int]:
    registered = client.post(
        "/auth/register",
        json={"org_name": org_name, "username": username, "password": "pw12345"},
    )
    assert registered.status_code == 201

    logged_in = client.post(
        "/auth/login",
        json={"org_name": org_name, "username": username, "password": "pw12345"},
    )
    assert logged_in.status_code == 200
    return logged_in.json()["access_token"], registered.json()["user_id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signed_token(
    token_type: str,
    sub: str = "abc",
    omit: str | None = None,
    jti: object | None = None,
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": sub,
        "org": 1,
        "role": "member",
        "jti": uuid.uuid4().hex if jti is None else jti,
        "iat": now,
        "exp": now + 900,
        "type": token_type,
    }
    if omit is not None:
        payload.pop(omit)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def test_admin_export_uses_exact_header_and_all_org_bookings_by_default():
    org = f"export-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    member_token, member_id = _register_and_login(org, "member")

    room = client.post(
        "/rooms",
        json={"name": "Export Room", "capacity": 2, "hourly_rate_cents": 700},
        headers=_headers(admin_token),
    )
    assert room.status_code == 201

    booking = client.post(
        "/bookings",
        json={
            "room_id": room.json()["id"],
            "start_time": _future(72),
            "end_time": _future(74),
        },
        headers=_headers(member_token),
    )
    assert booking.status_code == 201

    exported = client.get("/admin/export", headers=_headers(admin_token))
    assert exported.status_code == 200

    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0] == CSV_HEADER
    assert any(row[3] == str(member_id) for row in rows[1:])


def test_malformed_access_token_subject_returns_401():
    token = _signed_token("access", sub="abc")

    response = client.get("/rooms", headers=_headers(token))

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_access_token_missing_required_claim_returns_401():
    token = _signed_token("access", sub="1", omit="jti")

    response = client.get("/rooms", headers=_headers(token))

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_access_token_malformed_jti_returns_401():
    token = _signed_token("access", sub="1", jti=["not", "a", "string"])

    response = client.get("/rooms", headers=_headers(token))

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_malformed_refresh_token_subject_returns_401():
    token = _signed_token("refresh", sub="abc")

    response = client.post("/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_reference_code_continues_after_existing_database_values():
    marker = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    db = SessionLocal()
    try:
        org = Organization(name=f"seed-{uuid.uuid4().hex}")
        db.add(org)
        db.commit()
        db.refresh(org)

        user = User(
            org_id=org.id,
            username="seed-user",
            hashed_password=hash_password("pw12345"),
            role="member",
        )
        room = Room(org_id=org.id, name="Seed Room", capacity=1, hourly_rate_cents=100)
        db.add_all([user, room])
        db.commit()
        db.refresh(user)
        db.refresh(room)

        db.add(
            Booking(
                room_id=room.id,
                user_id=user.id,
                start_time=datetime.utcnow() + timedelta(days=30),
                end_time=datetime.utcnow() + timedelta(days=30, hours=1),
                status="confirmed",
                reference_code=f"CW-{marker}",
                price_cents=100,
            )
        )
        db.commit()
    finally:
        db.close()

    org_name = f"ref-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org_name, "admin")
    room = client.post(
        "/rooms",
        json={"name": "Reference Room", "capacity": 1, "hourly_rate_cents": 100},
        headers=_headers(admin_token),
    )
    assert room.status_code == 201

    booking = client.post(
        "/bookings",
        json={
            "room_id": room.json()["id"],
            "start_time": _future(96),
            "end_time": _future(97),
        },
        headers=_headers(admin_token),
    )
    assert booking.status_code == 201

    code_number = int(booking.json()["reference_code"].split("-")[1])
    assert code_number > marker


def test_cache_rejects_stale_set_after_invalidation():
    org_id = uuid.uuid4().int
    frm = "2026-01-01"
    to = "2026-01-01"
    stale_generation = cache.report_generation(org_id)

    cache.invalidate_report(org_id)
    cache.set_report(org_id, frm, to, {"stale": True}, stale_generation)

    assert cache.get_report(org_id, frm, to) is None

    fresh_generation = cache.report_generation(org_id)
    fresh_report = {"fresh": True}
    cache.set_report(org_id, frm, to, fresh_report, fresh_generation)
    assert cache.get_report(org_id, frm, to) == fresh_report

    room_id = uuid.uuid4().int
    date = "2026-01-01"
    stale_generation = cache.availability_generation(room_id, date)

    cache.invalidate_availability(room_id, date)
    cache.set_availability(room_id, date, {"stale": True}, stale_generation)

    assert cache.get_availability(room_id, date) is None

    fresh_generation = cache.availability_generation(room_id, date)
    fresh_availability = {"fresh": True}
    cache.set_availability(room_id, date, fresh_availability, fresh_generation)
    assert cache.get_availability(room_id, date) == fresh_availability
