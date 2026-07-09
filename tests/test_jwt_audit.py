import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app

client = TestClient(app)


def _register_user(role_seed: str = "admin") -> dict:
    org = f"jwt-audit-{uuid.uuid4().hex}"
    registered = client.post(
        "/auth/register",
        json={"org_name": org, "username": role_seed, "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"org_name": org, "username": role_seed, "password": "pw12345"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {"registered": registered.json(), "tokens": logged_in.json()}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signed_token(
    user: dict,
    *,
    secret: str = JWT_SECRET,
    token_type: object = "access",
    sub: object | None = None,
    org: object | None = None,
    role: object | None = None,
    jti: object | None = None,
    iat: object | None = None,
    exp: object | None = None,
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": str(user["user_id"]) if sub is None else sub,
        "org": user["org_id"] if org is None else org,
        "role": user["role"] if role is None else role,
        "jti": uuid.uuid4().hex if jti is None else jti,
        "iat": now if iat is None else iat,
        "exp": now + 900 if exp is None else exp,
        "type": token_type,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def test_created_tokens_use_hs256_required_claims_lifetimes_and_unique_jtis():
    org = f"jwt-audit-{uuid.uuid4().hex}"
    registered = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert registered.status_code == 201, registered.text
    user = registered.json()

    login_tokens = []
    for _ in range(4):
        login = client.post(
            "/auth/login",
            json={"org_name": org, "username": "alice", "password": "pw12345"},
        )
        assert login.status_code == 200, login.text
        login_tokens.append(login.json())

    seen_jtis = set()
    for pair in login_tokens:
        for field, expected_type, expected_lifetime in (
            ("access_token", "access", 900),
            ("refresh_token", "refresh", 7 * 86400),
        ):
            token = pair[field]
            header = jwt.get_unverified_header(token)
            assert header["alg"] == "HS256"
            claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            assert set(["sub", "org", "role", "jti", "iat", "exp", "type"]) <= set(claims)
            assert claims["sub"] == str(user["user_id"])
            assert claims["org"] == user["org_id"]
            assert claims["role"] == user["role"]
            assert claims["type"] == expected_type
            assert claims["exp"] - claims["iat"] == expected_lifetime
            assert claims["jti"] not in seen_jtis
            seen_jtis.add(claims["jti"])


def test_expired_and_invalid_signature_tokens_return_401():
    user = _register_user()["registered"]
    now = int(datetime.now(timezone.utc).timestamp())
    expired = _signed_token(user, iat=now - 1000, exp=now - 1)
    bad_signature = _signed_token(user, secret="wrong-secret")

    for token in (expired, bad_signature):
        response = client.get("/rooms", headers=_headers(token))
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"


def test_missing_malformed_or_invalid_authorization_headers_return_401():
    user = _register_user()["registered"]
    valid_token = _signed_token(user)
    bad_headers = [
        {},
        {"Authorization": ""},
        {"Authorization": valid_token},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic " + valid_token},
        {"Authorization": "Bearer not.a.jwt"},
    ]

    for headers in bad_headers:
        response = client.get("/rooms", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"


def test_access_and_refresh_endpoints_reject_wrong_token_types():
    account = _register_user()
    tokens = account["tokens"]

    access_with_refresh = client.get("/rooms", headers=_headers(tokens["refresh_token"]))
    assert access_with_refresh.status_code == 401
    assert access_with_refresh.json()["code"] == "UNAUTHORIZED"

    refresh_with_access = client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["access_token"]},
    )
    assert refresh_with_access.status_code == 401
    assert refresh_with_access.json()["code"] == "UNAUTHORIZED"


def test_invalid_claim_types_or_values_return_401():
    user = _register_user()["registered"]
    bad_claim_tokens = [
        _signed_token(user, sub=user["user_id"]),
        _signed_token(user, org=True),
        _signed_token(user, org=str(user["org_id"])),
        _signed_token(user, role=["admin"]),
        _signed_token(user, role="owner"),
        _signed_token(user, jti=123),
        _signed_token(user, iat="not-an-int"),
        _signed_token(user, token_type="session"),
        _signed_token(user, token_type=["access"]),
    ]

    for token in bad_claim_tokens:
        response = client.get("/rooms", headers=_headers(token))
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"
