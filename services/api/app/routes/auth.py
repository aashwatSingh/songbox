from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
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
    verify_password,
)
from app.db import SessionLocal
from app.models import User

router = APIRouter(prefix="/auth")

_GENERIC_LOGIN_FAILURE = "invalid email or password"


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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
def signup(body: SignupRequest, response: Response) -> AuthResponse:
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
def login(body: LoginRequest, response: Response) -> AuthResponse:
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
def logout(response: Response) -> dict[str, str]:
    # No-op (still 200) if there was never a session cookie to begin with -- calling /auth/logout
    # while already signed out is not an error. Does not need to look up or delete the sessions
    # row itself for correctness (an orphaned expired-eventually row is harmless, same class of
    # decision as this milestone's other explicit non-goals) -- clearing the cookie is sufficient
    # for the browser to stop presenting it.
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
def me(identity: Identity = Depends(get_identity)) -> MeResponse:
    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.id == identity.user_id)).scalar_one()
    finally:
        db.close()
    return MeResponse(tenant_id=identity.tenant_id, user_id=identity.user_id, email=user.email)
