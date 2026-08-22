# M4a: Alignment Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `POST /tracks/{track_id}/transcribe` endpoint that runs Whisper + wav2vec2 forced
alignment on a rights-gate-passed track's vocal stem, stores word-level timings with confidence, and
prove its accuracy with a real eval harness against JamendoLyrics Multi-Lang.

**Architecture:** A generic `gpu_backend.run_inference()` seam (built now per ADR-0001, retrofitting
M3's Demucs call as the first user) serializes and bounds every inference call in this process. A new
`transcription.py` module wraps faster-whisper and torchaudio's wav2vec2 forced alignment as pure
functions. A new `transcriptions` table stores results with a `lyrics_display_allowed` flag resolved
from the existing rights-lane/license data. `scripts/eval_alignment.py` measures real word-onset error
against a public benchmark, using every artifact ephemerally.

**Tech Stack:** faster-whisper (CTranslate2, MIT) for ASR. `torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H`
(MIT) for English forced alignment via `torchaudio.functional.forced_align` + `merge_tokens`. The
`datasets` library (Apache 2.0, eval-only) for loading JamendoLyrics Multi-Lang.

## Global Constraints

- **Nothing reaches a GPU without a rights-gate PASS** (`CLAUDE.md`) — `track.status == "passed"`,
  checked before any model loads.
- **Source separation always precedes transcription** (`CLAUDE.md`) — transcription must read the
  `vocals` stem via a `stems` row, never the original mix. No code path may reach Whisper without a
  vocals stem existing; tests must prove the models were never invoked on a gate failure, not just
  check the status code.
- **All internal audio is 44.1kHz stereo WAV, asserted at every stage boundary** (`CLAUDE.md`) — the
  vocals stem consumed here already carries that guarantee from M3. The transient 16kHz mono tensor
  built inside `align_words` for the wav2vec2 model is a model input, never persisted or returned, and
  does not violate this rule.
- **Never log raw audio, lyrics, or signed URLs** (`CLAUDE.md`) — no transcript or word text may appear
  in any exception message, log line, or HTTP error detail.
- **Lyric display rights are tracked separately from recording rights** (`CLAUDE.md`) — missing lyric
  clearance is a supported degraded state (timings returned, text withheld), never an error.
- **No fabricated accuracy, latency, or cost figure** (`CLAUDE.md`) — `docs/BENCHMARKS.md`'s M4 numbers
  must come from actually running `scripts/eval_alignment.py`, not be written from expectation.
- **Every table carries `tenant_id`, RLS `ENABLE`+`FORCE`d, `tenant_isolation` policy, granted to
  `songbox_app` not the superuser role** — exact pattern of `services/api/alembic/versions/
  0002_row_level_security.py` and `0004_add_stems_table.py`.
- **No `yt-dlp`/`youtube-dl`/`pytube`-class dependency, ever.**
- **JamendoLyrics licensing (design-spec correction, binding on Task 5):** the dataset is NOT rights-
  clean by construction — most tracks are CC BY-NC-ND/SA. `scripts/eval_alignment.py` must skip any
  row whose `license_type` field contains `"ND"`, and must delete every artifact derived from a
  track's audio (temp source file, separated stems, alignment output) immediately after scoring that
  track — only the aggregate numbers written to `docs/BENCHMARKS.md` may persist. No JamendoLyrics
  track may ever be uploaded through `/tracks/upload` or stored via the product's own pipeline.
- **`torchaudio>=2.1,<3.0`, `torch>=2.1,<3.0`** already pinned in `services/api/pyproject.toml` from
  M3 — sufficient for `forced_align`/`merge_tokens`, no version bump needed.

---

### Task 1: `gpu_backend` interface (ADR-0001 seam, retrofitting M3's Demucs call)

**Files:**
- Create: `services/api/app/gpu_backend.py`
- Test: `services/api/tests/test_gpu_backend.py`
- Modify: `services/api/app/routes/tracks.py` (remove `_separation_lock`, `_separate_audio_with_timeout`,
  the `threading` import; route `separate_track` through `run_inference`)
- Modify: `docs/adr/0001-gpu-backend-abstraction.md` (record that the M3 deferral ends here)

**Interfaces:**
- Produces: `app.gpu_backend.run_inference(fn: Callable[[], T], *, timeout_seconds: int) -> T`,
  `app.gpu_backend.BackendBusyError`, `app.gpu_backend.BackendTimeoutError`. Task 4 depends on all
  three.

This task is a pure refactor of already-shipped, already-reviewed M3 behavior: the acceptance bar is
that `tests/test_tracks_separate.py`'s four existing tests pass **completely unmodified**. To make that
possible, `SEPARATION_TIMEOUT_SECONDS` stays exactly where it is in `tracks.py` (same name, same
value, same location) — `run_inference` takes `timeout_seconds` as a **required** keyword argument
rather than owning a module-level default, so each call site stays in control of its own timeout and
the existing `monkeypatch.setattr("app.routes.tracks.SEPARATION_TIMEOUT_SECONDS", 0.05)` in that test
file keeps working with zero changes.

- [ ] **Step 1: Write the failing tests for `run_inference`**

Create `services/api/tests/test_gpu_backend.py`:

```python
from __future__ import annotations

import threading
import time

import pytest

from app.gpu_backend import BackendBusyError, BackendTimeoutError, run_inference


def test_run_inference_returns_the_function_result() -> None:
    result = run_inference(lambda: 42, timeout_seconds=5)
    assert result == 42


def test_run_inference_reraises_the_function_exception() -> None:
    def _boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_inference(_boom, timeout_seconds=5)


def test_run_inference_raises_backend_busy_error_when_lock_is_held() -> None:
    release_event = threading.Event()
    started_event = threading.Event()

    def _hold_lock() -> None:
        started_event.set()
        release_event.wait(timeout=5)

    holder = threading.Thread(target=lambda: run_inference(_hold_lock, timeout_seconds=5))
    holder.start()
    started_event.wait(timeout=5)
    try:
        with pytest.raises(BackendBusyError):
            run_inference(lambda: None, timeout_seconds=0.1)
    finally:
        release_event.set()
        holder.join(timeout=5)


def test_run_inference_raises_backend_timeout_error_when_fn_runs_too_long() -> None:
    def _slow() -> None:
        time.sleep(0.5)

    with pytest.raises(BackendTimeoutError):
        run_inference(_slow, timeout_seconds=0.05)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_gpu_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.gpu_backend'`

- [ ] **Step 3: Implement `gpu_backend.py`**

Create `services/api/app/gpu_backend.py`:

```python
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

# ADR-0001's `local` backend: one process-wide inference job at a time, bounded by a wall-clock
# timeout. M3 originally built this directly inside routes/tracks.py for the /separate endpoint;
# this module exists because the constraint -- "one heavy model at a time on this box" -- is a
# property of the backend (Demucs and Whisper/wav2vec2 contend for the same CPU/GPU/memory), not
# of any single endpoint. Every pipeline stage's inference call should go through run_inference()
# rather than each managing its own lock.
_inference_lock = threading.Lock()


class BackendBusyError(Exception):
    """Raised when the inference lock could not be acquired within the timeout -- another job
    is already running."""


class BackendTimeoutError(Exception):
    """Raised when fn() did not complete within the timeout. The underlying thread is left
    running to finish (or fail) on its own -- CPU-bound torch/ctranslate2 inference cannot be
    cancelled from Python once started. Its eventual result is discarded."""


@dataclass
class _ThreadOutcome(Generic[T]):
    value: T | None = None
    error: BaseException | None = None
    completed: bool = False


def run_inference(fn: Callable[[], T], *, timeout_seconds: int) -> T:
    """Run fn() on the `local` GPU backend, serialized against every other inference call in this
    process. Raises BackendBusyError if the lock itself can't be acquired within timeout_seconds,
    BackendTimeoutError if fn() doesn't finish within timeout_seconds, or re-raises whatever fn()
    itself raised.

    FastAPI's sync routes already run in a threadpool, so blocking the calling thread here (both
    on lock acquisition and on Thread.join) is fine -- it never blocks the event loop.
    """
    if not _inference_lock.acquire(timeout=timeout_seconds):
        raise BackendBusyError("inference backend is busy, try again")
    try:
        return _run_with_timeout(fn, timeout_seconds)
    finally:
        _inference_lock.release()


def _run_with_timeout(fn: Callable[[], T], timeout_seconds: int) -> T:
    outcome: _ThreadOutcome[T] = _ThreadOutcome()

    def _target() -> None:
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the calling thread below
            outcome.error = exc
            return
        outcome.value = result
        outcome.completed = True

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise BackendTimeoutError("inference timed out")

    if outcome.error is not None:
        raise outcome.error
    if not outcome.completed:
        raise BackendTimeoutError("inference thread exited unexpectedly")

    # `completed` being True guarantees `value` was set to a real result of fn() -- but mypy
    # can't infer that correlation from a plain bool flag (and a None check isn't a valid
    # narrowing here either, since T could legitimately be a type that allows None).
    return outcome.value  # type: ignore[return-value]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_gpu_backend.py -v`
Expected: PASS (4/4)

- [ ] **Step 5: Retrofit `separate_track` to use `run_inference`**

In `services/api/app/routes/tracks.py`:

Remove the `import threading` line, the `_separation_lock = threading.Lock()` module-level
assignment and its preceding comment block, and the entire `_separate_audio_with_timeout` function.

Add to the imports:

```python
from app.gpu_backend import BackendBusyError, BackendTimeoutError, run_inference
```

Replace the body of `separate_track`'s try block that currently reads:

```python
        # Only one Demucs run at a time (see _separation_lock's module-level comment). Wait up
        # to SEPARATION_TIMEOUT_SECONDS for the lock itself, too -- a request that can't even
        # start within that window is no better off than one that starts and then times out.
        if not _separation_lock.acquire(timeout=SEPARATION_TIMEOUT_SECONDS):
            raise HTTPException(status_code=503, detail="separation is busy, try again")
        try:
            stem_paths = _separate_audio_with_timeout(Path(tmp.name), model_name=model_name)
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc
        finally:
            _separation_lock.release()
```

with:

```python
        try:
            stem_paths = run_inference(
                lambda: separate_audio(Path(tmp.name), model_name=model_name),
                timeout_seconds=SEPARATION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc
```

`SEPARATION_TIMEOUT_SECONDS` itself and its comment stay exactly where they are in the file --
only the lock/thread mechanics move into `gpu_backend.py`.

- [ ] **Step 6: Run the existing M3 test suite to verify it's still green, unmodified**

Run: `cd services/api && pytest tests/test_tracks_separate.py -v`
Expected: PASS (4/4), with **zero changes** to `test_tracks_separate.py` itself. If any test fails,
the refactor changed observable behavior -- stop and fix `tracks.py`, do not edit the test to match.

- [ ] **Step 7: Update ADR-0001**

In `docs/adr/0001-gpu-backend-abstraction.md`, add after the existing "### M3 update" section:

```markdown

### M4a update

The deferral above ended here, as planned. `services/api/app/gpu_backend.py` now provides the
`local` backend's `run_inference()` interface this ADR describes: one process-wide inference job
at a time, bounded by a caller-supplied wall-clock timeout. M3's `separate_audio()` call in
`services/api/app/routes/tracks.py` was retrofitted to go through it (Task 1 of
`docs/superpowers/plans/2026-08-21-alignment-engine.md`), and M4a's transcription/alignment calls
(Task 4 of the same plan) use it from the start. The `modal`/`runpod` implementations remain M7's
work.
```

- [ ] **Step 8: Run ruff and mypy**

Run: `cd services/api && ruff check . && mypy app`
Expected: both clean, no errors.

- [ ] **Step 9: Run the full suite**

Run: `cd services/api && pytest -q`
Expected: PASS, same count as before this task plus the 4 new `test_gpu_backend.py` tests, no
regressions.

- [ ] **Step 10: Commit**

```bash
git add services/api/app/gpu_backend.py services/api/tests/test_gpu_backend.py \
  services/api/app/routes/tracks.py docs/adr/0001-gpu-backend-abstraction.md
git commit -m "M4a: add gpu_backend interface, retrofit M3's Demucs call through it"
```

---

### Task 2: `transcriptions` table + lyric-rights resolution

**Files:**
- Modify: `services/api/app/models.py` (add `Transcription`)
- Create: `services/api/alembic/versions/0005_add_transcriptions_table.py`
- Modify: `services/api/app/gate.py` (add `resolve_lyrics_display_allowed`)
- Test: `services/api/tests/test_gate.py` (add tests for the new function)
- Test: `services/api/tests/test_models.py`, `services/api/tests/test_db_rls.py` (existing,
  unmodified — generically cover the new table, as `stems` was in M3)

**Interfaces:**
- Produces: `app.models.Transcription` — `id`, `tenant_id`, `track_id` (FK), `whisper_model: str`,
  `aligner: str`, `language: str`, `lyrics_display_allowed: bool`, `words: list[dict]` (JSONB),
  `created_at: datetime`. `app.gate.resolve_lyrics_display_allowed(lane: str,
  license_covers_lyrics: bool | None) -> bool`. Task 4 imports and uses both.

- [ ] **Step 1: Add the `Transcription` model**

Append to `services/api/app/models.py`:

```python
class Transcription(Base):
    __tablename__ = "transcriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # whisper_model: which faster-whisper size produced this row, e.g. "base"
    whisper_model: Mapped[str] = mapped_column(String(20), nullable=False)
    # aligner: "wav2vec2" | "whisper_native"
    aligner: Mapped[str] = mapped_column(String(20), nullable=False)
    # language: Whisper's detected ISO language code, e.g. "en"
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    lyrics_display_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # words: [{"idx": int, "text": str, "start_ms": int, "end_ms": int, "confidence": float}, ...]
    words: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Run the generic model tests to see them fail**

Run: `cd services/api && pytest tests/test_models.py -v`
Expected: FAIL on `test_every_registered_model_table_exists_in_the_database` with
`{'transcriptions'} missing from DB -- did you run \`alembic upgrade head\`?`

- [ ] **Step 3: Write migration 0005**

Create `services/api/alembic/versions/0005_add_transcriptions_table.py`:

```python
"""add transcriptions table

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "transcriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("whisper_model", sa.String(length=20), nullable=False),
        sa.Column("aligner", sa.String(length=20), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("lyrics_display_allowed", sa.Boolean(), nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transcriptions_tenant_id", "transcriptions", ["tenant_id"])
    op.create_index("ix_transcriptions_track_id", "transcriptions", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON transcriptions TO {APP_ROLE}")
    op.execute("ALTER TABLE transcriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transcriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON transcriptions
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON transcriptions")
    op.execute("ALTER TABLE transcriptions DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON transcriptions FROM {APP_ROLE}")
    op.drop_index("ix_transcriptions_track_id", table_name="transcriptions")
    op.drop_index("ix_transcriptions_tenant_id", table_name="transcriptions")
    op.drop_table("transcriptions")
```

- [ ] **Step 4: Apply the migration**

Run: `cd services/api && python -m alembic upgrade head`
Expected: no errors; last line mentions upgrading to `0005`.

- [ ] **Step 5: Run the generic model and RLS tests to see them pass**

Run: `cd services/api && pytest tests/test_models.py tests/test_db_rls.py -v`
Expected: PASS — both now cover `transcriptions` with no test-file changes.

- [ ] **Step 6: Write the failing tests for lyric-rights resolution**

Add to `services/api/tests/test_gate.py`:

```python
from app.gate import resolve_lyrics_display_allowed


def test_lane_a_always_allows_lyric_display() -> None:
    assert resolve_lyrics_display_allowed("A", license_covers_lyrics=None) is True


def test_lane_c_always_allows_lyric_display() -> None:
    assert resolve_lyrics_display_allowed("C", license_covers_lyrics=None) is True


def test_lane_b_allows_lyric_display_only_when_license_covers_lyrics() -> None:
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=True) is True
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=False) is False
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=None) is False


def test_resolve_lyrics_display_allowed_rejects_unknown_lane() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown lane"):
        resolve_lyrics_display_allowed("Z", license_covers_lyrics=None)
```

(Move the `import pytest` to the top of the file alongside the file's existing imports rather than
inline, if `test_gate.py` doesn't already import `pytest` at module level -- check the existing file
first.)

- [ ] **Step 7: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_lyrics_display_allowed'`

- [ ] **Step 8: Implement `resolve_lyrics_display_allowed`**

Append to `services/api/app/gate.py`:

```python
def resolve_lyrics_display_allowed(lane: str, license_covers_lyrics: bool | None) -> bool:
    """Lyric display rights are tracked separately from recording rights (CLAUDE.md). Lane A
    (creator-owned) and Lane C (public domain / Creative Commons) always allow lyric display.
    Lane B (licensed) allows it only if the license on file explicitly covers lyrics -- a
    license that covers the recording but not the lyrics is a real, supported case, and missing
    lyric clearance is a supported degraded state (timings without text), not an error."""
    if lane in ("A", "C"):
        return True
    if lane == "B":
        return bool(license_covers_lyrics)
    raise ValueError(f"unknown lane: {lane!r}")
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_gate.py -v`
Expected: PASS (all tests in the file, including the 4 new ones).

- [ ] **Step 10: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 11: Commit**

```bash
git add services/api/app/models.py services/api/alembic/versions/0005_add_transcriptions_table.py \
  services/api/app/gate.py services/api/tests/test_gate.py
git commit -m "M4a: add transcriptions table and lyric-rights resolution"
```

---

### Task 3: `transcription.py` — Whisper + wav2vec2 forced alignment

**Files:**
- Modify: `services/api/pyproject.toml` (add `faster-whisper` to `dependencies`)
- Create: `services/api/app/transcription.py`
- Test: `services/api/tests/test_transcription.py`

**Interfaces:**
- Consumes: `services/api/tests/conftest.py`'s `synthetic_wav` fixture (3-second, 44.1kHz stereo,
  440Hz tone WAV) — note a pure sine tone has no speech, so tests against it check the pipeline
  *runs* and produces well-formed output, not that any particular word is recognized (mirroring how
  M3's `test_separation.py` uses the same fixture).
- Produces: `app.transcription.Word` (dataclass: `idx: int`, `text: str`, `start_ms: int`,
  `end_ms: int`, `confidence: float`), `app.transcription.Transcript` (`text: str`, `language: str`,
  `words: list[Word]`), `app.transcription.TranscriptionResult` (`text: str`, `language: str`,
  `aligner: str`, `words: list[Word]`), `app.transcription.TranscriptionError`,
  `app.transcription.AlignmentError`, `app.transcription.transcribe_audio(path: Path, model_size:
  str = DEFAULT_WHISPER_MODEL_SIZE) -> Transcript`, `app.transcription.align_words(path: Path,
  text: str) -> list[Word]`, `app.transcription.run_transcription_and_alignment(path: Path,
  model_size: str) -> TranscriptionResult`, `app.transcription.DEFAULT_WHISPER_MODEL_SIZE`,
  `app.transcription.ENGLISH_LANGUAGE_CODE`. Task 4 imports all of these; Task 5 imports
  `transcribe_audio`, `align_words`, and both dataclasses directly.

**Design notes carried from the spec, not to be "fixed" by a later reader:**

`align_words` runs the wav2vec2 forward pass over the whole (resampled) clip in one call rather than
chunking it, because the acoustic forward pass for a ~3 minute clip is not the memory problem here
(wav2vec2 emits roughly 50 frames/second — a 3-minute song is ~9,000 frames over ~29 labels, a small
matrix) and `torchaudio.functional.forced_align` is documented `batch_size==1`, which one call over
one sequence satisfies exactly. `align_words` takes plain `text`, not pre-segmented text, so the
identical function serves Whisper's transcript (Task 4), the eval's reference lyrics (Task 5), and
M4b's future user-corrected lyrics.

`WAV2VEC2_ASR_BASE_960H`'s label set includes a `'|'` word-boundary token, but this implementation
does not use it. Following the verified, current (non-deprecated) torchaudio forced-alignment
pattern, words are tokenized independently (no separator inserted) and character-level `TokenSpan`s
are grouped back into words using each word's own known token count via `_unflatten` — CTC's blank
token absorbs the inter-word gap frames automatically, so no separator token is needed for this to
work correctly. This generalizes the same pattern the official multilingual (`MMS_FA`) forced-
alignment tutorial uses for a model whose label set has no separator token at all.

Error messages from `AlignmentError` must never include the literal word/transcript text they failed
on (`CLAUDE.md`: never log raw lyrics) — messages describe *what kind* of failure occurred, not which
word triggered it.

- [ ] **Step 1: Add the `faster-whisper` dependency**

In `services/api/pyproject.toml`, add to the `dependencies` list (after the `numpy>=1.26,` line):

```toml
    "faster-whisper>=1.0",
```

- [ ] **Step 2: Install it**

Run: `cd services/api && pip install -e ".[dev]"`

- [ ] **Step 3: Write the failing tests**

Create `services/api/tests/test_transcription.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.transcription import AlignmentError, _unflatten, align_words


def test_unflatten_groups_a_flat_list_by_given_lengths() -> None:
    flat = ["a", "b", "c", "d", "e"]
    grouped = _unflatten(flat, [2, 1, 2])
    assert grouped == [["a", "b"], ["c"], ["d", "e"]]


def test_align_words_produces_word_level_timings_in_order(synthetic_wav: Path) -> None:
    # A pure sine tone has no real speech, so wav2vec2 will align garbage confidently to
    # whatever text we give it -- this test proves the PIPELINE runs end-to-end and returns
    # well-formed, monotonically-ordered Word objects, not that the alignment is meaningful.
    words = align_words(synthetic_wav, "hello world")

    assert [w.text for w in words] == ["hello", "world"]
    assert [w.idx for w in words] == [0, 1]
    for word in words:
        assert word.start_ms >= 0
        assert word.end_ms >= word.start_ms
        assert 0.0 <= word.confidence <= 1.0
    # Word timings must be non-decreasing in start time -- forced_align guarantees monotonic
    # frame indices, so this should hold by construction; asserting it here catches a regression
    # in the frame-to-millisecond conversion, not just its presence.
    assert words[0].start_ms <= words[1].start_ms


def test_align_words_rejects_empty_text(synthetic_wav: Path) -> None:
    with pytest.raises(AlignmentError):
        align_words(synthetic_wav, "")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_transcription.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.transcription'`

- [ ] **Step 5: Implement `transcription.py`**

Create `services/api/app/transcription.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import torch
import torchaudio
import torchaudio.functional as F
from faster_whisper import WhisperModel

ENGLISH_LANGUAGE_CODE = "en"
DEFAULT_WHISPER_MODEL_SIZE = "base"

T = TypeVar("T")


class TranscriptionError(Exception):
    """Raised when Whisper cannot transcribe the given file. Never includes transcript text in
    its message (CLAUDE.md: never log raw lyrics) -- transcription failures are about the
    process, not about specific words, so this is naturally satisfied by describing the failure
    mode rather than any content."""


class AlignmentError(Exception):
    """Raised when forced alignment cannot align the given text against the given audio. Never
    includes word or transcript text in its message (CLAUDE.md: never log raw lyrics)."""


@dataclass(frozen=True)
class Word:
    idx: int
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    words: list[Word]  # Whisper-native word timings (word_timestamps=True)


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    aligner: str  # "wav2vec2" | "whisper_native"
    words: list[Word]


def transcribe_audio(path: Path, model_size: str = DEFAULT_WHISPER_MODEL_SIZE) -> Transcript:
    """Transcribe `path` with faster-whisper, requesting word-level timestamps directly from
    Whisper. Used as-is for non-English tracks (the "whisper_native" aligner path) and as the
    source text for English tracks, which then get forced-aligned by align_words() for tighter
    timing precision."""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:
        raise TranscriptionError(f"could not load whisper model {model_size!r}: {exc}") from exc

    try:
        segments, info = model.transcribe(str(path), word_timestamps=True)
        segment_list = list(segments)
    except Exception as exc:
        raise TranscriptionError(f"transcription failed: {exc}") from exc

    text_parts: list[str] = []
    words: list[Word] = []
    idx = 0
    for segment in segment_list:
        text_parts.append(segment.text.strip())
        if segment.words is None:
            raise TranscriptionError("word_timestamps=True but a segment had no words")
        for w in segment.words:
            words.append(
                Word(
                    idx=idx,
                    text=w.word.strip(),
                    start_ms=int(w.start * 1000),
                    end_ms=int(w.end * 1000),
                    confidence=w.probability,
                )
            )
            idx += 1

    if not words:
        raise TranscriptionError("transcription produced no words")

    return Transcript(text=" ".join(text_parts).strip(), language=info.language, words=words)


_WAV2VEC2_BUNDLE = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H


def align_words(path: Path, text: str) -> list[Word]:
    """Force-align `text` (assumed correct) against the audio at `path` using the MIT-licensed
    English wav2vec2 ASR bundle. English only -- callers must route non-English tracks to
    transcribe_audio()'s own word timings instead (see run_transcription_and_alignment).

    Runs the acoustic forward pass over the whole clip in one call (not chunked) -- see this
    file's module-level design note in the plan for why that's correct and bounded. Word
    boundaries come from tokenizing per-word and regrouping by known word length, not from the
    bundle's '|' separator token.
    """
    words_text = text.split()
    if not words_text:
        raise AlignmentError("cannot align empty text")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = _WAV2VEC2_BUNDLE.get_model().to(device)
        model.eval()
    except Exception as exc:
        raise AlignmentError(f"could not load alignment model: {exc}") from exc

    labels = _WAV2VEC2_BUNDLE.get_labels()
    dictionary = {c: i for i, c in enumerate(labels)}

    tokens_per_word: list[list[int]] = []
    for word in words_text:
        word_tokens = [dictionary[c] for c in word.upper() if c in dictionary]
        if not word_tokens:
            raise AlignmentError("transcript contains a word with no alignable characters")
        tokens_per_word.append(word_tokens)
    flat_tokens = [t for word_tokens in tokens_per_word for t in word_tokens]

    try:
        waveform, sample_rate = torchaudio.load(str(path))
    except Exception as exc:
        raise AlignmentError(f"could not load audio: {exc}") from exc
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sample_rate != _WAV2VEC2_BUNDLE.sample_rate:
        waveform = F.resample(waveform, sample_rate, _WAV2VEC2_BUNDLE.sample_rate)

    with torch.inference_mode():
        emission, _ = model(waveform.to(device))
        emission = torch.log_softmax(emission, dim=-1)

    targets = torch.tensor([flat_tokens], dtype=torch.int32, device=device)
    try:
        aligned_tokens, alignment_scores = F.forced_align(emission, targets, blank=0)
    except Exception as exc:
        raise AlignmentError(f"forced alignment failed: {exc}") from exc
    aligned_tokens, alignment_scores = aligned_tokens[0], alignment_scores[0].exp()

    token_spans = F.merge_tokens(aligned_tokens, alignment_scores)
    if len(token_spans) != len(flat_tokens):
        raise AlignmentError("alignment produced an unexpected number of token spans")

    word_spans = _unflatten(token_spans, [len(wt) for wt in tokens_per_word])

    num_frames = emission.shape[1]
    ratio = waveform.shape[1] / num_frames

    words: list[Word] = []
    for idx, (word_text, spans) in enumerate(zip(words_text, word_spans, strict=True)):
        start_sample = ratio * spans[0].start
        end_sample = ratio * spans[-1].end
        start_ms = int(start_sample / _WAV2VEC2_BUNDLE.sample_rate * 1000)
        end_ms = int(end_sample / _WAV2VEC2_BUNDLE.sample_rate * 1000)
        confidence = sum(s.score for s in spans) / len(spans)
        words.append(
            Word(idx=idx, text=word_text, start_ms=start_ms, end_ms=end_ms, confidence=confidence)
        )

    return words


def _unflatten(items: list[T], lengths: list[int]) -> list[list[T]]:
    result: list[list[T]] = []
    i = 0
    for length in lengths:
        result.append(items[i : i + length])
        i += length
    return result


def run_transcription_and_alignment(path: Path, model_size: str) -> TranscriptionResult:
    """Orchestrates the full stage: transcribe, then align English tracks against their own
    transcript for tighter word-onset precision; non-English tracks keep Whisper's own word
    timings, since the alignment model here only covers English (see the design spec's
    licensing-blocked-multilingual-aligner scope decision)."""
    transcript = transcribe_audio(path, model_size=model_size)
    if transcript.language == ENGLISH_LANGUAGE_CODE:
        words = align_words(path, transcript.text)
        aligner = "wav2vec2"
    else:
        words = transcript.words
        aligner = "whisper_native"
    return TranscriptionResult(
        text=transcript.text, language=transcript.language, aligner=aligner, words=words
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_transcription.py -v`
Expected: PASS (3/3). First run downloads the wav2vec2 checkpoint (~360MB) — this happens once
per machine and is noticeably slower than subsequent runs, the same one-time-download pattern M3's
Demucs model had.

- [ ] **Step 7: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 8: Commit**

```bash
git add services/api/pyproject.toml services/api/app/transcription.py \
  services/api/tests/test_transcription.py
git commit -m "M4a: add transcribe_audio() and align_words()"
```

---

### Task 4: `POST /tracks/{track_id}/transcribe` and `GET /tracks/{track_id}/transcription`

**Files:**
- Modify: `services/api/app/routes/tracks.py`
- Test: `services/api/tests/test_tracks_transcribe.py` (new)

**Interfaces:**
- Consumes: `app.gpu_backend.run_inference`, `BackendBusyError`, `BackendTimeoutError` (Task 1);
  `app.models.Transcription`, `app.gate.resolve_lyrics_display_allowed` (Task 2);
  `app.transcription.run_transcription_and_alignment`, `TranscriptionError`, `AlignmentError`,
  `DEFAULT_WHISPER_MODEL_SIZE` (Task 3); `app.models.Stem` (existing, M3).
- Produces: `POST /tracks/{track_id}/transcribe` — optional JSON body `{"model_size": str}`
  (defaults to `DEFAULT_WHISPER_MODEL_SIZE`), returns `{"track_id": uuid, "language": str,
  "aligner": str, "lyrics_display_allowed": bool, "words": [{"idx": int, "start_ms": int,
  "end_ms": int, "confidence": float, "text": str | null}, ...]}` on 200; 404 if track not
  found/wrong tenant; 409 if `track.status != "passed"` or no `vocals` stem exists; 422 for an
  unknown `model_size` or a transcription/alignment failure; 503/504 from the shared inference
  backend. `GET /tracks/{track_id}/transcription` returns the same shape for the most recent
  transcription, 404 if none exists.

Ordering inside the route matters and is the point of this task, mirroring M3's proven pattern:
whitelist check → track lookup/tenant check → gate check → vocals-stem check, **all before any
model loads**, with a test proving `run_transcription_and_alignment` is never invoked when either
gate fails.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_tracks_transcribe.py`:

```python
from __future__ import annotations

import time
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
from app.transcription import TranscriptionResult, Word

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


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    track_id = _upload_and_pass_track(synthetic_wav)
    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def test_transcribe_stores_words_and_marks_lyrics_display_allowed_for_lane_a(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    assert body["lyrics_display_allowed"] is True
    assert len(body["words"]) > 0
    for word in body["words"]:
        assert word["text"] is not None
        assert word["start_ms"] >= 0
        assert word["end_ms"] >= word["start_ms"]

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text("SELECT whisper_model, aligner, language FROM transcriptions WHERE track_id = :track_id"),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 1


def test_transcribe_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)

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

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 409


def test_transcribe_rejects_track_with_no_vocals_stem(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run when no vocals stem exists")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)
    track_id = _upload_and_pass_track(synthetic_wav)
    # Deliberately NOT calling /separate -- no vocals stem exists for this track.

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 409


def test_transcribe_rejects_unknown_model_size(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run for an unrecognized model_size")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/transcribe",
        headers=HEADERS,
        json={"model_size": "not-a-real-size"},
    )

    assert response.status_code == 422


def test_transcribe_withholds_text_but_keeps_timings_when_lyrics_not_allowed(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    fake_result = TranscriptionResult(
        text="hello world",
        language="en",
        aligner="wav2vec2",
        words=[
            Word(idx=0, text="hello", start_ms=0, end_ms=400, confidence=0.9),
            Word(idx=1, text="world", start_ms=400, end_ms=800, confidence=0.9),
        ],
    )
    monkeypatch.setattr(
        "app.routes.tracks.run_transcription_and_alignment", lambda *a, **k: fake_result
    )
    # Lane B with no license_covers_lyrics=True on file -> lyrics_display_allowed must be False.
    monkeypatch.setattr("app.routes.tracks.resolve_lyrics_display_allowed", lambda *a, **k: False)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["lyrics_display_allowed"] is False
    assert len(body["words"]) == 2
    for word in body["words"]:
        assert word["text"] is None
        assert word["start_ms"] is not None


def test_get_transcription_returns_the_stored_result(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    post_response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)
    assert post_response.status_code == 200

    get_response = client.get(f"/tracks/{track_id}/transcription", headers=HEADERS)

    assert get_response.status_code == 200
    assert get_response.json() == post_response.json()


def test_get_transcription_returns_404_when_none_exists(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.get(f"/tracks/{track_id}/transcription", headers=HEADERS)

    assert response.status_code == 404


def test_transcribe_returns_504_when_it_exceeds_the_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setattr("app.routes.tracks.TRANSCRIPTION_TIMEOUT_SECONDS", 0.05)

    def _slow(*args: object, **kwargs: object) -> TranscriptionResult:
        time.sleep(0.5)
        return TranscriptionResult(text="", language="en", aligner="wav2vec2", words=[])

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _slow)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 504
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_tracks_transcribe.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on every test.

- [ ] **Step 3: Implement the routes**

In `services/api/app/routes/tracks.py`, add to the imports:

```python
from sqlalchemy import select

from app.gate import resolve_lane_outcome, resolve_lyrics_display_allowed
from app.gpu_backend import BackendBusyError, BackendTimeoutError, run_inference
from app.models import FingerprintMatch, License, RightsDeclaration, Stem, Track, Transcription
from app.transcription import (
    AlignmentError,
    DEFAULT_WHISPER_MODEL_SIZE,
    TranscriptionError,
    run_transcription_and_alignment,
)
```

(This replaces the existing `from app.gate import resolve_lane_outcome` and `from app.models
import ...` lines — add `resolve_lyrics_display_allowed` to the first, `Transcription` to the
second, and add `from sqlalchemy import select` alongside the existing top-of-file imports.)

Add near `ALLOWED_SEPARATION_MODELS`:

```python
ALLOWED_WHISPER_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")

# Whisper + wav2vec2 forced alignment is slower per second of audio than Demucs separation was
# (see docs/BENCHMARKS.md's M4 section once measured) but shares the same "one heavy model at a
# time on this box" reasoning as SEPARATION_TIMEOUT_SECONDS above. Using the same 1800s bound
# until real numbers say otherwise.
TRANSCRIPTION_TIMEOUT_SECONDS = 1800
```

Add at the end of the file:

```python
class WordInfo(BaseModel):
    idx: int
    start_ms: int
    end_ms: int
    confidence: float
    text: str | None


class TranscribeRequest(BaseModel):
    model_size: str = DEFAULT_WHISPER_MODEL_SIZE


class TranscribeResponse(BaseModel):
    track_id: uuid.UUID
    language: str
    aligner: str
    lyrics_display_allowed: bool
    words: list[WordInfo]


def _transcription_to_response(transcription: Transcription) -> TranscribeResponse:
    words = [
        WordInfo(
            idx=w["idx"],
            start_ms=w["start_ms"],
            end_ms=w["end_ms"],
            confidence=w["confidence"],
            text=w["text"] if transcription.lyrics_display_allowed else None,
        )
        for w in transcription.words
    ]
    return TranscribeResponse(
        track_id=transcription.track_id,
        language=transcription.language,
        aligner=transcription.aligner,
        lyrics_display_allowed=transcription.lyrics_display_allowed,
        words=words,
    )


@router.post("/tracks/{track_id}/transcribe", response_model=TranscribeResponse)
def transcribe_track(
    track_id: uuid.UUID,
    body: TranscribeRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    model_size = body.model_size if body is not None else DEFAULT_WHISPER_MODEL_SIZE
    if model_size not in ALLOWED_WHISPER_MODEL_SIZES:
        raise HTTPException(
            status_code=422,
            detail=f"model_size must be one of {ALLOWED_WHISPER_MODEL_SIZES}",
        )

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    vocals_stem = db.execute(
        select(Stem).where(Stem.track_id == track.id, Stem.stem_type == "vocals")
    ).scalar_one_or_none()
    if vocals_stem is None:
        raise HTTPException(
            status_code=409, detail="track has no vocals stem -- run /separate first"
        )

    minio_client = get_minio_client()
    vocal_bytes = fetch_track_file(minio_client, vocals_stem.storage_key)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(vocal_bytes)
        tmp.flush()
        tmp.close()
        try:
            result = run_inference(
                lambda: run_transcription_and_alignment(Path(tmp.name), model_size=model_size),
                timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except TranscriptionError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not transcribe audio: {exc}"
            ) from exc
        except AlignmentError as exc:
            # Deliberately NOT interpolating `exc` into the response -- CLAUDE.md forbids
            # logging raw lyrics, and an alignment failure can legitimately occur mid-transcript,
            # so its message must never be trusted to be content-free by construction the way
            # TranscriptionError's failures (which occur before any text exists) are.
            raise HTTPException(status_code=422, detail="could not align transcript to audio") from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    assert declaration is not None
    license_covers_lyrics: bool | None = None
    if declaration.license_id is not None:
        license_row = db.get(License, declaration.license_id)
        license_covers_lyrics = license_row.covers_lyrics if license_row else None
    lyrics_display_allowed = resolve_lyrics_display_allowed(declaration.lane, license_covers_lyrics)

    words_json = [
        {
            "idx": w.idx,
            "text": w.text,
            "start_ms": w.start_ms,
            "end_ms": w.end_ms,
            "confidence": w.confidence,
        }
        for w in result.words
    ]
    transcription = Transcription(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        whisper_model=model_size,
        aligner=result.aligner,
        language=result.language,
        lyrics_display_allowed=lyrics_display_allowed,
        words=words_json,
        created_at=datetime.now(UTC),
    )
    db.add(transcription)
    db.flush()

    return _transcription_to_response(transcription)


@router.get("/tracks/{track_id}/transcription", response_model=TranscribeResponse)
def get_transcription(
    track_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")

    transcription = db.execute(
        select(Transcription)
        .where(Transcription.track_id == track.id)
        .order_by(Transcription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if transcription is None:
        raise HTTPException(status_code=404, detail="no transcription found for this track")

    return _transcription_to_response(transcription)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_tracks_transcribe.py -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 6: Commit**

```bash
git add services/api/app/routes/tracks.py services/api/tests/test_tracks_transcribe.py
git commit -m "M4a: add POST /tracks/{track_id}/transcribe and GET .../transcription"
```

---

### Task 5: Eval harness against JamendoLyrics Multi-Lang

**Files:**
- Modify: `services/api/pyproject.toml` (add a new `eval` optional-dependencies group)
- Create: `services/api/scripts/eval_alignment.py`
- Modify: `docs/BENCHMARKS.md` (add the M4 section)
- Modify: `docs/STATUS.md` (record this milestone's real deviations, per the working agreement M1-M3
  followed)

**Interfaces:**
- Consumes: `app.gpu_backend.run_inference` (Task 1), `app.separation.separate_audio` (M3),
  `app.transcription.align_words`, `transcribe_audio`, `Word` (Task 3). No other task depends on
  this one.

This task produces no importable interface and adds no product code — it is a standalone script plus
its real, measured output. Per `CLAUDE.md`, the numbers in `docs/BENCHMARKS.md` must come from
actually running the script; do not write plausible-looking numbers into the doc file directly.

- [ ] **Step 1: Add the `eval` optional-dependencies group**

In `services/api/pyproject.toml`, add a new group after `[project.optional-dependencies]`'s existing
`dev` group:

```toml
eval = [
    "datasets>=2.14",
]
```

This is separate from `dev`/core `dependencies` deliberately: `datasets` is only needed by this
manual eval script, never by the running API, so production and CI installs (`pip install -e
".[dev]"`) stay unaffected.

- [ ] **Step 2: Install it**

Run: `cd services/api && pip install -e ".[eval]"`

- [ ] **Step 3: Write the eval script**

Create `services/api/scripts/eval_alignment.py`:

```python
"""Measures real word-onset alignment accuracy against JamendoLyrics Multi-Lang
(jamendolyrics/jamendolyrics on Hugging Face). Not a test -- run manually, paste its real output
into docs/BENCHMARKS.md.

Two measurements, matching the two production code paths in app/transcription.py's
run_transcription_and_alignment():

1. "aligned" (English only, primary -- this is what PLAN.md's +/-50ms criterion is about): force-
   align the KNOWN-CORRECT reference lyrics against the audio via align_words(). Predicted and
   reference word lists are identical in count and order by construction, since align_words() is
   given the reference text as its own alignment target, so predicted/reference word pairs are
   compared directly with no matching step needed.

2. "whisper_native" (non-English fallback, and a secondary number for English): run
   transcribe_audio() and compare ITS OWN predicted words against the ground truth. Whisper's
   transcript may not exactly match the reference lyrics (real recognition errors), so predicted
   and reference words are reconciled with a difflib sequence match before scoring -- unmatched
   words are excluded from the onset-error number, and the match rate is reported alongside it so
   a low match rate can't hide inside an artificially good number.

Rights handling (binding, not optional -- see docs/superpowers/specs/2026-08-21-alignment-engine-
design.md's licensing correction): most JamendoLyrics tracks are CC BY-NC-ND/SA, not rights-clean.
This script (a) skips any row whose license_type contains "ND", and (b) deletes every artifact
derived from a track's audio (temp source file, separated stems, alignment output) immediately
after that track is scored. Only the aggregate numbers this script prints may ever be committed --
never the audio, never any per-track derived file.
"""
from __future__ import annotations

import difflib
import shutil
import statistics
import tempfile
from pathlib import Path

from datasets import Audio, load_dataset

from app.gpu_backend import run_inference
from app.separation import separate_audio
from app.transcription import Word, align_words, transcribe_audio

INFERENCE_TIMEOUT_SECONDS = 1800
ENGLISH_LANGUAGE_CODE = "en"
ONSET_TOLERANCE_MS = 50
_PUNCTUATION = ".,!?;:\"'"


def _normalize(word: str) -> str:
    return word.lower().strip(_PUNCTUATION)


def _score_aligned(vocals_path: Path, reference_words: list[dict[str, object]]) -> list[float]:
    reference_text = " ".join(str(w["text"]) for w in reference_words)
    predicted = run_inference(
        lambda: align_words(vocals_path, reference_text), timeout_seconds=INFERENCE_TIMEOUT_SECONDS
    )
    errors: list[float] = []
    for pred, ref in zip(predicted, reference_words, strict=True):
        ref_start_ms = float(ref["start"]) * 1000
        errors.append(abs(pred.start_ms - ref_start_ms))
    return errors


def _score_whisper_native(
    vocals_path: Path, reference_words: list[dict[str, object]], model_size: str
) -> tuple[list[float], float]:
    transcript = run_inference(
        lambda: transcribe_audio(vocals_path, model_size=model_size),
        timeout_seconds=INFERENCE_TIMEOUT_SECONDS,
    )
    reference_texts = [str(w["text"]) for w in reference_words]
    reference_starts_ms = [float(w["start"]) * 1000 for w in reference_words]

    predicted_norm = [_normalize(w.text) for w in transcript.words]
    reference_norm = [_normalize(t) for t in reference_texts]
    matcher = difflib.SequenceMatcher(None, predicted_norm, reference_norm, autojunk=False)

    errors: list[float] = []
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            predicted_word = transcript.words[block.a + offset]
            ref_start_ms = reference_starts_ms[block.b + offset]
            errors.append(abs(predicted_word.start_ms - ref_start_ms))
            matched += 1

    match_rate = matched / len(reference_words) if reference_words else 0.0
    return errors, match_rate


def _summarize(errors: list[float]) -> tuple[float, float] | tuple[None, None]:
    if not errors:
        return None, None
    median = statistics.median(errors)
    within_tolerance = sum(1 for e in errors if e <= ONSET_TOLERANCE_MS) / len(errors) * 100
    return median, within_tolerance


def main(whisper_model_size: str = "base") -> None:
    dataset = load_dataset("jamendolyrics/jamendolyrics", split="test")
    dataset = dataset.cast_column("audio", Audio(decode=False))

    aligned_errors_by_lang: dict[str, list[float]] = {}
    native_errors_by_lang: dict[str, list[float]] = {}
    native_match_rates: list[float] = []
    skipped_nd = 0
    scored = 0

    for row in dataset:
        license_type = str(row["license_type"])
        if "ND" in license_type:
            skipped_nd += 1
            continue

        language = str(row["language"])
        audio_bytes = row["audio"]["bytes"]
        reference_words = list(row["words"])

        with tempfile.TemporaryDirectory(prefix="songbox-eval-") as tmp_dir:
            source_path = Path(tmp_dir) / "source.mp3"
            source_path.write_bytes(audio_bytes)

            stem_paths = run_inference(
                lambda: separate_audio(source_path), timeout_seconds=INFERENCE_TIMEOUT_SECONDS
            )
            vocals_path = stem_paths["vocals"]
            try:
                if language == ENGLISH_LANGUAGE_CODE:
                    aligned_errors = _score_aligned(vocals_path, reference_words)
                    aligned_errors_by_lang.setdefault(language, []).extend(aligned_errors)

                native_errors, match_rate = _score_whisper_native(
                    vocals_path, reference_words, whisper_model_size
                )
                native_errors_by_lang.setdefault(language, []).extend(native_errors)
                native_match_rates.append(match_rate)
                scored += 1
            finally:
                shutil.rmtree(vocals_path.parent, ignore_errors=True)

    print(f"Scored {scored} tracks, skipped {skipped_nd} ND-licensed tracks.\n")

    print("=== Aligned (wav2vec2 forced alignment against reference lyrics, English only) ===")
    for language, errors in sorted(aligned_errors_by_lang.items()):
        median, within = _summarize(errors)
        print(
            f"  {language}: n={len(errors)} words, median error={median:.1f}ms, "
            f"within {ONSET_TOLERANCE_MS}ms={within:.1f}%"
        )

    print(
        f"\n=== Whisper-native (whisper_model_size={whisper_model_size!r}, "
        "matched against reference via difflib) ==="
    )
    for language, errors in sorted(native_errors_by_lang.items()):
        median, within = _summarize(errors)
        print(
            f"  {language}: n={len(errors)} words, median error={median:.1f}ms, "
            f"within {ONSET_TOLERANCE_MS}ms={within:.1f}%"
        )
    if native_match_rates:
        print(f"  mean match rate across tracks: {statistics.mean(native_match_rates) * 100:.1f}%")


if __name__ == "__main__":
    import sys

    size = sys.argv[1] if len(sys.argv) > 1 else "base"
    main(whisper_model_size=size)
```

- [ ] **Step 4: Run it and record the real output**

Run, from `services/api`, with `pip install -e ".[dev,eval]"` completed:

```bash
python scripts/eval_alignment.py base
```

This downloads the JamendoLyrics Multi-Lang dataset (~79 tracks of audio) on first run and processes
every non-ND track through separation, transcription, and alignment — expect this to take a
substantial amount of wall-clock time on CPU (likely over an hour; there is no shortcut here that
doesn't compromise the measurement — see `docs/BENCHMARKS.md`'s M3 section for what happened when a
prior benchmark was measured under time pressure instead of run to completion). Let it finish. Copy
the exact printed output — do not paraphrase, round, or invent any number.

- [ ] **Step 5: Write the M4 section of `docs/BENCHMARKS.md`**

Read the existing file first (it has an M3 section already). Append a new `## M4: Alignment engine
(Whisper + wav2vec2)` section following the same style: what was measured, on what machine, against
what real command, with a table of the real per-language median error and % within 50ms for both the
"aligned" and "whisper_native" paths, and the real match-rate number. Include the same rights note
this plan's Global Constraints section states (ephemeral processing, ND tracks skipped, nothing but
these aggregate numbers persists). If any language produced zero scored words (e.g. an edge case in
the dataset), write `TODO: unmeasured` for that cell — never a plausible-looking number.

- [ ] **Step 6: Update `docs/STATUS.md`**

Following the exact pattern of the existing M1/M2/M3 entries in this file (read them first for
format/tone), add an M4a entry covering: what was built (the five tasks above), the real test count
and lint/type-check status, the two spec corrections made during this milestone (the M4/M4b split,
and the JamendoLyrics rights-claim correction — both worth recording precisely, since the second one
was a real mistake caught before it shipped, not a clean decision), the gpu_backend/ADR-0001 closure,
and what's deliberately still deferred (M4b's editor UI and re-alignment; commercially-licensed
multilingual forced alignment, a genuine open question; the RQ queue; M7's sandboxing).

- [ ] **Step 7: Commit**

```bash
git add services/api/pyproject.toml services/api/scripts/eval_alignment.py \
  docs/BENCHMARKS.md docs/STATUS.md
git commit -m "M4a: add real alignment accuracy eval against JamendoLyrics Multi-Lang"
```

---

## Self-Review Notes

**Spec coverage:** `gpu_backend` seam built and M3 retrofitted (Task 1) — covered, and the ADR-0001
deferral trigger from M3 is explicitly closed. `transcriptions` table + lyric-rights resolution
(Task 2) — covered, including the Lane B `covers_lyrics` distinction. `transcribe_audio`/`align_words`
with the verified (non-deprecated) `forced_align`/`merge_tokens` API, MIT-licensed
`WAV2VEC2_ASR_BASE_960H`, faster-whisper (Task 3) — covered; the chunking design was corrected during
spec self-review from segment-based to whole-clip-with-single-alignment specifically so the same
function serves the eval's reference-lyric alignment, and that correction is carried through
verbatim here. Rights gating (`status == "passed"` AND vocals stem exists, both before any model
load, both proven by non-invocation tests) (Task 4) — covered, following M3's exact proven pattern.
`lyrics_display_allowed` gating text without dropping timings (Task 4) — covered with a dedicated
test. Real eval against JamendoLyrics with the corrected licensing handling (ephemeral processing,
ND-filtering) (Task 5) — covered as a hard, tested constraint in the script itself, not left to
discretion.

**Placeholder scan:** No TBD/TODO in this plan's own instructions. `docs/BENCHMARKS.md`'s eventual
`TODO: unmeasured` cells (Task 5, Step 5) are the established, deliberate pattern from M2/M3, not a
plan gap.

**Type consistency:** `Word` (idx, text, start_ms, end_ms, confidence) is identical across
`transcribe_audio`'s output, `align_words`'s output, and the `words` JSONB shape written in Task 4 —
checked by hand across all three. `run_inference(fn, *, timeout_seconds)`'s signature (Task 1) matches
every call site in Tasks 1, 4, and 5 exactly, including the required (non-defaulted) `timeout_seconds`
keyword, which is what keeps Task 1's retrofit of `SEPARATION_TIMEOUT_SECONDS` a genuine no-behavior-
change refactor. `TranscriptionResult`'s `aligner` field values (`"wav2vec2"` / `"whisper_native"`)
match the `Transcription.aligner` column's documented values in Task 2 and the eval's own two
measurement labels in Task 5.
