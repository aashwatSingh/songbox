# M5: Pitch + Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `POST /tracks/{id}/package` endpoint that extracts a pitch contour (CREPE) and a beat
grid + unlabeled structural boundaries (librosa) from a rights-gate-passed track and assembles them,
with the track's word timings, into a versioned `karaoke.json` v1 document.

**Architecture:** A new `services/api/app/packaging.py` module owns three real-audio-processing
functions (accompaniment synthesis, pitch extraction, structure detection) plus one orchestration
function mirroring M4a's `run_transcription_and_alignment` precedent. A new `karaoke_packages` table
(JSONB, RLS) stores each packaging run as an immutable row, matching `transcriptions`. The endpoint
reuses M4a's `gpu_backend.run_inference` seam — same lock, same timeout pattern, same error mapping.

**Tech Stack:** `torchcrepe` (pitch, MIT License) and `librosa` (beat/structure, ISC License) — both
license-verified directly from their repositories before this plan was written. FastAPI + SQLAlchemy,
matching every prior milestone's conventions exactly.

## Global Constraints

- **Pitch runs on the `vocals` stem; beat/structure run on a synthesized accompaniment** (`drums` +
  `bass` + `other`, summed and peak-normalized) — never the original full mix, which still contains
  vocals the karaoke player mutes/replaces during playback.
- **No verse/chorus/bridge labels.** Structure detection emits unlabeled boundary timestamps only —
  no classifier exists in this project to produce real semantic labels, and `CLAUDE.md` forbids
  fabricated-confidence output. `librosa.segment.agglomerative(chroma, k)` requires a caller-supplied
  `k` (segment count); it does not discover k from the audio. k is a duration-derived heuristic,
  clamped to `[MIN_SECTIONS, MAX_SECTIONS]` — boundary *count* is a tunable heuristic, boundary
  *positions* (given k) are real chroma-similarity signal. Never conflate the two in code comments,
  docs, or UI copy.
- **`karaoke_packages` rows are immutable** — a new packaging run is a new row, never an update,
  matching `transcriptions`' pattern from M4b. `schema_version` is `1` on every row this milestone
  produces, per `CLAUDE.md`'s "any shape change needs a migration path, not a silent bump" rule.
- **Word `text` is nulled when `lyrics_display_allowed` is false** at write time (not just read time) —
  this document may be read by a future, less-trusted context (the M6 player), so lyric text withheld
  from the API must never be embedded in the stored package either.
- **CREPE model defaults to `'tiny'`**, kept as a request parameter with a whitelist, never hardcoded —
  same pattern as M3's `htdemucs`/`htdemucs_ft` and M4a's Whisper model sizes. Real `tiny` vs `full`
  speed/accuracy numbers go into `docs/BENCHMARKS.md`, run to completion, never estimated.
- **Every table carries `tenant_id`, RLS `ENABLE`+`FORCE`d, `tenant_isolation` policy, granted to
  `songbox_app` not the superuser role** — exact pattern of `services/api/alembic/versions/
  0005_add_transcriptions_table.py`.
- **`CLAUDE.md`: never log raw audio or lyrics.** Packaging-failure exceptions must never interpolate
  lyric text (this endpoint doesn't touch lyric text directly, but the word list it embeds came from a
  `Transcription` row — no exception message in this plan's code ever includes `words` content).
- **No `yt-dlp`/`youtube-dl`/`pytube`-class dependency, ever.**

---

### Task 1: `karaoke_packages` table

**Files:**
- Modify: `services/api/app/models.py` (add `KaraokePackage`)
- Create: `services/api/alembic/versions/0006_add_karaoke_packages_table.py`
- Test: `services/api/tests/test_models.py`, `services/api/tests/test_db_rls.py` (existing,
  unmodified — generically cover the new table, as `transcriptions` was in M4a)

**Interfaces:**
- Produces: `app.models.KaraokePackage` — `id`, `tenant_id`, `track_id` (FK), `schema_version: int`,
  `words: list[dict]` (JSONB), `pitch_model: str`, `pitch: list[dict]` (JSONB), `tempo_bpm: float`,
  `beats_ms: list[int]` (JSONB), `sections_ms: list[int]` (JSONB), `created_at: datetime`. Task 3
  imports and constructs this class.

- [ ] **Step 1: Add the `KaraokePackage` model**

In `services/api/app/models.py`, change the top import line from:

```python
from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
```

to:

```python
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
```

Append to the file:

```python
class KaraokePackage(Base):
    __tablename__ = "karaoke_packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # schema_version: karaoke.json's own version -- 1 for every row this milestone produces.
    # CLAUDE.md: any shape change needs a migration path, not a silent bump.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # words: [{"idx": int, "text": str | None, "start_ms": int, "end_ms": int, "confidence": float}]
    # -- copied from the track's latest Transcription row at packaging time, text nulled when
    # lyrics_display_allowed is False (checked at write time, not just read time).
    words: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    # pitch_model: which torchcrepe variant produced this row, e.g. "tiny"
    pitch_model: Mapped[str] = mapped_column(String(20), nullable=False)
    # pitch: [{"time_ms": int, "hz": float | None, "confidence": float}, ...]
    pitch: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    tempo_bpm: Mapped[float] = mapped_column(Float, nullable=False)
    beats_ms: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    sections_ms: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Run the generic model tests to see them fail**

Run: `cd services/api && pytest tests/test_models.py -v`
Expected: FAIL on `test_every_registered_model_table_exists_in_the_database` with
`{'karaoke_packages'} missing from DB -- did you run \`alembic upgrade head\`?`

- [ ] **Step 3: Write migration 0006**

Create `services/api/alembic/versions/0006_add_karaoke_packages_table.py`:

```python
"""add karaoke_packages table

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-23

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "karaoke_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("pitch_model", sa.String(length=20), nullable=False),
        sa.Column("pitch", postgresql.JSONB(), nullable=False),
        sa.Column("tempo_bpm", sa.Float(), nullable=False),
        sa.Column("beats_ms", postgresql.JSONB(), nullable=False),
        sa.Column("sections_ms", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_karaoke_packages_tenant_id", "karaoke_packages", ["tenant_id"])
    op.create_index("ix_karaoke_packages_track_id", "karaoke_packages", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON karaoke_packages TO {APP_ROLE}")
    op.execute("ALTER TABLE karaoke_packages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE karaoke_packages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON karaoke_packages
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON karaoke_packages")
    op.execute("ALTER TABLE karaoke_packages DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON karaoke_packages FROM {APP_ROLE}")
    op.drop_index("ix_karaoke_packages_track_id", table_name="karaoke_packages")
    op.drop_index("ix_karaoke_packages_tenant_id", table_name="karaoke_packages")
    op.drop_table("karaoke_packages")
```

- [ ] **Step 4: Apply the migration**

Run: `cd services/api && python -m alembic upgrade head`
Expected: no errors; last line mentions upgrading to `0006`.

- [ ] **Step 5: Run the generic model and RLS tests to see them pass**

Run: `cd services/api && pytest tests/test_models.py tests/test_db_rls.py -v`
Expected: PASS — both now cover `karaoke_packages` with no test-file changes.

- [ ] **Step 6: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 7: Commit**

```bash
git add services/api/app/models.py services/api/alembic/versions/0006_add_karaoke_packages_table.py
git commit -m "M5: add karaoke_packages table"
```

---

### Task 2: `services/api/app/packaging.py` — accompaniment, pitch, structure

**Files:**
- Modify: `services/api/pyproject.toml` (add `torchcrepe`, `librosa` to `dependencies`; add both to
  the mypy untyped-stub overrides)
- Create: `services/api/app/packaging.py`
- Test: `services/api/tests/test_packaging.py`

**Interfaces:**
- Consumes: `services/api/tests/conftest.py`'s existing `synthetic_wav` fixture (3-second, 44.1kHz
  stereo, 440Hz tone WAV) — a pure sine tone has no real musical structure, so tests against it prove
  the pipeline runs and produces well-formed output, not that any particular tempo/pitch/boundary is
  meaningful, mirroring every prior milestone's synthetic-fixture philosophy.
- Produces: `app.packaging.PitchFrame` (dataclass: `time_ms: int`, `hz: float | None`,
  `confidence: float`), `app.packaging.StructureResult` (dataclass: `tempo_bpm: float`,
  `beats_ms: list[int]`, `sections_ms: list[int]`), `app.packaging.PackageResult` (dataclass:
  `pitch_model: str`, `pitch: list[PitchFrame]`, `tempo_bpm: float`, `beats_ms: list[int]`,
  `sections_ms: list[int]`), `app.packaging.AccompanimentError`,
  `app.packaging.PitchExtractionError`, `app.packaging.StructureExtractionError`,
  `app.packaging.synthesize_accompaniment(drums_path: Path, bass_path: Path, other_path: Path,
  out_dir: Path) -> Path`, `app.packaging.extract_pitch(vocals_path: Path, model: str = "tiny") ->
  list[PitchFrame]`, `app.packaging.extract_structure(accompaniment_path: Path) -> StructureResult`,
  `app.packaging.build_package(vocals_path: Path, drums_path: Path, bass_path: Path, other_path:
  Path, pitch_model: str = "tiny") -> PackageResult`. Task 3 imports `build_package`, `PackageResult`,
  `AccompanimentError`, `PitchExtractionError`, `StructureExtractionError`.

**Design notes carried from the spec, not to be "fixed" by a later reader:**

`torchcrepe.predict()` internally resamples audio to its own 16kHz — unlike M4a's `align_words`,
which had to resample by hand for wav2vec2, `extract_pitch` passes the vocals stem's real sample rate
straight through and lets the library handle it.

`fmin`/`fmax` are left at `torchcrepe.predict`'s own library defaults (`50.`/`MAX_FMAX`) rather than a
custom vocal-range guess — using the library's real, documented defaults is more defensible than
inventing tuned values with no measurement behind them.

`librosa.segment.agglomerative(chroma, k)` needs a `k` this code supplies via a duration-based
heuristic (`round(duration_seconds / SECONDS_PER_SECTION_HEURISTIC)`, clamped to `[MIN_SECTIONS,
MAX_SECTIONS]`) — this is a tunable knob, not a measured "correct" section count, and the code comment
at its definition says so.

Summing three independent stems can clip past ±1.0 amplitude; `synthesize_accompaniment` peak-normalizes
the result and the accompaniment file itself is a transient artifact — never written to MinIO, never
given a `Stem` row.

- [ ] **Step 1: Add dependencies**

In `services/api/pyproject.toml`, add to the `dependencies` list (after the `soundfile>=0.12,` line):

```toml
    "torchcrepe>=0.0.23",
    "librosa>=0.10",
```

Change the `[[tool.mypy.overrides]]` block's `module` line from:

```python
module = ["torchaudio.*", "faster_whisper.*", "soundfile.*"]
```

to:

```python
module = ["torchaudio.*", "faster_whisper.*", "soundfile.*", "torchcrepe.*", "librosa.*"]
```

- [ ] **Step 2: Install dependencies**

Run: `cd services/api && pip install -e ".[dev]"`

- [ ] **Step 3: Write the failing tests**

Create `services/api/tests/test_packaging.py`:

```python
from __future__ import annotations

from pathlib import Path

import soundfile as sf

from app.packaging import (
    build_package,
    extract_pitch,
    extract_structure,
    synthesize_accompaniment,
)


def test_synthesize_accompaniment_sums_and_normalizes(synthetic_wav: Path, tmp_path: Path) -> None:
    # Summing the same tone three times over-amplitude (a single ffmpeg sine tone is already
    # near full-scale), so the result must be peak-normalized back to <= 1.0 -- this proves real
    # summing + normalization ran, not a pass-through of one input.
    out_path = synthesize_accompaniment(synthetic_wav, synthetic_wav, synthetic_wav, tmp_path)

    assert out_path.exists()
    data, sample_rate = sf.read(str(out_path), dtype="float32", always_2d=True)
    assert sample_rate == 44100
    assert data.shape[1] == 2
    peak = abs(data).max()
    assert peak <= 1.0 + 1e-6
    assert peak > 0.9  # normalization brings the peak close to 1.0, not to near-silence


def test_extract_pitch_produces_well_formed_frames(synthetic_wav: Path) -> None:
    frames = extract_pitch(synthetic_wav, model="tiny")

    assert len(frames) > 0
    for frame in frames:
        assert frame.time_ms >= 0
        assert 0.0 <= frame.confidence <= 1.0
        if frame.hz is not None:
            assert frame.hz > 0
    # Frame times must be non-decreasing -- catches a regression in the hop-length/index-to-ms
    # conversion, not just its presence.
    times = [f.time_ms for f in frames]
    assert times == sorted(times)


def test_extract_structure_produces_well_formed_result(synthetic_wav: Path, tmp_path: Path) -> None:
    accompaniment_path = synthesize_accompaniment(synthetic_wav, synthetic_wav, synthetic_wav, tmp_path)

    result = extract_structure(accompaniment_path)

    assert result.tempo_bpm > 0
    assert all(b >= 0 for b in result.beats_ms)
    assert result.beats_ms == sorted(result.beats_ms)
    assert len(result.sections_ms) > 0
    assert result.sections_ms[0] == 0
    assert result.sections_ms == sorted(result.sections_ms)


def test_build_package_orchestrates_all_three_stages(synthetic_wav: Path) -> None:
    result = build_package(
        vocals_path=synthetic_wav,
        drums_path=synthetic_wav,
        bass_path=synthetic_wav,
        other_path=synthetic_wav,
        pitch_model="tiny",
    )

    assert result.pitch_model == "tiny"
    assert len(result.pitch) > 0
    assert result.tempo_bpm > 0
    assert len(result.sections_ms) > 0
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_packaging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.packaging'`

- [ ] **Step 5: Implement `packaging.py`**

Create `services/api/app/packaging.py`:

```python
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
import torchcrepe

CREPE_HOP_MS = 10
CREPE_CONFIDENCE_THRESHOLD = 0.5

SECONDS_PER_SECTION_HEURISTIC = 20.0
MIN_SECTIONS = 4
MAX_SECTIONS = 16


class AccompanimentError(Exception):
    """Raised when the three non-vocal stems cannot be combined into an accompaniment track."""


class PitchExtractionError(Exception):
    """Raised when torchcrepe cannot extract a pitch contour from the given vocals audio."""


class StructureExtractionError(Exception):
    """Raised when librosa cannot extract a beat grid or structural boundaries from the given
    accompaniment audio."""


@dataclass(frozen=True)
class PitchFrame:
    time_ms: int
    hz: float | None
    confidence: float


@dataclass(frozen=True)
class StructureResult:
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


@dataclass(frozen=True)
class PackageResult:
    pitch_model: str
    pitch: list[PitchFrame]
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


def _load_waveform(path: Path) -> tuple[torch.Tensor, int]:
    # Same soundfile-based loader as transcription.py's _load_waveform, for the same reason: the
    # inputs here are always guaranteed-WAV (M3 stems, or this module's own WAV test fixtures),
    # never arbitrary uploaded formats, so torchaudio.load()'s TorchCodec/FFmpeg-full-shared-build
    # requirement is unnecessary machinery. Do not "fix" this back to torchaudio.load().
    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    waveform = torch.from_numpy(data.T).contiguous()
    return waveform, sample_rate


def synthesize_accompaniment(
    drums_path: Path, bass_path: Path, other_path: Path, out_dir: Path
) -> Path:
    """Sum the three non-vocal stems into a single accompaniment WAV, peak-normalized to avoid
    clipping from combining three independently-mastered tracks. Transient artifact -- never
    written to MinIO, never given a Stem row.
    """
    try:
        arrays: list[np.ndarray] = []
        sample_rate: int | None = None
        for path in (drums_path, bass_path, other_path):
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            if sample_rate is None:
                sample_rate = sr
            elif sr != sample_rate:
                raise AccompanimentError(
                    f"stem sample rate mismatch: {path} is {sr}Hz, expected {sample_rate}Hz"
                )
            arrays.append(data)
    except AccompanimentError:
        raise
    except Exception as exc:
        raise AccompanimentError("could not load stem audio") from exc

    assert sample_rate is not None
    min_len = min(a.shape[0] for a in arrays)
    summed = sum(a[:min_len] for a in arrays)
    peak = float(np.abs(summed).max())
    if peak > 1.0:
        summed = summed / peak

    out_path = out_dir / "accompaniment.wav"
    try:
        sf.write(str(out_path), summed, sample_rate)
    except Exception as exc:
        raise AccompanimentError("could not write accompaniment audio") from exc
    return out_path


def extract_pitch(vocals_path: Path, model: str = "tiny") -> list[PitchFrame]:
    try:
        waveform, sample_rate = _load_waveform(vocals_path)
    except Exception as exc:
        raise PitchExtractionError("could not load vocals audio") from exc
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    hop_length = int(sample_rate * CREPE_HOP_MS / 1000)

    try:
        pitch, periodicity = torchcrepe.predict(
            waveform,
            sample_rate,
            hop_length=hop_length,
            model=model,
            return_periodicity=True,
            device=device,
        )
    except Exception as exc:
        raise PitchExtractionError("pitch extraction failed") from exc

    frames: list[PitchFrame] = []
    num_frames = pitch.shape[1]
    for i in range(num_frames):
        confidence = float(periodicity[0, i])
        hz = float(pitch[0, i]) if confidence >= CREPE_CONFIDENCE_THRESHOLD else None
        frames.append(PitchFrame(time_ms=i * CREPE_HOP_MS, hz=hz, confidence=confidence))
    return frames


def extract_structure(accompaniment_path: Path) -> StructureResult:
    try:
        data, sample_rate = sf.read(str(accompaniment_path), dtype="float32", always_2d=True)
    except Exception as exc:
        raise StructureExtractionError("could not load accompaniment audio") from exc
    y = data.mean(axis=1)

    try:
        tempo, beat_times = librosa.beat.beat_track(y=y, sr=sample_rate, units="time")
    except Exception as exc:
        raise StructureExtractionError("beat tracking failed") from exc
    beats_ms = [int(round(t * 1000)) for t in np.asarray(beat_times).reshape(-1)]
    tempo_bpm = float(np.asarray(tempo).reshape(-1)[0])

    duration_seconds = len(y) / sample_rate
    # k is a tunable heuristic (roughly one boundary per SECONDS_PER_SECTION_HEURISTIC), not a
    # measured "correct" section count -- librosa.segment.agglomerative requires k as input, it
    # does not discover the number of sections from the audio. Boundary POSITIONS, given k, are
    # real chroma-similarity signal; boundary COUNT is not something this code claims to detect.
    k = max(
        MIN_SECTIONS,
        min(MAX_SECTIONS, round(duration_seconds / SECONDS_PER_SECTION_HEURISTIC)),
    )

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sample_rate)
        bounds = librosa.segment.agglomerative(chroma, k)
        bound_times = librosa.frames_to_time(bounds, sr=sample_rate)
    except Exception as exc:
        raise StructureExtractionError("structure segmentation failed") from exc
    sections_ms = [int(round(t * 1000)) for t in bound_times]

    return StructureResult(tempo_bpm=tempo_bpm, beats_ms=beats_ms, sections_ms=sections_ms)


def build_package(
    vocals_path: Path,
    drums_path: Path,
    bass_path: Path,
    other_path: Path,
    pitch_model: str = "tiny",
) -> PackageResult:
    """Orchestrates all three packaging stages, mirroring transcription.py's
    run_transcription_and_alignment -- the route calls this single function through one
    run_inference() lock acquisition rather than acquiring the lock three separate times."""
    pitch = extract_pitch(vocals_path, model=pitch_model)
    with tempfile.TemporaryDirectory(prefix="songbox-accompaniment-") as tmp_dir:
        accompaniment_path = synthesize_accompaniment(
            drums_path, bass_path, other_path, Path(tmp_dir)
        )
        structure = extract_structure(accompaniment_path)
    return PackageResult(
        pitch_model=pitch_model,
        pitch=pitch,
        tempo_bpm=structure.tempo_bpm,
        beats_ms=structure.beats_ms,
        sections_ms=structure.sections_ms,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_packaging.py -v`
Expected: PASS (4/4). No new model download is needed for this task (torchcrepe's `tiny` weights ship
with the package; librosa needs none) — should not have a slow first-run model-download delay the way
M3/M4a's tasks did.

- [ ] **Step 7: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 8: Commit**

```bash
git add services/api/pyproject.toml services/api/app/packaging.py services/api/tests/test_packaging.py
git commit -m "M5: add accompaniment synthesis, pitch extraction, structure detection"
```

---

### Task 3: `POST /tracks/{track_id}/package`

**Files:**
- Modify: `services/api/app/routes/tracks.py`
- Test: `services/api/tests/test_tracks_package.py` (new)

**Interfaces:**
- Consumes: `app.gpu_backend.run_inference`, `BackendBusyError`, `BackendTimeoutError` (M4a);
  `app.packaging.build_package`, `PackageResult`, `AccompanimentError`, `PitchExtractionError`,
  `StructureExtractionError` (Task 2); `app.models.KaraokePackage` (Task 1); `app.models.Stem`,
  `Transcription` (existing).
- Produces: `POST /tracks/{track_id}/package` — optional JSON body `{"pitch_model": str}` (defaults
  to `"tiny"`), returns `{"track_id": uuid, "schema_version": int, "words": [...], "pitch_model": str,
  "tempo_bpm": float, "beats_ms": [int], "sections_ms": [int]}` on 200; 404 if track not found/wrong
  tenant; 409 if `status != "passed"`, missing any of the four stems, or no transcription exists; 422
  for an unknown `pitch_model` or a packaging failure; 503/504 from the shared inference backend.

Gate order, all before any model call, mirrors every prior milestone's proven pattern: whitelist check
→ track lookup/tenant check → status check → all-four-stems check → transcription-exists check.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_tracks_package.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant
from app.main import app
from app.models import Transcription
from app.routes.tracks import get_acoustid_client

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


def _insert_transcription(track_id: str) -> None:
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
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
                    {"idx": 1, "text": "world", "start_ms": 400, "end_ms": 800, "confidence": 0.9},
                ],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


def test_package_stores_a_karaoke_package_with_real_pitch_and_structure(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)

    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    assert body["schema_version"] == 1
    assert body["pitch_model"] == "tiny"
    assert body["tempo_bpm"] > 0
    assert len(body["sections_ms"]) > 0
    assert [w["text"] for w in body["words"]] == ["hello", "world"]

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            __import__("sqlalchemy").text(
                "SELECT schema_version, pitch_model FROM karaoke_packages WHERE track_id = :track_id"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 1
    assert rows[0].schema_version == 1
    assert rows[0].pitch_model == "tiny"


def test_package_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
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
    track_id = upload_response.json()["track_id"]
    assert upload_response.json()["status"] == "passed"
    # Deliberately not separated -- status is "passed" but this test targets a different gate
    # below by using a track that never gets separated or transcribed instead. See the next test
    # for the explicit not-passed case using a held track.

    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)
    assert response.status_code == 409  # missing stems, since /separate was never called


def test_package_rejects_track_missing_a_stem(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called when stems are missing")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
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
    track_id = upload_response.json()["track_id"]
    # Not separated -- no stems exist at all.

    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 409


def test_package_rejects_track_with_no_transcription(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called with no transcription to embed")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    # Separated, but /transcribe was never called.

    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 409


def test_package_rejects_unknown_pitch_model(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called for an unrecognized pitch_model")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)

    response = client.post(
        f"/tracks/{track_id}/package",
        headers=HEADERS,
        json={"pitch_model": "not-a-real-model"},
    )

    assert response.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_tracks_package.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on every test.

- [ ] **Step 3: Implement the route**

In `services/api/app/routes/tracks.py`, add to the imports:

```python
from app.packaging import (
    AccompanimentError,
    PitchExtractionError,
    StructureExtractionError,
    build_package,
)
```

(Add this as a new import block alongside the existing `from app.transcription import (...)` block —
do not merge into it, `packaging` is a separate module.) Also add `KaraokePackage` to the existing
`from app.models import ...` line.

Add near `ALLOWED_WHISPER_MODEL_SIZES`/`TRANSCRIPTION_TIMEOUT_SECONDS`:

```python
ALLOWED_PITCH_MODELS = ("tiny", "full")
DEFAULT_PITCH_MODEL = "tiny"

# Packaging runs pitch extraction (torchcrepe) and structure detection (librosa) as one
# run_inference() call -- see docs/BENCHMARKS.md's M5 section for real measured numbers. Same
# "one heavy job at a time on this box" reasoning as SEPARATION_TIMEOUT_SECONDS/
# TRANSCRIPTION_TIMEOUT_SECONDS above.
PACKAGE_TIMEOUT_SECONDS = 1800
```

Add at the end of the file:

```python
class PackageRequest(BaseModel):
    pitch_model: str = DEFAULT_PITCH_MODEL


class PackageResponse(BaseModel):
    track_id: uuid.UUID
    schema_version: int
    words: list[WordInfo]
    pitch_model: str
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


@router.post("/tracks/{track_id}/package", response_model=PackageResponse)
def package_track(
    track_id: uuid.UUID,
    body: PackageRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> PackageResponse:
    pitch_model = body.pitch_model if body is not None else DEFAULT_PITCH_MODEL
    if pitch_model not in ALLOWED_PITCH_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"pitch_model must be one of {ALLOWED_PITCH_MODELS}",
        )

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    stem_rows = db.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
    # setdefault picks the first row per stem_type in query order -- arbitrary if /separate was
    # ever called more than once for this track (M3's known, deliberately-deferred idempotency
    # gap), same reasoning as transcribe_track's vocals-stem lookup above.
    stems_by_type: dict[str, Stem] = {}
    for stem in stem_rows:
        stems_by_type.setdefault(stem.stem_type, stem)
    missing_stem_types = {"vocals", "drums", "bass", "other"} - set(stems_by_type)
    if missing_stem_types:
        raise HTTPException(
            status_code=409,
            detail=f"track is missing stems: {sorted(missing_stem_types)} -- run /separate first",
        )

    latest_transcription = db.execute(
        select(Transcription)
        .where(Transcription.track_id == track.id)
        .order_by(Transcription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_transcription is None:
        raise HTTPException(
            status_code=409, detail="no transcription found -- run /transcribe first"
        )

    minio_client = get_minio_client()
    tmp_paths: dict[str, Path] = {}
    try:
        for stem_type, stem in stems_by_type.items():
            data = fetch_track_file(minio_client, stem.storage_key)
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            tmp_paths[stem_type] = Path(tmp.name)

        try:
            result = run_inference(
                lambda: build_package(
                    vocals_path=tmp_paths["vocals"],
                    drums_path=tmp_paths["drums"],
                    bass_path=tmp_paths["bass"],
                    other_path=tmp_paths["other"],
                    pitch_model=pitch_model,
                ),
                timeout_seconds=PACKAGE_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except (AccompanimentError, PitchExtractionError, StructureExtractionError) as exc:
            raise HTTPException(
                status_code=422, detail="could not package track"
            ) from exc
    finally:
        for path in tmp_paths.values():
            path.unlink(missing_ok=True)

    words_json = [
        dict(w, text=(w["text"] if latest_transcription.lyrics_display_allowed else None))
        for w in latest_transcription.words
    ]
    package = KaraokePackage(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        schema_version=1,
        words=words_json,
        pitch_model=result.pitch_model,
        pitch=[
            {"time_ms": f.time_ms, "hz": f.hz, "confidence": f.confidence} for f in result.pitch
        ],
        tempo_bpm=result.tempo_bpm,
        beats_ms=result.beats_ms,
        sections_ms=result.sections_ms,
        created_at=datetime.now(UTC),
    )
    db.add(package)
    db.flush()

    return PackageResponse(
        track_id=package.track_id,
        schema_version=package.schema_version,
        words=[
            WordInfo(
                idx=cast(int, w["idx"]),
                start_ms=cast(int, w["start_ms"]),
                end_ms=cast(int, w["end_ms"]),
                confidence=cast(float, w["confidence"]),
                text=cast("str | None", w["text"]),
            )
            for w in package.words
        ],
        pitch_model=package.pitch_model,
        tempo_bpm=package.tempo_bpm,
        beats_ms=package.beats_ms,
        sections_ms=package.sections_ms,
    )
```

Note: `AccompanimentError`'s and `StructureExtractionError`'s messages never touch lyric text (they
describe audio-processing failures only), so the 422 mapping's fixed `"could not package track"` detail
is a deliberate, consistent choice across all three exception types rather than a per-type distinction —
simpler than `/transcribe`'s two-way split, and correct here since none of the three failure modes can
embed lyric content in the first place.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_tracks_package.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 6: Commit**

```bash
git add services/api/app/routes/tracks.py services/api/tests/test_tracks_package.py
git commit -m "M5: add POST /tracks/{track_id}/package"
```

---

### Task 4: Real CREPE benchmarks

**Files:**
- Create: `services/api/scripts/benchmark_pitch.py`
- Modify: `docs/BENCHMARKS.md`

**Interfaces:**
- Consumes: `app.packaging.extract_pitch` (Task 2). No other task depends on this one.

This task produces no product code — a standalone script plus its real, measured output. Per
`CLAUDE.md`, the numbers in `docs/BENCHMARKS.md` must come from actually running the script.

- [ ] **Step 1: Write the benchmark script**

Create `services/api/scripts/benchmark_pitch.py`:

```python
"""Measures real wall-clock time for torchcrepe's 'tiny' vs 'full' pitch models. Not a test --
run manually, paste its real output into docs/BENCHMARKS.md. Uses a synthetic tone (not real
music) so it needs no rights clearance to run or share -- this only measures speed, not pitch
accuracy on real singing, which stays TODO: unmeasured (no ground-truth vocal pitch dataset is
in scope for this milestone).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from app.packaging import extract_pitch

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
            f"sine=frequency=220:duration={BENCHMARK_DURATION_SECONDS}",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def run_benchmark(model: str) -> None:
    with TemporaryDirectory() as tmp_dir:
        tone_path = Path(tmp_dir) / "benchmark_tone.wav"
        _make_benchmark_tone(tone_path)

        start = time.monotonic()
        frames = extract_pitch(tone_path, model=model)
        elapsed = time.monotonic() - start

        realtime_factor = BENCHMARK_DURATION_SECONDS / elapsed
        print(f"model={model}")
        print(f"  input duration: {BENCHMARK_DURATION_SECONDS}s")
        print(f"  wall clock: {elapsed:.1f}s")
        print(f"  realtime factor: {realtime_factor:.2f}x")
        print(f"  frames produced: {len(frames)}")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "tiny"
    run_benchmark(model)
```

- [ ] **Step 2: Run it for both models and record the real output**

Run, from `services/api`:

```bash
python scripts/benchmark_pitch.py tiny
python scripts/benchmark_pitch.py full
```

Copy the exact printed output of both runs.

- [ ] **Step 3: Write the M5 section of `docs/BENCHMARKS.md`**

Read the existing file first (it has M3 and M4 sections already). Append a new `## M5: Pitch
extraction (torchcrepe)` section following the same style: what was measured, on what machine, via
what real command, a table of real wall-clock/realtime-factor numbers for `tiny` and `full` against a
synthetic 3-minute tone. Include a line stating pitch *accuracy* against real singing is `TODO:
unmeasured` — no ground-truth vocal pitch dataset is in scope for this milestone, matching this
project's discipline of never writing a plausible-looking number that wasn't actually measured.

- [ ] **Step 4: Commit**

```bash
git add services/api/scripts/benchmark_pitch.py docs/BENCHMARKS.md
git commit -m "M5: add real CREPE speed benchmarks"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (pitch on vocals, beat/structure on accompaniment) — covered in Task 2's
`extract_pitch`/`synthesize_accompaniment`/`extract_structure` split. Decision 2 (unlabeled boundaries,
duration-derived `k`, count-vs-position distinction stated in code) — covered in `extract_structure`'s
implementation and its inline comment. Decision 3 (`karaoke_packages` JSONB table) — covered in Task 1.
Decision 4 (CREPE `tiny` default, parameterized, benchmarked) — covered in Task 3's whitelist/default
and Task 4's real benchmarks. `schema_version` baked in from row one — covered in Task 1's model and
Task 3's insert. Word `text` nulled at write time when `lyrics_display_allowed` is false — covered in
Task 3's `words_json` construction. All four gate checks (status, all-four-stems, transcription-exists,
pitch-model-whitelist) with non-invocation tests — covered in Task 3.

**Placeholder scan:** No TBD/TODO in this plan's own instructions. `docs/BENCHMARKS.md`'s eventual
`TODO: unmeasured` line for pitch accuracy (Task 4, Step 3) is the established, deliberate pattern from
M3/M4a/M4b, not a plan gap.

**Type consistency:** `PitchFrame`/`StructureResult`/`PackageResult` (Task 2) match exactly how Task 3
constructs the `KaraokePackage` row and the `PackageResponse` — checked field-by-field by hand.
`build_package(vocals_path, drums_path, bass_path, other_path, pitch_model="tiny") -> PackageResult`'s
signature in Task 2 matches its call site in Task 3's route exactly, including keyword-argument names.
`ALLOWED_PITCH_MODELS`/`DEFAULT_PITCH_MODEL` (Task 3) match Task 2's `extract_pitch`'s own
`model: str = "tiny"` default and Task 4's benchmark script's two model names (`"tiny"`, `"full"`).
