from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# InvalidHashError doesn't subclass VerificationError, so it needs listing too.
_VERIFY_FAILURES = (VerificationError, InvalidHashError)

_password_hasher = PasswordHasher()

JWT_ALGORITHM = "HS256"

DUMMY_PASSWORD_HASH = _password_hasher.hash("no-such-account-timing-safety-placeholder")


def hash_account_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_account_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except _VERIFY_FAILURES:
        return False


def hash_namespace_passphrase(passphrase: str) -> str:
    return _password_hasher.hash(passphrase)


def verify_namespace_passphrase(passphrase: str, passphrase_hash: str) -> bool:
    try:
        return _password_hasher.verify(passphrase_hash, passphrase)
    except _VERIFY_FAILURES:
        return False


def create_session_token(account_id: int, secret_key: str, expires_delta: timedelta) -> str:
    expires_at = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": str(account_id), "exp": expires_at}
    return jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)


def decode_session_token(token: str, secret_key: str) -> int:
    payload = jwt.decode(
        token,
        secret_key,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as err:
        # Surface as a PyJWTError so callers only need one except clause.
        raise jwt.InvalidTokenError("session subject is not an account id") from err
