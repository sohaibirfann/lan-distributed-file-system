from __future__ import annotations

import os
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from coordinator.db import get_db
from coordinator.models import Account
from coordinator.schemas import AccountOut, LoginRequest, NamespaceVerifierRequest, RegisterRequest
from coordinator.security import (
    DUMMY_PASSWORD_HASH,
    create_session_token,
    decode_session_token,
    hash_account_password,
    verify_account_password,
    verify_namespace_passphrase,
)
from coordinator.settings import (
    JWT_SECRET_KEY_KEY,
    NAMESPACE_PASSPHRASE_HASH_KEY,
    NAMESPACE_SALT_KEY,
    NAMESPACE_VERIFIER_KEY,
    get_required_setting,
    get_setting,
    set_setting,
)

SESSION_COOKIE_NAME = "session"
SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))

PUBLIC_PATHS = frozenset({"/health", "/register", "/login", "/logout"})

router = APIRouter()


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


def issue_session_cookie(response: Response, request: Request, account: Account, db: Session) -> None:
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


@router.post("/register", response_model=AccountOut, status_code=201)
def register(
    body: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> Account:
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
    issue_session_cookie(response, request, account, db)
    return account


@router.post("/login", response_model=AccountOut)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> Account:
    account = db.query(Account).filter(Account.username == body.username).first()
    invalid_credentials = HTTPException(status_code=401, detail="Invalid username or password")

    # Hash unconditionally so "no such user" and "wrong password" take the same time.
    password_hash = account.password_hash if account is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_account_password(body.password, password_hash)
    if account is None or not password_valid:
        raise invalid_credentials

    issue_session_cookie(response, request, account, db)
    return account


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    # Public and unconditional, so it's idempotent even against an already-stale cookie.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@router.get("/me", response_model=AccountOut)
def me(account: Account = Depends(require_session)) -> Account:
    return account


@router.get("/namespace/salt")
def namespace_salt(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str]:
    return {"salt": get_required_setting(db, NAMESPACE_SALT_KEY)}


@router.get("/namespace/verifier")
def get_namespace_verifier(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str | None]:
    return {"verifier": get_setting(db, NAMESPACE_VERIFIER_KEY)}


@router.put("/namespace/verifier")
def put_namespace_verifier(
    body: NamespaceVerifierRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    # First-write-wins: the coordinator can't tell a "better" verifier apart.
    if get_setting(db, NAMESPACE_VERIFIER_KEY) is not None:
        raise HTTPException(status_code=409, detail="Namespace verifier is already set")
    set_setting(db, NAMESPACE_VERIFIER_KEY, body.verifier)
    db.commit()
    return {"verifier": body.verifier}
