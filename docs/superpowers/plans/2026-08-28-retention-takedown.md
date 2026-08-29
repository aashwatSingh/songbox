# M7a: Retention Purge + Takedown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a retention purge script that deletes uploads which never passed the rights gate, and
an admin-gated takedown endpoint that tombstones a track while removing its content — both built on
one shared deletion core.

**Architecture:** A new `app/deletion.py` module owns "delete everything a track's content-bearing
tables/storage own" as one function, reused by both features. Both features are the first
legitimate cross-tenant database access in this codebase, using the existing unrestricted
`SessionLocal` (the `songbox` superuser role) rather than the RLS-scoped `AppSessionLocal` every
other endpoint uses.

**Tech Stack:** Same FastAPI + SQLAlchemy 2.0 + Alembic + MinIO stack as the rest of `services/api`.
No new dependencies.

## Global Constraints

- Retention purge only ever touches tracks whose `status` is `pending_review` or `rejected` —
  never `passed`, regardless of age. `RETENTION_WINDOW_DAYS = 30` is a stated policy choice, not a
  measured value.
- Retention-purged tracks are **hard-deleted** (`Track` row and its `RightsDeclaration` both
  removed). Takedown tracks are **tombstoned** (`Track` row survives with `status="taken_down"`,
  `takedown_reason`, `takedown_at`; content is removed).
- The takedown endpoint is gated by `X-Admin-Key` (checked via `secrets.compare_digest`, constant
  time), separate from the dev-tenant-header identity scheme every other endpoint uses. If
  `ADMIN_API_KEY` is unset, the gate fails closed (500), never silently open.
- Both retention purge and takedown use `SessionLocal`/a new `get_admin_db` dependency (the
  unrestricted superuser role) — never the tenant-scoped session, since both must reach across
  tenants by design.
- `CLAUDE.md`: never log raw audio or lyrics. The purge script's stdout output is a count and
  track IDs only.
- No frontend changes. No new scheduler/cron infrastructure — the purge script is standalone,
  matching `scripts/benchmark_pitch.py`'s existing convention.

---

### Task 1: Shared deletion core

**Files:**
- Create: `services/api/app/deletion.py`
- Modify: `services/api/app/storage.py`
- Test: `services/api/tests/test_deletion.py`

**Interfaces:**
- Consumes: `app.models.Track/Stem/FingerprintMatch/Transcription/KaraokePackage` (all existing,
  unchanged), `app.storage.get_minio_client` (existing).
- Produces: `app.storage.delete_track_file(client: Minio, storage_key: str) -> None`,
  `app.deletion.delete_track_content(session: Session, track: Track) -> None` — both consumed by
  Task 2 and Task 3. `delete_track_content` does NOT delete `track` itself or its
  `RightsDeclaration` — callers do that separately.

- [ ] **Step 1: Write the failing test**

Create `services/api/tests/test_deletion.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from minio.error import S3Error
from sqlalchemy import select

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal, db_session_for_tenant
from app.deletion import delete_track_content
from app.main import app
from app.models import KaraokePackage, Stem, Track, Transcription
from app.routes.tracks import get_acoustid_client
from app.storage import fetch_track_file, get_minio_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
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
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def _insert_transcription_and_package(track_id: str) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language="en",
                lyrics_display_allowed=True,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9}
                ],
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            KaraokePackage(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                schema_version=1,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9}
                ],
                pitch_model="tiny",
                pitch=[{"time_ms": 0, "hz": 220.0, "confidence": 0.9}],
                tempo_bpm=120.0,
                beats_ms=[0, 500],
                sections_ms=[0],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


def test_delete_track_content_removes_rows_and_storage_but_not_the_track(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription_and_package(track_id)

    minio_client = get_minio_client()
    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
        assert len(stems) == 4
        stem_keys = [s.storage_key for s in stems]
        track_key = track.storage_key
        # Confirm the real objects exist before deletion (fetch raises if missing).
        for key in [*stem_keys, track_key]:
            fetch_track_file(minio_client, key)

        delete_track_content(session, track)
        session.commit()

        # The track row itself and its rights_declaration_id must survive -- deletion_content()
        # never touches Track or RightsDeclaration, callers decide that part.
        surviving_track = session.get(Track, uuid.UUID(track_id))
        assert surviving_track is not None
        assert surviving_track.rights_declaration_id == track.rights_declaration_id

        assert session.execute(
            select(Stem).where(Stem.track_id == track.id)
        ).scalars().all() == []
        assert session.execute(
            select(Transcription).where(Transcription.track_id == track.id)
        ).scalars().all() == []
        assert session.execute(
            select(KaraokePackage).where(KaraokePackage.track_id == track.id)
        ).scalars().all() == []

        for key in [*stem_keys, track_key]:
            try:
                fetch_track_file(minio_client, key)
                raise AssertionError(f"expected {key} to be deleted from storage")
            except S3Error:
                pass
    finally:
        session.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && python -m pytest tests/test_deletion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.deletion'`

- [ ] **Step 3: Add `delete_track_file` to `storage.py`**

In `services/api/app/storage.py`, add at the end of the file:

```python
def delete_track_file(client: Minio, storage_key: str) -> None:
    """Removes an object from storage. MinIO's remove_object follows standard S3 idempotent-
    delete semantics -- it does not raise if the object is already gone, so callers don't need to
    guard against a double-delete or a storage_key that was never actually uploaded."""
    client.remove_object(_BUCKET, storage_key)
```

- [ ] **Step 4: Write `app/deletion.py`**

Create `services/api/app/deletion.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FingerprintMatch, KaraokePackage, Stem, Track, Transcription
from app.storage import delete_track_file, get_minio_client


def delete_track_content(session: Session, track: Track) -> None:
    """Deletes every row and object-storage blob a track owns -- FingerprintMatch, Stem (+ each
    stem's MinIO object), Transcription, KaraokePackage, and the original upload's MinIO object.
    Does NOT delete the Track row itself or its RightsDeclaration -- callers decide that part,
    since retention purge (hard delete) and takedown (tombstone) want different endings.

    Retention-purged tracks never had a Stem/Transcription/KaraokePackage row in the first place
    (those pipeline stages only run after the rights gate passes) -- for them, these queries are
    cheap no-ops, not dead code. Reusing one function for both cases is simpler than maintaining
    two purpose-built deletion paths that would drift apart over time.
    """
    minio_client = get_minio_client()

    stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
    for stem in stems:
        delete_track_file(minio_client, stem.storage_key)
        session.delete(stem)

    for match in (
        session.execute(select(FingerprintMatch).where(FingerprintMatch.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(match)

    for transcription in (
        session.execute(select(Transcription).where(Transcription.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(transcription)

    for package in (
        session.execute(select(KaraokePackage).where(KaraokePackage.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(package)

    delete_track_file(minio_client, track.storage_key)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/api && python -m pytest tests/test_deletion.py -v`
Expected: PASS.

- [ ] **Step 6: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 7: Commit**

```bash
git add services/api/app/deletion.py services/api/app/storage.py services/api/tests/test_deletion.py
git commit -m "M7a: add shared track-content deletion core"
```

---

### Task 2: Retention purge script

**Files:**
- Create: `services/api/scripts/purge_expired_tracks.py`
- Test: `services/api/tests/test_purge_expired_tracks.py`

**Interfaces:**
- Consumes: `app.deletion.delete_track_content` (Task 1), `app.db.SessionLocal` (existing),
  `app.models.Track`/`RightsDeclaration` (existing).
- Produces: `scripts.purge_expired_tracks.purge_expired_tracks() -> int` and the module-level
  `RETENTION_WINDOW_DAYS = 30` constant, consumed by this task's own tests.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_purge_expired_tracks.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal
from app.main import app
from app.models import RightsDeclaration, Track
from app.routes.tracks import get_acoustid_client
from scripts.purge_expired_tracks import purge_expired_tracks

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_track(synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.status_code == 200
    return response.json()["track_id"]


def _backdate_and_set_status(track_id: str, *, status: str, created_at: datetime) -> None:
    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        track.status = status
        declaration = session.get(RightsDeclaration, track.rights_declaration_id)
        assert declaration is not None
        declaration.created_at = created_at
        session.commit()
    finally:
        session.close()


def test_purge_deletes_old_rejected_track(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    old = datetime.now(UTC) - timedelta(days=31)
    _backdate_and_set_status(track_id, status="rejected", created_at=old)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is None
    finally:
        session.close()


def test_purge_does_not_delete_recent_pending_track(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    recent = datetime.now(UTC) - timedelta(days=5)
    _backdate_and_set_status(track_id, status="pending_review", created_at=recent)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is not None
    finally:
        session.close()


def test_purge_never_deletes_passed_tracks_regardless_of_age(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    old = datetime.now(UTC) - timedelta(days=365)
    _backdate_and_set_status(track_id, status="passed", created_at=old)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is not None
    finally:
        session.close()
```

(These tests do not assert `purge_expired_tracks()`'s exact return count, since it's a genuinely
cross-tenant/cross-test-run operation — asserting a specific track's before/after existence is the
real, deterministic thing under test, independent of what else might exist in the database.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_purge_expired_tracks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.purge_expired_tracks'` (or a
`ModuleNotFoundError` for `scripts` itself if this is the first script test in this project needing
`scripts` to be importable — if so, check whether `services/api/scripts/` needs an `__init__.py`;
existing scripts like `benchmark_pitch.py` are invoked via `python scripts/x.py`, not imported as a
package, so this may be the first time a script's function is imported directly by a test. Add
`services/api/scripts/__init__.py` (empty file) if needed to make `scripts` importable, and confirm
`services/api/pyproject.toml`'s `[tool.pytest.ini_options]` `testpaths = ["tests"]` doesn't need
adjustment for this — it shouldn't, since pytest resolves imports relative to the project root
where `pytest` is invoked from, not just `testpaths`).

- [ ] **Step 3: Write `scripts/purge_expired_tracks.py`**

Create `services/api/scripts/purge_expired_tracks.py`:

```python
"""Deletes tracks that never passed the rights gate (status stays pending_review or rejected)
older than RETENTION_WINDOW_DAYS. Run manually or via an external OS-level scheduled task -- this
project has no in-process scheduler/cron infrastructure, and building one just for this script
would be new infrastructure this milestone doesn't otherwise need (see the design spec's
Decision 2).

RETENTION_WINDOW_DAYS is a policy choice, not a measured or validated number -- easy to change,
not backed by a real compliance review.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.deletion import delete_track_content
from app.models import RightsDeclaration, Track

RETENTION_WINDOW_DAYS = 30


def purge_expired_tracks() -> int:
    """Returns the number of tracks purged."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_WINDOW_DAYS)
    session = SessionLocal()
    purged = 0
    try:
        stmt = (
            select(Track, RightsDeclaration)
            .join(RightsDeclaration, RightsDeclaration.id == Track.rights_declaration_id)
            .where(
                Track.status.in_(("pending_review", "rejected")),
                RightsDeclaration.created_at < cutoff,
            )
        )
        rows = session.execute(stmt).all()
        for track, declaration in rows:
            delete_track_content(session, track)
            session.delete(track)
            session.delete(declaration)
            purged += 1
        session.commit()
        return purged
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    count = purge_expired_tracks()
    # Never logs attestation text, audio, or lyrics -- only a count.
    print(
        f"Purged {count} expired track(s) (status pending_review/rejected, "
        f"older than {RETENTION_WINDOW_DAYS} days)"
    )
    sys.exit(0)
```

If Step 2 found `scripts` needs an `__init__.py` to be importable, create
`services/api/scripts/__init__.py` as an empty file now too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_purge_expired_tracks.py -v`
Expected: PASS (3/3).

- [ ] **Step 5: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 6: Commit**

```bash
git add services/api/scripts/purge_expired_tracks.py services/api/tests/test_purge_expired_tracks.py
git add services/api/scripts/__init__.py 2>/dev/null || true
git commit -m "M7a: add retention purge script for rejected/pending tracks"
```

---

### Task 3: Takedown endpoint

**Files:**
- Create: `services/api/alembic/versions/0007_add_track_takedown_columns.py`
- Modify: `services/api/app/models.py` (add `takedown_reason`, `takedown_at` to `Track`)
- Modify: `services/api/app/db.py` (add `get_admin_db`)
- Modify: `services/api/app/auth.py` (add `require_admin_key`)
- Create: `services/api/app/routes/admin.py`
- Modify: `services/api/app/main.py` (register the new router)
- Test: `services/api/tests/test_admin_takedown.py`

**Interfaces:**
- Consumes: `app.deletion.delete_track_content` (Task 1), `app.db.SessionLocal` (existing).
- Produces: `app.db.get_admin_db` (FastAPI dependency, cross-tenant session),
  `app.auth.require_admin_key` (FastAPI dependency, gate), `POST
  /admin/tracks/{track_id}/takedown` — no later task depends on this, it's the final task in this
  milestone.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_admin_takedown.py`:

```python
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal
from app.main import app
from app.models import Stem, Track
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}

TEST_ADMIN_KEY = "test-admin-key-for-pytest-only"


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
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
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def test_takedown_rejects_missing_admin_key(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("delete_track_content must not be called without a valid admin key")

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/admin/tracks/{track_id}/takedown", json={"reason": "test"})

    assert response.status_code == 401


def test_takedown_rejects_wrong_admin_key(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("delete_track_content must not be called with a wrong admin key")

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "test"},
        headers={"X-Admin-Key": "wrong-key"},
    )

    assert response.status_code == 401


def test_takedown_fails_closed_when_admin_key_not_configured(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "delete_track_content must not be called with no admin key configured"
        )

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "test"},
        headers={"X-Admin-Key": "anything"},
    )

    assert response.status_code == 500


def test_takedown_creates_tombstone_and_removes_content_across_tenants(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    # This request carries NO X-Dev-Tenant-Id at all -- proving the endpoint reaches across
    # tenants by design (via the admin key alone), not by accident.
    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "rights holder request"},
        headers={"X-Admin-Key": TEST_ADMIN_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "taken_down"
    assert body["takedown_reason"] == "rights holder request"
    assert body["takedown_at"] is not None

    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        assert track.status == "taken_down"
        assert track.takedown_reason == "rights holder request"
        stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
        assert stems == []
    finally:
        session.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_admin_takedown.py -v`
Expected: FAIL — route doesn't exist yet (404s), or a collection error if `app.routes.admin`
doesn't exist yet.

- [ ] **Step 3: Write the migration**

Create `services/api/alembic/versions/0007_add_track_takedown_columns.py`:

```python
"""add track takedown columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("takedown_reason", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("takedown_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "takedown_at")
    op.drop_column("tracks", "takedown_reason")
```

Run: `cd services/api && python -m alembic upgrade head`
Expected: no errors; last line mentions upgrading to `0007`.

- [ ] **Step 4: Update the `Track` model**

In `services/api/app/models.py`, the `Track` class currently ends after its `storage_key` column
(directly before `class FingerprintMatch`). Add two new columns immediately after `storage_key`:

```python
    # New in M7a: "taken_down" is a new value for status, alongside the existing
    # pending_review|passed|rejected. takedown_reason/takedown_at are only ever set together, by
    # the takedown endpoint -- both null for every other status.
    takedown_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    takedown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Confirm `datetime` and `Text`/`DateTime` are already imported at the top of `models.py` (they are —
used by other models in this same file).

- [ ] **Step 5: Add `get_admin_db` to `db.py`**

In `services/api/app/db.py`, add at the end of the file:

```python
def get_admin_db() -> Generator[Session, None, None]:
    """Cross-tenant session using the unrestricted `songbox` superuser role, bypassing RLS. For
    operations that legitimately need to reach across tenants by design -- retention purge,
    takedown -- not for anything a normal per-request endpoint should ever use. Every route that
    depends on this MUST be gated behind something stronger than the dev-tenant-header identity
    scheme (see app.auth.require_admin_key), since it has no tenant boundary at all.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 6: Add `require_admin_key` to `auth.py`**

In `services/api/app/auth.py`, add `import os` and `import secrets` to the top imports, then add at
the end of the file:

```python
def require_admin_key(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="admin API key not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Key")
```

- [ ] **Step 7: Write `app/routes/admin.py`**

Create `services/api/app/routes/admin.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_admin_key
from app.db import get_admin_db
from app.deletion import delete_track_content
from app.models import Track

router = APIRouter()


class TakedownRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class TakedownResponse(BaseModel):
    track_id: uuid.UUID
    status: str
    takedown_reason: str
    takedown_at: datetime


@router.post(
    "/admin/tracks/{track_id}/takedown",
    response_model=TakedownResponse,
    dependencies=[Depends(require_admin_key)],
)
def takedown_track(
    track_id: uuid.UUID,
    body: TakedownRequest,
    db: Session = Depends(get_admin_db),
) -> TakedownResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="track not found")

    delete_track_content(db, track)
    track.status = "taken_down"
    track.takedown_reason = body.reason
    track.takedown_at = datetime.now(UTC)
    db.flush()

    return TakedownResponse(
        track_id=track.id,
        status=track.status,
        takedown_reason=track.takedown_reason,
        takedown_at=track.takedown_at,
    )
```

- [ ] **Step 8: Register the router in `main.py`**

In `services/api/app/main.py`, add the import and registration:

```python
from app.routes.admin import router as admin_router
```

(alongside the existing `review_queue_router`/`tracks_router` imports), and:

```python
app.include_router(admin_router)
```

(alongside the existing `app.include_router(tracks_router)` / `app.include_router(review_queue_router)`
calls).

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_admin_takedown.py -v`
Expected: PASS (4/4).

- [ ] **Step 10: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 11: Commit**

```bash
git add services/api/alembic/versions/0007_add_track_takedown_columns.py \
    services/api/app/models.py services/api/app/db.py services/api/app/auth.py \
    services/api/app/routes/admin.py services/api/app/main.py \
    services/api/tests/test_admin_takedown.py
git commit -m "M7a: add admin-gated track takedown endpoint"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (shared deletion core, `delete_track_content` never touches
`Track`/`RightsDeclaration`) — covered in Task 1, explicitly tested (the "surviving_track" assertion).
Decision 2 (retention purge as a standalone script, `RETENTION_WINDOW_DAYS` stated as policy not
measurement, hard delete, pending_review/rejected only, never passed) — covered in Task 2, all three
scope-boundary cases tested. Decision 3 (takedown tombstone, `taken_down` status,
`takedown_reason`/`takedown_at`, bounded reason text) — covered in Task 3. Decision 4 (`X-Admin-Key`,
constant-time comparison, fail-closed when unset) — covered in Task 3's `require_admin_key` and all
three of its negative-path tests. The cross-tenant testing-strategy requirement (a real test proving
`SessionLocal`/`get_admin_db` reaches across tenants) — covered by Task 3's final test sending no
`X-Dev-Tenant-Id` header at all.

**Placeholder scan:** No TBD/TODO in this plan's own instructions.

**Type consistency:** `delete_track_content(session: Session, track: Track) -> None` (Task 1)'s
signature matches its two call sites exactly — Task 2's `purge_expired_tracks()` and Task 3's
`takedown_track()`, both passing a real `Session` and a real `Track` instance, neither passing
`track_id`/other mismatched types. `get_admin_db` (Task 3, in `db.py`) matches `get_db`'s existing
`Generator[Session, None, None]` return shape exactly, so `Depends(get_admin_db)` type-checks the
same way `Depends(get_db)` already does throughout `tracks.py`.
