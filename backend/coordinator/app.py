from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy.orm import Session

from coordinator.db import get_db, init_db, SessionLocal
from coordinator.models import Account, Settings
from coordinator.schemas import AccountOut, LoginRequest, RegisterRequest
from coordinator.security import (
    DUMMY_PASSWORD_HASH,
    create_session_token,
    decode_session_token,
    hash_account_password,
    hash_namespace_passphrase,
    verify_account_password,
    verify_namespace_passphrase,
)

SESSION_COOKIE_NAME = "session"
SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))

PUBLIC_PATHS = frozenset({"/health", "/register", "/login"})

NAMESPACE_SALT_KEY = "namespace_salt"
NAMESPACE_PASSPHRASE_HASH_KEY = "namespace_passphrase_hash"
JWT_SECRET_KEY_KEY = "jwt_secret_key"


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(Settings, key)
    return row.value if row else None


def get_required_setting(db: Session, key: str) -> str:
    """Missing means the coordinator never finished startup seeding."""
    value = get_setting(db, key)
    if value is None:
        raise HTTPException(status_code=500, detail="Coordinator is not initialized")
    return value


def set_setting(db: Session, key: str, value: str) -> None:
    db.merge(Settings(key=key, value=value))


def seed_settings() -> None:
    db = SessionLocal()
    try:
        if get_setting(db, NAMESPACE_SALT_KEY) is None:
            set_setting(db, NAMESPACE_SALT_KEY, secrets.token_hex(16))

        if get_setting(db, NAMESPACE_PASSPHRASE_HASH_KEY) is None:
            passphrase = os.environ.get("NAMESPACE_PASSPHRASE")
            if not passphrase:
                raise RuntimeError(
                    "NAMESPACE_PASSPHRASE must be set in the environment on first run "
                    "(no namespace-passphrase hash exists yet)."
                )
            set_setting(
                db, NAMESPACE_PASSPHRASE_HASH_KEY, hash_namespace_passphrase(passphrase)
            )

        if get_setting(db, JWT_SECRET_KEY_KEY) is None:
            set_setting(db, JWT_SECRET_KEY_KEY, secrets.token_hex(32))

        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_settings()
    yield


def require_session(request: Request, db: Session = Depends(get_db)) -> Account:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    jwt_secret_key = get_required_setting(db, JWT_SECRET_KEY_KEY)
    try:
        account_id = decode_session_token(token, jwt_secret_key)
    except jwt.PyJWTError as err:
        raise HTTPException(status_code=401, detail="Not authenticated") from err

    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return account


def enforce_session(request: Request, db: Session = Depends(get_db)) -> None:
    if request.url.path in PUBLIC_PATHS:
        return
    require_session(request, db)


app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(enforce_session)],
    # These bypass the dependency above, so disable them rather than leave them open.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/register", response_model=AccountOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> Account:
    namespace_passphrase_hash = get_required_setting(db, NAMESPACE_PASSPHRASE_HASH_KEY)
    if not verify_namespace_passphrase(body.namespace_passphrase, namespace_passphrase_hash):
        raise HTTPException(status_code=401, detail="Invalid namespace passphrase")

    existing = db.query(Account).filter(Account.username == body.username).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username already taken")

    account = Account(
        username=body.username,
        password_hash=hash_account_password(body.password),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.post("/login", response_model=AccountOut)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> Account:
    account = db.query(Account).filter(Account.username == body.username).first()
    invalid_credentials = HTTPException(status_code=401, detail="Invalid username or password")

    # Hash unconditionally so "no such user" and "wrong password" take the same time.
    password_hash = account.password_hash if account is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_account_password(body.password, password_hash)
    if account is None or not password_valid:
        raise invalid_credentials

    jwt_secret_key = get_required_setting(db, JWT_SECRET_KEY_KEY)
    token = create_session_token(account.id, jwt_secret_key, SESSION_LIFETIME)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    return account


@app.get("/me", response_model=AccountOut)
def me(account: Account = Depends(require_session)) -> Account:
    return account


@app.get("/namespace/salt")
def namespace_salt(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str]:
    return {"salt": get_required_setting(db, NAMESPACE_SALT_KEY)}
