# M3: Source Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a synchronous `POST /tracks/{track_id}/separate` endpoint that runs Demucs on a
rights-gate-passed track and stores its four stems (vocals/drums/bass/other) in MinIO with DB
provenance rows.

**Architecture:** A new `stems` table (RLS-protected, same pattern as every M1/M2 table); a pure
`separate_audio()` wrapper around `demucs.api.Separator` that asserts 44.1kHz stereo WAV output;
a new route in the existing `tracks.py` that gates on `track.status == "passed"`, fetches the
original file from MinIO, separates it, and persists the four resulting stems.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (no `relationship()`), Alembic, MinIO, `demucs` (Meta's
source separation library, via its `demucs.api.Separator` programmatic interface), `torch`/
`torchaudio`.

## Global Constraints

- All internal audio is 44.1kHz stereo WAV — assert this at every stage boundary (`CLAUDE.md`).
  Every stem `separate_audio()` produces must be checked, not assumed.
- Nothing reaches a GPU without a rights-gate PASS (`CLAUDE.md`) — `POST /tracks/{id}/separate`
  must reject (409) any track whose `status != "passed"` **before** invoking Demucs, and a test
  must prove Demucs was never invoked, not just that the response was a 409.
- Every table carries `tenant_id`, every query filters on it, RLS is `ENABLE`d and `FORCE`d
  (`CLAUDE.md`). The new `stems` table follows the exact pattern of `services/api/alembic/
  versions/0002_row_level_security.py`.
- No fabricated accuracy/latency/cost figure — write `TODO: unmeasured` for anything not actually
  run on this machine (`CLAUDE.md`). `docs/BENCHMARKS.md`'s numbers must come from a real local
  run, not be invented while writing this plan.
- ffmpeg/ffprobe invocations already in this codebase stay argument-array + `-protocol_whitelist
  file` — this milestone does not touch `app/fingerprint.py` or `app/validation.py`.
- No `yt-dlp`/`youtube-dl`/`pytube`-class dependency, ever.

---

### Task 1: `stems` table (migration + model)

**Files:**
- Modify: `services/api/app/models.py` (add `Stem` class)
- Create: `services/api/alembic/versions/0004_add_stems_table.py`
- Test: `services/api/tests/test_models.py` (existing, unmodified — generically covers new models)
- Test: `services/api/tests/test_db_rls.py` (existing, unmodified — `RLS_TABLES` is derived from
  `Base.metadata.tables.keys()`, so it picks up `stems` automatically)

**Interfaces:**
- Produces: `app.models.Stem` — columns `id: uuid.UUID`, `tenant_id: uuid.UUID`,
  `track_id: uuid.UUID` (FK → `tracks.id`), `stem_type: str` (one of `"vocals"`, `"drums"`,
  `"bass"`, `"other"`), `storage_key: str`, `model_name: str` (one of `"htdemucs"`,
  `"htdemucs_ft"`). Task 3 imports this class to construct rows.

- [ ] **Step 1: Add the `Stem` model**

Append to `services/api/app/models.py`:

```python
class Stem(Base):
    __tablename__ = "stems"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # stem_type: "vocals" | "drums" | "bass" | "other"
    stem_type: Mapped[str] = mapped_column(String(10), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    # model_name: "htdemucs" | "htdemucs_ft" -- which model variant actually produced this row
    model_name: Mapped[str] = mapped_column(String(20), nullable=False)
```

- [ ] **Step 2: Run the existing generic model tests to see them fail**

Run: `cd services/api && pytest tests/test_models.py -v`
Expected: FAIL on `test_every_registered_model_table_exists_in_the_database` with
`{'stems'} missing from DB -- did you run \`alembic upgrade head\`?`

- [ ] **Step 3: Write migration 0004**

Create `services/api/alembic/versions/0004_add_stems_table.py`:

```python
"""add stems table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "stems",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("stem_type", sa.String(length=10), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(length=20), nullable=False),
    )
    op.create_index("ix_stems_tenant_id", "stems", ["tenant_id"])
    op.create_index("ix_stems_track_id", "stems", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON stems TO {APP_ROLE}")
    op.execute("ALTER TABLE stems ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stems FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON stems
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON stems")
    op.execute("ALTER TABLE stems DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON stems FROM {APP_ROLE}")
    op.drop_index("ix_stems_track_id", table_name="stems")
    op.drop_index("ix_stems_tenant_id", table_name="stems")
    op.drop_table("stems")
```

- [ ] **Step 4: Apply the migration**

Run: `cd services/api && python -m alembic upgrade head`
Expected: no errors; last line mentions upgrading to `0004`.

- [ ] **Step 5: Run the generic model and RLS tests to see them pass**

Run: `cd services/api && pytest tests/test_models.py tests/test_db_rls.py -v`
Expected: PASS — `test_every_registered_model_table_exists_in_the_database` and
`test_every_table_has_row_level_security_enabled_and_forced` both now cover `stems` with no
test-file changes needed.

- [ ] **Step 6: Commit**

```bash
git add services/api/app/models.py services/api/alembic/versions/0004_add_stems_table.py
git commit -m "M3: add stems table"
```

---

### Task 2: `separate_audio()` — the Demucs wrapper

**Files:**
- Modify: `services/api/pyproject.toml` (add `torch`, `torchaudio`, `demucs` dependencies)
- Create: `services/api/app/separation.py`
- Test: `services/api/tests/test_separation.py`

**Interfaces:**
- Consumes: `services/api/tests/conftest.py`'s existing `synthetic_wav` fixture (3-second,
  44.1kHz stereo, 440Hz tone WAV).
- Produces: `app.separation.separate_audio(path: Path, model_name: str = "htdemucs") ->
  dict[str, Path]` (keys are the four stem types, values are paths to temp WAV files),
  `app.separation.SeparationError` (exception), `app.separation.STEM_TYPES` (tuple of the four
  stem-type strings) — Task 3 imports all three.

**Setup note:** `demucs` pulls in `torch`/`torchaudio` as multi-GB transitive dependencies. The
version added to `pyproject.toml` resolves the default PyPI wheel, which is CPU-only — that's
what CI uses (no GPU on GitHub-hosted runners) and it's correct there. On this dev machine, after
installing, additionally run the command in Step 2 to replace the CPU wheel with the CUDA 12.1
build (this machine's driver is CUDA 12.6; 12.1-built wheels run fine against it) — otherwise
separation silently falls back to slow CPU inference with no error. This second command is a
manual local step, not part of `pyproject.toml` and not run in CI.

- [ ] **Step 1: Add dependencies**

In `services/api/pyproject.toml`, add to the `dependencies` list (after `"httpx>=0.27",`):

```toml
    "torch>=2.1",
    "torchaudio>=2.1",
    "demucs>=4.0",
```

- [ ] **Step 2: Install dependencies**

Run: `cd services/api && pip install -e ".[dev]"`
Expected: installs successfully (downloads torch/torchaudio — this is a multi-GB download the
first time).

Then, on this machine only (skip in CI), get the CUDA-enabled build:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --force-reinstall
```

- [ ] **Step 3: Write the failing test**

Create `services/api/tests/test_separation.py`:

```python
from __future__ import annotations

import wave
from pathlib import Path

from app.separation import STEM_TYPES, separate_audio


def test_separate_audio_produces_all_four_stems_as_44100hz_stereo_wav(synthetic_wav: Path) -> None:
    stems = separate_audio(synthetic_wav)

    assert set(stems) == set(STEM_TYPES)
    for stem_type, stem_path in stems.items():
        assert stem_path.exists(), f"{stem_type} stem file was not written"
        with wave.open(str(stem_path), "rb") as wav_file:
            assert wav_file.getframerate() == 44100, f"{stem_type} stem is not 44.1kHz"
            assert wav_file.getnchannels() == 2, f"{stem_type} stem is not stereo"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd services/api && pytest tests/test_separation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.separation'`

- [ ] **Step 5: Implement `separate_audio()`**

Create `services/api/app/separation.py`:

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import torch
from demucs.api import Separator, save_audio

EXPECTED_SAMPLE_RATE = 44100
EXPECTED_CHANNELS = 2
STEM_TYPES = ("vocals", "drums", "bass", "other")


class SeparationError(Exception):
    """Raised when Demucs cannot separate the given file, or its output fails the
    44.1kHz-stereo-WAV invariant every stage boundary in this codebase must assert."""


def separate_audio(path: Path, model_name: str = "htdemucs") -> dict[str, Path]:
    """Run Demucs source separation on `path`, returning a dict of stem_type -> temp WAV path
    for all four of STEM_TYPES. Uses Demucs' own segmented/overlap-crossfade mode (`split=True`,
    `overlap=0.25` -- the library's defaults, passed explicitly here to document that this is
    deliberate) so memory is bounded by segment length rather than track length. Runs on GPU
    when available, CPU otherwise -- CI and any machine without a CUDA-enabled torch build fall
    back to CPU automatically rather than erroring.
    """
    try:
        separator = Separator(
            model=model_name,
            device="cuda" if torch.cuda.is_available() else "cpu",
            split=True,
            overlap=0.25,
        )
    except Exception as exc:
        raise SeparationError(f"could not load model {model_name!r}: {exc}") from exc

    if separator.samplerate != EXPECTED_SAMPLE_RATE or separator.audio_channels != EXPECTED_CHANNELS:
        raise SeparationError(
            f"model {model_name!r} operates at {separator.samplerate}Hz/"
            f"{separator.audio_channels}ch, expected "
            f"{EXPECTED_SAMPLE_RATE}Hz/{EXPECTED_CHANNELS}ch"
        )

    try:
        _origin, separated = separator.separate_audio_file(path)
    except Exception as exc:
        raise SeparationError(f"separation failed: {exc}") from exc

    missing = set(STEM_TYPES) - set(separated)
    if missing:
        raise SeparationError(f"model {model_name!r} did not produce stems: {sorted(missing)}")

    out_dir = Path(tempfile.mkdtemp(prefix="songbox-stems-"))
    stem_paths: dict[str, Path] = {}
    for stem_type in STEM_TYPES:
        tensor = separated[stem_type]
        if tensor.shape[0] != EXPECTED_CHANNELS:
            raise SeparationError(
                f"{stem_type} stem has {tensor.shape[0]} channels, expected {EXPECTED_CHANNELS}"
            )
        out_path = out_dir / f"{stem_type}.wav"
        save_audio(tensor, str(out_path), samplerate=separator.samplerate)
        stem_paths[stem_type] = out_path

    return stem_paths
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd services/api && pytest tests/test_separation.py -v`
Expected: PASS. Note: the first run downloads the `htdemucs` model checkpoint (~80MB, from
Demucs' own hosted URL) and caches it — that download only happens once per machine, but makes
the first run noticeably slower than subsequent ones.

- [ ] **Step 7: Commit**

```bash
git add services/api/pyproject.toml services/api/app/separation.py services/api/tests/test_separation.py
git commit -m "M3: add separate_audio() Demucs wrapper"
```

---

### Task 3: `POST /tracks/{track_id}/separate`

**Files:**
- Modify: `services/api/app/storage.py` (add `fetch_track_file`)
- Modify: `services/api/app/routes/tracks.py` (add the route)
- Test: `services/api/tests/test_storage.py` (add one test)
- Test: `services/api/tests/test_tracks_separate.py` (new)

**Interfaces:**
- Consumes: `app.separation.separate_audio`, `app.separation.SeparationError`,
  `app.separation.STEM_TYPES` (Task 2); `app.models.Stem` (Task 1); `app.storage.save_track_file`,
  `app.storage.get_minio_client` (existing); `app.validation.detect_audio_format` (existing,
  already imported in `tracks.py`).
- Produces: `POST /tracks/{track_id}/separate` — optional JSON body `{"model_name": str}`
  (defaults to `"htdemucs"` if body omitted), returns `{"track_id": uuid, "stems": [{"stem_type":
  str, "storage_key": str}, ...]}` on 200; 404 if track not found/wrong tenant; 409 if
  `track.status != "passed"`; 422 if `model_name` is not `"htdemucs"`/`"htdemucs_ft"`, or if
  separation fails.

- [ ] **Step 1: Add `fetch_track_file` to storage.py**

In `services/api/app/storage.py`, add after `save_track_file`:

```python
def fetch_track_file(client: Minio, storage_key: str) -> bytes:
    response = client.get_object(_BUCKET, storage_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
```

- [ ] **Step 2: Write the failing storage test**

Add to `services/api/tests/test_storage.py`:

```python
from app.storage import fetch_track_file


def test_fetch_track_file_returns_the_bytes_that_were_saved() -> None:
    client = get_minio_client()
    tenant_id = uuid.uuid4()
    data = b"not real audio, just test bytes"

    storage_key = save_track_file(client, tenant_id, data)
    fetched = fetch_track_file(client, storage_key)

    assert fetched == data
```

(Add the `fetch_track_file` import to the existing `from app.storage import ...` line rather than
as a second import line, if one already exists in the file.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/api && pytest tests/test_storage.py -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_track_file'`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && pytest tests/test_storage.py -v`
Expected: PASS (the implementation was already written in Step 1 — this confirms it).

- [ ] **Step 5: Write the failing endpoint tests**

Create `services/api/tests/test_tracks_separate.py`:

```python
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import db_session_for_tenant
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_and_pass_track(synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def test_separate_stores_four_stems_for_a_passed_track(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    stem_types = {s["stem_type"] for s in body["stems"]}
    assert stem_types == {"vocals", "drums", "bass", "other"}
    for stem in body["stems"]:
        assert stem["storage_key"].startswith(f"{HEADERS['X-Dev-Tenant-Id']}/")

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text("SELECT stem_type, model_name FROM stems WHERE track_id = :track_id"),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 4
    assert {row.model_name for row in rows} == {"htdemucs"}


def test_separate_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)

    known_fp = fingerprint_audio(synthetic_wav)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert upload_response.json()["status"] == "pending_review"
    track_id = upload_response.json()["track_id"]

    response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)

    assert response.status_code == 409


def test_separate_rejects_unknown_model_name(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for an unrecognized model_name")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/separate",
        headers=HEADERS,
        json={"model_name": "not-a-real-model"},
    )

    assert response.status_code == 422
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_tracks_separate.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on all three tests.

- [ ] **Step 7: Implement the route**

In `services/api/app/routes/tracks.py`, add to the imports:

```python
from app.models import FingerprintMatch, License, RightsDeclaration, Stem, Track
from app.separation import SeparationError, separate_audio
from app.storage import fetch_track_file, get_minio_client, save_track_file
```

(This replaces the existing `from app.models import ...` and `from app.storage import ...` lines
— add `Stem` to the first, `fetch_track_file` to the second.)

Add near the top of the file, alongside `MAX_UPLOAD_BYTES`:

```python
ALLOWED_SEPARATION_MODELS = ("htdemucs", "htdemucs_ft")
```

Add at the end of the file:

```python
class StemInfo(BaseModel):
    stem_type: str
    storage_key: str


class SeparateRequest(BaseModel):
    model_name: str = "htdemucs"


class SeparateResponse(BaseModel):
    track_id: uuid.UUID
    stems: list[StemInfo]


@router.post("/tracks/{track_id}/separate", response_model=SeparateResponse)
def separate_track(
    track_id: uuid.UUID,
    body: SeparateRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> SeparateResponse:
    model_name = body.model_name if body is not None else "htdemucs"
    if model_name not in ALLOWED_SEPARATION_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"model_name must be one of {ALLOWED_SEPARATION_MODELS}",
        )

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    minio_client = get_minio_client()
    original_bytes = fetch_track_file(minio_client, track.storage_key)

    # Re-detect format from the stored bytes rather than trusting anything client-supplied --
    # same reasoning as upload_track: the suffix ffmpeg/Demucs use to pick a demuxer must come
    # from the actual bytes, never from an attacker-controlled name.
    audio_format = detect_audio_format(original_bytes)
    if audio_format is None:
        raise HTTPException(
            status_code=422, detail="stored file no longer matches any accepted audio format"
        )

    tmp = tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False)
    try:
        tmp.write(original_bytes)
        tmp.flush()
        tmp.close()
        try:
            stem_paths = separate_audio(Path(tmp.name), model_name=model_name)
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    stems: list[StemInfo] = []
    for stem_type, stem_path in stem_paths.items():
        stem_bytes = stem_path.read_bytes()
        storage_key = save_track_file(minio_client, identity.tenant_id, stem_bytes)
        db.add(
            Stem(
                id=uuid.uuid4(),
                tenant_id=identity.tenant_id,
                track_id=track.id,
                stem_type=stem_type,
                storage_key=storage_key,
                model_name=model_name,
            )
        )
        stems.append(StemInfo(stem_type=stem_type, storage_key=storage_key))

    return SeparateResponse(track_id=track.id, stems=stems)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_tracks_separate.py -v`
Expected: PASS (all three tests).

- [ ] **Step 9: Run the full test suite**

Run: `cd services/api && pytest -q`
Expected: PASS, no regressions in M1/M2 tests.

- [ ] **Step 10: Commit**

```bash
git add services/api/app/storage.py services/api/app/routes/tracks.py \
  services/api/tests/test_storage.py services/api/tests/test_tracks_separate.py
git commit -m "M3: add POST /tracks/{track_id}/separate"
```

---

### Task 4: Real benchmark numbers

**Files:**
- Create: `services/api/scripts/benchmark_separation.py`
- Create: `docs/BENCHMARKS.md`

**Interfaces:**
- Consumes: `app.separation.separate_audio` (Task 2). No other task depends on this one.

- [ ] **Step 1: Write the benchmark script**

Create `services/api/scripts/benchmark_separation.py`:

```python
"""Measures real wall-clock separation time and GPU memory on this machine. Not a test --
run manually and paste its output into docs/BENCHMARKS.md. Uses a synthetic tone (not real
music) so it needs no rights clearance to run or share, per CLAUDE.md's rights-gate rules --
this only measures speed, not separation quality.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import torch

from app.separation import separate_audio

BENCHMARK_DURATION_SECONDS = 180  # 3 minutes -- a realistic track length


def _make_benchmark_tone(out_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={BENCHMARK_DURATION_SECONDS}",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def run_benchmark(model_name: str) -> None:
    with TemporaryDirectory() as tmp_dir:
        tone_path = Path(tmp_dir) / "benchmark_tone.wav"
        _make_benchmark_tone(tone_path)

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        start = time.monotonic()
        separate_audio(tone_path, model_name=model_name)
        elapsed = time.monotonic() - start

        realtime_factor = BENCHMARK_DURATION_SECONDS / elapsed
        print(f"model={model_name}")
        print(f"  input duration: {BENCHMARK_DURATION_SECONDS}s")
        print(f"  wall clock: {elapsed:.1f}s")
        print(f"  realtime factor: {realtime_factor:.2f}x")
        if torch.cuda.is_available():
            peak_mib = torch.cuda.max_memory_allocated() / (1024 * 1024)
            print(f"  peak GPU memory: {peak_mib:.0f} MiB")
            print(f"  device: {torch.cuda.get_device_name(0)}")
        else:
            print("  device: cpu (torch.cuda.is_available() was False)")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "htdemucs"
    run_benchmark(model)
```

- [ ] **Step 2: Run it for both models and record the real output**

Run, from `services/api`, with the CUDA-enabled torch build installed (Task 2, Step 2):

```bash
python scripts/benchmark_separation.py htdemucs
python scripts/benchmark_separation.py htdemucs_ft
```

Copy the actual printed output of both runs — do not paraphrase or round-trip through memory.

- [ ] **Step 3: Write docs/BENCHMARKS.md**

Create `docs/BENCHMARKS.md` with a header and a table, filling in the `<...>` placeholders with
the exact numbers Step 2 printed (if a value wasn't measured, write `TODO: unmeasured` — never a
plausible-looking number, per `CLAUDE.md`):

```markdown
# Benchmarks

Real, measured numbers only. `TODO: unmeasured` for anything not actually run — never a
plausible-looking placeholder (per `CLAUDE.md`).

## M3: Source separation (Demucs)

Measured on: <date>, on this dev machine (NVIDIA GeForce RTX 4060 Laptop GPU, 8188 MiB VRAM,
driver 560.94, CUDA 12.6), via `services/api/scripts/benchmark_separation.py` against a
synthetic 3-minute 440Hz tone (not real music — no rights clearance needed to run or share this
number).

| Model | Wall clock (3min input) | Realtime factor | Peak GPU memory |
|---|---|---|---|
| `htdemucs` | <...>s | <...>x | <...> MiB |
| `htdemucs_ft` | <...>s | <...>x | <...> MiB |

Quality comparison between `htdemucs` and `htdemucs_ft`: `TODO: unmeasured` — needs a real
listening test with real songs and human judgment, out of scope for M3 (see
`docs/superpowers/specs/2026-08-21-source-separation-design.md`).

Note: this is measured against the `local` GPU backend (this dev machine), not the eventual
Modal/RunPod production backend — per `docs/adr/0001-gpu-backend-abstraction.md`, production
cost/speed figures are `TODO: unmeasured` until that backend exists in M7.
```

- [ ] **Step 4: Commit**

```bash
git add services/api/scripts/benchmark_separation.py docs/BENCHMARKS.md
git commit -m "M3: add real Demucs speed benchmarks"
```

---

## Self-Review Notes

**Spec coverage:** `stems` table (Task 1) — covered. `POST /tracks/{id}/separate` gated on
`status == "passed"` (Task 3) — covered, plus a test proving Demucs isn't invoked on a rejected
gate. `separation.py` module with 44.1kHz stereo WAV assertion (Task 2) — covered. Model name as
a parameter with `htdemucs` default (Task 3) — covered, plus a whitelist not in the original spec
text but directly justified by the codebase's existing input-validation discipline. torch/demucs
CUDA setup note (Task 2) — covered. `docs/BENCHMARKS.md` with real numbers (Task 4) — covered,
explicitly deferred to execution time rather than fabricated during planning. Synthetic-fixture
testing strategy — covered in Tasks 2 and 3, reusing the existing `synthetic_wav` fixture.

**Placeholder scan:** No TBD/TODO in the plan's own instructions. `docs/BENCHMARKS.md`'s `<...>`
placeholders are deliberate — they're filled with real measured values in Task 4, Step 3, not left
as placeholders in the committed file.

**Type consistency:** `separate_audio(path: Path, model_name: str = "htdemucs") -> dict[str,
Path]` (Task 2) matches its usage in Task 3's route. `STEM_TYPES` (Task 2) matches the four
`stem_type` values asserted in Task 3's tests. `Stem` model fields (Task 1) match exactly what
Task 3's route constructs.
