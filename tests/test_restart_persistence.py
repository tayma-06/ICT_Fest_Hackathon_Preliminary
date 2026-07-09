"""Restart-focused black-box regressions for persistent state."""
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"


def _start_server(db_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite:///" + str(db_path).replace("\\", "/"),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/health", timeout=1.0).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("server failed to start")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _future_iso(hours: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).replace(minute=0, second=0, microsecond=0, tzinfo=None).isoformat()


def test_stats_and_token_revocations_survive_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    org = f"restart-{uuid.uuid4().hex}"

    proc = _start_server(db_path)
    try:
        registered = httpx.post(
            f"{BASE}/auth/register",
            json={"org_name": org, "username": "admin", "password": "pw12345"},
            timeout=10.0,
        )
        assert registered.status_code == 201, registered.text

        logged_in = httpx.post(
            f"{BASE}/auth/login",
            json={"org_name": org, "username": "admin", "password": "pw12345"},
            timeout=10.0,
        )
        assert logged_in.status_code == 200, logged_in.text
        token_pair = logged_in.json()
        token = token_pair["access_token"]
        refresh_token = token_pair["refresh_token"]
        headers = {"Authorization": f"Bearer {token}"}

        room = httpx.post(
            f"{BASE}/rooms",
            json={"name": "Restart Room", "capacity": 2, "hourly_rate_cents": 500},
            headers=headers,
            timeout=10.0,
        )
        assert room.status_code == 201, room.text
        room_id = room.json()["id"]

        booked = httpx.post(
            f"{BASE}/bookings",
            json={
                "room_id": room_id,
                "start_time": _future_iso(72),
                "end_time": _future_iso(74),
            },
            headers=headers,
            timeout=20.0,
        )
        assert booked.status_code == 201, booked.text
        booking_id = booked.json()["id"]

        stats = httpx.get(f"{BASE}/rooms/{room_id}/stats", headers=headers, timeout=10.0)
        assert stats.json()["total_confirmed_bookings"] == 1
        assert stats.json()["total_revenue_cents"] == 1000

        rotated = httpx.post(
            f"{BASE}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=10.0,
        )
        assert rotated.status_code == 200, rotated.text

        logged_out = httpx.post(f"{BASE}/auth/logout", headers=headers, timeout=10.0)
        assert logged_out.status_code == 200, logged_out.text
    finally:
        _stop_server(proc)

    proc = _start_server(db_path)
    try:
        assert httpx.get(f"{BASE}/rooms", headers=headers, timeout=10.0).status_code == 401
        reused = httpx.post(
            f"{BASE}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=10.0,
        )
        assert reused.status_code == 401, reused.text

        logged_in = httpx.post(
            f"{BASE}/auth/login",
            json={"org_name": org, "username": "admin", "password": "pw12345"},
            timeout=10.0,
        )
        assert logged_in.status_code == 200, logged_in.text
        new_headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}

        stats = httpx.get(
            f"{BASE}/rooms/{room_id}/stats",
            headers=new_headers,
            timeout=10.0,
        )
        assert stats.json()["total_confirmed_bookings"] == 1
        assert stats.json()["total_revenue_cents"] == 1000

        cancelled = httpx.post(
            f"{BASE}/bookings/{booking_id}/cancel",
            headers=new_headers,
            timeout=20.0,
        )
        assert cancelled.status_code == 200, cancelled.text

        stats = httpx.get(
            f"{BASE}/rooms/{room_id}/stats",
            headers=new_headers,
            timeout=10.0,
        )
        assert stats.json()["total_confirmed_bookings"] == 0
        assert stats.json()["total_revenue_cents"] == 0
    finally:
        _stop_server(proc)
