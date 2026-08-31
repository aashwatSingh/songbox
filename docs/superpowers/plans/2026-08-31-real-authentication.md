# Real Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dev-only `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub with real email+password
signup/login, httpOnly DB-backed session cookies, and per-user tenant provisioning — closing
`docs/PLAN.md` open question 9.

**Architecture:** `app/auth.py`'s `get_identity()` already returns an opaque `Identity(tenant_id,
user_id)` that every route and `app/db.py`'s RLS wiring consumes — only `get_identity()`'s internals
change (verified session-cookie lookup instead of trusted headers), so existing route handlers are
untouched. New `users`/`sessions` tables are deliberately excluded from Postgres row-level security
(they're the identity substrate RLS depends on, not tenant content).

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic (existing) · `argon2-cffi` for password hashing
(new) · `email-validator` for `pydantic.EmailStr` (new) · Next.js App Router client components
(existing) · httpOnly cookies via `fastapi.Cookie`/`Response.set_cookie` (stdlib to FastAPI, no new
frontend dependency).

## Global Constraints

- One user per tenant — signup auto-provisions its own `tenant_id`. No teams, invites, or roles.
- Credentials are email + password only. No OAuth, no magic links.
- Sessions are httpOnly, `Secure`-in-production, `SameSite=Lax` cookies holding an opaque
  `secrets.token_urlsafe(32)` token. Only `sha256(token)` is ever persisted — a database read alone
  must never produce a valid session.
- Fixed 30-day session expiry from creation. No sliding-window renewal.
- Password hashing: `argon2-cffi`'s `PasswordHasher` (argon2id, library defaults) — never a
  hand-rolled hash or a fast general-purpose hash (SHA-256/MD5) for passwords.
- No email verification, no password reset, no migration path for existing dev-stub data, no admin
  role folded into user accounts — `X-Admin-Key` (`app/auth.py`'s `require_admin_key`) is untouched.
- `users` and `sessions` tables get NO Postgres row-level security policy — this is deliberate, not
  an oversight (see architecture note above).
- Wrong password and unknown email at login return the identical generic `401` message — never
  reveal which one was wrong.
- CLAUDE.md: every table carries `tenant_id` — `sessions` does (denormalized, avoids a join to
  `users` on every request); `users` itself has no `tenant_id` *filter* to apply since it holds
  exactly one row per tenant by construction, but the column exists on the row.
- CLAUDE.md: never log raw audio, lyrics, or signed URLs. Passwords and session tokens fall under
  the same rule — never log a raw password or raw session-cookie token, hashed or not.
- ruff (`select = ["E", "F", "I", "UP", "B"]`) and `mypy --strict` must pass on every Python change.
  This codebase's mypy config (`services/api/pyproject.toml`) already ignores missing stubs for a
  fixed list of third-party packages — `argon2` is NOT on that list and does ship a `py.typed`
  marker, so no override is needed for it.

---

### Task 1: `users`/`sessions` tables and models

**Files:**
- Create: `services/api/alembic/versions/0009_add_users_and_sessions.py`
- Modify: `services/api/app/models.py` (add `User`, `UserSession`)
- Modify: `services/api/tests/test_db_rls.py` (exclude `users`/`sessions` from the blanket
  every-table-has-RLS assertion — see below, this is a real, necessary fix, not incidental cleanup)
- Test: `services/api/tests/test_db_rls.py` (existing tests must still pass), new test in
  `services/api/tests/test_users_sessions_schema.py`

**Interfaces:**
- Produces: `app.models.User` (`id: uuid.UUID`, `tenant_id: uuid.UUID`, `email: str`,
  `password_hash: str`, `created_at: datetime`) and `app.models.UserSession` (`id: uuid.UUID`,
  `user_id: uuid.UUID`, `tenant_id: uuid.UUID`, `token_hash: str`, `created_at: datetime`,
  `expires_at: datetime`) — Task 2 imports both directly.

**Why `test_db_rls.py` needs a real fix, not just a note:** `services/api/tests/test_db_rls.py:10`
currently computes `RLS_TABLES = tuple(Base.metadata.tables.keys())` — every table registered on the
shared `Base` declarative class — and asserts every single one has RLS enabled and forced. Adding
`User`/`UserSession` to `Base` (this task) would make that test start asserting RLS on `users` and
`sessions`, directly contradicting this plan's design (they're deliberately excluded from RLS, see
Global Constraints). This must be fixed as part of *this* task, not left for a later task to
discover as a surprise failure.

- [ ] **Step 1: Write the migration**

Create `services/api/alembic/versions/0009_add_users_and_sessions.py`:

```python
"""add users and sessions tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-31

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # citext gives case-insensitive email uniqueness ("Foo@x.com" and "foo@x.com" collide) without
    # application-layer lowercasing -- ships in postgres:16-alpine's bundled contrib extensions, no
    # extra image/dependency needed.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Deliberately NOT enabling row-level security here, unlike migration 0002's TABLES tuple.
    # users/sessions are the identity substrate RLS depends on (how a request's tenant is
    # discovered in the first place), not tenant content -- the same category of exception
    # app/db.py's get_admin_db() already documents for cross-tenant operations. A session lookup
    # in app/auth.py's get_identity() uses the unrestricted `songbox` role directly.
    #
    # Also deliberately NOT granting the restricted songbox_app role access to these tables --
    # only the unrestricted `songbox` role (used by app/db.py's SessionLocal, same as
    # get_admin_db()) can read/write them. app/auth.py's session lookup must use SessionLocal, not
    # AppSessionLocal, or every request would fail with a permissions error.


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_table("users")
    # citext extension intentionally left installed on downgrade -- DROP EXTENSION could affect
    # other objects, and an unused extension installed is harmless (same reasoning migration 0002
    # uses for leaving the songbox_app role in place on downgrade).
```

- [ ] **Step 2: Run the migration**

```bash
cd services/api
python -m alembic upgrade head
```

Expected: `Running upgrade 0008 -> 0009, add users and sessions tables` with no errors.

- [ ] **Step 3: Add the SQLAlchemy models**

In `services/api/app/models.py`, change the import line:

```python
from sqlalchemy.dialects.postgresql import JSONB, UUID
```

to:

```python
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
```

Then append at the end of the file:

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

(Named `UserSession`, not `Session` — `Session` already refers to SQLAlchemy's own class,
imported throughout this codebase, e.g. `from sqlalchemy.orm import Session`.)

- [ ] **Step 4: Fix `test_db_rls.py`'s blanket RLS assertion**

In `services/api/tests/test_db_rls.py`, change:

```python
RLS_TABLES = tuple(Base.metadata.tables.keys())
```

to:

```python
# users/sessions are deliberately excluded from row-level security -- they're the identity
# substrate RLS depends on (how a request's tenant is even discovered), not tenant content. See
# alembic/versions/0009_add_users_and_sessions.py's upgrade() comment for the full reasoning.
RLS_TABLES = tuple(
    name for name in Base.metadata.tables.keys() if name not in ("users", "sessions")
)
```

- [ ] **Step 5: Run test_db_rls.py to confirm it still passes**

```bash
pytest tests/test_db_rls.py -v
```

Expected: `2 passed`.

- [ ] **Step 6: Write a new test proving users/sessions are real and outside RLS**

Create `services/api/tests/test_users_sessions_schema.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db import SessionLocal
from app.models import User, UserSession


def test_users_and_sessions_tables_have_no_rls_policy() -> None:
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname IN ('users', 'sessions')"
            )
        ).all()
    finally:
        session.close()

    found = {row.relname: row.relrowsecurity for row in rows}
    assert found == {"users": False, "sessions": False}


def test_a_user_and_session_row_can_be_inserted_and_read_back() -> None:
    session = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.com",
            password_hash="not-a-real-hash-just-schema-test",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()

        user_session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_hash="deadbeef",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(user_session)
        session.commit()

        session.refresh(user)
        session.refresh(user_session)
        assert user_session.user_id == user.id
        assert user_session.tenant_id == user.tenant_id
    finally:
        session.close()
```

- [ ] **Step 7: Run the new test and confirm it passes**

```bash
pytest tests/test_users_sessions_schema.py -v
```

Expected: `2 passed`.

- [ ] **Step 8: Lint and type-check**

```bash
ruff check app/models.py tests/test_db_rls.py tests/test_users_sessions_schema.py
mypy app/models.py
```

Expected: both clean.

- [ ] **Step 9: Commit**

```bash
git add alembic/versions/0009_add_users_and_sessions.py app/models.py tests/test_db_rls.py tests/test_users_sessions_schema.py
git commit -m "M8: add users and sessions tables, outside row-level security"
```

---

### Task 2: Password hashing, session helpers, and `get_identity()` rewrite

**Files:**
- Modify: `services/api/app/auth.py` (full rewrite of `get_identity()`; `require_admin_key`
  untouched)
- Modify: `services/api/pyproject.toml` (add `argon2-cffi`)
- Modify: `services/api/tests/test_auth.py` (full rewrite — the old file tests header-trust
  behavior that no longer exists)

**Interfaces:**
- Consumes: `app.models.User`, `app.models.UserSession` (Task 1), `app.db.SessionLocal` (existing).
- Produces: `app.auth.Identity` (unchanged shape: `tenant_id: uuid.UUID`, `user_id: uuid.UUID`),
  `app.auth.hash_password(password: str) -> str`, `app.auth.verify_password(password_hash: str,
  password: str) -> bool`, `app.auth.create_session(user: User) -> str` (returns the RAW cookie
  token), `app.auth.SESSION_COOKIE_NAME: str` (`"songbox_session"`), `app.auth.SESSION_TTL:
  timedelta` (30 days), `app.auth.is_production() -> bool`, `app.auth.get_identity` (same FastAPI
  dependency signature pattern as before, now cookie-based). Task 3's `app/routes/auth.py` imports
  all of these.

- [ ] **Step 1: Add the new dependency**

In `services/api/pyproject.toml`, add to the `dependencies` list (after `"pyyaml>=6.0",`):

```python
    "argon2-cffi>=23.1",
```

Then install:

```bash
cd services/api
pip install -e ".[dev]"
```

Expected: installs `argon2-cffi` (and its `argon2-cffi-bindings` dependency) with no errors.

- [ ] **Step 2: Write the failing tests**

Replace the entire contents of `services/api/tests/test_auth.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    Identity,
    SESSION_COOKIE_NAME,
    create_session,
    get_identity,
    hash_password,
    verify_password,
)
from app.db import SessionLocal
from app.models import User, UserSession

app = FastAPI()


@app.get("/whoami")
def whoami(identity: Identity = Depends(get_identity)) -> dict[str, str]:
    return {"tenant_id": str(identity.tenant_id), "user_id": str(identity.user_id)}


client = TestClient(app)


def _make_user() -> User:
    session = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    finally:
        session.close()


def test_hash_password_produces_a_real_argon2_hash_not_the_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_the_correct_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "correct horse battery staple") is True


def test_verify_password_rejects_the_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "wrong password entirely") is False


def test_missing_cookie_returns_401() -> None:
    response = client.get("/whoami")
    assert response.status_code == 401


def test_garbage_cookie_returns_401() -> None:
    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: "not-a-real-token"})
    assert response.status_code == 401


def test_valid_session_returns_the_real_identity() -> None:
    user = _make_user()
    raw_token = create_session(user)

    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: raw_token})

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(user.tenant_id), "user_id": str(user.id)}


def test_expired_session_returns_401() -> None:
    user = _make_user()
    session = SessionLocal()
    try:
        raw_token = "expired-token-fixture"
        import hashlib

        session.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                created_at=datetime.now(UTC) - timedelta(days=31),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 401


def test_create_session_never_persists_the_raw_token() -> None:
    user = _make_user()
    raw_token = create_session(user)

    session = SessionLocal()
    try:
        rows = session.query(UserSession).filter(UserSession.user_id == user.id).all()
    finally:
        session.close()

    assert len(rows) == 1
    assert rows[0].token_hash != raw_token
```

(This test file uses `session.query(...)` in its own setup/assertion code only — Task 2's actual
`app/auth.py` implementation must use the `select()`-style API per this codebase's established
convention, see Step 3 below. Test files verifying schema state directly are not bound by the same
production-code convention, but for consistency use `select()` there too if you prefer — either
works for a test-only read.)

- [ ] **Step 3: Run the tests to verify they fail**

```bash
pytest tests/test_auth.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` (`create_session`, `hash_password`,
`verify_password`, `SESSION_COOKIE_NAME` don't exist yet), or collection errors.

- [ ] **Step 4: Rewrite `app/auth.py`**

Replace the entire contents of `services/api/app/auth.py`:

```python
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Header, HTTPException, Request
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User, UserSession

SESSION_COOKIE_NAME = "songbox_session"
SESSION_TTL = timedelta(days=30)

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(user: User) -> str:
    """Creates a new sessions row for `user` and returns the RAW token to set as the cookie
    value -- only its sha256 hash is ever persisted (see _hash_token), so a database read alone
    can never produce a valid session cookie. Uses SessionLocal (the unrestricted `songbox` role),
    not AppSessionLocal -- sessions has no RLS policy and songbox_app has no grant on it (see
    alembic/versions/0009_add_users_and_sessions.py).
    """
    raw_token = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        db.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=_hash_token(raw_token),
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + SESSION_TTL,
            )
        )
        db.commit()
    finally:
        db.close()
    return raw_token


def is_production() -> bool:
    return os.environ.get("SONGBOX_ENV", "development") == "production"


def get_identity(
    request: Request,
    songbox_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Identity:
    if not songbox_session:
        raise HTTPException(status_code=401, detail="not signed in")

    token_hash = _hash_token(songbox_session)
    db = SessionLocal()
    try:
        session_row = db.execute(
            select(UserSession).where(UserSession.token_hash == token_hash)
        ).scalar_one_or_none()
        if session_row is None or session_row.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=401, detail="session expired or invalid")
        identity = Identity(tenant_id=session_row.tenant_id, user_id=session_row.user_id)
    finally:
        db.close()

    # Read by app/main.py's access-log middleware after call_next() returns -- a real, verified
    # tenant_id now, not an unverified client-supplied header (see main.py's log_requests).
    request.state.tenant_id = str(identity.tenant_id)
    return identity


def require_admin_key(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="admin API key not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Key")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_auth.py -v
```

Expected: `7 passed`.

- [ ] **Step 6: Lint and type-check**

```bash
ruff check app/auth.py tests/test_auth.py
mypy app/auth.py
```

Expected: both clean. If mypy complains about `argon2` having no type stubs, check whether
`argon2-cffi` ships a `py.typed` marker in the installed package (it does, as of the version this
plan pins) — if a real mypy error appears, add `"argon2.*"` to `services/api/pyproject.toml`'s
`[[tool.mypy.overrides]]` `module` list alongside the existing entries, matching that section's
established pattern for third-party packages without stubs.

- [ ] **Step 7: Commit**

```bash
git add app/auth.py pyproject.toml tests/test_auth.py
git commit -m "M8: replace dev-header identity trust with real argon2 password hashing and session verification"
```

---

### Task 3: Signup/login/logout/me endpoints, CORS credentials, access-log fix

**Files:**
- Create: `services/api/app/routes/auth.py`
- Modify: `services/api/app/main.py` (register router, `allow_credentials=True`, access-log
  middleware reads `request.state.tenant_id` instead of the `X-Dev-Tenant-Id` header)
- Modify: `services/api/pyproject.toml` (add `email-validator`)
- Modify: `services/api/tests/test_cors.py` (add one assertion for the new credentials header)
- Test: `services/api/tests/test_auth_routes.py` (new)

**Interfaces:**
- Consumes: `app.auth.Identity`, `app.auth.get_identity`, `app.auth.hash_password`,
  `app.auth.verify_password`, `app.auth.create_session`, `app.auth.SESSION_COOKIE_NAME`,
  `app.auth.SESSION_TTL`, `app.auth.is_production` (Task 2). `app.db.SessionLocal`.
- Produces: `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` — consumed
  by Task 9's frontend work and by every test-migration task (Tasks 4-7) via the fixture Task 4
  builds on top of `/auth/signup`.

- [ ] **Step 1: Add the new dependency**

In `services/api/pyproject.toml`, add to the `dependencies` list (after the `argon2-cffi` line
Task 2 added):

```python
    "email-validator>=2.2",
```

Install:

```bash
cd services/api
pip install -e ".[dev]"
```

- [ ] **Step 2: Write the failing tests**

Create `services/api/tests/test_auth_routes.py`:

```python
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE_NAME
from app.main import app

client = TestClient(app)


def _unique_email() -> str:
    # NOT @example.test -- email-validator (which EmailStr delegates to) permanently rejects RFC
    # 2606's reserved .test TLD; .com is required for real EmailStr validation to pass.
    return f"{uuid.uuid4()}@example.com"


def test_signup_creates_a_real_account_and_sets_a_session_cookie() -> None:
    email = _unique_email()
    response = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["tenant_id"])
    assert uuid.UUID(body["user_id"])
    assert SESSION_COOKIE_NAME in response.cookies


def test_signup_rejects_a_too_short_password() -> None:
    response = client.post("/auth/signup", json={"email": _unique_email(), "password": "short"})
    assert response.status_code == 422


def test_signup_rejects_a_malformed_email() -> None:
    response = client.post(
        "/auth/signup", json={"email": "not-an-email", "password": "hunter22ab"}
    )
    assert response.status_code == 422


def test_signup_with_a_duplicate_email_returns_409() -> None:
    email = _unique_email()
    first = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})
    assert first.status_code == 200

    second = client.post("/auth/signup", json={"email": email, "password": "different-password"})
    assert second.status_code == 409


def test_login_with_correct_credentials_succeeds() -> None:
    email = _unique_email()
    client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    response = client.post("/auth/login", json={"email": email, "password": "hunter22ab"})

    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_with_wrong_password_returns_401() -> None:
    email = _unique_email()
    client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    response = client.post("/auth/login", json={"email": email, "password": "wrong-password"})

    assert response.status_code == 401


def test_login_with_unknown_email_returns_401_with_the_same_message_as_wrong_password() -> None:
    real_email = _unique_email()
    client.post("/auth/signup", json={"email": real_email, "password": "hunter22ab"})

    wrong_password_response = client.post(
        "/auth/login", json={"email": real_email, "password": "wrong-password"}
    )
    unknown_email_response = client.post(
        "/auth/login", json={"email": _unique_email(), "password": "hunter22ab"}
    )

    assert wrong_password_response.status_code == 401
    assert unknown_email_response.status_code == 401
    assert wrong_password_response.json()["detail"] == unknown_email_response.json()["detail"]


def test_me_returns_the_signed_in_users_identity_and_email() -> None:
    email = _unique_email()
    signup_response = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})
    session_client = TestClient(app)
    session_client.cookies.set(SESSION_COOKIE_NAME, signup_response.cookies[SESSION_COOKIE_NAME])

    response = session_client.get("/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert uuid.UUID(body["tenant_id"]) == uuid.UUID(signup_response.json()["tenant_id"])


def test_me_without_a_session_returns_401() -> None:
    response = TestClient(app).get("/auth/me")
    assert response.status_code == 401


def test_logout_clears_the_session_so_me_then_401s() -> None:
    email = _unique_email()
    session_client = TestClient(app)
    session_client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    logout_response = session_client.post("/auth/logout")
    me_response = session_client.get("/auth/me")

    assert logout_response.status_code == 200
    assert me_response.status_code == 401


def test_logout_without_a_session_is_a_no_op_200() -> None:
    response = TestClient(app).post("/auth/logout")
    assert response.status_code == 200
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
pytest tests/test_auth_routes.py -v
```

Expected: every test fails with a `404` (routes don't exist yet).

- [ ] **Step 4: Write `app/routes/auth.py`**

Create `services/api/app/routes/auth.py`:

```python
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
```

- [ ] **Step 5: Register the router, CORS credentials, and access-log fix in `app/main.py`**

In `services/api/app/main.py`, change the import block:

```python
from app.routes.admin import router as admin_router
from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router
```

to:

```python
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router
```

Change the CORS block:

```python
# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Dev-Tenant-Id", "X-Dev-User-Id", "Content-Type"],
)
```

to:

```python
# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
# allow_credentials=True is required for the browser to send/receive the httpOnly session cookie
# cross-origin (localhost:3000 -> localhost:8000) -- safe here specifically because allow_origins
# is a concrete origin, not "*" (the CORS spec forbids combining allow_credentials with a wildcard
# origin, and browsers enforce this).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    allow_credentials=True,
)
```

Change the access-log middleware's `tenant_id` field:

```python
                "tenant_id": request.headers.get("X-Dev-Tenant-Id"),
```

to:

```python
                # Set by app/auth.py's get_identity() on request.state after a real session
                # lookup succeeds -- None for unauthenticated requests (e.g. /health, or a 401
                # before identity ever resolves), same as before. Unlike the old
                # X-Dev-Tenant-Id header this replaces, this value is now verified, not merely
                # whatever the caller claimed.
                "tenant_id": getattr(request.state, "tenant_id", None),
```

Change the router registration:

```python
app.include_router(tracks_router)
app.include_router(review_queue_router)
app.include_router(admin_router)
```

to:

```python
app.include_router(auth_router)
app.include_router(tracks_router)
app.include_router(review_queue_router)
app.include_router(admin_router)
```

- [ ] **Step 6: Add a credentials assertion to `test_cors.py`**

In `services/api/tests/test_cors.py`, add after the existing assertion:

```python
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
pytest tests/test_auth_routes.py tests/test_cors.py -v
```

Expected: `11 passed` (9 from `test_auth_routes.py`, 2 from `test_cors.py`).

- [ ] **Step 8: Lint and type-check**

```bash
ruff check app/routes/auth.py app/main.py tests/test_auth_routes.py tests/test_cors.py
mypy app/routes/auth.py app/main.py
```

Expected: both clean.

- [ ] **Step 9: Run the full existing suite to confirm nothing else broke yet**

```bash
pytest -q
```

Expected: many pre-existing failures — every test still using `X-Dev-Tenant-Id`/`X-Dev-User-Id`
headers now gets `401`, since `get_identity()` no longer accepts them (Task 2). This is expected and
exactly what Tasks 4-7 fix. Confirm specifically that `test_auth.py`, `test_auth_routes.py`,
`test_cors.py`, `test_health.py`, `test_db.py`, `test_db_rls.py`, and `test_users_sessions_schema.py`
all pass — those are the ones unaffected by (or already migrated for) the header change.

- [ ] **Step 10: Commit**

```bash
git add app/routes/auth.py app/main.py pyproject.toml tests/test_auth_routes.py tests/test_cors.py
git commit -m "M8: add signup/login/logout/me endpoints, CORS credentials, verified-identity access logging"
```

---

### Task 4: Test fixture infrastructure + migrate the `test_tracks_upload.py` cluster

**Files:**
- Modify: `services/api/tests/conftest.py` (add `AuthedClient`, `sign_up`, `authed_client` fixture)
- Modify: `services/api/tests/test_tracks_upload.py`
- Modify: `services/api/tests/test_confirm_attestation.py`
- Modify: `services/api/tests/test_review_queue.py`

**Interfaces:**
- Consumes: `POST /auth/signup` (Task 3).
- Produces: `tests.conftest.AuthedClient` (`client: TestClient`, `tenant_id: uuid.UUID`,
  `user_id: uuid.UUID`), `tests.conftest.sign_up(client: TestClient, *, email: str | None = None) ->
  AuthedClient`, `tests.conftest.authed_client` (pytest fixture, `-> AuthedClient`, a fresh signed-up
  user on a fresh `TestClient`). Every remaining migration task (5, 6, 7) imports these.

**Why this exact shape:** every one of the 15 files needing migration currently does one or both of
(a) send `client.<verb>(path, headers=HEADERS, ...)` and (b) extract the raw tenant_id value out of
`HEADERS["X-Dev-Tenant-Id"]` to open a direct `db_session_for_tenant(...)` session for test setup
(verified via `grep -rn "db_session_for_tenant\|HEADERS\[" tests/*.py` — nearly every track test
does this). A bare authenticated `TestClient` alone isn't enough; tests need the real tenant_id back
too. `sign_up()` is a plain function (not only a fixture) because `test_rate_limiting.py` (Task 5)
constructs its own `TestClient(app, client=(ip, port))` with a specific simulated peer IP and needs
to sign up *on that specific instance* — a fixture that always constructs its own fresh client can't
serve that case.

**Why `test_tracks_upload.py` + `test_confirm_attestation.py` + `test_review_queue.py` are one
task:** `test_confirm_attestation.py` and `test_review_queue.py` both currently do
`from tests.test_tracks_upload import HEADERS, _make_tone` — importing the module-level `HEADERS`
dict `test_tracks_upload.py` defines. Once `HEADERS` is deleted from `test_tracks_upload.py` (this
task), those two imports break unless fixed in the same commit. `_make_tone` is unrelated to auth
(an ffmpeg tone-generation helper) and its import stays unchanged in both files.

- [ ] **Step 1: Add the fixture infrastructure to `conftest.py`**

In `services/api/tests/conftest.py`, add these imports at the top (alongside the existing ones):

```python
import uuid
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.main import app
```

Then add, anywhere after the existing fixtures:

```python
_TEST_PASSWORD = "correct horse battery staple"


@dataclass(frozen=True)
class AuthedClient:
    client: TestClient
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def sign_up(client: TestClient, *, email: str | None = None) -> AuthedClient:
    """Signs up a fresh real user on the given TestClient instance (mutating its cookie jar with
    the resulting session) and returns the real tenant_id/user_id -- the real-auth replacement for
    constructing an arbitrary X-Dev-Tenant-Id/X-Dev-User-Id headers dict. Most tests should use the
    `authed_client` fixture below instead of calling this directly; call this directly only when a
    test needs to control the TestClient itself (e.g. test_rate_limiting.py's per-test simulated
    peer IP) or needs more than one distinct identity in the same test (e.g. a second tenant to
    prove cross-tenant isolation).
    """
    if email is None:
        # NOT @example.test -- RFC 2606 reserves .test as a special-use TLD, and the
        # email-validator package pydantic's EmailStr delegates to permanently rejects it as
        # undeliverable regardless of configuration. Task 3's implementer discovered this the hard
        # way (every real signup call 422'd); .com is the correct choice for synthetic test emails
        # that must pass real EmailStr validation.
        email = f"{uuid.uuid4()}@example.com"
    response = client.post("/auth/signup", json={"email": email, "password": _TEST_PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    return AuthedClient(
        client=client,
        tenant_id=uuid.UUID(body["tenant_id"]),
        user_id=uuid.UUID(body["user_id"]),
    )


@pytest.fixture
def authed_client() -> AuthedClient:
    return sign_up(TestClient(app))
```

- [ ] **Step 2: Run a smoke test to confirm the fixture works**

```bash
pytest tests/test_auth_routes.py -v
```

Expected: still `9 passed` (conftest.py changes shouldn't affect this file, this just confirms
nothing broke on import).

- [ ] **Step 3: Migrate `test_tracks_upload.py`**

Read the current full file first (`services/api/tests/test_tracks_upload.py`) before editing —
this step's transformation is mechanical but must be applied to the file's real, current content,
not reconstructed from memory. Apply this transformation:

1. Remove the module-level block:
   ```python
   client = TestClient(app)

   HEADERS = {
       "X-Dev-Tenant-Id": str(uuid.uuid4()),
       "X-Dev-User-Id": str(uuid.uuid4()),
   }
   ```
2. Add `from tests.conftest import AuthedClient` to the imports.
3. Every test function that currently has no fixture parameters relying on `client`/`HEADERS`
   gets `authed_client: AuthedClient` added to its parameter list.
4. Inside each such function, add a first line `client = authed_client.client` (shadowing the
   removed module global with a local of the same name, so the rest of the function body — every
   `client.post(...)`/`client.get(...)` call — needs no further edits beyond removing the
   `headers=HEADERS` kwarg from each call).
5. Remove every `headers=HEADERS` (or `headers=HEADERS,`) keyword argument from every
   `client.post(...)`/`client.get(...)` call in the file.
6. If `import uuid` becomes unused after removing the `HEADERS` block, check whether any other
   line in the file still uses `uuid.` (e.g. `uuid.uuid4()` for a fake track id) — if so, keep the
   import; if genuinely unused, remove it (ruff's `F401` will catch this either way in Step 6).

- [ ] **Step 4: Migrate `test_confirm_attestation.py`**

Read the current full file first. Apply:

1. Change `from tests.test_tracks_upload import HEADERS, _make_tone` to
   `from tests.test_tracks_upload import _make_tone` and add
   `from tests.conftest import AuthedClient` to the imports.
2. Remove the module-level `client = TestClient(app)` line.
3. Apply the same per-function transformation as Step 3 items 3-5 above (add `authed_client:
   AuthedClient` parameter, `client = authed_client.client` local, drop `headers=HEADERS`).

- [ ] **Step 5: Migrate `test_review_queue.py`**

Read the current full file first. Apply the identical transformation as Step 4 (same import
pattern: `from tests.test_tracks_upload import HEADERS, _make_tone` -> `from
tests.test_tracks_upload import _make_tone` plus the `AuthedClient` import; same
module-level-`client`-removal and per-function fixture-parameter pattern).

- [ ] **Step 6: Run the migrated files and lint/type-check**

```bash
pytest tests/test_tracks_upload.py tests/test_confirm_attestation.py tests/test_review_queue.py -v
ruff check tests/test_tracks_upload.py tests/test_confirm_attestation.py tests/test_review_queue.py tests/conftest.py
mypy tests/conftest.py
```

Expected: all tests in the three files pass (same pass count as before this milestone started —
confirm by checking `git log` / the test file's test count didn't change, only how identity is
obtained). Ruff and mypy clean. If ruff reports `F401` for an unused `uuid` import in any file,
remove it.

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_tracks_upload.py tests/test_confirm_attestation.py tests/test_review_queue.py
git commit -m "M8: add authed_client test fixture, migrate upload/attestation/review-queue tests off dev headers"
```

---

### Task 5: Migrate `test_rate_limiting.py`

**Files:**
- Modify: `services/api/tests/test_rate_limiting.py`

**Interfaces:**
- Consumes: `tests.conftest.sign_up`, `tests.conftest.AuthedClient` (Task 4).

**Why this file is its own task:** every other test constructs a plain `TestClient(app)`. This file
constructs `TestClient(app, client=(_random_test_ip(), 1))` *per test function* to control the
simulated source IP slowapi's `get_remote_address` reads — the entire point of the test (rate limits
are per-IP) depends on that. The migration must sign up *on that specific client instance* via the
low-level `sign_up()` function, not the `authed_client` fixture (which always builds its own,
unconfigurable `TestClient`).

Also note: with real authentication, an unauthenticated request now gets `401` before the
`@limiter.limit(...)`-wrapped route function ever runs (FastAPI resolves `Depends(get_identity)`
before calling the decorated endpoint function slowapi wraps) — so every request in this file's
21-in-a-row loops must come from a real signed-up session, or the test would see `401` repeated 21
times instead of the expected `404`-then-`429` (or `200`-then-`429`) pattern it currently asserts.

- [ ] **Step 1: Read the current full file**

Read `services/api/tests/test_rate_limiting.py` in full before editing.

- [ ] **Step 2: Apply the transformation**

1. Remove the module-level block:
   ```python
   HEADERS = {
       "X-Dev-Tenant-Id": str(uuid.uuid4()),
       "X-Dev-User-Id": str(uuid.uuid4()),
   }
   ```
2. Add `from tests.conftest import sign_up` to the imports.
3. In every test function, immediately after the line that constructs
   `client = TestClient(app, client=(_random_test_ip(), 1))`, add:
   ```python
   sign_up(client)
   ```
   (Discard the return value if the test doesn't need the raw tenant_id — most of these don't,
   since they only care about request counts and status codes, not tenant content.)
4. Remove every `headers=HEADERS` keyword argument from every `client.post(...)`/`client.get(...)`
   call in the file.
5. The two `headers={"X-Admin-Key": "definitely-the-wrong-key"}` occurrences (lines ~103, ~187 in
   the pre-migration file) are for the admin takedown rate-limit test and are UNCHANGED — admin
   auth is untouched by this milestone.

- [ ] **Step 3: Run the tests**

```bash
pytest tests/test_rate_limiting.py -v
```

Expected: same pass count as before this milestone (check `git log` for the pre-migration test
count in this file if unsure — every test in this file should still assert the same 404/200-then-429
boundary it did before, just via a real signed-up identity instead of an arbitrary header pair).

- [ ] **Step 4: Lint and type-check**

```bash
ruff check tests/test_rate_limiting.py
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/test_rate_limiting.py
git commit -m "M8: migrate rate-limiting tests to real signed-up sessions"
```

---

### Task 6: Migrate the remaining "plain" test files, batch A

**Files:**
- Modify: `services/api/tests/test_admin_takedown.py`
- Modify: `services/api/tests/test_deletion.py`
- Modify: `services/api/tests/test_job_cost.py`
- Modify: `services/api/tests/test_purge_expired_tracks.py`
- Modify: `services/api/tests/test_tracks_list.py`
- Modify: `services/api/tests/test_request_logging.py`

**Interfaces:**
- Consumes: `tests.conftest.AuthedClient`, `tests.conftest.authed_client`, `tests.conftest.sign_up`
  (Task 4).

Apply the same general transformation as Task 4 Step 3 to each file below — read the file's current
full content first, then transform. Each file's specific wrinkles are called out.

- [ ] **Step 1: Migrate `test_admin_takedown.py`**

Same pattern as Task 4: remove module-level `client = TestClient(app)` and `HEADERS = {...}`, add
`authed_client: AuthedClient` fixture parameter to functions using `client`/`HEADERS`, add
`client = authed_client.client` local, drop `headers=HEADERS` kwargs. The `headers={"X-Admin-Key":
...}` calls (wrong-key, correct-key) are unchanged.

- [ ] **Step 2: Migrate `test_deletion.py`**

Same pattern, with one addition: this file does
`db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))` (and reuses that extracted
`tenant_id` in two `RightsDeclaration(... tenant_id=...)` constructions) — replace
`uuid.UUID(HEADERS["X-Dev-Tenant-Id"])` with `authed_client.tenant_id` (already a real `uuid.UUID`,
no `uuid.UUID(...)` wrapper needed) in all three places.

- [ ] **Step 3: Migrate `test_job_cost.py`**

Same pattern. This file was already touched in the prior M7c milestone (backend-aware cost
gating) — its `test_separate_endpoint_logs_a_real_gpu_job_cost_line` test builds a raw
`headers = {"X-Dev-Tenant-Id": ..., "X-Dev-User-Id": ...}` dict inline (not a module-level
constant); replace it with an `authed_client: AuthedClient` fixture parameter and
`client = authed_client.client`, dropping the `headers=` kwarg from its `client.post(...)` calls.

- [ ] **Step 4: Migrate `test_purge_expired_tracks.py`**

Same pattern as `test_admin_takedown.py`. This file imports `_make_tone` (not `HEADERS`) from
`tests.test_tracks_upload` — that import is unaffected and stays as-is.

- [ ] **Step 5: Migrate `test_tracks_list.py`**

This file has the `OTHER_TENANT_HEADERS` two-tenant pattern (not just `HEADERS`). Transform:

1. Remove `client = TestClient(app)`, `HEADERS = {...}`, and `OTHER_TENANT_HEADERS = {...}`.
2. Add `from tests.conftest import AuthedClient, sign_up` to the imports.
3. `_upload_and_pass_track` currently takes `(synthetic_wav: Path)` — change its signature to
   `(client: TestClient, synthetic_wav: Path)` and use that parameter instead of the removed
   module global; drop its `headers=HEADERS` kwarg.
4. `test_list_tracks_returns_only_the_calling_tenants_tracks` and
   `test_list_tracks_reports_has_transcription_accurately` both need TWO real identities where the
   original used `HEADERS`/`OTHER_TENANT_HEADERS`. Give them `authed_client: AuthedClient` as a
   fixture parameter (replacing `HEADERS`), and for the second tenant, call
   `other = sign_up(TestClient(app))` inline. Pass `authed_client.client` (or `other.client`) as the
   new `client` argument to `_upload_and_pass_track` and to every direct `client.get(...)` call,
   dropping every `headers=` kwarg.

- [ ] **Step 6: Migrate `test_request_logging.py`**

This file needs the most care — it directly tests the access-log middleware behavior Task 3
changed. Read the current full file first. Apply:

1. Remove the module-level `client = TestClient(app)` line — but check first whether other tests
   in the file (the `/health` test, the 4xx test) still want an *unauthenticated* plain
   `TestClient(app)` for requests that don't need identity at all; if so, keep a local
   `client = TestClient(app)` inside those specific test functions rather than removing that
   pattern everywhere.
2. `test_request_with_tenant_header_logs_that_tenant_id` (currently sends an arbitrary
   `X-Dev-Tenant-Id` header and asserts the access log mirrors it verbatim) must be rewritten, not
   just have its headers swapped — the old test proved something no longer true (an unverified
   header value flows into the log). Replace it with:
   ```python
   def test_authenticated_request_logs_the_real_verified_tenant_id(
       caplog: pytest.LogCaptureFixture, authed_client: AuthedClient
   ) -> None:
       with caplog.at_level(logging.INFO, logger="songbox.access"):
           authed_client.client.get("/tracks")

       access_records = [r for r in caplog.records if r.name == "songbox.access"]
       assert len(access_records) == 1
       assert access_records[0].tenant_id == str(authed_client.tenant_id)  # type: ignore[attr-defined]
   ```
   Add `from tests.conftest import AuthedClient` to the imports.
3. `test_upload_request_log_never_leaks_track_content` currently builds an inline
   `headers = {"X-Dev-Tenant-Id": ..., "X-Dev-User-Id": ...}` dict — replace with an
   `authed_client: AuthedClient` fixture parameter, `client = authed_client.client`, drop the
   `headers=` kwarg.
4. `test_unhandled_exception_still_logs_a_request_line` overrides `get_identity` directly via
   `app.dependency_overrides[get_identity] = _broken_identity`, bypassing real identity resolution
   entirely — its `headers={"X-Dev-Tenant-Id": "test", "X-Dev-User-Id": "test"}` argument is now
   vestigial (the override makes the real header/cookie parsing irrelevant either way); simply
   remove that `headers=` kwarg with no other change.

- [ ] **Step 7: Run all six migrated files**

```bash
pytest tests/test_admin_takedown.py tests/test_deletion.py tests/test_job_cost.py tests/test_purge_expired_tracks.py tests/test_tracks_list.py tests/test_request_logging.py -v
```

Expected: every test in all six files passes, same test count as before this milestone in each
file.

- [ ] **Step 8: Lint and type-check**

```bash
ruff check tests/test_admin_takedown.py tests/test_deletion.py tests/test_job_cost.py tests/test_purge_expired_tracks.py tests/test_tracks_list.py tests/test_request_logging.py
```

Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add tests/test_admin_takedown.py tests/test_deletion.py tests/test_job_cost.py tests/test_purge_expired_tracks.py tests/test_tracks_list.py tests/test_request_logging.py
git commit -m "M8: migrate admin-takedown/deletion/job-cost/purge/tracks-list/request-logging tests off dev headers"
```

---

### Task 7: Migrate remaining test files, batch B, and confirm the full suite is green

**Files:**
- Modify: `services/api/tests/test_tracks_package.py`
- Modify: `services/api/tests/test_tracks_package_get.py`
- Modify: `services/api/tests/test_tracks_realign.py`
- Modify: `services/api/tests/test_tracks_separate.py`
- Modify: `services/api/tests/test_tracks_transcribe.py`

**Interfaces:**
- Consumes: `tests.conftest.AuthedClient`, `tests.conftest.authed_client` (Task 4).

All five files follow the exact same module-level `client = TestClient(app)` + `HEADERS = {...}` +
`db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))` pattern already described in Task 4
Step 3 and Task 6 Step 2 (verified via `grep -n "TestClient(app)\|^HEADERS\|db_session_for_tenant"`
across all five — every one matches this shape, none has an `OTHER_TENANT_HEADERS`-style
multi-tenant case beyond what `test_deletion.py` already covers in Task 6).

- [ ] **Step 1: Migrate `test_tracks_package.py`**

Read the current full file first. Apply the Task 4 Step 3 transformation (remove module-level
`client`/`HEADERS`, add `authed_client: AuthedClient` parameter + `client = authed_client.client`
local to each function using them, drop `headers=HEADERS` kwargs). Replace every
`uuid.UUID(HEADERS["X-Dev-Tenant-Id"])` with `authed_client.tenant_id`.

- [ ] **Step 2: Migrate `test_tracks_package_get.py`**

Same transformation as Step 1.

- [ ] **Step 3: Migrate `test_tracks_realign.py`**

Same transformation as Step 1.

- [ ] **Step 4: Migrate `test_tracks_separate.py`**

Same transformation as Step 1. This file also has
`assert stem["storage_key"].startswith(f"{HEADERS['X-Dev-Tenant-Id']}/")` — replace with
`assert stem["storage_key"].startswith(f"{authed_client.tenant_id}/")`.

- [ ] **Step 5: Migrate `test_tracks_transcribe.py`**

Same transformation as Step 1.

- [ ] **Step 6: Run all five migrated files**

```bash
pytest tests/test_tracks_package.py tests/test_tracks_package_get.py tests/test_tracks_realign.py tests/test_tracks_separate.py tests/test_tracks_transcribe.py -v
```

Expected: every test passes, same count as before this milestone in each file.

- [ ] **Step 7: Lint and type-check**

```bash
ruff check tests/test_tracks_package.py tests/test_tracks_package_get.py tests/test_tracks_realign.py tests/test_tracks_separate.py tests/test_tracks_transcribe.py
```

Expected: clean.

- [ ] **Step 8: Run the ENTIRE local suite**

```bash
pytest -q
```

Expected: every test passes (or the same skip count as before this milestone, for the
`SONGBOX_MODAL_LIVE_TESTS`-gated Modal tests) — zero `401`s, zero failures anywhere in the suite.
This is the real confirmation that every file needing migration was found and fixed; if anything
still fails with a `401`, it's a file this plan's investigation missed and must be migrated with
the same pattern before this task is done.

- [ ] **Step 9: Full mypy run**

```bash
mypy app/
```

Expected: `Success: no issues found`.

- [ ] **Step 10: Commit**

```bash
git add tests/test_tracks_package.py tests/test_tracks_package_get.py tests/test_tracks_realign.py tests/test_tracks_separate.py tests/test_tracks_transcribe.py
git commit -m "M8: migrate remaining track-pipeline tests off dev headers, full suite green"
```

---

### Task 8: Frontend API client — remove dev identity, add real auth calls

**Files:**
- Modify: `apps/web/lib/api.ts`

**Interfaces:**
- Consumes: `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` (Task 3).
- Produces: `signup(email: string, password: string): Promise<{tenant_id: string; user_id: string}>`,
  `login(email: string, password: string): Promise<{tenant_id: string; user_id: string}>`,
  `logout(): Promise<void>`, `me(): Promise<{tenant_id: string; user_id: string; email: string} |
  null>` — Task 9's login/signup pages and `AuthContext` consume these.

This is frontend glue code — per this project's working agreement (`docs/PLAN.md`: "Test-first for
the rights gate, the alignment engine, and the upload handler. UI and glue code are exempt."), no
automated test is required here. Task 9's manual browser verification covers this file's real
behavior end to end.

- [ ] **Step 1: Read the current full file**

Read `apps/web/lib/api.ts` in full before editing.

- [ ] **Step 2: Remove the dev-identity code and rewire `apiFetch`**

Remove `TENANT_ID_KEY`, `USER_ID_KEY`, `getDevIdentity()`, and `getDevIdentityHeaders()` entirely.

Replace `apiFetch`'s body:

```typescript
async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.headers ?? {}),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText;
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}
```

- [ ] **Step 3: Add the auth API wrappers**

Add near the top of the file, after `apiFetch`'s definition:

```typescript
export interface Identity {
  tenant_id: string;
  user_id: string;
}

export interface CurrentUser extends Identity {
  email: string;
}

export function signup(email: string, password: string): Promise<Identity> {
  return apiFetch<Identity>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function login(email: string, password: string): Promise<Identity> {
  return apiFetch<Identity>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<void> {
  await apiFetch<{ status: string }>("/auth/logout", { method: "POST" });
}

export async function me(): Promise<CurrentUser | null> {
  try {
    return await apiFetch<CurrentUser>("/auth/me");
  } catch {
    return null;
  }
}
```

- [ ] **Step 4: Find and fix the one caller of the removed `getDevIdentityHeaders()`**

Search the codebase for `getDevIdentityHeaders`:

```bash
grep -rn "getDevIdentityHeaders" apps/web
```

The plan's earlier investigation (M4b/M6a work, per `docs/STATUS.md`) noted exactly one call site —
the stem audio fetch in the player page (`apps/web/app/tracks/[id]/play/page.tsx`), which must hit
the API directly with the browser's `fetch()` (not through `apiFetch()`) for streaming reasons.
Read that call site, remove the `getDevIdentityHeaders()` call and the header spread it fed into
the `fetch(...)` options object, and add `credentials: "include"` to that same `fetch(...)` call's
options instead — the session cookie now travels the same way `apiFetch()`'s does.

- [ ] **Step 5: Verify the frontend still builds**

```bash
cd apps/web
npm run build
```

Expected: build succeeds with no TypeScript errors. (This will show real errors from any other
`getDevIdentity`/`getDevIdentityHeaders` reference Step 4's grep might have caught beyond the one
described — fix any that appear before proceeding.)

- [ ] **Step 6: Commit**

```bash
git add apps/web/lib/api.ts apps/web/app/tracks/[id]/play/page.tsx
git commit -m "M8: replace frontend dev-identity generation with credentialed fetch and real auth API calls"
```

---

### Task 9: Login/signup pages, auth context, route gating, manual verification

**Files:**
- Create: `apps/web/app/login/page.tsx`
- Create: `apps/web/app/signup/page.tsx`
- Create: `apps/web/lib/AuthContext.tsx`
- Modify: `apps/web/app/layout.tsx` (wrap children in the new provider)
- Modify: `apps/web/app/tracks/page.tsx` (redirect to `/login` when unauthenticated)
- Modify: `apps/web/app/tracks/[id]/page.tsx` (same gating — read this file first to find its exact
  current top-level structure before editing)

**Interfaces:**
- Consumes: `signup`, `login`, `logout`, `me`, `Identity`, `CurrentUser` (Task 8).

Per this project's working agreement, UI/glue code is exempt from test-first — this task's real
verification is the manual browser pass in the final step, not automated tests.

- [ ] **Step 1: Write `apps/web/lib/AuthContext.tsx`**

```typescript
"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { me, type CurrentUser } from "@/lib/api";

interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    const current = await me();
    setUser(current);
  };

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, refresh }}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth() must be used inside an AuthProvider");
  }
  return context;
}
```

- [ ] **Step 2: Wrap the app in `AuthProvider`**

Read `apps/web/app/layout.tsx`'s current full content first (shown in this plan's investigation as
of this milestone's start — confirm it still matches before editing, since Tasks 1-8 didn't touch
it). Add the import:

```typescript
import { AuthProvider } from "@/lib/AuthContext";
```

Change:

```tsx
      <body className="min-h-full flex flex-col">{children}</body>
```

to:

```tsx
      <body className="min-h-full flex flex-col">
        <AuthProvider>{children}</AuthProvider>
      </body>
```

- [ ] **Step 3: Write the signup page**

Create `apps/web/app/signup/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { signup } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";

export default function SignupPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      await signup(email, password);
      await refresh();
      router.push("/tracks");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="max-w-sm mx-auto py-12 px-6">
      <h1 className="text-2xl font-semibold mb-6">Sign up</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm text-zinc-600">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="border border-zinc-300 rounded px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm text-zinc-600">Password</span>
          <input
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="border border-zinc-300 rounded px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm text-zinc-600">Confirm password</span>
          <input
            type="password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            className="border border-zinc-300 rounded px-3 py-2"
          />
        </label>
        {error && <p className="text-red-600 text-sm">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="bg-zinc-900 text-white rounded px-3 py-2 disabled:opacity-50"
        >
          {submitting ? "Signing up..." : "Sign up"}
        </button>
      </form>
      <p className="text-sm text-zinc-500 mt-4">
        Already have an account? <Link href="/login" className="underline">Log in</Link>
      </p>
    </main>
  );
}
```

- [ ] **Step 4: Write the login page**

Create `apps/web/app/login/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { login } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";

export default function LoginPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      await refresh();
      router.push("/tracks");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="max-w-sm mx-auto py-12 px-6">
      <h1 className="text-2xl font-semibold mb-6">Log in</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm text-zinc-600">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="border border-zinc-300 rounded px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm text-zinc-600">Password</span>
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="border border-zinc-300 rounded px-3 py-2"
          />
        </label>
        {error && <p className="text-red-600 text-sm">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="bg-zinc-900 text-white rounded px-3 py-2 disabled:opacity-50"
        >
          {submitting ? "Logging in..." : "Log in"}
        </button>
      </form>
      <p className="text-sm text-zinc-500 mt-4">
        Need an account? <Link href="/signup" className="underline">Sign up</Link>
      </p>
    </main>
  );
}
```

- [ ] **Step 5: Gate `/tracks` and add a logout control**

Read `apps/web/app/tracks/page.tsx`'s current full content (shown in this plan's investigation).
Add the imports:

```typescript
import { useRouter } from "next/navigation";
import { logout } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";
```

At the top of the `TracksPage` component function body, add:

```typescript
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && user === null) {
      router.push("/login");
    }
  }, [authLoading, user, router]);
```

(This is a second `useEffect` alongside the existing `listTracks()` one — do not merge them, they
have different dependency arrays and different purposes.)

Add a logout button to the page's returned JSX, near the `<h1>`:

```tsx
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold">Tracks</h1>
        <button
          onClick={() => logout().then(() => router.push("/login"))}
          className="text-sm text-zinc-500 underline"
        >
          Log out
        </button>
      </div>
```

(Remove the old standalone `<h1 className="text-2xl font-semibold mb-6">Tracks</h1>` line this
replaces.)

Add an early return so the page doesn't flash track content before the redirect fires:

```typescript
  if (authLoading || user === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }
```

(Place this check before the existing `if (error)`/`if (tracks === null)` early returns.)

- [ ] **Step 6: Gate `apps/web/app/tracks/[id]/page.tsx`**

This file has no logout button of its own — it already links back to `/tracks` via
`BackToTracksLink` (where Step 5 put the logout control), so this step adds gating only, no
logout UI. Add the imports:

```typescript
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";
```

In `TrackEditorPage`, immediately after the existing `const { id } = use(props.params);` line, add:

```typescript
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && user === null) {
      router.push("/login");
    }
  }, [authLoading, user, router]);
```

Then add an early return for the unauthenticated/loading case immediately before the existing
`if (error && transcription === null) {` block:

```typescript
  if (authLoading || user === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }
```

No other lines in this file change — the existing `error`/`transcription === null`/
`lyrics_display_allowed`/`language !== "en"` branches and the main return are all unaffected.

- [ ] **Step 7: Verify the frontend builds**

```bash
cd apps/web
npm run build
```

Expected: succeeds with no TypeScript errors.

- [ ] **Step 8: Manual browser verification**

Start both the API (`docker compose up -d`, then `alembic upgrade head` if not already current, then
`uvicorn app.main:app --reload` from `services/api`) and the web app (`npm run dev` from
`apps/web`). In a real browser:

1. Visit `/tracks` while signed out — confirm it redirects to `/login`.
2. Sign up with a real email/password on `/signup` — confirm redirect to `/tracks`, and that the
   page loads (empty track list is fine for a fresh account).
3. Upload a track (existing upload flow) — confirm it succeeds and appears in the list, proving the
   session cookie is genuinely carrying identity through to the API.
4. Click "Log out" — confirm redirect to `/login`, and that visiting `/tracks` directly afterward
   redirects to `/login` again (not just the button-triggered navigation).
5. Log back in with the same email/password on `/login` — confirm it succeeds and the previously
   uploaded track is still there (proving the same account/tenant, not a new one).
6. Attempt to sign up again with the same email — confirm a visible error message (not a silent
   failure or a blank screen).
7. Attempt to log in with a wrong password — confirm a visible generic error message.
8. Open browser devtools' Application/Storage tab and confirm the `songbox_session` cookie is
   marked `HttpOnly` (not readable via `document.cookie` in the console) and that `localStorage` no
   longer contains `songbox-dev-tenant-id`/`songbox-dev-user-id` keys.

Report any failures found and fix them before proceeding to Task 10.

- [ ] **Step 9: Commit**

```bash
git add apps/web/lib/AuthContext.tsx apps/web/app/login/page.tsx apps/web/app/signup/page.tsx apps/web/app/layout.tsx apps/web/app/tracks/page.tsx "apps/web/app/tracks/[id]/page.tsx"
git commit -m "M8: add login/signup pages, auth context, and route gating"
```

---

### Task 10: Docs — ADR, PLAN.md, STATUS.md, DECISIONS_LOG.md

**Files:**
- Create: `docs/adr/0002-authentication-model.md`
- Modify: `docs/PLAN.md` (mark open question 9 resolved)
- Modify: `docs/STATUS.md` (new M8 entry)
- Modify: `docs/DECISIONS_LOG.md` (new entry)

**Interfaces:** None — this task only touches documentation, no code.

- [ ] **Step 1: Write the ADR**

Create `docs/adr/0002-authentication-model.md` with the standard Context/Decision/Consequences
structure (matching `docs/adr/0001-gpu-backend-abstraction.md`'s format), covering: the
one-user-per-tenant decision, DB-backed opaque session cookies chosen over JWT (and why —
server-side revocability without a blocklist), argon2id over a general-purpose hash, and each
explicit non-goal (no OAuth, no teams/roles, no email verification/reset, no dev-data migration, no
admin role on user accounts) with the reasoning from the approved design spec
(`docs/superpowers/specs/2026-08-31-real-authentication-design.md`) for each.

- [ ] **Step 2: Update `docs/PLAN.md`**

Find open question 9 (search for "New in M4b" near the authentication question) and add a note
that it's resolved by M8, pointing at `docs/adr/0002-authentication-model.md` — following the same
pattern open question 10 used when M6a resolved its read-path half (see that entry's "Read-path
resolved in M6a" phrasing for the exact style to match).

- [ ] **Step 3: Update `docs/STATUS.md`**

Add a new `## Done — M8 complete` section (matching the existing per-milestone section format),
listing what was built, the real files touched, and the honest scope boundary (no email
verification/reset, no OAuth, no teams). Update the "In flight"/"Next three actions" sections to
remove the real-authentication item and reflect what's actually still open afterward (the mic-bleed
manual test, the alignment accuracy gap, GitHub remote/CI).

- [ ] **Step 4: Update `docs/DECISIONS_LOG.md`**

Add an entry for the session-cookie-vs-JWT choice and the users/sessions-outside-RLS decision,
dated to when this task is actually executed (not this plan's authoring date, if they differ).

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0002-authentication-model.md docs/PLAN.md docs/STATUS.md docs/DECISIONS_LOG.md
git commit -m "M8: document the real-authentication milestone in ADR, PLAN, STATUS, and DECISIONS_LOG"
```
