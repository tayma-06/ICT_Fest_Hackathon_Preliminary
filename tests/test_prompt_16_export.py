"""Focused tests for GET /admin/export (CSV).

Covers:
- Admin-only access (member gets 403)
- Org scoping (only caller's org bookings)
- Room filtering (room_id param)
- Include-all behavior (include_all overrides room_id)
- Exact CSV header
- Column order
- Datetime UTC format
- Cross-org room_id does not leak data
- Empty export still contains header
"""
import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

EXPECTED_HEADER = [
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


def _book(token: str, room_id: int, start: str, end: str) -> dict:
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=_headers(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Auth and scoping
# --------------------------------------------------------------------------- #


def test_export_admin_only():
    org = f"export-auth-{uuid.uuid4().hex}"
    _register_and_login(org, "admin")
    member_token, _ = _register_and_login(org, "member")

    r = client.get("/admin/export", headers=_headers(member_token))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_export_org_scoping():
    org_a = f"export-scope-a-{uuid.uuid4().hex}"
    org_b = f"export-scope-b-{uuid.uuid4().hex}"
    token_a, _ = _register_and_login(org_a, "admin")
    token_b, _ = _register_and_login(org_b, "admin")

    room_a = _make_room(token_a, name="A-Room")
    _make_room(token_b, name="B-Room")

    start = _future(72)
    _book(token_a, room_a, start, _future(73))

    csv_a = client.get("/admin/export", headers=_headers(token_a)).text
    rows_a = list(csv.DictReader(io.StringIO(csv_a)))
    assert len(rows_a) == 1
    assert int(rows_a[0]["room_id"]) == room_a

    csv_b = client.get("/admin/export", headers=_headers(token_b)).text
    rows_b = list(csv.DictReader(io.StringIO(csv_b)))
    assert len(rows_b) == 0


def test_export_cross_org_room_id_404():
    org_a = f"export-cross-a-{uuid.uuid4().hex}"
    org_b = f"export-cross-b-{uuid.uuid4().hex}"
    token_a, _ = _register_and_login(org_a, "admin")
    token_b, _ = _register_and_login(org_b, "admin")

    room_a = _make_room(token_a)

    r = client.get(
        "/admin/export",
        params={"room_id": room_a},
        headers=_headers(token_b),
    )
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


# --------------------------------------------------------------------------- #
# Room filtering and include_all
# --------------------------------------------------------------------------- #


def test_export_room_filter():
    org = f"export-filter-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")

    r1 = _make_room(admin_token, name="R1")
    r2 = _make_room(admin_token, name="R2")

    start = _future(72)
    _book(admin_token, r1, start, _future(73))
    _book(admin_token, r2, start + timedelta(hours=2).isoformat().replace("+00:00", ""), _future(75))

    csv_all = client.get("/admin/export", headers=_headers(admin_token)).text
    rows_all = list(csv.DictReader(io.StringIO(csv_all)))
    assert len(rows_all) == 2

    csv_r1 = client.get(
        "/admin/export",
        params={"room_id": r1},
        headers=_headers(admin_token),
    ).text
    rows_r1 = list(csv.DictReader(io.StringIO(csv_r1)))
    assert len(rows_r1) == 1
    assert int(rows_r1[0]["room_id"]) == r1


def test_export_include_all_overrides_room_filter():
    org = f"export-include-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")

    r1 = _make_room(admin_token, name="R1")
    r2 = _make_room(admin_token, name="R2")

    start = _future(72)
    _book(admin_token, r1, start, _future(73))
    _book(admin_token, r2, _future(74), _future(75))

    csv_include = client.get(
        "/admin/export",
        params={"room_id": r1, "include_all": "true"},
        headers=_headers(admin_token),
    ).text
    rows = list(csv.DictReader(io.StringIO(csv_include)))
    assert len(rows) == 2, "include_all=true should return all rooms"
    room_ids = {int(r["room_id"]) for r in rows}
    assert room_ids == {r1, r2}


# --------------------------------------------------------------------------- #
# CSV format
# --------------------------------------------------------------------------- #


def test_export_header_exact():
    org = f"export-hdr-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    _make_room(admin_token)

    csv_text = client.get("/admin/export", headers=_headers(admin_token)).text
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    assert header == EXPECTED_HEADER, f"header mismatch: {header}"


def test_export_empty_still_has_header():
    org = f"export-empty-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")

    csv_text = client.get("/admin/export", headers=_headers(admin_token)).text
    lines = csv_text.strip().splitlines()
    assert len(lines) == 1, "should only have header row"
    assert lines[0] == ",".join(EXPECTED_HEADER)


def test_export_column_order():
    org = f"export-order-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token)

    start = _future(72)
    b = _book(admin_token, room_id, start, _future(73))

    csv_text = client.get("/admin/export", headers=_headers(admin_token)).text
    lines = csv_text.strip().splitlines()
    header = lines[0].split(",")
    assert header == EXPECTED_HEADER

    data = lines[1].split(",")
    row_dict = dict(zip(header, data))
    assert int(row_dict["id"]) == b["id"]
    assert row_dict["reference_code"] == b["reference_code"]
    assert int(row_dict["room_id"]) == room_id
    assert int(row_dict["user_id"]) == b["user_id"]
    assert row_dict["status"] == "confirmed"
    assert int(row_dict["price_cents"]) == b["price_cents"]


# --------------------------------------------------------------------------- #
# Datetime format
# --------------------------------------------------------------------------- #


def test_export_datetime_utc_format():
    org = f"export-dt-{uuid.uuid4().hex}"
    admin_token, _ = _register_and_login(org, "admin")
    room_id = _make_room(admin_token)

    start = _future(72)
    end = _future(74)
    b = _book(admin_token, room_id, start, end)

    csv_text = client.get("/admin/export", headers=_headers(admin_token)).text
    reader = csv.DictReader(io.StringIO(csv_text))
    row = next(reader)

    for field in ("start_time", "end_time"):
        parsed = datetime.fromisoformat(row[field])
        assert parsed.utcoffset() == timedelta(0), f"{field} not UTC: {row[field]}"

    assert datetime.fromisoformat(row["start_time"]) == datetime.fromisoformat(b["start_time"])
    assert datetime.fromisoformat(row["end_time"]) == datetime.fromisoformat(b["end_time"])
