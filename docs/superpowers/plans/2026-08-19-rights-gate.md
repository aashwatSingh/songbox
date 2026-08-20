# M1 Rights Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the rights gate — attestation records for the three ingress lanes, Chromaprint/AcoustID fingerprint checking, lane-specific resolution, and a hold-and-review flow — so that a known commercial recording uploaded under Lane A is held and an original recording passes, end to end through a real HTTP endpoint.

**Architecture:** A synchronous FastAPI endpoint (`POST /tracks/upload`) that fingerprints the uploaded file via ffmpeg's built-in Chromaprint muxer, looks it up through a swappable `AcoustIDClient` interface (a fixture-driven test double for now — no real API key exists yet), resolves the lane × match-result table into pass/hold, writes immutable `rights_declarations` + `tracks` + `fingerprint_matches` rows under Postgres row-level security, and exposes `confirm-attestation` (Lane A's second attestation) and `review-queue` (human hold-and-review) endpoints.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + Alembic (Postgres via `psycopg[binary]`), `minio` SDK (MinIO, already running via `docker-compose.yml`), ffmpeg (already installed, compiled with `--enable-chromaprint`), pytest.

## Global Constraints

These apply to every task below — copied from `CLAUDE.md` and `docs/superpowers/specs/2026-08-19-rights-gate-design.md`:

- Every table carries `tenant_id`; every query is tenant-scoped. Enforced here via Postgres RLS (`FORCE ROW LEVEL SECURITY`, not just `ENABLE`, since the app's DB role owns the tables and owners bypass RLS by default unless forced).
- **Two DB roles, not one** (discovered during Task 3): `songbox` (a genuine Postgres superuser, from `POSTGRES_USER`) is for migrations and admin-level introspection only — superusers unconditionally bypass RLS, so this role must never be used for tenant-scoped queries. `songbox_app` (created by Task 3's migration, no superuser/bypassrls) is what `db_session_for_tenant`/`AppSessionLocal` connects as, and is the only role RLS policies can ever actually constrain. Any future code that queries tenant data must go through `AppSessionLocal`/`db_session_for_tenant`, never `SessionLocal` directly.
- No `yt-dlp`/`youtube-dl`/`pytube`-class dependency, ever. `scripts/check_forbidden_deps.py` in CI covers this automatically — no manual step needed, just don't add one.
- ffmpeg is invoked as an argument array only, `-protocol_whitelist file`, never `shell=True`.
- Never log raw audio, lyrics, or signed URLs.
- No fabricated benchmark/accuracy numbers.
- **Auth is stubbed for M1**: `X-Dev-Tenant-Id` / `X-Dev-User-Id` request headers stand in for a real session (see design spec §"Auth is stubbed, not real"). Real auth is a separate future milestone.
- **AcoustID is mocked for M1**: no real API key exists. `HTTPAcoustIDClient` exists and is tested for its no-key-set behavior, but the only client actually exercised end-to-end in tests is `FixtureAcoustIDClient`.
- **This M1 upload endpoint is deliberately NOT hardened** (no magic-byte validation, no size limits, no sandboxing) — that is explicitly M2's job, applied to this exact endpoint.
- **AcoustID lookup failure (timeout/5xx/malformed) always holds, never silently passes.**
- **Lane C always holds on a fingerprint match**, even though the match might turn out to be a legitimately different PD/CC recording — that distinction needs a human, per the design spec.
- This is a Windows dev machine. All commands below use `services/api/.venv/Scripts/python.exe` (not `bin/python`), matching the pattern already established in M0.
- Every task's final verification step includes `ruff check .` and `mypy app` passing (in addition to the task's own tests), matching the bar `services/api` already holds from M0 — do not commit code that regresses either.
- Postgres (via `docker-compose.yml`, already running), Redis, and MinIO must be up (`docker compose up -d` from the repo root) before running any test in this plan — all of Tasks 1, 3, 8, 9, 10, 11 hit real Postgres and/or MinIO, not mocks.
- **Postgres is on host port 5433, not the default 5432.** This machine also runs a native Windows PostgreSQL 18 service that wins the port-5432 race on the host's IPv4 wildcard, silently shadowing the container for any `localhost`/`127.0.0.1` client. `docker-compose.yml` maps `5433:5432` for exactly this reason — every `DATABASE_URL` default in this plan already reflects that.

---

### Task 1: Database dependencies + engine/session module

**Files:**
- Modify: `services/api/pyproject.toml`
- Create: `services/api/app/db.py`
- Test: `services/api/tests/test_db.py`

**Interfaces:**
- Produces: `get_engine() -> Engine`, `SessionLocal: sessionmaker[Session]`, `db_session_for_tenant(tenant_id: uuid.UUID) -> Session` (later tasks depend on all three — `db_session_for_tenant` is what `get_db` in Task 4 wraps).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_db.py
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db import db_session_for_tenant, get_engine


def test_can_connect_and_select_1() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_db_session_for_tenant_sets_readable_tenant_context() -> None:
    tenant_id = uuid.uuid4()
    session = db_session_for_tenant(tenant_id)
    try:
        result = session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == str(tenant_id)
    finally:
        session.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Add dependencies and write the implementation**

Edit `services/api/pyproject.toml`'s `dependencies` and `dev` lists to:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.2",
    "alembic>=1.13",
    "python-multipart>=0.0.9",
    "minio>=7.2",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "ruff>=0.7",
    "mypy>=1.13",
    "pytest>=8.3",
]
```

(`httpx` moves to main `dependencies` since `HTTPAcoustIDClient` needs it at runtime, not just in tests.)

Run: `cd services/api && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"`

Create `services/api/app/db.py`:

```python
from __future__ import annotations

import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://songbox:songbox@localhost:5433/songbox"
)
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://songbox_app:songbox_app@localhost:5433/songbox"
)

_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

_app_engine = create_engine(APP_DATABASE_URL, pool_pre_ping=True)
AppSessionLocal = sessionmaker(bind=_app_engine, expire_on_commit=False)


def get_engine() -> Engine:
    return _engine


def db_session_for_tenant(tenant_id: uuid.UUID) -> Session:
    """Open a session, AS THE RESTRICTED songbox_app ROLE, and set the RLS tenant context
    for its transaction.

    This must connect through AppSessionLocal, not SessionLocal: SessionLocal's DATABASE_URL
    connects as the songbox role, which is a genuine Postgres superuser (POSTGRES_USER in the
    official postgres image is created via initdb as one) -- and superusers unconditionally
    bypass every RLS policy regardless of FORCE ROW LEVEL SECURITY, so no policy this project
    writes could ever actually apply to that connection. Real tenant isolation requires a
    non-superuser, non-BYPASSRLS role -- songbox_app, created and granted table privileges by
    Task 3's migration -- which is exactly what AppSessionLocal is for. SessionLocal / DATABASE_URL
    remain for migrations and admin-level introspection (e.g. the RLS-enabled/forced check in
    Task 3's test_db_rls.py, which reads pg_class and needs no elevated privilege but is
    conceptually an admin check, not a tenant-scoped query).

    Postgres's SET/SET LOCAL grammar does not accept bound parameters -- only literals -- so a
    parameterized SET LOCAL raises a syntax error at the driver level. set_config() is a regular
    function call and does accept one; its third argument (true) gives it the same
    transaction-scoped "local" semantics SET LOCAL would have, readable back via current_setting()
    exactly the same way (see Task 3's RLS policies). This must be the first statement executed
    on the session -- SQLAlchemy's Session begins its transaction lazily on first execute(), so
    this call itself starts it.
    """
    session = AppSessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
    except Exception:
        session.close()
        raise
    return session
```

(`get_db`, the FastAPI dependency version of this, is added in Task 4 once `Identity` exists to scope it by
— no throwaway version is needed here first.)

**Addendum, added when Task 3 discovered this gap:** the original version of this file only had
`DATABASE_URL`/`SessionLocal`, and `db_session_for_tenant` used `SessionLocal` directly. That works
for Task 1's own test (which only checks connectivity) but silently defeats the entire point of
Task 3's RLS migration, because the `songbox` role is a real Postgres superuser and superusers
always bypass RLS. `APP_DATABASE_URL`/`AppSessionLocal` and the role split above are the fix --
applied retroactively to this already-merged file as part of Task 3, since Task 3 is what surfaces
the gap and what creates the `songbox_app` role this now depends on.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: `2 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add services/api/pyproject.toml services/api/app/db.py services/api/tests/test_db.py
git commit -m "M1: add DB dependencies and engine/session module"
```

---

### Task 2: SQLAlchemy models + initial migration

**Files:**
- Create: `services/api/app/models.py`
- Create: `services/api/alembic.ini`
- Create: `services/api/alembic/env.py`
- Create: `services/api/alembic/script.py.mako`
- Create: `services/api/alembic/versions/0001_initial_tables.py`
- Test: `services/api/tests/test_models.py`

**Interfaces:**
- Consumes: nothing from Task 1 directly (models are independent of `db.py`, but the migration needs `DATABASE_URL`'s default from Task 1's convention).
- Produces: `Base`, `License`, `RightsDeclaration`, `Track`, `FingerprintMatch` (SQLAlchemy declarative model classes) — every later task that touches the DB imports these.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_models.py
from __future__ import annotations

from sqlalchemy import inspect

from app.db import get_engine
from app.models import FingerprintMatch, License, RightsDeclaration, Track

EXPECTED_TABLES = {
    "licenses": License,
    "rights_declarations": RightsDeclaration,
    "tracks": Track,
    "fingerprint_matches": FingerprintMatch,
}


def test_all_expected_tables_exist_after_migration() -> None:
    inspector = inspect(get_engine())
    existing = set(inspector.get_table_names())
    for table_name in EXPECTED_TABLES:
        assert table_name in existing, f"{table_name} missing -- did you run `alembic upgrade head`?"


def test_every_model_table_has_a_tenant_id_column() -> None:
    for table_name, model in EXPECTED_TABLES.items():
        columns = {c.name for c in model.__table__.columns}
        assert "tenant_id" in columns, f"{table_name} has no tenant_id column"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: Write the models**

Create `services/api/app/models.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, Boolean, DateTime, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    covers_recording: Mapped[bool] = mapped_column(Boolean, nullable=False)
    covers_lyrics: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RightsDeclaration(Base):
    __tablename__ = "rights_declarations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lane: Mapped[str] = mapped_column(String(1), nullable=False)  # "A" | "B" | "C"
    attestation_text: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("licenses.id"), nullable=True
    )
    pd_cc_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    pd_cc_license: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution_string: Mapped[str | None] = mapped_column(Text, nullable=True)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    rights_declaration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rights_declarations.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending_review|passed|rejected
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)


class FingerprintMatch(Base):
    __tablename__ = "fingerprint_matches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False)
    acoustid_response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    matched_release: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str] = mapped_column(String(20), nullable=False)  # no_match|held|confirmed|mismatch
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
```

Note: `key`/`tempo` are deliberately NOT on `Track` yet — they're M5 (structure analysis) outputs, added via a future migration when something populates them (see design spec).

Create `services/api/alembic.ini`:

```ini
[alembic]
script_location = alembic

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

Create `services/api/alembic/env.py`:

```python
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://songbox:songbox@localhost:5433/songbox"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `services/api/alembic/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Create `services/api/alembic/versions/0001_initial_tables.py`:

```python
"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-08-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "licenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("covers_recording", sa.Boolean(), nullable=False),
        sa.Column("covers_lyrics", sa.Boolean(), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "rights_declarations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lane", sa.String(length=1), nullable=False),
        sa.Column("attestation_text", sa.Text(), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_name", sa.Text(), nullable=True),
        sa.Column("license_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("licenses.id"), nullable=True),
        sa.Column("pd_cc_source", sa.Text(), nullable=True),
        sa.Column("pd_cc_license", sa.Text(), nullable=True),
        sa.Column("attribution_string", sa.Text(), nullable=True),
    )

    op.create_table(
        "tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "rights_declaration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rights_declarations.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
    )

    op.create_table(
        "fingerprint_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("track_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracks.id"), nullable=False),
        sa.Column("acoustid_response", postgresql.JSONB(), nullable=False),
        sa.Column("matched_release", sa.Text(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("fingerprint_matches")
    op.drop_table("tracks")
    op.drop_table("rights_declarations")
    op.drop_table("licenses")
```

Apply the migration:

Run: `cd services/api && ./.venv/Scripts/python.exe -m alembic upgrade head`
Expected: no errors; ends at revision `0001`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: `2 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/models.py services/api/alembic.ini services/api/alembic/ services/api/tests/test_models.py
git commit -m "M1: add rights-gate models and initial migration"
```

---

### Task 3: Row-level security migration + RLS tests

**Discovered while implementing this task:** RLS policies alone are not enough. `services/api/app/db.py`'s
only DB role so far is `songbox`, which is a genuine Postgres superuser (the official `postgres` image
creates `POSTGRES_USER` via initdb as one), and **Postgres superusers unconditionally bypass every RLS
policy regardless of `FORCE ROW LEVEL SECURITY`.** No policy this migration writes can ever apply to a
connection using that role. This task therefore also creates a second, restricted, non-superuser role
(`songbox_app`) with table-level grants but no bypass privilege, and updates `db.py` so
`db_session_for_tenant` connects through it — `db.py` was written in Task 1 before this gap was known, so
this task retroactively fixes it as part of making RLS actually real. See the addendum on `db.py` in
Task 1's section above for the full explanation.

**Files:**
- Create: `services/api/alembic/versions/0002_row_level_security.py`
- Modify: `services/api/app/db.py` (add `APP_DATABASE_URL`, `_app_engine`, `AppSessionLocal`; change
  `db_session_for_tenant` to use `AppSessionLocal` instead of `SessionLocal` — full replacement code is in
  Task 1's section above, already updated with this addendum)
- Test: `services/api/tests/test_db_rls.py`

**Interfaces:**
- Consumes: `db_session_for_tenant` (Task 1, modified by this task), `SessionLocal` (Task 1), the four model tables (Task 2).
- Produces: `AppSessionLocal` (Task 1's `db.py`, added by this task) — no other new callable; this task's
  main deliverable is enforced DB behavior, verified by the test itself.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_db_rls.py
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db import SessionLocal, db_session_for_tenant

RLS_TABLES = ("licenses", "rights_declarations", "tracks", "fingerprint_matches")


def test_every_table_has_row_level_security_enabled_and_forced() -> None:
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(:tables)"
            ),
            {"tables": list(RLS_TABLES)},
        ).all()
    finally:
        session.close()

    found = {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in rows}
    assert set(found) == set(RLS_TABLES)
    for table, (enabled, forced) in found.items():
        assert enabled, f"{table} does not have RLS enabled"
        assert forced, f"{table} does not FORCE RLS -- the owning role would bypass policies otherwise"


def test_tenant_cannot_see_another_tenants_license_row() -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    session_b = db_session_for_tenant(tenant_b)
    license_id = uuid.uuid4()
    session_b.execute(
        text(
            "INSERT INTO licenses (id, tenant_id, reference, covers_recording, covers_lyrics) "
            "VALUES (:id, :tenant_id, 'ref', true, true)"
        ),
        {"id": license_id, "tenant_id": tenant_b},
    )
    session_b.commit()
    session_b.close()

    session_a = db_session_for_tenant(tenant_a)
    rows = session_a.execute(text("SELECT id FROM licenses WHERE id = :id"), {"id": license_id}).all()
    session_a.close()
    assert rows == []

    session_b_read = db_session_for_tenant(tenant_b)
    rows_b = session_b_read.execute(
        text("SELECT id FROM licenses WHERE id = :id"), {"id": license_id}
    ).all()
    session_b_read.close()
    assert len(rows_b) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_db_rls.py -v`
Expected: FAIL — `relrowsecurity` is false (RLS not enabled yet), so the first test fails.

- [ ] **Step 3: Update `db.py`, then write the migration**

First, replace `services/api/app/db.py` in full with the updated version from Task 1's section above
(the one with `APP_DATABASE_URL`, `_app_engine`, `AppSessionLocal`, and `db_session_for_tenant` using
`AppSessionLocal`) — this file already exists from Task 1 and is being modified, not created.

Then create `services/api/alembic/versions/0002_row_level_security.py`:

```python
"""enable row level security

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19

"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("licenses", "rights_declarations", "tracks", "fingerprint_matches")

APP_ROLE = "songbox_app"
APP_ROLE_PASSWORD = "songbox_app"  # dev-only, matches the plaintext dev creds already in docker-compose.yml


def upgrade() -> None:
    # songbox_app is intentionally NOT superuser and NOT bypassrls -- that's the entire point.
    # CREATE ROLE has no IF NOT EXISTS in Postgres, so guard it with a DO block for idempotency
    # (re-running migrations against an existing dev DB, or migrating a fresh one, both work).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}';
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE songbox TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE CONNECT ON DATABASE songbox FROM {APP_ROLE}")
    # The role itself is intentionally left in place on downgrade (DROP ROLE can fail if anything
    # else references it, and leaving an unprivileged, ungranted role around is harmless).
```

The `current_setting('app.tenant_id', true)` second argument means "return NULL instead of erroring if unset" — so a session that never calls `db_session_for_tenant` (and thus never sets `app.tenant_id`) sees the policy evaluate to `tenant_id = NULL`, which is never true: default-deny, not an error.

`APP_DATABASE_URL`'s default in `db.py` (`postgresql+psycopg://songbox_app:songbox_app@localhost:5433/songbox`)
must match `APP_ROLE`/`APP_ROLE_PASSWORD` here exactly.

Run: `cd services/api && ./.venv/Scripts/python.exe -m alembic upgrade head`
Expected: ends at revision `0002`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_db_rls.py -v`
Expected: `2 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/alembic/versions/0002_row_level_security.py services/api/tests/test_db_rls.py services/api/app/db.py
git commit -m "M1: enforce row-level security on all rights-gate tables"
```

---

### Task 4: Dev auth stub

**Files:**
- Create: `services/api/app/auth.py`
- Test: `services/api/tests/test_auth.py`
- Modify: `services/api/app/db.py` (append the new `get_db` function — Task 1 deliberately left it out)

**Interfaces:**
- Produces: `Identity` (frozen dataclass: `tenant_id: uuid.UUID`, `user_id: uuid.UUID`), `get_identity` (FastAPI dependency, `Header` params `X-Dev-Tenant-Id`/`X-Dev-User-Id`) — every route from Task 9 onward depends on this. `get_db` (new) depends on `get_identity` and yields a tenant-scoped `Session` — every route from Task 9 onward depends on this too.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_auth.py
from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import Identity, get_identity

app = FastAPI()


@app.get("/whoami")
def whoami(identity: Identity = Depends(get_identity)) -> dict[str, str]:
    return {"tenant_id": str(identity.tenant_id), "user_id": str(identity.user_id)}


client = TestClient(app)


def test_missing_headers_returns_401() -> None:
    response = client.get("/whoami")
    assert response.status_code == 401


def test_valid_headers_returns_identity() -> None:
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    response = client.get("/whoami", headers={"X-Dev-Tenant-Id": tenant_id, "X-Dev-User-Id": user_id})
    assert response.status_code == 200
    assert response.json() == {"tenant_id": tenant_id, "user_id": user_id}


def test_invalid_uuid_returns_401() -> None:
    response = client.get(
        "/whoami", headers={"X-Dev-Tenant-Id": "not-a-uuid", "X-Dev-User-Id": "not-a-uuid"}
    )
    assert response.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/auth.py`:

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def get_identity(
    x_dev_tenant_id: str | None = Header(default=None, alias="X-Dev-Tenant-Id"),
    x_dev_user_id: str | None = Header(default=None, alias="X-Dev-User-Id"),
) -> Identity:
    if not x_dev_tenant_id or not x_dev_user_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "X-Dev-Tenant-Id and X-Dev-User-Id headers are required (dev auth stub -- see "
                "docs/superpowers/specs/2026-08-19-rights-gate-design.md)"
            ),
        )
    try:
        return Identity(tenant_id=uuid.UUID(x_dev_tenant_id), user_id=uuid.UUID(x_dev_user_id))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Dev identity headers must be valid UUIDs") from exc
```

Append to `services/api/app/db.py` (Task 1 deliberately left `get_db` out until `Identity` existed to scope it by):

```python
from collections.abc import Generator

from fastapi import Depends

from app.auth import Identity, get_identity


def get_db(identity: Identity = Depends(get_identity)) -> Generator[Session, None, None]:
    session = db_session_for_tenant(identity.tenant_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

(Add the three new imports — `from collections.abc import Generator`, `from fastapi import Depends`, and
`from app.auth import Identity, get_identity` — near the top of `db.py` alongside its existing imports.)

`services/api/tests/test_db.py` and `services/api/tests/test_models.py`'s use of `get_engine`/raw
`SessionLocal` is unaffected (they don't call `get_db`), so no changes needed there.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all tests pass (Tasks 1-4's tests together).

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/auth.py services/api/app/db.py services/api/tests/test_auth.py
git commit -m "M1: add dev auth stub and wire it into get_db for RLS"
```

---

### Task 5: AcoustID client (fixture + real) and fixtures

**Files:**
- Create: `services/api/app/acoustid/__init__.py`
- Create: `services/api/app/acoustid/client.py`
- Create: `services/api/app/acoustid/fixtures.py`
- Test: `services/api/tests/test_acoustid_client.py`

**Interfaces:**
- Produces: `AcoustIDMatch` (frozen dataclass: `release_title: str`, `recording_id: str`, `score: float`), `AcoustIDResult` (frozen dataclass: `matches: list[AcoustIDMatch]`, `error: str | None = None`, property `matched: bool`), `AcoustIDClient` (Protocol: `lookup(fingerprint: str, duration_seconds: float) -> AcoustIDResult`), `HTTPAcoustIDClient`, `FixtureAcoustIDClient`, and constants `KNOWN_MATCH_RESULT`, `NO_MATCH_RESULT`, `ERROR_RESULT` in `fixtures.py`. Task 7 (gate logic) and Task 9 (upload endpoint) both depend on these exact names.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_acoustid_client.py
from __future__ import annotations

from app.acoustid.client import FixtureAcoustIDClient, HTTPAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT, NO_MATCH_RESULT


def test_fixture_client_returns_configured_match() -> None:
    client = FixtureAcoustIDClient({"fp-known-match": KNOWN_MATCH_RESULT})
    result = client.lookup("fp-known-match", duration_seconds=180.0)
    assert result.matched
    assert result.matches[0].release_title == "A Commercial Release (fixture)"


def test_fixture_client_returns_no_match_for_unknown_fingerprint() -> None:
    client = FixtureAcoustIDClient({"fp-known-match": KNOWN_MATCH_RESULT})
    result = client.lookup("fp-totally-unknown", duration_seconds=180.0)
    assert result == NO_MATCH_RESULT
    assert not result.matched


def test_http_client_without_api_key_returns_error(monkeypatch) -> None:
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    client = HTTPAcoustIDClient(api_key=None)
    result = client.lookup("some-fingerprint", duration_seconds=180.0)
    assert result.error == "ACOUSTID_API_KEY is not set"
    assert not result.matched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_acoustid_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.acoustid'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/acoustid/__init__.py` (empty file).

Create `services/api/app/acoustid/client.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class AcoustIDMatch:
    release_title: str
    recording_id: str
    score: float


@dataclass(frozen=True)
class AcoustIDResult:
    matches: list[AcoustIDMatch]
    error: str | None = None  # set when the lookup itself failed (timeout, 5xx, malformed)

    @property
    def matched(self) -> bool:
        return bool(self.matches)


class AcoustIDClient(Protocol):
    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult: ...


class HTTPAcoustIDClient:
    """Real AcoustID API client. Reads the API key from ACOUSTID_API_KEY (unset until one exists)."""

    BASE_URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, api_key: str | None = None, timeout_seconds: float = 5.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("ACOUSTID_API_KEY")
        self._timeout_seconds = timeout_seconds

    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult:
        if not self._api_key:
            return AcoustIDResult(matches=[], error="ACOUSTID_API_KEY is not set")
        try:
            response = httpx.get(
                self.BASE_URL,
                params={
                    "client": self._api_key,
                    "fingerprint": fingerprint,
                    "duration": int(duration_seconds),
                    "meta": "recordings+releasegroups",
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return AcoustIDResult(matches=[], error=str(exc))

        if data.get("status") != "ok":
            return AcoustIDResult(matches=[], error=f"AcoustID returned status={data.get('status')}")

        matches = [
            AcoustIDMatch(
                release_title=(recording.get("releasegroups") or [{}])[0].get("title", "unknown"),
                recording_id=recording.get("id", ""),
                score=result.get("score", 0.0),
            )
            for result in data.get("results", [])
            for recording in result.get("recordings", [])
        ]
        return AcoustIDResult(matches=matches)


class FixtureAcoustIDClient:
    """Test double: returns canned results keyed by exact fingerprint string."""

    def __init__(self, fixtures: dict[str, AcoustIDResult]) -> None:
        self._fixtures = fixtures

    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult:
        return self._fixtures.get(fingerprint, AcoustIDResult(matches=[]))
```

Create `services/api/app/acoustid/fixtures.py`:

```python
"""Canned AcoustID fixture data for tests. Not real AcoustID responses -- synthetic data shaped
like the real API's output, keyed at use-site by whatever fingerprint the test's own synthetic
audio actually produces (see tests/conftest.py's synthetic_wav fixture)."""

from __future__ import annotations

from app.acoustid.client import AcoustIDMatch, AcoustIDResult

KNOWN_MATCH_RESULT = AcoustIDResult(
    matches=[
        AcoustIDMatch(
            release_title="A Commercial Release (fixture)",
            recording_id="11111111-1111-1111-1111-111111111111",
            score=0.95,
        )
    ]
)

NO_MATCH_RESULT = AcoustIDResult(matches=[])

ERROR_RESULT = AcoustIDResult(matches=[], error="simulated AcoustID timeout")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_acoustid_client.py -v`
Expected: `3 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/acoustid/ services/api/tests/test_acoustid_client.py
git commit -m "M1: add AcoustID client interface, HTTP impl, and fixture test double"
```

---

### Task 6: Chromaprint fingerprinting via ffmpeg

**Files:**
- Create: `services/api/app/fingerprint.py`
- Create: `services/api/tests/conftest.py`
- Test: `services/api/tests/test_fingerprint.py`

**Interfaces:**
- Produces: `Fingerprint` (frozen dataclass: `value: str`, `duration_seconds: float`), `FingerprintError` (exception), `fingerprint_audio(path: Path) -> Fingerprint`. Task 9 depends on all three. `conftest.py`'s `synthetic_wav` fixture is reused by Task 9's tests too.

- [ ] **Step 1: Write the failing test**

Create `services/api/tests/conftest.py`:

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def synthetic_wav(tmp_path: Path) -> Path:
    """A tiny synthetic tone, generated fresh each test run -- not a real recording."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH to run this test"
    out_path = tmp_path / "tone.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return out_path
```

Create `services/api/tests/test_fingerprint.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.fingerprint import FingerprintError, fingerprint_audio


def test_fingerprint_audio_returns_value_and_duration(synthetic_wav: Path) -> None:
    result = fingerprint_audio(synthetic_wav)
    assert result.value
    assert 2.5 < result.duration_seconds < 3.5


def test_fingerprint_audio_is_deterministic_for_same_input(synthetic_wav: Path) -> None:
    first = fingerprint_audio(synthetic_wav)
    second = fingerprint_audio(synthetic_wav)
    assert first.value == second.value


def test_fingerprint_audio_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FingerprintError):
        fingerprint_audio(tmp_path / "does-not-exist.wav")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.fingerprint'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/fingerprint.py`:

```python
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FingerprintError(Exception):
    """Raised when ffmpeg/ffprobe cannot produce a fingerprint for the given file."""


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration_seconds: float


def fingerprint_audio(path: Path) -> Fingerprint:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise FingerprintError("ffmpeg/ffprobe not found on PATH")

    duration_result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if duration_result.returncode != 0 or not duration_result.stdout.strip():
        raise FingerprintError(f"ffprobe could not read duration: {duration_result.stderr.strip()}")
    duration_seconds = float(duration_result.stdout.strip())

    fp_result = subprocess.run(
        [
            ffmpeg,
            "-protocol_whitelist",
            "file",
            "-i",
            str(path),
            "-f",
            "chromaprint",
            "-fp_format",
            "base64",
            "-",
        ],
        capture_output=True,
    )
    if fp_result.returncode != 0 or not fp_result.stdout.strip():
        raise FingerprintError(
            f"ffmpeg could not fingerprint {path}: {fp_result.stderr.decode(errors='replace').strip()}"
        )

    return Fingerprint(value=fp_result.stdout.decode().strip(), duration_seconds=duration_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py -v`
Expected: `3 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/fingerprint.py services/api/tests/conftest.py services/api/tests/test_fingerprint.py
git commit -m "M1: add ffmpeg-based Chromaprint fingerprinting"
```

---

### Task 7: Gate resolution logic

**Files:**
- Create: `services/api/app/gate.py`
- Test: `services/api/tests/test_gate.py`

**Interfaces:**
- Consumes: `AcoustIDResult` (Task 5).
- Produces: `GateOutcome` (str Enum: `PASSED = "passed"`, `HELD = "pending_review"`), `FingerprintResolution` (str Enum: `NO_MATCH = "no_match"`, `HELD = "held"`, `CONFIRMED = "confirmed"`, `MISMATCH = "mismatch"`), `GateDecision` (frozen dataclass: `outcome: GateOutcome`, `resolution: FingerprintResolution`, `reason: str`), `resolve_lane_outcome(lane: str, acoustid_result: AcoustIDResult, license_covers_recording: bool | None = None) -> GateDecision`. Task 9 (upload), Task 10 (confirm-attestation), and Task 11 (review-queue) all import `FingerprintResolution` for its exact `.value` strings.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_gate.py
from __future__ import annotations

from app.acoustid.fixtures import ERROR_RESULT, KNOWN_MATCH_RESULT, NO_MATCH_RESULT
from app.gate import FingerprintResolution, GateOutcome, resolve_lane_outcome


def test_no_match_always_passes_regardless_of_lane() -> None:
    for lane in ("A", "B", "C"):
        decision = resolve_lane_outcome(lane, NO_MATCH_RESULT)
        assert decision.outcome == GateOutcome.PASSED
        assert decision.resolution == FingerprintResolution.NO_MATCH


def test_lane_a_match_always_holds() -> None:
    decision = resolve_lane_outcome("A", KNOWN_MATCH_RESULT)
    assert decision.outcome == GateOutcome.HELD


def test_lane_b_match_with_covering_license_passes() -> None:
    decision = resolve_lane_outcome("B", KNOWN_MATCH_RESULT, license_covers_recording=True)
    assert decision.outcome == GateOutcome.PASSED
    assert decision.resolution == FingerprintResolution.CONFIRMED


def test_lane_b_match_without_covering_license_holds() -> None:
    decision = resolve_lane_outcome("B", KNOWN_MATCH_RESULT, license_covers_recording=False)
    assert decision.outcome == GateOutcome.HELD
    assert decision.resolution == FingerprintResolution.MISMATCH


def test_lane_c_match_always_holds_even_though_it_might_be_legitimately_pd() -> None:
    decision = resolve_lane_outcome("C", KNOWN_MATCH_RESULT)
    assert decision.outcome == GateOutcome.HELD


def test_acoustid_error_holds_rather_than_passing_silently() -> None:
    for lane in ("A", "B", "C"):
        decision = resolve_lane_outcome(lane, ERROR_RESULT)
        assert decision.outcome == GateOutcome.HELD, "a flaky AcoustID call must never silently pass"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.gate'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/gate.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.acoustid.client import AcoustIDResult


class GateOutcome(str, Enum):
    PASSED = "passed"
    HELD = "pending_review"


class FingerprintResolution(str, Enum):
    NO_MATCH = "no_match"
    HELD = "held"
    CONFIRMED = "confirmed"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    resolution: FingerprintResolution
    reason: str


def resolve_lane_outcome(
    lane: str,
    acoustid_result: AcoustIDResult,
    license_covers_recording: bool | None = None,
) -> GateDecision:
    """Implements the lane x match-result table from
    docs/superpowers/specs/2026-08-19-rights-gate-design.md's Gate flow section."""

    if acoustid_result.error:
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason=f"AcoustID lookup failed ({acoustid_result.error}); holding for manual review",
        )

    if not acoustid_result.matched:
        return GateDecision(
            outcome=GateOutcome.PASSED,
            resolution=FingerprintResolution.NO_MATCH,
            reason="no fingerprint match found",
        )

    if lane == "A":
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason="fingerprint matched a commercial release; needs a confirming attestation",
        )

    if lane == "B":
        if license_covers_recording:
            return GateDecision(
                outcome=GateOutcome.PASSED,
                resolution=FingerprintResolution.CONFIRMED,
                reason="matched release is covered by the license on file",
            )
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.MISMATCH,
            reason="matched release is not covered by the license on file",
        )

    if lane == "C":
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason=(
                "fingerprint matched an existing recording; PD/CC claims always need manual "
                "verification on a match"
            ),
        )

    raise ValueError(f"unknown lane: {lane!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_gate.py -v`
Expected: `6 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/gate.py services/api/tests/test_gate.py
git commit -m "M1: add lane x match-result gate resolution logic"
```

---

### Task 8: MinIO storage wrapper

**Files:**
- Create: `services/api/app/storage.py`
- Test: `services/api/tests/test_storage.py`

**Interfaces:**
- Produces: `get_minio_client() -> Minio`, `ensure_bucket(client: Minio) -> None`, `save_track_file(client: Minio, tenant_id: uuid.UUID, filename: str, data: bytes) -> str` (returns the storage key). Task 9 depends on `get_minio_client` and `save_track_file`.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_storage.py
from __future__ import annotations

import uuid

from app.storage import get_minio_client, save_track_file


def test_save_track_file_round_trips_through_minio() -> None:
    client = get_minio_client()
    tenant_id = uuid.uuid4()
    data = b"not real audio, just test bytes"

    storage_key = save_track_file(client, tenant_id, "song.wav", data)

    assert storage_key.startswith(f"{tenant_id}/")
    response = client.get_object("songbox-tracks", storage_key)
    try:
        assert response.read() == data
    finally:
        response.close()
        response.release_conn()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.storage'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/storage.py`:

```python
from __future__ import annotations

import io
import os
import uuid

from minio import Minio

_BUCKET = "songbox-tracks"


def get_minio_client() -> Minio:
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    access_key = os.environ.get("MINIO_ACCESS_KEY", "songbox")
    secret_key = os.environ.get("MINIO_SECRET_KEY", "songbox-dev-only")
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)


def ensure_bucket(client: Minio) -> None:
    if not client.bucket_exists(_BUCKET):
        client.make_bucket(_BUCKET)


def save_track_file(client: Minio, tenant_id: uuid.UUID, filename: str, data: bytes) -> str:
    ensure_bucket(client)
    storage_key = f"{tenant_id}/{uuid.uuid4()}-{filename}"
    client.put_object(_BUCKET, storage_key, io.BytesIO(data), length=len(data))
    return storage_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_storage.py -v`
Expected: `1 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/storage.py services/api/tests/test_storage.py
git commit -m "M1: add MinIO storage wrapper for uploaded tracks"
```

---

### Task 9: `POST /tracks/upload` endpoint (end-to-end integration)

**Files:**
- Create: `services/api/app/routes/__init__.py`
- Create: `services/api/app/routes/tracks.py`
- Modify: `services/api/app/main.py`
- Test: `services/api/tests/test_tracks_upload.py`

**Interfaces:**
- Consumes: `Identity`/`get_identity` (Task 4), `get_db` (Task 4), `AcoustIDClient`/`HTTPAcoustIDClient`/`FixtureAcoustIDClient` (Task 5), `fingerprint_audio`/`FingerprintError` (Task 6), `resolve_lane_outcome`/`FingerprintResolution` (Task 7), `get_minio_client`/`save_track_file` (Task 8), `License`/`RightsDeclaration`/`Track`/`FingerprintMatch` (Task 2).
- Produces: `router` (FastAPI `APIRouter`), `get_acoustid_client` (dependency function — Task 10 and 11's tests import this exact name to override it), `UploadResponse` (pydantic model: `track_id: uuid.UUID`, `status: str`, `reason: str`).

This is the task that proves M1's actual "done when" criterion, so its test is the most important one in this plan.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_tracks_upload.py
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _make_tone(tmp_path: Path, frequency: int) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / f"tone-{frequency}.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0
    return out_path


@pytest.fixture
def commercial_tone(tmp_path: Path) -> Path:
    return _make_tone(tmp_path, frequency=440)


@pytest.fixture
def original_tone(tmp_path: Path) -> Path:
    return _make_tone(tmp_path, frequency=880)


def test_lane_a_upload_of_known_commercial_fingerprint_is_held(commercial_tone: Path) -> None:
    known_fp = fingerprint_audio(commercial_tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with commercial_tone.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert response.status_code == 200
    assert response.json()["status"] == "pending_review"


def test_lane_a_upload_of_original_recording_passes(original_tone: Path) -> None:
    # No entry for this fingerprint in the fixture client at all -> no match -> passes.
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with original_tone.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert response.status_code == 200
    assert response.json()["status"] == "passed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_tracks_upload.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routes'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/routes/__init__.py` (empty file).

Create `services/api/app/routes/tracks.py`:

```python
from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.acoustid.client import AcoustIDClient, HTTPAcoustIDClient
from app.auth import Identity, get_identity
from app.db import get_db
from app.fingerprint import FingerprintError, fingerprint_audio
from app.gate import resolve_lane_outcome
from app.models import FingerprintMatch, License, RightsDeclaration, Track
from app.storage import get_minio_client, save_track_file

router = APIRouter()


def get_acoustid_client() -> AcoustIDClient:
    return HTTPAcoustIDClient()


class UploadResponse(BaseModel):
    track_id: uuid.UUID
    status: str
    reason: str


@router.post("/tracks/upload", response_model=UploadResponse)
def upload_track(
    request: Request,
    file: UploadFile = File(...),
    lane: str = Form(...),
    attestation_text: str = Form(...),
    license_id: uuid.UUID | None = Form(default=None),
    pd_cc_source: str | None = Form(default=None),
    pd_cc_license: str | None = Form(default=None),
    attribution_string: str | None = Form(default=None),
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
    acoustid_client: AcoustIDClient = Depends(get_acoustid_client),
) -> UploadResponse:
    if lane not in ("A", "B", "C"):
        raise HTTPException(status_code=422, detail="lane must be one of A, B, C")

    data = file.file.read()
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "upload").suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            fp = fingerprint_audio(Path(tmp.name))
        except FingerprintError as exc:
            raise HTTPException(status_code=422, detail=f"could not fingerprint audio: {exc}") from exc

    license_covers_recording: bool | None = None
    if lane == "B":
        if license_id is None:
            raise HTTPException(status_code=422, detail="lane B requires license_id")
        license_row = db.get(License, license_id)
        if license_row is None or license_row.tenant_id != identity.tenant_id:
            raise HTTPException(status_code=422, detail="license_id not found for this tenant")
        license_covers_recording = license_row.covers_recording

    acoustid_result = acoustid_client.lookup(fp.value, fp.duration_seconds)
    decision = resolve_lane_outcome(lane, acoustid_result, license_covers_recording)

    minio_client = get_minio_client()
    storage_key = save_track_file(minio_client, identity.tenant_id, file.filename or "upload", data)

    declaration = RightsDeclaration(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        lane=lane,
        attestation_text=attestation_text,
        ip_address=request.client.host if request.client else "unknown",
        created_at=datetime.now(UTC),
        license_id=license_id,
        pd_cc_source=pd_cc_source,
        pd_cc_license=pd_cc_license,
        attribution_string=attribution_string,
    )
    db.add(declaration)

    track = Track(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        duration_seconds=fp.duration_seconds,
        fingerprint=fp.value,
        rights_declaration_id=declaration.id,
        status=decision.outcome.value,
        storage_key=storage_key,
    )
    db.add(track)

    match_row = FingerprintMatch(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        acoustid_response={"matched": acoustid_result.matched, "error": acoustid_result.error},
        matched_release=acoustid_result.matches[0].release_title if acoustid_result.matches else None,
        resolution=decision.resolution.value,
    )
    db.add(match_row)

    return UploadResponse(track_id=track.id, status=track.status, reason=decision.reason)
```

Update `services/api/app/main.py` (replace its full contents):

```python
from fastapi import FastAPI

from app.routes.tracks import router as tracks_router

app = FastAPI(title="Songbox API")

app.include_router(tracks_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_tracks_upload.py -v`
Expected: `2 passed` — this is M1's actual "done when": a known-commercial fixture is held, an original one passes, through the real HTTP endpoint.

Run the full suite too: `./.venv/Scripts/python.exe -m pytest -v`
Expected: all tests across Tasks 1-9 pass together.

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/routes/ services/api/app/main.py services/api/tests/test_tracks_upload.py
git commit -m "M1: wire up POST /tracks/upload end to end"
```

---

### Task 10: `POST /tracks/{id}/confirm-attestation` endpoint

**Files:**
- Modify: `services/api/app/routes/tracks.py` (append to end of file)
- Test: `services/api/tests/test_confirm_attestation.py`

**Interfaces:**
- Consumes: `HEADERS`, `_make_tone` from `tests/test_tracks_upload.py` (Task 9), `get_acoustid_client` (Task 9), `FingerprintResolution` (Task 7).
- Produces: `ConfirmAttestationRequest` (pydantic: `release_name: str`), `ConfirmAttestationResponse` (pydantic: `track_id: uuid.UUID`, `status: str`).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_confirm_attestation.py
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.test_tracks_upload import HEADERS, _make_tone

client = TestClient(app)


def test_confirm_attestation_moves_held_track_to_passed(tmp_path: Path) -> None:
    tone = _make_tone(tmp_path, frequency=523)
    known_fp = fingerprint_audio(tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with tone.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert upload_response.json()["status"] == "pending_review"
        track_id = upload_response.json()["track_id"]

        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "My Own Unreleased Demo"},
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "passed"


def test_confirm_attestation_404s_for_unknown_track() -> None:
    response = client.post(
        f"/tracks/{uuid.uuid4()}/confirm-attestation",
        headers=HEADERS,
        json={"release_name": "whatever"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_confirm_attestation.py -v`
Expected: FAIL — `404 Not Found` for the route itself (endpoint doesn't exist yet), so both tests fail (the first because the initial POST 404s, the second's assertion of 404 might coincidentally "pass" for the wrong reason — verify by checking the first test's failure message shows a routing 404 before trusting the second).

- [ ] **Step 3: Write the implementation**

Append to `services/api/app/routes/tracks.py` (add `from app.gate import FingerprintResolution` to the existing imports, and add `from sqlalchemy import select` too):

```python
from sqlalchemy import select

from app.gate import FingerprintResolution


class ConfirmAttestationRequest(BaseModel):
    release_name: str


class ConfirmAttestationResponse(BaseModel):
    track_id: uuid.UUID
    status: str


@router.post("/tracks/{track_id}/confirm-attestation", response_model=ConfirmAttestationResponse)
def confirm_attestation(
    track_id: uuid.UUID,
    body: ConfirmAttestationRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> ConfirmAttestationResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "pending_review":
        raise HTTPException(
            status_code=409, detail=f"track is not pending review (status={track.status})"
        )

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    if declaration is None or declaration.lane != "A":
        raise HTTPException(status_code=409, detail="confirm-attestation is only valid for lane A")

    match_stmt = (
        select(FingerprintMatch)
        .where(FingerprintMatch.track_id == track.id)
        .order_by(FingerprintMatch.id.desc())
    )
    match_row = db.execute(match_stmt).scalars().first()
    if match_row is not None:
        match_row.resolution = FingerprintResolution.CONFIRMED.value
        match_row.reviewer_id = identity.user_id

    # rights_declarations rows are immutable -- record the stronger attestation as a new
    # row that supersedes the original, never mutate the original in place.
    stronger = RightsDeclaration(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        lane="A",
        attestation_text=declaration.attestation_text,
        ip_address=declaration.ip_address,
        created_at=datetime.now(UTC),
        release_name=body.release_name,
    )
    db.add(stronger)

    track.status = "passed"
    track.rights_declaration_id = stronger.id

    return ConfirmAttestationResponse(track_id=track.id, status=track.status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_confirm_attestation.py -v`
Expected: `2 passed`

Run the full suite: `./.venv/Scripts/python.exe -m pytest -v`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/routes/tracks.py services/api/tests/test_confirm_attestation.py
git commit -m "M1: add Lane A confirm-attestation endpoint"
```

---

### Task 11: `GET /review-queue` and `POST /review-queue/{id}/resolve` endpoints

**Files:**
- Create: `services/api/app/routes/review_queue.py`
- Modify: `services/api/app/main.py`
- Test: `services/api/tests/test_review_queue.py`

**Interfaces:**
- Consumes: `Identity`/`get_identity` (Task 4), `get_db` (Task 4), `Track`/`FingerprintMatch` (Task 2), `FingerprintResolution` (Task 7), `HEADERS`/`_make_tone` (Task 9's test file).
- Produces: `router` (a second `APIRouter`, included alongside `tracks_router` in `main.py`).

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_review_queue.py
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.test_tracks_upload import HEADERS, _make_tone

client = TestClient(app)


def _upload_held_track(tmp_path: Path, frequency: int) -> str:
    tone = _make_tone(tmp_path, frequency=frequency)
    known_fp = fingerprint_audio(tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with tone.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.json()["status"] == "pending_review"
    track_id: str = response.json()["track_id"]
    return track_id


def test_held_track_appears_in_review_queue(tmp_path: Path) -> None:
    track_id = _upload_held_track(tmp_path, frequency=659)
    response = client.get("/review-queue", headers=HEADERS)
    assert response.status_code == 200
    ids = [item["track_id"] for item in response.json()]
    assert track_id in ids


def test_resolving_review_approve_passes_the_track(tmp_path: Path) -> None:
    track_id = _upload_held_track(tmp_path, frequency=698)
    response = client.post(f"/review-queue/{track_id}/resolve", headers=HEADERS, json={"approve": True})
    assert response.status_code == 200
    assert response.json()["status"] == "passed"


def test_resolving_review_reject_rejects_the_track(tmp_path: Path) -> None:
    track_id = _upload_held_track(tmp_path, frequency=740)
    response = client.post(f"/review-queue/{track_id}/resolve", headers=HEADERS, json={"approve": False})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_review_queue.py -v`
Expected: FAIL with `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Write the implementation**

Create `services/api/app/routes/review_queue.py`:

```python
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Identity, get_identity
from app.db import get_db
from app.gate import FingerprintResolution
from app.models import FingerprintMatch, Track

router = APIRouter()


class ReviewQueueItem(BaseModel):
    track_id: uuid.UUID
    status: str
    match_id: uuid.UUID
    resolution: str
    matched_release: str | None


class ResolveReviewRequest(BaseModel):
    approve: bool


class ResolveReviewResponse(BaseModel):
    track_id: uuid.UUID
    status: str


@router.get("/review-queue", response_model=list[ReviewQueueItem])
def list_review_queue(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[ReviewQueueItem]:
    stmt = (
        select(Track, FingerprintMatch)
        .join(FingerprintMatch, FingerprintMatch.track_id == Track.id)
        .where(Track.status == "pending_review")
    )
    rows = db.execute(stmt).all()
    return [
        ReviewQueueItem(
            track_id=track.id,
            status=track.status,
            match_id=match.id,
            resolution=match.resolution,
            matched_release=match.matched_release,
        )
        for track, match in rows
    ]


@router.post("/review-queue/{track_id}/resolve", response_model=ResolveReviewResponse)
def resolve_review(
    track_id: uuid.UUID,
    body: ResolveReviewRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> ResolveReviewResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "pending_review":
        raise HTTPException(
            status_code=409, detail=f"track is not pending review (status={track.status})"
        )

    match_stmt = (
        select(FingerprintMatch)
        .where(FingerprintMatch.track_id == track.id)
        .order_by(FingerprintMatch.id.desc())
    )
    match_row = db.execute(match_stmt).scalars().first()
    if match_row is not None:
        match_row.resolution = (
            FingerprintResolution.CONFIRMED if body.approve else FingerprintResolution.MISMATCH
        ).value
        match_row.reviewer_id = identity.user_id

    # "rejected" is a human-review outcome, not something the automated gate ever produces
    # itself (GateOutcome only has PASSED/HELD) -- it only exists from this endpoint.
    track.status = "passed" if body.approve else "rejected"

    return ResolveReviewResponse(track_id=track.id, status=track.status)
```

Update `services/api/app/main.py` (replace its full contents):

```python
from fastapi import FastAPI

from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

app = FastAPI(title="Songbox API")

app.include_router(tracks_router)
app.include_router(review_queue_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_review_queue.py -v`
Expected: `3 passed`

Run the full suite: `./.venv/Scripts/python.exe -m pytest -v`
Expected: every test across all 11 tasks passes.

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/routes/review_queue.py services/api/app/main.py services/api/tests/test_review_queue.py
git commit -m "M1: add review-queue list and resolve endpoints"
```

---

## After Task 11

Update `docs/STATUS.md` to mark M1 done (mirroring how M0's completion was recorded), noting explicitly:
- M1's own "done when" criterion is proven by `test_lane_a_upload_of_known_commercial_fingerprint_is_held` and `test_lane_a_upload_of_original_recording_passes` in Task 9.
- What's deliberately deferred: real auth, a real AcoustID key, upload hardening (M2), rate-limiting/abuse-alerting, `key`/`tempo` track fields, admin roles — all listed in the design spec's "Out of scope for M1" section, so nothing here should come as a surprise later.
