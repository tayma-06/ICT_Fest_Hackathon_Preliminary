"""Authentication: password hashing, JWT issue/verify, request dependencies."""
import hashlib
import hmac
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from .database import get_db
from .errors import AppError
from .models import User

# Access tokens presented to /auth/logout are recorded here so they can no
# longer be used. Used refresh tokens are recorded here as well so each
# refresh token can only be redeemed once.
_revoked_tokens: set[str] = set()
_revocation_lock = threading.Lock()

_PBKDF2_ROUNDS = 100_000
_REQUIRED_TOKEN_CLAIMS = ["sub", "org", "role", "jti", "iat", "exp", "type"]


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), dk_hex)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def create_access_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": _REQUIRED_TOKEN_CLAIMS},
        )
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")


def revoke_access_token(payload: dict) -> None:
    jti = token_jti_from_payload(payload)
    with _revocation_lock:
        _revoked_tokens.add(jti)


def consume_refresh_token(payload: dict) -> None:
    """Mark a refresh token's jti as used; reusing it raises 401."""
    jti = token_jti_from_payload(payload)
    with _revocation_lock:
        if jti in _revoked_tokens:
            raise AppError(401, "UNAUTHORIZED", "Refresh token already used")
        _revoked_tokens.add(jti)


def get_token_payload(request: Request) -> dict:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise AppError(401, "UNAUTHORIZED", "Missing bearer token")
    token = header[len("Bearer "):].strip()
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")
    jti = token_jti_from_payload(payload)
    with _revocation_lock:
        revoked = jti in _revoked_tokens
    if revoked:
        raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
    return payload


def token_jti_from_payload(payload: dict) -> str:
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise AppError(401, "UNAUTHORIZED", "Invalid token")
    return jti


def user_id_from_payload(payload: dict) -> int:
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AppError(401, "UNAUTHORIZED", "Invalid token")
    try:
        return int(sub)
    except ValueError:
        raise AppError(401, "UNAUTHORIZED", "Invalid token")


def get_current_user(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
) -> User:
    user_id = user_id_from_payload(payload)
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise AppError(401, "UNAUTHORIZED", "Unknown user")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise AppError(403, "FORBIDDEN", "Admin privileges required")
    return user
