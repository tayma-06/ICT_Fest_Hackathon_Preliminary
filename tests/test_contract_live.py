"""Full black-box contract + concurrency tests against a live uvicorn server.

Spawns uvicorn as a subprocess on a fresh SQLite database and talks to it over
real HTTP with thread-based concurrency, mirroring how the grader interacts
with the service.

Run:  python -m pytest tests/test_contract_live.py -v
"""
import csv
import io
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "cowork-dev-secret-change-me"  # config.py default; env not overridden

CSV_HEADER = "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"

# Every reference code seen across the whole session; checked for global
# uniqueness at the end.
ALL_REFERENCE_CODES: list[str] = []

client = httpx.Client(base_url=BASE, timeout=120.0)


# --------------------------------------------------------------------------- #
# server fixture
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session", autouse=True)
def server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test_cowork.db"
    if db_path.exists():
        db_path.unlink()
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite:///" + str(db_path).replace("\\", "/"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
    )
    try:
        deadline = time.time() + 30
        while True:
            try:
                r = httpx.get(f"{BASE}/health", timeout=2.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("server failed to start")
            time.sleep(0.3)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def register(org: str, username: str, password: str = "pw12345") -> dict:
    r = client.post("/auth/register",
                    json={"org_name": org, "username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def login(org: str, username: str, password: str = "pw12345") -> dict:
    r = client.post("/auth/login",
                    json={"org_name": org, "username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def new_user(org: str, username: str) -> dict:
    """Register + login; returns {token, refresh, user_id, org_id, role}."""
    reg = register(org, username)
    tok = login(org, username)
    return {
        "token": tok["access_token"],
        "refresh": tok["refresh_token"],
        "user_id": reg["user_id"],
        "org_id": reg["org_id"],
        "role": reg["role"],
    }


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_room(token: str, rate: int = 500, name: str = "Room") -> int:
    r = client.post("/rooms",
                    json={"name": name, "capacity": 4, "hourly_rate_cents": rate},
                    headers=H(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def hours_from_now(h: float) -> datetime:
    """Aware UTC datetime h hours from now, snapped to the minute."""
    return (utc_now() + timedelta(hours=h)).replace(second=0)


def naive_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def book(token: str, room_id: int, start: datetime, end: datetime):
    return client.post("/bookings",
                       json={"room_id": room_id,
                             "start_time": naive_iso(start),
                             "end_time": naive_iso(end)},
                       headers=H(token))


def book_ok(token: str, room_id: int, start: datetime, end: datetime) -> dict:
    r = book(token, room_id, start, end)
    assert r.status_code == 201, r.text
    data = r.json()
    ALL_REFERENCE_CODES.append(data["reference_code"])
    return data


def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# rule 15 + auth basics
# --------------------------------------------------------------------------- #

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_register_roles_duplicates_and_cross_org_username():
    org_a, org_b = uniq("rega"), uniq("regb")
    first = register(org_a, "alice")
    assert first["role"] == "admin"
    second = register(org_a, "bob")
    assert second["role"] == "member"

    dup = client.post("/auth/register",
                      json={"org_name": org_a, "username": "alice", "password": "x"})
    assert dup.status_code == 409
    assert dup.json()["code"] == "USERNAME_TAKEN"
    assert isinstance(dup.json()["detail"], str)

    other = register(org_b, "alice")  # same username, different org: allowed
    assert other["role"] == "admin"
    assert other["org_id"] != first["org_id"]


def test_login_bad_credentials():
    org = uniq("login")
    register(org, "u1")
    bad_pw = client.post("/auth/login",
                         json={"org_name": org, "username": "u1", "password": "wrong"})
    assert bad_pw.status_code == 401
    assert bad_pw.json()["code"] == "INVALID_CREDENTIALS"
    bad_org = client.post("/auth/login",
                          json={"org_name": uniq("ghost"), "username": "u1", "password": "pw12345"})
    assert bad_org.status_code == 401
    assert bad_org.json()["code"] == "INVALID_CREDENTIALS"


# --------------------------------------------------------------------------- #
# rule 8: JWT claims, expiry, logout, refresh rotation
# --------------------------------------------------------------------------- #

def test_jwt_claims_and_lifetimes():
    org = uniq("jwt")
    u = new_user(org, "admin")
    access = jwt.decode(u["token"], SECRET, algorithms=["HS256"])
    refresh = jwt.decode(u["refresh"], SECRET, algorithms=["HS256"])

    for claims, typ in ((access, "access"), (refresh, "refresh")):
        assert claims["sub"] == str(u["user_id"])
        assert isinstance(claims["sub"], str)
        assert claims["org"] == u["org_id"]
        assert claims["role"] == "admin"
        assert claims["jti"]
        assert claims["type"] == typ
    assert access["exp"] - access["iat"] == 900
    assert refresh["exp"] - refresh["iat"] == 7 * 86400
    assert access["jti"] != refresh["jti"]


def test_token_type_enforcement_and_bad_tokens():
    org = uniq("toktype")
    u = new_user(org, "admin")
    # refresh token used as access token -> 401
    r = client.get("/rooms", headers=H(u["refresh"]))
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    # access token used at /auth/refresh -> 401
    r = client.post("/auth/refresh", json={"refresh_token": u["token"]})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    # garbage / missing token -> 401
    assert client.get("/rooms", headers=H("not.a.jwt")).status_code == 401
    assert client.get("/rooms").status_code == 401


def test_logout_blacklists_only_presented_token():
    org = uniq("logout")
    new_user(org, "admin")
    tok1 = login(org, "admin")["access_token"]
    tok2 = login(org, "admin")["access_token"]
    assert client.get("/rooms", headers=H(tok1)).status_code == 200

    assert client.post("/auth/logout", headers=H(tok1)).status_code == 200
    r = client.get("/rooms", headers=H(tok1))
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"  # revoked
    assert client.get("/rooms", headers=H(tok2)).status_code == 200  # untouched


def test_refresh_rotation_single_use():
    org = uniq("rot")
    u = new_user(org, "admin")
    first = client.post("/auth/refresh", json={"refresh_token": u["refresh"]})
    assert first.status_code == 200
    pair = first.json()
    assert pair["token_type"] == "bearer"
    assert client.get("/rooms", headers=H(pair["access_token"])).status_code == 200

    reuse = client.post("/auth/refresh", json={"refresh_token": u["refresh"]})
    assert reuse.status_code == 401 and reuse.json()["code"] == "UNAUTHORIZED"  # single-use

    second = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert second.status_code == 200  # rotated token works once


# --------------------------------------------------------------------------- #
# rule 1: datetime handling
# --------------------------------------------------------------------------- #

def test_offset_input_converted_to_utc_and_responses_utc():
    org = uniq("tz")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=300)

    start_utc = hours_from_now(30).replace(minute=0)
    end_utc = start_utc + timedelta(hours=2)
    plus6 = timezone(timedelta(hours=6))
    r = client.post("/bookings",
                    json={"room_id": room,
                          "start_time": start_utc.astimezone(plus6).isoformat(),
                          "end_time": end_utc.astimezone(plus6).isoformat()},
                    headers=H(u["token"]))
    assert r.status_code == 201, r.text
    data = r.json()
    ALL_REFERENCE_CODES.append(data["reference_code"])

    for field, expected in (("start_time", start_utc), ("end_time", end_utc)):
        parsed = datetime.fromisoformat(data[field])
        assert parsed.utcoffset() == timedelta(0), f"{field} lacks explicit UTC designator: {data[field]}"
        assert parsed == expected, f"{field}: {parsed} != {expected} (offset not converted)"
    created = datetime.fromisoformat(data["created_at"])
    assert created.utcoffset() == timedelta(0)
    assert data["price_cents"] == 300 * 2


def test_naive_input_treated_as_utc():
    org = uniq("naive")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=100)
    start = hours_from_now(31).replace(minute=0)
    data = book_ok(u["token"], room, start, start + timedelta(hours=1))
    assert datetime.fromisoformat(data["start_time"]) == start


# --------------------------------------------------------------------------- #
# rule 2: booking window / price validation
# --------------------------------------------------------------------------- #

def test_booking_window_validation():
    org = uniq("valid")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=250)

    def expect_invalid(start, end):
        r = book(u["token"], room, start, end)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INVALID_BOOKING_WINDOW"

    past = utc_now() - timedelta(hours=2)
    expect_invalid(past, past + timedelta(hours=1))            # past start
    s = hours_from_now(40).replace(minute=0)
    expect_invalid(s, s)                                       # end == start
    expect_invalid(s, s - timedelta(hours=1))                  # end before start
    expect_invalid(s, s + timedelta(minutes=90))               # non-whole hours
    expect_invalid(s, s + timedelta(hours=9))                  # > 8 hours

    ok = book_ok(u["token"], room, s, s + timedelta(hours=8))  # 8h allowed
    assert ok["price_cents"] == 250 * 8


# --------------------------------------------------------------------------- #
# rule 3: overlap + back-to-back + concurrency
# --------------------------------------------------------------------------- #

def test_overlap_and_back_to_back():
    org = uniq("overlap")
    u = new_user(org, "admin")
    room = make_room(u["token"])
    s = hours_from_now(50).replace(minute=0)

    book_ok(u["token"], room, s, s + timedelta(hours=2))
    conflict = book(u["token"], room, s + timedelta(hours=1), s + timedelta(hours=3))
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ROOM_CONFLICT"
    # exact boundary: new.start == existing.end -> allowed
    b2 = book_ok(u["token"], room, s + timedelta(hours=2), s + timedelta(hours=3))
    assert b2["status"] == "confirmed"
    # new.end == existing.start -> allowed
    book_ok(u["token"], room, s - timedelta(hours=1), s)


def test_concurrent_double_booking_exactly_one_wins():
    org = uniq("race")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=700)
    s = hours_from_now(60).replace(minute=0)

    def attempt(_):
        return book(u["token"], room, s, s + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    codes = [r.status_code for r in results]
    assert codes.count(201) == 1, f"expected exactly 1 success, got {codes}"
    assert all(c == 409 for c in codes if c != 201)
    for r in results:
        if r.status_code == 201:
            ALL_REFERENCE_CODES.append(r.json()["reference_code"])
        else:
            assert r.json()["code"] == "ROOM_CONFLICT"

    stats = client.get(f"/rooms/{room}/stats", headers=H(u["token"])).json()
    assert stats["total_confirmed_bookings"] == 1
    assert stats["total_revenue_cents"] == 700


# --------------------------------------------------------------------------- #
# rule 4: quota
# --------------------------------------------------------------------------- #

def test_quota_sequential_and_beyond_window():
    org = uniq("quota")
    adm = new_user(org, "admin")
    mem = new_user(org, "member")
    room = make_room(adm["token"])

    starts = [hours_from_now(2 + i).replace(minute=0) for i in range(4)]
    for s in starts[:3]:
        book_ok(mem["token"], room, s, s + timedelta(hours=1))
    over = book(mem["token"], room, starts[3], starts[3] + timedelta(hours=1))
    assert over.status_code == 409
    assert over.json()["code"] == "QUOTA_EXCEEDED"

    # beyond the 24h window: allowed
    far = hours_from_now(26).replace(minute=0)
    book_ok(mem["token"], room, far, far + timedelta(hours=1))

    # cancelled bookings do not count: cancel one and book again
    lst = client.get("/bookings", headers=H(mem["token"])).json()
    within = [b for b in lst["items"]
              if datetime.fromisoformat(b["start_time"]) < utc_now() + timedelta(hours=24)
              and b["status"] == "confirmed"]
    r = client.post(f"/bookings/{within[0]['id']}/cancel", headers=H(mem["token"]))
    assert r.status_code == 200
    retry = book(mem["token"], room, starts[3], starts[3] + timedelta(hours=1))
    assert retry.status_code == 201, retry.text
    ALL_REFERENCE_CODES.append(retry.json()["reference_code"])


def test_quota_concurrent_exactly_three_succeed():
    org = uniq("quotarace")
    adm = new_user(org, "admin")
    mem = new_user(org, "member")
    room = make_room(adm["token"])
    starts = [hours_from_now(2 + i).replace(minute=0) for i in range(10)]

    def attempt(s):
        return book(mem["token"], room, s, s + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, starts))

    codes = [r.status_code for r in results]
    assert codes.count(201) == 3, f"expected exactly 3 successes, got {codes}"
    for r in results:
        if r.status_code == 201:
            ALL_REFERENCE_CODES.append(r.json()["reference_code"])
        else:
            assert r.status_code == 409
            assert r.json()["code"] == "QUOTA_EXCEEDED"


# --------------------------------------------------------------------------- #
# rule 5: rate limit
# --------------------------------------------------------------------------- #

def test_rate_limit_25_requests_exactly_5_rejected():
    org = uniq("rate")
    u = new_user(org, "admin")
    room = make_room(u["token"])
    past = utc_now() - timedelta(hours=3)  # invalid window: fails fast but still counts

    def attempt(_):
        return book(u["token"], room, past, past + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(attempt, range(25)))

    codes = [r.status_code for r in results]
    assert codes.count(429) == 5, f"expected 5 rate-limited, got {codes}"
    assert codes.count(400) == 20
    for r in results:
        if r.status_code == 429:
            assert r.json()["code"] == "RATE_LIMITED"


def test_rate_limit_is_per_user():
    org = uniq("rateuser")
    u1 = new_user(org, "admin")
    u2 = new_user(org, "member")
    room = make_room(u1["token"])
    past = utc_now() - timedelta(hours=3)

    for _ in range(21):
        last = book(u1["token"], room, past, past + timedelta(hours=1))
    assert last.status_code == 429
    # different user unaffected
    other = book(u2["token"], room, past, past + timedelta(hours=1))
    assert other.status_code == 400


# --------------------------------------------------------------------------- #
# rule 6: cancellation refunds
# --------------------------------------------------------------------------- #

def test_refund_tiers_and_half_cent_rounding():
    org = uniq("refund")
    adm = new_user(org, "admin")
    mem = new_user(org, "member")
    room_odd = make_room(adm["token"], rate=101)  # odd price for rounding check
    room = make_room(adm["token"], rate=400)

    # >= 48h notice -> 100%
    s = hours_from_now(72).replace(minute=0)
    b100 = book_ok(mem["token"], room, s, s + timedelta(hours=2))
    r = client.post(f"/bookings/{b100['id']}/cancel", headers=H(mem["token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["refund_percent"] == 100
    assert body["refund_amount_cents"] == 800

    # 24-48h notice -> 50%, 101 cents -> 50.5 -> rounds UP to 51
    s = hours_from_now(30).replace(minute=0)
    b50 = book_ok(mem["token"], room_odd, s, s + timedelta(hours=1))
    assert b50["price_cents"] == 101
    r = client.post(f"/bookings/{b50['id']}/cancel", headers=H(mem["token"]))
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 50
    assert r.json()["refund_amount_cents"] == 51

    # response amount == stored RefundLog amount
    detail = client.get(f"/bookings/{b50['id']}", headers=H(mem["token"])).json()
    assert len(detail["refunds"]) == 1
    assert detail["refunds"][0]["amount_cents"] == 51
    assert detail["refunds"][0]["status"] == "processed"
    assert datetime.fromisoformat(detail["refunds"][0]["processed_at"]).utcoffset() == timedelta(0)

    # < 24h notice -> 0%
    s = hours_from_now(2).replace(minute=0)
    b0 = book_ok(mem["token"], room, s, s + timedelta(hours=1))
    r = client.post(f"/bookings/{b0['id']}/cancel", headers=H(mem["token"]))
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 0
    assert r.json()["refund_amount_cents"] == 0

    # already cancelled -> 409
    again = client.post(f"/bookings/{b0['id']}/cancel", headers=H(mem["token"]))
    assert again.status_code == 409
    assert again.json()["code"] == "ALREADY_CANCELLED"


def test_cancel_permissions():
    org = uniq("cperm")
    adm = new_user(org, "admin")
    m1 = new_user(org, "m1")
    m2 = new_user(org, "m2")
    room = make_room(adm["token"])
    s = hours_from_now(70).replace(minute=0)
    b = book_ok(m1["token"], room, s, s + timedelta(hours=1))

    r = client.post(f"/bookings/{b['id']}/cancel", headers=H(m2["token"]))
    assert r.status_code == 404
    assert r.json()["code"] == "BOOKING_NOT_FOUND"

    r = client.post(f"/bookings/{b['id']}/cancel", headers=H(adm["token"]))
    assert r.status_code == 200  # same-org admin may cancel


def test_concurrent_cancel_one_success_one_refund_log():
    org = uniq("crace")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=333)
    s = hours_from_now(72).replace(minute=0)
    b = book_ok(u["token"], room, s, s + timedelta(hours=1))

    def attempt(_):
        return client.post(f"/bookings/{b['id']}/cancel", headers=H(u["token"]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))

    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, f"expected exactly 1 success, got {codes}"
    assert all(c == 409 for c in codes if c != 200)
    winner = next(r for r in results if r.status_code == 200)
    assert winner.json()["refund_amount_cents"] == 333  # 100%

    detail = client.get(f"/bookings/{b['id']}", headers=H(u["token"])).json()
    assert len(detail["refunds"]) == 1, "must have exactly one RefundLog"
    assert detail["refunds"][0]["amount_cents"] == 333


# --------------------------------------------------------------------------- #
# rule 7: reference code uniqueness under concurrent creation
# --------------------------------------------------------------------------- #

def test_reference_codes_unique_under_concurrent_creation():
    org = uniq("refcode")
    u = new_user(org, "admin")
    rooms = [make_room(u["token"], name=f"R{i}") for i in range(8)]
    s = hours_from_now(80).replace(minute=0)

    def attempt(room_id):
        return book(u["token"], room_id, s, s + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, rooms))

    codes = []
    for r in results:
        assert r.status_code == 201, r.text
        codes.append(r.json()["reference_code"])
    ALL_REFERENCE_CODES.extend(codes)
    assert len(set(codes)) == len(codes), f"duplicate reference codes: {codes}"


# --------------------------------------------------------------------------- #
# rules 9/10: multi-tenancy and booking visibility
# --------------------------------------------------------------------------- #

def test_multi_tenancy_cross_org_404():
    org_a, org_b = uniq("tenA"), uniq("tenB")
    a_adm = new_user(org_a, "admin")
    b_adm = new_user(org_b, "admin")
    a_room = make_room(a_adm["token"])
    s = hours_from_now(55).replace(minute=0)
    a_booking = book_ok(a_adm["token"], a_room, s, s + timedelta(hours=1))
    day = s.date().isoformat()

    t = b_adm["token"]
    r = client.get(f"/rooms/{a_room}/availability", params={"date": day}, headers=H(t))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"
    r = client.get(f"/rooms/{a_room}/stats", headers=H(t))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"
    r = book(t, a_room, s + timedelta(hours=2), s + timedelta(hours=3))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"
    r = client.get(f"/bookings/{a_booking['id']}", headers=H(t))
    assert r.status_code == 404 and r.json()["code"] == "BOOKING_NOT_FOUND"
    r = client.post(f"/bookings/{a_booking['id']}/cancel", headers=H(t))
    assert r.status_code == 404 and r.json()["code"] == "BOOKING_NOT_FOUND"
    r = client.get("/admin/export", params={"room_id": a_room}, headers=H(t))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"

    # org B's room list and usage report contain only org B rooms
    assert client.get("/rooms", headers=H(t)).json() == []
    rep = client.get("/admin/usage-report",
                     params={"from": day, "to": day}, headers=H(t)).json()
    assert rep["rooms"] == []


def test_member_visibility_and_admin_access():
    org = uniq("vis")
    adm = new_user(org, "admin")
    m1 = new_user(org, "m1")
    m2 = new_user(org, "m2")
    room = make_room(adm["token"])
    s = hours_from_now(58).replace(minute=0)
    b = book_ok(m1["token"], room, s, s + timedelta(hours=1))

    r = client.get(f"/bookings/{b['id']}", headers=H(m2["token"]))
    assert r.status_code == 404 and r.json()["code"] == "BOOKING_NOT_FOUND"
    assert client.get(f"/bookings/{b['id']}", headers=H(m1["token"])).status_code == 200
    assert client.get(f"/bookings/{b['id']}", headers=H(adm["token"])).status_code == 200
    # GET /bookings lists only the caller's own bookings
    assert client.get("/bookings", headers=H(m2["token"])).json()["total"] == 0


def test_admin_only_endpoints_forbidden_for_members():
    org = uniq("forb")
    new_user(org, "admin")
    mem = new_user(org, "member")
    t = mem["token"]
    r = client.post("/rooms", json={"name": "X", "capacity": 1, "hourly_rate_cents": 100},
                    headers=H(t))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    r = client.get("/admin/usage-report",
                   params={"from": "2026-07-10", "to": "2026-07-10"}, headers=H(t))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    r = client.get("/admin/export", headers=H(t))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


# --------------------------------------------------------------------------- #
# rule 11: pagination
# --------------------------------------------------------------------------- #

def test_pagination_ordering_and_no_skips():
    org = uniq("page")
    u = new_user(org, "admin")
    room = make_room(u["token"])
    starts = [hours_from_now(48 + i).replace(minute=0) for i in range(7)]
    created = [book_ok(u["token"], room, s, s + timedelta(hours=1)) for s in starts]

    default = client.get("/bookings", headers=H(u["token"])).json()
    assert default["page"] == 1 and default["limit"] == 10 and default["total"] == 7
    assert len(default["items"]) == 7

    seen = []
    for page in (1, 2, 3):
        body = client.get("/bookings", params={"page": page, "limit": 3},
                          headers=H(u["token"])).json()
        assert body["total"] == 7
        assert body["page"] == page and body["limit"] == 3
        seen.extend(body["items"])
    assert len(seen) == 7
    assert [b["id"] for b in seen] == sorted(b["id"] for b in created), "skips/repeats across pages"
    times = [b["start_time"] for b in seen]
    assert times == sorted(times), "not ascending by start_time"

    beyond = client.get("/bookings", params={"page": 4, "limit": 3}, headers=H(u["token"])).json()
    assert beyond["items"] == [] and beyond["total"] == 7


# --------------------------------------------------------------------------- #
# rules 12/13: usage report + availability freshness (cache invalidation)
# --------------------------------------------------------------------------- #

def test_usage_report_zero_rooms_ranges_and_freshness():
    org = uniq("report")
    adm = new_user(org, "admin")
    r1 = make_room(adm["token"], rate=600, name="R1")
    r2 = make_room(adm["token"], rate=600, name="R2")  # stays zero-booking

    s1 = hours_from_now(26).replace(minute=0)
    d0 = s1.date().isoformat()
    s2 = s1 + timedelta(hours=24)
    d1 = s2.date().isoformat()

    # prime the cache BEFORE any booking exists
    rep = client.get("/admin/usage-report", params={"from": d0, "to": d0},
                     headers=H(adm["token"])).json()
    assert {r["room_id"]: r["confirmed_bookings"] for r in rep["rooms"]} == {r1: 0, r2: 0}

    b1 = book_ok(adm["token"], r1, s1, s1 + timedelta(hours=2))  # on d0
    book_ok(adm["token"], r1, s2, s2 + timedelta(hours=1))       # on d1

    # must reflect the new booking immediately (cache invalidated on create)
    rep = client.get("/admin/usage-report", params={"from": d0, "to": d0},
                     headers=H(adm["token"])).json()
    by_room = {r["room_id"]: r for r in rep["rooms"]}
    assert by_room[r1]["confirmed_bookings"] == 1
    assert by_room[r1]["revenue_cents"] == 1200
    assert by_room[r2]["confirmed_bookings"] == 0  # zero-booking room included
    assert by_room[r2]["room_name"] == "R2"

    # inclusive 'to' boundary: booking starting on d1 counted when to=d1
    rep = client.get("/admin/usage-report", params={"from": d0, "to": d1},
                     headers=H(adm["token"])).json()
    by_room = {r["room_id"]: r for r in rep["rooms"]}
    assert by_room[r1]["confirmed_bookings"] == 2
    assert by_room[r1]["revenue_cents"] == 1200 + 600

    # cancelled bookings drop out immediately (cache invalidated on cancel)
    assert client.post(f"/bookings/{b1['id']}/cancel", headers=H(adm["token"])).status_code == 200
    rep = client.get("/admin/usage-report", params={"from": d0, "to": d0},
                     headers=H(adm["token"])).json()
    by_room = {r["room_id"]: r for r in rep["rooms"]}
    assert by_room[r1]["confirmed_bookings"] == 0
    assert by_room[r1]["revenue_cents"] == 0

    # a newly created room appears immediately (cache invalidated on room create)
    r3 = make_room(adm["token"], name="R3")
    rep = client.get("/admin/usage-report", params={"from": d0, "to": d0},
                     headers=H(adm["token"])).json()
    assert r3 in {r["room_id"] for r in rep["rooms"]}


def test_availability_sorted_and_fresh():
    org = uniq("avail")
    u = new_user(org, "admin")
    room = make_room(u["token"])
    s = hours_from_now(49).replace(minute=0)
    day = s.date().isoformat()
    if (s + timedelta(hours=6)).date().isoformat() != day:
        s = s + timedelta(hours=6)   # keep all slots on one UTC date
        day = s.date().isoformat()

    # prime cache while empty
    body = client.get(f"/rooms/{room}/availability", params={"date": day},
                      headers=H(u["token"])).json()
    assert body == {"room_id": room, "date": day, "busy": []}

    b_late = book_ok(u["token"], room, s + timedelta(hours=2), s + timedelta(hours=3))
    b_early = book_ok(u["token"], room, s, s + timedelta(hours=1))

    body = client.get(f"/rooms/{room}/availability", params={"date": day},
                      headers=H(u["token"])).json()
    busy = body["busy"]
    assert len(busy) == 2, f"stale availability cache: {busy}"
    assert busy[0]["start_time"] < busy[1]["start_time"], "not sorted ascending"
    for iv in busy:
        assert datetime.fromisoformat(iv["start_time"]).utcoffset() == timedelta(0)

    # cancellation reflected immediately
    assert client.post(f"/bookings/{b_early['id']}/cancel", headers=H(u["token"])).status_code == 200
    body = client.get(f"/rooms/{room}/availability", params={"date": day},
                      headers=H(u["token"])).json()
    assert len(body["busy"]) == 1
    assert body["busy"][0]["start_time"] == datetime.fromisoformat(b_late["start_time"]).isoformat()


# --------------------------------------------------------------------------- #
# rule 14: stats consistency after concurrent bursts
# --------------------------------------------------------------------------- #

def test_stats_consistent_after_concurrent_burst():
    org = uniq("stats")
    u = new_user(org, "admin")
    room = make_room(u["token"], rate=500)
    slots = [hours_from_now(30 + i).replace(minute=0) for i in range(6)]

    def create(s):
        return book(u["token"], room, s, s + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create, slots))
    ids = []
    for r in results:
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
        ALL_REFERENCE_CODES.append(r.json()["reference_code"])

    stats = client.get(f"/rooms/{room}/stats", headers=H(u["token"])).json()
    assert stats["total_confirmed_bookings"] == 6
    assert stats["total_revenue_cents"] == 6 * 500

    def cancel(bid):
        return client.post(f"/bookings/{bid}/cancel", headers=H(u["token"]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        cres = list(pool.map(cancel, ids[:2]))
    assert all(r.status_code == 200 for r in cres)

    stats = client.get(f"/rooms/{room}/stats", headers=H(u["token"])).json()
    assert stats["total_confirmed_bookings"] == 4
    assert stats["total_revenue_cents"] == 4 * 500


# --------------------------------------------------------------------------- #
# rule 16: liveness under mixed concurrent creates + cancels (deadlock check)
# --------------------------------------------------------------------------- #

def test_liveness_mixed_creates_and_cancels():
    org = uniq("live")
    u = new_user(org, "admin")
    room_a = make_room(u["token"], name="A")
    room_b = make_room(u["token"], name="B")
    pre = [book_ok(u["token"], room_b, s, s + timedelta(hours=1))
           for s in (hours_from_now(40 + i).replace(minute=0) for i in range(4))]
    slots = [hours_from_now(50 + i).replace(minute=0) for i in range(4)]

    def create(s):
        return book(u["token"], room_a, s, s + timedelta(hours=1))

    def cancel(b):
        return client.post(f"/bookings/{b['id']}/cancel", headers=H(u["token"]))

    start = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        create_futs = [pool.submit(create, s) for s in slots]
        cancel_futs = [pool.submit(cancel, b) for b in pre]
        results = [f.result(timeout=60) for f in create_futs + cancel_futs]
    elapsed = time.time() - start

    for r in results[:4]:
        assert r.status_code == 201, r.text
        ALL_REFERENCE_CODES.append(r.json()["reference_code"])
    for r in results[4:]:
        assert r.status_code == 200, r.text
    assert elapsed < 60, f"requests took {elapsed:.1f}s - possible deadlock"
    assert client.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
# CSV export contract
# --------------------------------------------------------------------------- #

def test_export_header_and_scoping():
    org = uniq("csv")
    adm = new_user(org, "admin")
    r1 = make_room(adm["token"], name="R1")
    r2 = make_room(adm["token"], name="R2")
    s = hours_from_now(90).replace(minute=0)
    b1 = book_ok(adm["token"], r1, s, s + timedelta(hours=1))
    b2 = book_ok(adm["token"], r2, s, s + timedelta(hours=1))

    resp = client.get("/admin/export", headers=H(adm["token"]))
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    assert lines[0] == CSV_HEADER, f"header mismatch: {lines[0]!r}"
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    ids = {int(r["id"]) for r in rows}
    assert {b1["id"], b2["id"]} <= ids
    # every row belongs to this org's rooms (no cross-org leak)
    assert all(int(r["room_id"]) in (r1, r2) for r in rows)

    # room filter
    resp = client.get("/admin/export", params={"room_id": r1}, headers=H(adm["token"]))
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert {int(r["room_id"]) for r in rows} == {r1}
    # include_all accepted
    assert client.get("/admin/export", params={"include_all": "true"},
                      headers=H(adm["token"])).status_code == 200


# --------------------------------------------------------------------------- #
# global reference-code uniqueness (must run last)
# --------------------------------------------------------------------------- #

def test_zz_all_reference_codes_globally_unique():
    assert len(ALL_REFERENCE_CODES) > 25
    dupes = [c for c in set(ALL_REFERENCE_CODES) if ALL_REFERENCE_CODES.count(c) > 1]
    assert not dupes, f"duplicate reference codes: {dupes}"
