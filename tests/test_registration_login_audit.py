import uuid

import jwt
from fastapi.testclient import TestClient

from app.auth import verify_password
from app.config import JWT_SECRET
from app.database import SessionLocal
from app.main import app
from app.models import Organization, User

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _register(org: str, username: str, password: str = "pw12345"):
    return client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": password},
    )


def _login(org: str, username: str, password: str = "pw12345"):
    return client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": password},
    )


def test_registration_roles_response_fields_and_username_scope():
    org_a = _unique("reg-audit-a")
    org_b = _unique("reg-audit-b")

    first = _register(org_a, "alice")
    assert first.status_code == 201, first.text
    assert set(first.json()) == {"user_id", "org_id", "username", "role"}
    assert first.json()["username"] == "alice"
    assert first.json()["role"] == "admin"

    second = _register(org_a, "bob")
    assert second.status_code == 201, second.text
    assert second.json()["org_id"] == first.json()["org_id"]
    assert second.json()["role"] == "member"

    same_username_other_org = _register(org_b, "alice")
    assert same_username_other_org.status_code == 201, same_username_other_org.text
    assert same_username_other_org.json()["org_id"] != first.json()["org_id"]
    assert same_username_other_org.json()["role"] == "admin"


def test_duplicate_username_within_same_org_returns_username_taken():
    org = _unique("dup-audit")
    assert _register(org, "alice").status_code == 201

    duplicate = _register(org, "alice", password="different")

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_TAKEN"
    assert isinstance(duplicate.json()["detail"], str)


def test_login_success_shape_and_password_hashing():
    org = _unique("login-audit")
    password = "correct-password"
    registered = _register(org, "alice", password=password)
    assert registered.status_code == 201, registered.text

    login = _login(org, "alice", password=password)
    assert login.status_code == 200, login.text
    assert set(login.json()) == {"access_token", "refresh_token", "token_type"}
    assert login.json()["token_type"] == "bearer"

    access_claims = jwt.decode(
        login.json()["access_token"],
        JWT_SECRET,
        algorithms=["HS256"],
    )
    assert access_claims["sub"] == str(registered.json()["user_id"])
    assert access_claims["org"] == registered.json()["org_id"]
    assert access_claims["role"] == "admin"

    db = SessionLocal()
    try:
        stored = (
            db.query(User)
            .join(Organization, User.org_id == Organization.id)
            .filter(Organization.name == org, User.username == "alice")
            .one()
        )
        assert stored.hashed_password != password
        assert ":" in stored.hashed_password
        assert verify_password(password, stored.hashed_password)
        assert not verify_password("wrong-password", stored.hashed_password)
    finally:
        db.close()


def test_bad_login_inputs_share_invalid_credentials_response():
    org = _unique("bad-login-audit")
    assert _register(org, "alice", password="correct").status_code == 201

    responses = [
        _login(_unique("missing-org"), "alice", password="correct"),
        _login(org, "missing-user", password="correct"),
        _login(org, "alice", password="wrong"),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.json()["code"] == "INVALID_CREDENTIALS"
        assert response.json()["detail"] == "Invalid username or password"


def test_login_is_scoped_by_organization_name():
    org_a = _unique("scope-audit-a")
    org_b = _unique("scope-audit-b")
    a = _register(org_a, "shared", password="password-a")
    b = _register(org_b, "shared", password="password-b")
    assert a.status_code == 201
    assert b.status_code == 201

    wrong_cross_org_password = _login(org_a, "shared", password="password-b")
    assert wrong_cross_org_password.status_code == 401
    assert wrong_cross_org_password.json()["code"] == "INVALID_CREDENTIALS"

    login_a = _login(org_a, "shared", password="password-a")
    login_b = _login(org_b, "shared", password="password-b")
    assert login_a.status_code == 200, login_a.text
    assert login_b.status_code == 200, login_b.text

    claims_a = jwt.decode(login_a.json()["access_token"], JWT_SECRET, algorithms=["HS256"])
    claims_b = jwt.decode(login_b.json()["access_token"], JWT_SECRET, algorithms=["HS256"])
    assert claims_a["sub"] == str(a.json()["user_id"])
    assert claims_a["org"] == a.json()["org_id"]
    assert claims_b["sub"] == str(b.json()["user_id"])
    assert claims_b["org"] == b.json()["org_id"]
