from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    Identity,
    create_session,
    get_identity,
    hash_password,
    is_production,
    revoke_session,
    verify_password,
)
from app.db import SessionLocal
from app.models import User
from app.rate_limit import limiter

router = APIRouter(prefix="/auth")

_GENERIC_LOGIN_FAILURE = "invalid email or password"


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=1024)


class LoginRequest(BaseModel):
    email: EmailStr
    # max_length matches SignupRequest's -- same argon2 DoS-amplification reasoning (finding #6):
    # an unbounded password is fed straight into argon2's memory-hard verify() here too.
    password: str = Field(max_length=1024)


class AuthResponse(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID


class MeResponse(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=is_production(),
        samesite="lax",
        path="/",
    )


@router.post("/signup", response_model=AuthResponse)
@limiter.limit("10/minute")
def signup(request: Request, body: SignupRequest, response: Response) -> AuthResponse:
    db = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            email=body.email,
            password_hash=hash_password(body.password),
            created_at=datetime.now(UTC),
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="an account with this email already exists"
            ) from None
        # No db.refresh(user) needed -- every column was set explicitly above (client-side UUID
        # defaults, no server-generated values), and SessionLocal's expire_on_commit=False means
        # commit() doesn't invalidate what's already in memory either.
    finally:
        db.close()

    raw_token = create_session(user)
    _set_session_cookie(response, raw_token)
    return AuthResponse(tenant_id=user.tenant_id, user_id=user.id)


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response) -> AuthResponse:
    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()
    finally:
        db.close()

    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=401, detail=_GENERIC_LOGIN_FAILURE)

    raw_token = create_session(user)
    _set_session_cookie(response, raw_token)
    return AuthResponse(tenant_id=user.tenant_id, user_id=user.id)


@router.post("/logout")
def logout(
    response: Response,
    songbox_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, str]:
    # Real, server-side revocation (finding #1 from final review): a leaked cookie must stop
    # working immediately, not just get cleared from the client that happened to call /logout.
    # revoke_session() deletes the matching `sessions` row by its token's hash; it's already a
    # no-op if no row matches, so this stays a no-op 200 (not an error) when there's no cookie or
    # no matching session -- logging out while already logged out must never fail.
    if songbox_session:
        revoke_session(songbox_session)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
def me(identity: Identity = Depends(get_identity)) -> MeResponse:
    db = SessionLocal()
    try:
        user = db.execute(
            select(User).where(User.id == identity.user_id)
        ).scalar_one_or_none()
    finally:
        db.close()
    if user is None:
        # Theoretically unreachable today given ON DELETE CASCADE on sessions.user_id -- a valid
        # session can't outlive its user row. Defensive anyway: fail the same way get_identity()
        # fails for every other invalid-session case (401, not an unhandled 500).
        raise HTTPException(status_code=401, detail="not signed in")
    return MeResponse(tenant_id=identity.tenant_id, user_id=identity.user_id, email=user.email)
