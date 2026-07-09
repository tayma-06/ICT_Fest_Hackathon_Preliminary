import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_SECRET
from app.database import SessionLocal
from app.main import app
from app.models import RevokedToken

client = TestClient(app)


def _new_account() -> dict:
    org = f"logout-refresh-{uuid.uuid4().hex}"
    registered = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {"org": org, "registered": registered.json(), "tokens": logged_in.json()}


def _login(org: str) -> dict:
    response = client.post(
        "/auth/login",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claims(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


def _revoked_row(jti: str) -> RevokedToken | None:
    db = SessionLocal()
    try:
        return db.query(RevokedToken).filter(RevokedToken.jti == jti).first()
    finally:
        db.close()


def _stored_exp_timestamp(row: RevokedToken) -> int:
    return int(row.expires_at.replace(tzinfo=timezone.utc).timestamp())


def test_logout_requires_access_token_and_revokes_only_presented_token():
    account = _new_account()
    first_pair = account["tokens"]
    second_pair = _login(account["org"])

    refresh_logout = client.post(
        "/auth/logout",
        headers=_headers(first_pair["refresh_token"]),
    )
    assert refresh_logout.status_code == 401
    assert refresh_logout.json()["code"] == "UNAUTHORIZED"

    logged_out = client.post(
        "/auth/logout",
        headers=_headers(first_pair["access_token"]),
    )
    assert logged_out.status_code == 200
    assert logged_out.json() == {"status": "ok"}

    old_access = client.get("/rooms", headers=_headers(first_pair["access_token"]))
    assert old_access.status_code == 401
    assert old_access.json()["code"] == "UNAUTHORIZED"

    unrelated_access = client.get("/rooms", headers=_headers(second_pair["access_token"]))
    assert unrelated_access.status_code == 200


def test_logout_revocation_is_persisted_until_access_token_expiry():
    access_token = _new_account()["tokens"]["access_token"]
    claims = _claims(access_token)

    response = client.post("/auth/logout", headers=_headers(access_token))
    assert response.status_code == 200, response.text

    row = _revoked_row(claims["jti"])
    assert row is not None
    assert _stored_exp_timestamp(row) == claims["exp"]


def test_refresh_rotates_tokens_and_consumes_old_refresh_token():
    account = _new_account()
    original = account["tokens"]
    original_access_claims = _claims(original["access_token"])
    original_refresh_claims = _claims(original["refresh_token"])

    rotated = client.post(
        "/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert set(body) == {"access_token", "refresh_token", "token_type"}
    assert body["token_type"] == "bearer"

    new_access_claims = _claims(body["access_token"])
    new_refresh_claims = _claims(body["refresh_token"])
    assert new_access_claims["type"] == "access"
    assert new_refresh_claims["type"] == "refresh"
    assert new_access_claims["jti"] not in {
        original_access_claims["jti"],
        original_refresh_claims["jti"],
        new_refresh_claims["jti"],
    }
    assert new_refresh_claims["jti"] not in {
        original_access_claims["jti"],
        original_refresh_claims["jti"],
    }

    old_refresh_reuse = client.post(
        "/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )
    assert old_refresh_reuse.status_code == 401
    assert old_refresh_reuse.json()["code"] == "UNAUTHORIZED"

    authenticated = client.get("/rooms", headers=_headers(body["access_token"]))
    assert authenticated.status_code == 200


def test_refresh_revocation_is_persisted_until_refresh_token_expiry():
    refresh_token = _new_account()["tokens"]["refresh_token"]
    claims = _claims(refresh_token)

    response = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 200, response.text

    row = _revoked_row(claims["jti"])
    assert row is not None
    assert _stored_exp_timestamp(row) == claims["exp"]


def test_concurrent_refresh_of_same_token_has_at_most_one_success():
    refresh_token = _new_account()["tokens"]["refresh_token"]

    def attempt_refresh(_):
        return client.post("/auth/refresh", json={"refresh_token": refresh_token})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt_refresh, range(8)))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1, statuses
    assert statuses.count(401) == 7, statuses

    winners = [response for response in responses if response.status_code == 200]
    winner_body = winners[0].json()
    assert winner_body["token_type"] == "bearer"
    old_refresh_claims = _claims(refresh_token)
    new_access_claims = _claims(winner_body["access_token"])
    new_refresh_claims = _claims(winner_body["refresh_token"])
    assert len(
        {
            old_refresh_claims["jti"],
            new_access_claims["jti"],
            new_refresh_claims["jti"],
        }
    ) == 3

    for response in responses:
        if response.status_code == 401:
            assert response.json()["code"] == "UNAUTHORIZED"
