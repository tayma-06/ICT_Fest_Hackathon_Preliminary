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


def _future_datetime(hours: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0, tzinfo=None
    )


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


def test_booking_list_scope_defaults_tie_order_and_sequential_pages():
    org = f"booking-read-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    member_token, member_id = _register_and_login(org, "member")
    other_token, other_id = _register_and_login(org, "other")

    room_ids = []
    for index in range(13):
        room = client.post(
            "/rooms",
            json={
                "name": f"Read Room {index}",
                "capacity": 2,
                "hourly_rate_cents": 100,
            },
            headers=_headers(admin_token),
        )
        assert room.status_code == 201, room.text
        room_ids.append(room.json()["id"])

    base = _future_datetime(200)
    starts = [
        base + timedelta(hours=8),
        base + timedelta(hours=1),
        base + timedelta(hours=3),
        base + timedelta(hours=3),
        base + timedelta(hours=2),
        base + timedelta(hours=5),
        base + timedelta(hours=3),
        base + timedelta(hours=6),
        base + timedelta(hours=7),
        base + timedelta(hours=9),
        base + timedelta(hours=4),
        base + timedelta(hours=10),
    ]

    db = SessionLocal()
    try:
        for index, start in enumerate(starts):
            db.add(
                Booking(
                    room_id=room_ids[index],
                    user_id=member_id,
                    start_time=start,
                    end_time=start + timedelta(hours=1),
                    status="confirmed",
                    reference_code=f"CW-read-{uuid.uuid4().hex}",
                    price_cents=100,
                    created_at=base,
                )
            )
        db.add(
            Booking(
                room_id=room_ids[-1],
                user_id=other_id,
                start_time=base - timedelta(hours=1),
                end_time=base,
                status="confirmed",
                reference_code=f"CW-read-{uuid.uuid4().hex}",
                price_cents=100,
                created_at=base,
            )
        )
        db.commit()
        expected_ids = [
            booking.id
            for booking in db.query(Booking)
            .filter(Booking.user_id == member_id)
            .order_by(Booking.start_time.asc(), Booking.id.asc())
            .all()
        ]
    finally:
        db.close()

    default = client.get("/bookings", headers=_headers(member_token))
    assert default.status_code == 200, default.text
    default_body = default.json()
    assert set(default_body) == {"items", "page", "limit", "total"}
    assert default_body["page"] == 1
    assert default_body["limit"] == 10
    assert default_body["total"] == 12
    assert [item["id"] for item in default_body["items"]] == expected_ids[:10]

    seen_ids = []
    for page in (1, 2, 3):
        response = client.get(
            "/bookings",
            params={"page": page, "limit": 5},
            headers=_headers(member_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["page"] == page
        assert body["limit"] == 5
        assert body["total"] == 12
        seen_ids.extend(item["id"] for item in body["items"])

    assert seen_ids == expected_ids
    assert len(seen_ids) == len(set(seen_ids))

    capped = client.get(
        "/bookings",
        params={"limit": 100},
        headers=_headers(member_token),
    )
    assert capped.status_code == 200, capped.text
    assert capped.json()["limit"] == 100
    assert len(capped.json()["items"]) == 12

    too_large = client.get(
        "/bookings",
        params={"limit": 101},
        headers=_headers(member_token),
    )
    assert too_large.status_code == 422

    other_listing = client.get("/bookings", headers=_headers(other_token))
    assert other_listing.status_code == 200, other_listing.text
    assert other_listing.json()["total"] == 1


def test_booking_detail_masks_inaccessible_bookings_and_refund_fields_are_exact():
    org = f"booking-detail-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    member_token, _ = _register_and_login(org, "member")
    peer_token, _ = _register_and_login(org, "peer")
    other_admin_token, _ = _register_and_login(f"booking-detail-x-{uuid.uuid4().hex}", "admin")

    room = client.post(
        "/rooms",
        json={"name": "Detail Room", "capacity": 2, "hourly_rate_cents": 101},
        headers=_headers(admin_token),
    )
    assert room.status_code == 201, room.text

    start = _future_datetime(30).replace(tzinfo=timezone.utc)
    booking = client.post(
        "/bookings",
        json={
            "room_id": room.json()["id"],
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(member_token),
    )
    assert booking.status_code == 201, booking.text

    cancelled = client.post(
        f"/bookings/{booking.json()['id']}/cancel",
        headers=_headers(member_token),
    )
    assert cancelled.status_code == 200, cancelled.text

    detail = client.get(
        f"/bookings/{booking.json()['id']}",
        headers=_headers(member_token),
    )
    assert detail.status_code == 200, detail.text
    refunds = detail.json()["refunds"]
    assert len(refunds) == 1
    assert set(refunds[0]) == {"amount_cents", "status", "processed_at"}
    assert refunds[0]["amount_cents"] == cancelled.json()["refund_amount_cents"]
    assert refunds[0]["status"] == "processed"
    assert datetime.fromisoformat(refunds[0]["processed_at"]).utcoffset() == timedelta(0)

    same_org_admin = client.get(
        f"/bookings/{booking.json()['id']}",
        headers=_headers(admin_token),
    )
    assert same_org_admin.status_code == 200, same_org_admin.text

    for token in (peer_token, other_admin_token):
        hidden = client.get(f"/bookings/{booking.json()['id']}", headers=_headers(token))
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "BOOKING_NOT_FOUND"


def test_availability_utc_boundary_sorting_and_create_cancel_freshness():
    org = f"availability-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room = client.post(
        "/rooms",
        json={"name": "Availability Room", "capacity": 2, "hourly_rate_cents": 300},
        headers=_headers(admin_token),
    )
    assert room.status_code == 201, room.text
    room_id = room.json()["id"]

    base = datetime.now(timezone.utc) + timedelta(days=5)
    late_start_utc = base.replace(hour=23, minute=0, second=0, microsecond=0)
    early_start_utc = late_start_utc - timedelta(hours=2)
    utc_day = late_start_utc.date().isoformat()
    plus_six = timezone(timedelta(hours=6))
    local_day = late_start_utc.astimezone(plus_six).date().isoformat()
    assert local_day != utc_day

    empty = client.get(
        f"/rooms/{room_id}/availability",
        params={"date": utc_day},
        headers=_headers(admin_token),
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["busy"] == []

    late = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": late_start_utc.astimezone(plus_six).isoformat(),
            "end_time": (late_start_utc + timedelta(hours=1)).astimezone(plus_six).isoformat(),
        },
        headers=_headers(admin_token),
    )
    assert late.status_code == 201, late.text

    after_late = client.get(
        f"/rooms/{room_id}/availability",
        params={"date": utc_day},
        headers=_headers(admin_token),
    )
    assert len(after_late.json()["busy"]) == 1

    early = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": early_start_utc.astimezone(plus_six).isoformat(),
            "end_time": (early_start_utc + timedelta(hours=1)).astimezone(plus_six).isoformat(),
        },
        headers=_headers(admin_token),
    )
    assert early.status_code == 201, early.text

    availability = client.get(
        f"/rooms/{room_id}/availability",
        params={"date": utc_day},
        headers=_headers(admin_token),
    )
    assert availability.status_code == 200, availability.text
    busy = availability.json()["busy"]
    assert [item["start_time"] for item in busy] == [
        early.json()["start_time"],
        late.json()["start_time"],
    ]

    next_utc_day = client.get(
        f"/rooms/{room_id}/availability",
        params={"date": local_day},
        headers=_headers(admin_token),
    )
    assert next_utc_day.status_code == 200, next_utc_day.text
    assert next_utc_day.json()["busy"] == []

    cancelled = client.post(
        f"/bookings/{early.json()['id']}/cancel",
        headers=_headers(admin_token),
    )
    assert cancelled.status_code == 200, cancelled.text

    after_cancel = client.get(
        f"/rooms/{room_id}/availability",
        params={"date": utc_day},
        headers=_headers(admin_token),
    )
    assert after_cancel.status_code == 200, after_cancel.text
    assert [item["start_time"] for item in after_cancel.json()["busy"]] == [
        late.json()["start_time"]
    ]


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
    stale_generation = cache.availability_generation(org_id, room_id, date)

    cache.invalidate_availability(org_id, room_id, date)
    cache.set_availability(org_id, room_id, date, {"stale": True}, stale_generation)

    assert cache.get_availability(org_id, room_id, date) is None

    fresh_generation = cache.availability_generation(org_id, room_id, date)
    fresh_availability = {"fresh": True}
    cache.set_availability(org_id, room_id, date, fresh_availability, fresh_generation)
    assert cache.get_availability(org_id, room_id, date) == fresh_availability

    other_org_id = uuid.uuid4().int
    other_generation = cache.availability_generation(other_org_id, room_id, date)
    cache.set_availability(
        other_org_id, room_id, date, {"other_org": True}, other_generation
    )
    assert cache.get_availability(org_id, room_id, date) == fresh_availability
    assert cache.get_availability(other_org_id, room_id, date) == {"other_org": True}

    cache.invalidate_availability(org_id, room_id, date)
    assert cache.get_availability(org_id, room_id, date) is None
    assert cache.get_availability(other_org_id, room_id, date) == {"other_org": True}
