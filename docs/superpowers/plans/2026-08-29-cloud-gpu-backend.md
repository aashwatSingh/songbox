# M7c: Cloud GPU Backend Swap + Sandbox Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the GPU pipeline's execution layer from a bytes-blind, closure-based local-only
interface to a bytes-based, backend-swappable one (`local` | `modal`), then use it to deploy real
Modal Functions and prove the no-network-egress sandbox claim for real.

**Architecture:** `app/gpu_backend.py` grows four stage-specific public functions
(`run_separate`/`run_transcribe`/`run_realign`/`run_package`), each dispatching on a
`GPU_BACKEND` environment variable to either the existing local execution machinery (unchanged
behavior, just relocated) or a real Modal Function call. The four pipeline functions themselves
(`separate_audio`, `run_transcription_and_alignment`, `align_words`, `build_package`) never
change — only how their input arrives (bytes vs. a caller-managed local temp file) and how the
call is dispatched.

**Tech Stack:** `modal` Python SDK (new, optional dependency — only needed when
`GPU_BACKEND=modal`), the existing FastAPI/PyTorch/Demucs/faster-whisper/torchcrepe/librosa stack
(unchanged).

## Global Constraints

- **This milestone has a real, external, non-engineering dependency: Tasks 1–3 can be fully
  implemented and tested without live credentials; Task 4 (deploy + real sandbox validation + load
  test) cannot proceed until a real Modal account exists with an API token available.** Task 4's
  steps are complete and real, but execution stops at the point credentials are required — that is
  expected, not a plan failure.
- Provider: **Modal**, not RunPod (see the design spec's verified comparison). GPU type: `A10`
  (Modal's real name — not AWS's `A10G`), $0.000306/second per
  [modal.com/pricing](https://modal.com/pricing).
- `block_network=True` is a real, verified parameter directly on `@app.function(...)` (confirmed
  against the real installed `modal==1.5.5` package's `App.function` signature during planning —
  `block_network: bool = False`, default off, must be set explicitly).
- The API layer already fetches bytes from MinIO before any GPU call — the GPU call itself never
  needs network access to storage, so `block_network=True` (zero egress) is correct, not merely an
  approximation of "restricted egress" (see the design spec's Decision 2).
- `GPU_BACKEND` environment variable, `"local"` | `"modal"`, defaults to `"local"` if unset —
  `local` stays the default so routine dev work stays free and fast, per ADR-0001.
- Budget for Task 4's real testing: **under $10**, comfortably inside Modal's Starter-plan
  $30/month free credit — no real charge is expected.
- Load test scope: light — 3–5 real concurrent jobs, not a production-scale test (per the approved
  design spec scope).
- Never log raw audio, lyrics, or signed URLs anywhere in this milestone's new code (`CLAUDE.md`).
- No fabricated cost/measurement figures — `config/gpu_costs.yaml`'s new entry and
  `docs/BENCHMARKS.md`'s cost-per-track figure must both be real, measured numbers from Task 4,
  never estimated.

---

### Task 1: Restructure `gpu_backend.py` to stage-specific, bytes-based dispatch (local backend only)

**Files:**
- Modify: `services/api/app/gpu_backend.py`
- Modify: `services/api/app/routes/tracks.py`
- Modify: `services/api/tests/test_gpu_backend.py`
- Test: `services/api/tests/test_gpu_backend_dispatch.py` (new)

**Interfaces:**
- Consumes: `app.separation.separate_audio`, `app.transcription.run_transcription_and_alignment`,
  `app.transcription.align_words`, `app.packaging.build_package` (all existing, unchanged
  signatures).
- Produces (consumed by Task 3):
  - `run_separate(audio_bytes: bytes, *, model_name: str, timeout_seconds: float) -> dict[str, bytes]`
  - `run_transcribe(audio_bytes: bytes, *, model_size: str, timeout_seconds: float) -> TranscriptionResult`
  - `run_realign(audio_bytes: bytes, *, text: str, timeout_seconds: float) -> list[Word]`
  - `run_package(vocals_bytes: bytes, drums_bytes: bytes, bass_bytes: bytes, other_bytes: bytes, *, pitch_model: str, timeout_seconds: float) -> PackageResult`
  - All four raise `BackendBusyError`/`BackendTimeoutError` (existing exception types, unchanged)
    plus whatever exception the underlying pipeline function itself raises
    (`SeparationError`/`TranscriptionError`/`AlignmentError`/`AccompanimentError`/etc.).
  - All four dispatch on `GPU_BACKEND`; the `"modal"` branch raises
    `NotImplementedError("modal backend not yet implemented -- see M7c Task 3")` in this task —
    Task 3 replaces that with the real dispatch.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_gpu_backend_dispatch.py`:

```python
from __future__ import annotations

import pytest

from app.gpu_backend import run_package, run_realign, run_separate, run_transcribe
from app.transcription import Word


def test_run_separate_dispatches_locally_by_default(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav_bytes: bytes
) -> None:
    monkeypatch.delenv("GPU_BACKEND", raising=False)
    stems = run_separate(synthetic_wav_bytes, model_name="htdemucs", timeout_seconds=1800)
    assert set(stems) == {"vocals", "drums", "bass", "other"}
    for stem_bytes in stems.values():
        assert isinstance(stem_bytes, bytes)
        assert len(stem_bytes) > 0


def test_run_separate_raises_not_implemented_for_modal_backend(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav_bytes: bytes
) -> None:
    monkeypatch.setenv("GPU_BACKEND", "modal")
    with pytest.raises(NotImplementedError, match="modal backend not yet implemented"):
        run_separate(synthetic_wav_bytes, model_name="htdemucs", timeout_seconds=1800)


def test_run_transcribe_returns_a_real_result_for_synthetic_audio(
    synthetic_wav_bytes: bytes,
) -> None:
    result = run_transcribe(synthetic_wav_bytes, model_size="tiny", timeout_seconds=1800)
    assert result.language
    assert isinstance(result.words, list)


def test_run_realign_returns_word_timings(synthetic_wav_bytes: bytes) -> None:
    words = run_realign(synthetic_wav_bytes, text="la la la", timeout_seconds=1800)
    assert isinstance(words, list)
    assert all(isinstance(w, Word) for w in words)


def test_run_package_accepts_four_separate_stem_byte_strings(
    synthetic_wav_bytes: bytes,
) -> None:
    result = run_package(
        vocals_bytes=synthetic_wav_bytes,
        drums_bytes=synthetic_wav_bytes,
        bass_bytes=synthetic_wav_bytes,
        other_bytes=synthetic_wav_bytes,
        pitch_model="tiny",
        timeout_seconds=3600,
    )
    assert result.tempo_bpm > 0
```

Add a `synthetic_wav_bytes` fixture to `services/api/tests/conftest.py`, reusing the existing
`synthetic_wav` fixture (read the current file first — it already has `synthetic_wav(tmp_path) ->
Path`):

```python
@pytest.fixture
def synthetic_wav_bytes(synthetic_wav: Path) -> bytes:
    return synthetic_wav.read_bytes()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_gpu_backend_dispatch.py -v`
Expected: FAIL — `run_separate`/`run_transcribe`/`run_realign`/`run_package` don't exist yet
(`ImportError`).

- [ ] **Step 3: Rewrite `app/gpu_backend.py`**

Read the current file first (it has `BackendBusyError`, `BackendTimeoutError`, `_inference_lock`,
`run_inference`, `_run_with_timeout`, `_ThreadOutcome` — all of this stays, `run_inference` is
renamed to a private `_run_local`). Replace the file's contents with:

```python
from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

# ADR-0001's `local` backend: one process-wide inference job at a time, bounded by a wall-clock
# timeout. Only used by the "local" branch of each run_*() function below -- Modal's backend has
# no equivalent single-machine contention, since each call runs in its own isolated container.
_inference_lock = threading.Lock()


class BackendBusyError(Exception):
    """Raised when the local inference lock could not be acquired within the timeout -- another
    job is already running. Only ever raised by the `local` backend."""


class BackendTimeoutError(Exception):
    """Raised when a job did not complete within the timeout, on either backend. The `local`
    backend's underlying thread is left running to finish (or fail) on its own -- CPU-bound
    torch/ctranslate2 inference cannot be cancelled from Python once started; its eventual result
    is discarded. The `modal` backend maps Modal's own FunctionTimeoutError to this same type so
    callers never need backend-specific exception handling."""


@dataclass
class _ThreadOutcome[T]:
    value: T | None = None
    error: BaseException | None = None
    completed: bool = False


def _active_backend() -> str:
    """Reads GPU_BACKEND fresh on every call (never cached at import time), so tests can
    monkeypatch.setenv("GPU_BACKEND", ...) per-test without reloading this module."""
    return os.environ.get("GPU_BACKEND", "local")


def _run_local[T](fn: Callable[[], T], *, timeout_seconds: float) -> T:
    """Runs fn() on the `local` GPU backend, serialized against every other local inference call
    in this process via one process-wide lock. Raises BackendBusyError if the lock itself can't
    be acquired within timeout_seconds, BackendTimeoutError if fn() doesn't finish within
    timeout_seconds, or re-raises whatever fn() itself raised.

    FastAPI's sync routes already run in a threadpool, so blocking the calling thread here (both
    on lock acquisition and on Thread.join) is fine -- it never blocks the event loop.
    """
    if not _inference_lock.acquire(timeout=timeout_seconds):
        raise BackendBusyError("inference backend is busy, try again")
    try:
        return _run_with_timeout(fn, timeout_seconds)
    finally:
        _inference_lock.release()


def _run_with_timeout[T](fn: Callable[[], T], timeout_seconds: float) -> T:
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
    # can't infer that correlation from a plain bool flag.
    return outcome.value  # type: ignore[return-value]


def run_separate(
    audio_bytes: bytes, *, model_name: str, timeout_seconds: float
) -> dict[str, bytes]:
    """Runs Demucs source separation. Returns stem_type -> WAV bytes for all four stems.
    Dispatches to the `local` or `modal` backend based on the GPU_BACKEND environment variable
    (default "local")."""
    if _active_backend() == "modal":
        raise NotImplementedError("modal backend not yet implemented -- see M7c Task 3")
    return _run_separate_local(audio_bytes, model_name=model_name, timeout_seconds=timeout_seconds)


def _run_separate_local(
    audio_bytes: bytes, *, model_name: str, timeout_seconds: float
) -> dict[str, bytes]:
    from app.separation import separate_audio

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        stem_paths = _run_local(
            lambda: separate_audio(Path(tmp.name), model_name=model_name),
            timeout_seconds=timeout_seconds,
        )
        try:
            return {stem_type: path.read_bytes() for stem_type, path in stem_paths.items()}
        finally:
            stem_dir = next(iter(stem_paths.values())).parent
            shutil.rmtree(stem_dir, ignore_errors=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def run_transcribe(audio_bytes: bytes, *, model_size: str, timeout_seconds: float):  # noqa: ANN201
    from app.transcription import TranscriptionResult, run_transcription_and_alignment

    if _active_backend() == "modal":
        raise NotImplementedError("modal backend not yet implemented -- see M7c Task 3")

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        result: TranscriptionResult = _run_local(
            lambda: run_transcription_and_alignment(Path(tmp.name), model_size=model_size),
            timeout_seconds=timeout_seconds,
        )
        return result
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def run_realign(audio_bytes: bytes, *, text: str, timeout_seconds: float):  # noqa: ANN201
    from app.transcription import align_words

    if _active_backend() == "modal":
        raise NotImplementedError("modal backend not yet implemented -- see M7c Task 3")

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return _run_local(
            lambda: align_words(Path(tmp.name), text),
            timeout_seconds=timeout_seconds,
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def run_package(
    vocals_bytes: bytes,
    drums_bytes: bytes,
    bass_bytes: bytes,
    other_bytes: bytes,
    *,
    pitch_model: str,
    timeout_seconds: float,
):  # noqa: ANN201
    from app.packaging import build_package

    if _active_backend() == "modal":
        raise NotImplementedError("modal backend not yet implemented -- see M7c Task 3")

    stem_bytes_by_name = {
        "vocals": vocals_bytes,
        "drums": drums_bytes,
        "bass": bass_bytes,
        "other": other_bytes,
    }
    tmp_paths: dict[str, Path] = {}
    try:
        for stem_name, data in stem_bytes_by_name.items():
            tmp = NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            tmp_paths[stem_name] = Path(tmp.name)

        return _run_local(
            lambda: build_package(
                vocals_path=tmp_paths["vocals"],
                drums_path=tmp_paths["drums"],
                bass_path=tmp_paths["bass"],
                other_path=tmp_paths["other"],
                pitch_model=pitch_model,
            ),
            timeout_seconds=timeout_seconds,
        )
    finally:
        for path in tmp_paths.values():
            path.unlink(missing_ok=True)
```

(The `# noqa: ANN201`/bare-return-type-omission on `run_transcribe`/`run_realign`/`run_package` is
temporary scaffolding for this step only — Step 3 immediately below adds precise return-type
annotations once the imports are in scope at module level. See Step 3b.)

- [ ] **Step 3b: Add precise return-type annotations**

`mypy --strict` requires every function to have a return type. Since `TranscriptionResult`/`Word`/
`PackageResult` are needed as type annotations (not just inside function bodies), add these
imports under `TYPE_CHECKING` at the top of `gpu_backend.py` and use them in the three functions
above:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.packaging import PackageResult
    from app.transcription import TranscriptionResult, Word
```

Then change the three signatures to:

```python
def run_transcribe(
    audio_bytes: bytes, *, model_size: str, timeout_seconds: float
) -> TranscriptionResult:
```

```python
def run_realign(audio_bytes: bytes, *, text: str, timeout_seconds: float) -> list[Word]:
```

```python
def run_package(
    vocals_bytes: bytes,
    drums_bytes: bytes,
    bass_bytes: bytes,
    other_bytes: bytes,
    *,
    pitch_model: str,
    timeout_seconds: float,
) -> PackageResult:
```

Remove the `# noqa: ANN201` comments now that real return types are present.

- [ ] **Step 4: Update `services/api/tests/test_gpu_backend.py`**

The old tests exercised the generic, now-private `_run_local`/`_run_with_timeout` lock+timeout
machinery via the old public `run_inference` name. Update the import and calls to use the new
private name (this machinery's behavior is unchanged, only its name/visibility changed):

```python
from __future__ import annotations

import threading
import time

import pytest

from app.gpu_backend import BackendBusyError, BackendTimeoutError, _run_local


def test_run_local_returns_the_function_result() -> None:
    result = _run_local(lambda: 42, timeout_seconds=5)
    assert result == 42


def test_run_local_reraises_the_function_exception() -> None:
    def _boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _run_local(_boom, timeout_seconds=5)


def test_run_local_raises_backend_busy_error_when_lock_is_held() -> None:
    release_event = threading.Event()
    started_event = threading.Event()

    def _hold_lock() -> None:
        started_event.set()
        release_event.wait(timeout=5)

    holder = threading.Thread(target=lambda: _run_local(_hold_lock, timeout_seconds=5))
    holder.start()
    started_event.wait(timeout=5)
    try:
        with pytest.raises(BackendBusyError):
            _run_local(lambda: None, timeout_seconds=0.1)
    finally:
        release_event.set()
        holder.join(timeout=5)


def test_run_local_raises_backend_timeout_error_when_fn_runs_too_long() -> None:
    def _slow() -> None:
        time.sleep(0.5)

    with pytest.raises(BackendTimeoutError):
        _run_local(_slow, timeout_seconds=0.05)
```

- [ ] **Step 5: Rewrite the four route handlers in `app/routes/tracks.py`**

First, update the imports: remove `import shutil` (no longer used anywhere in this file once Step
5's changes land — `tempfile` stays, it's still used by `upload_track`), remove `separate_audio`
from the `app.separation` import (keep `SeparationError`), remove `align_words` and
`run_transcription_and_alignment` from the `app.transcription` import (keep
`DEFAULT_WHISPER_MODEL_SIZE`, `AlignmentError`, `TranscriptionError`), remove `build_package` from
the `app.packaging` import (keep `CREPE_HOP_MS`, `AccompanimentError`, `PitchExtractionError`,
`StructureExtractionError`). Change:

```python
from app.gpu_backend import BackendBusyError, BackendTimeoutError, run_inference
```

to:

```python
from app.gpu_backend import (
    BackendBusyError,
    BackendTimeoutError,
    run_package,
    run_realign,
    run_separate,
    run_transcribe,
)
```

In `separate_track`, replace the entire block from `tmp = tempfile.NamedTemporaryFile(...)`
through the end of the stem-cleanup `finally` (i.e. everything between fetching `original_bytes`
and building the `stems` list) with:

```python
    with track_job_cost(track.id, "separate"):
        try:
            stems_bytes = run_separate(
                original_bytes, model_name=model_name, timeout_seconds=SEPARATION_TIMEOUT_SECONDS
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc

    stems: list[StemInfo] = []
    for stem_type, stem_bytes in stems_bytes.items():
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

(`detect_audio_format`'s check right before this block stays untouched — it's still needed to
reject a stored file that no longer matches an accepted format, before anything downstream is
attempted.)

In `transcribe_track`, replace everything from `tmp = tempfile.NamedTemporaryFile(suffix=".wav",
...)` through its `finally: Path(tmp.name).unlink(...)` with:

```python
    with track_job_cost(track.id, "transcribe"):
        try:
            result = run_transcribe(
                vocal_bytes, model_size=model_size, timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS
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
            raise HTTPException(
                status_code=422, detail="could not align transcript to audio"
            ) from exc
```

In `realign_track`, replace everything from `tmp = tempfile.NamedTemporaryFile(suffix=".wav",
...)` through its `finally: Path(tmp.name).unlink(...)` with:

```python
    with track_job_cost(track.id, "realign"):
        try:
            words = run_realign(
                vocal_bytes, text=body.text, timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except AlignmentError as exc:
            raise HTTPException(
                status_code=422, detail="could not align transcript to audio"
            ) from exc
```

In `package_track`, replace the whole `try:`/`finally:` block that fetches each stem into a temp
file and calls `run_inference(lambda: build_package(...))` with:

```python
    minio_client = get_minio_client()
    stem_bytes_by_type = {
        stem_type: fetch_track_file(minio_client, stem.storage_key)
        for stem_type, stem in stems_by_type.items()
    }

    with track_job_cost(track.id, "package"):
        try:
            result = run_package(
                vocals_bytes=stem_bytes_by_type["vocals"],
                drums_bytes=stem_bytes_by_type["drums"],
                bass_bytes=stem_bytes_by_type["bass"],
                other_bytes=stem_bytes_by_type["other"],
                pitch_model=pitch_model,
                timeout_seconds=PACKAGE_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except (AccompanimentError, PitchExtractionError, StructureExtractionError) as exc:
            raise HTTPException(status_code=422, detail="could not package track") from exc
```

(This also removes the now-redundant separate `minio_client = get_minio_client()` line earlier in
`package_track` if one exists before this block — keep only one.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_gpu_backend.py tests/test_gpu_backend_dispatch.py -v`
Expected: PASS.

Run: `cd services/api && python -m pytest tests/test_tracks_separate.py tests/test_tracks_transcribe.py tests/test_tracks_realign.py tests/test_tracks_package.py tests/test_tracks_package_get.py -v`
Expected: PASS, unchanged behavior — these tests must not need any changes themselves, since HTTP
request/response shapes and error-status-code behavior are identical before and after this task.

- [ ] **Step 7: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 8: Commit**

```bash
git add services/api/app/gpu_backend.py services/api/app/routes/tracks.py \
    services/api/tests/test_gpu_backend.py services/api/tests/test_gpu_backend_dispatch.py \
    services/api/tests/conftest.py
git commit -m "M7c: restructure GPU backend to stage-specific, bytes-based dispatch"
```

---

### Task 2: Modal Function and Image definitions

**Files:**
- Create: `services/api/app/modal_app.py`
- Modify: `services/api/pyproject.toml`

**Interfaces:**
- Consumes: `app.separation.separate_audio`, `app.transcription.run_transcription_and_alignment`,
  `app.transcription.align_words`, `app.packaging.build_package` (same functions Task 1 uses for
  the local backend — no duplicated ML logic).
- Produces: a deployable Modal `App` named `"songbox-gpu"` with four Functions named
  `run_separate`, `run_transcribe`, `run_realign`, `run_package` — Task 3 looks these up by these
  exact `(app_name, function_name)` pairs via `modal.Function.from_name("songbox-gpu", "run_separate")` etc.

**This task's code is complete and can be verified for import/structural correctness locally, but
cannot be deployed or invoked without a real Modal account and API token — that happens in Task
4.**

- [ ] **Step 1: Add the `modal` optional dependency**

In `services/api/pyproject.toml`, add a new optional-dependency group (matching the existing
`eval = ["datasets>=2.14"]` pattern — `modal` is NOT a required base dependency, since only
`GPU_BACKEND=modal` usage and this file need it):

```toml
[project.optional-dependencies]
dev = [
    ...
]
eval = [
    "datasets>=2.14",
]
modal = [
    "modal>=1.5",
]
```

Run: `cd services/api && pip install -e ".[dev,modal]"` (installs it locally so this task's code
can be import-checked; CI's own install command stays `pip install -e ".[dev]"` unchanged, so
`modal` never becomes a hard CI dependency).

- [ ] **Step 2: Write `app/modal_app.py`**

Create `services/api/app/modal_app.py`:

```python
"""Modal Function definitions for the `modal` GPU backend (M7c). Deployed via:

    modal deploy services/api/app/modal_app.py

after `modal setup` (or setting MODAL_TOKEN_ID/MODAL_TOKEN_SECRET) has configured real
credentials -- this file cannot be deployed or tested without them. Every function below is
decorated with block_network=True: none of them need network access, since the caller (this
project's FastAPI backend) already fetches audio bytes from MinIO before calling any of these, and
each function returns its result directly through Modal's own call/response marshaling rather than
writing anywhere reachable over a network. This is a stronger guarantee than the original spec's
"no egress except object storage and the queue" wording assumed was necessary (see the M7c design
spec's Decision 2) -- zero egress, not restricted egress.

GPU: "A10" (Modal's real name -- not AWS's "A10G"), $0.000306/second per modal.com/pricing as of
this file's authoring. Sized for this pipeline's model sizes (Demucs, faster-whisper, wav2vec2,
torchcrepe) -- none of which need a top-tier H100/B200-class card.
"""

from __future__ import annotations

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch>=2.1,<3.0",
        "torchaudio>=2.1,<3.0",
        "demucs>=4.0",
        "numpy>=1.26",
        "faster-whisper>=1.0",
        "soundfile>=0.12",
        "torchcrepe>=0.0.23",
        "librosa>=0.10",
    )
    .add_local_python_source("app")
)

app = modal.App("songbox-gpu", image=image)

# Wall-clock timeouts mirror services/api/app/routes/tracks.py's SEPARATION_TIMEOUT_SECONDS /
# TRANSCRIPTION_TIMEOUT_SECONDS / PACKAGE_TIMEOUT_SECONDS -- Modal's own `timeout` kwarg is the
# real backstop when running on Modal (the `local` backend's _run_with_timeout thread-join timeout
# is a separate, local-only mechanism that doesn't apply here).
_SEPARATION_TIMEOUT_SECONDS = 1800
_TRANSCRIPTION_TIMEOUT_SECONDS = 1800
_PACKAGE_TIMEOUT_SECONDS = 3600


@app.function(gpu="A10", block_network=True, timeout=_SEPARATION_TIMEOUT_SECONDS)
def run_separate(audio_bytes: bytes, model_name: str) -> dict[str, bytes]:
    import shutil
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.separation import separate_audio

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        stem_paths = separate_audio(Path(tmp.name), model_name=model_name)
        try:
            return {stem_type: path.read_bytes() for stem_type, path in stem_paths.items()}
        finally:
            stem_dir = next(iter(stem_paths.values())).parent
            shutil.rmtree(stem_dir, ignore_errors=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(gpu="A10", block_network=True, timeout=_TRANSCRIPTION_TIMEOUT_SECONDS)
def run_transcribe(audio_bytes: bytes, model_size: str):  # noqa: ANN201
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.transcription import run_transcription_and_alignment

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return run_transcription_and_alignment(Path(tmp.name), model_size=model_size)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(gpu="A10", block_network=True, timeout=_TRANSCRIPTION_TIMEOUT_SECONDS)
def run_realign(audio_bytes: bytes, text: str):  # noqa: ANN201
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.transcription import align_words

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return align_words(Path(tmp.name), text)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(gpu="A10", block_network=True, timeout=_PACKAGE_TIMEOUT_SECONDS)
def run_package(
    vocals_bytes: bytes, drums_bytes: bytes, bass_bytes: bytes, other_bytes: bytes, pitch_model: str
):  # noqa: ANN201
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.packaging import build_package

    stem_bytes_by_name = {
        "vocals": vocals_bytes,
        "drums": drums_bytes,
        "bass": bass_bytes,
        "other": other_bytes,
    }
    tmp_paths: dict[str, Path] = {}
    try:
        for stem_name, data in stem_bytes_by_name.items():
            tmp = NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            tmp_paths[stem_name] = Path(tmp.name)

        return build_package(
            vocals_path=tmp_paths["vocals"],
            drums_path=tmp_paths["drums"],
            bass_path=tmp_paths["bass"],
            other_path=tmp_paths["other"],
            pitch_model=pitch_model,
        )
    finally:
        for path in tmp_paths.values():
            path.unlink(missing_ok=True)


@app.function(block_network=False, timeout=30)
def egress_probe() -> str:
    """M7c Task 4's deliberate sandbox-validation check -- NOT block_network=True, on purpose,
    since this function's entire job is proving the OTHER functions' block_network=True actually
    blocks traffic. If this function (with networking allowed) can reach a public endpoint but the
    four block_network=True functions above cannot, that's the real proof the sandbox is enforced,
    not merely unconfigured-and-accidentally-permissive. Never deployed with block_network=True --
    that would defeat its purpose.
    """
    import urllib.request

    with urllib.request.urlopen("https://example.com", timeout=10) as response:
        return f"reached example.com, status {response.status}"
```

Note: `.add_local_python_source("app")` makes this project's own `app` package (containing
`separation.py`, `transcription.py`, `packaging.py`) available inside the Modal container — verify
this is the correct current API for shipping local source code into a Modal Image against the real
installed `modal` package's `Image` class methods (`python -c "import modal; help(modal.Image)"`
or equivalent) before deploying in Task 4, since Modal's exact mechanism for this can change
between SDK versions faster than other parts of its API.

- [ ] **Step 3: Verify the file imports cleanly and passes static checks**

Run: `cd services/api && python -c "import app.modal_app"`
Expected: succeeds with no error (this only checks Python syntax/import structure — it does NOT
contact Modal's servers or require credentials, since `modal.App(...)`/`@app.function(...)` are
pure local object construction until `modal deploy`/`modal run` is actually invoked).

Run: `cd services/api && python -m ruff check app/modal_app.py && python -m mypy app/modal_app.py`
Expected: clean. (`mypy` may need `--ignore-missing-imports` for the `modal` package specifically
if it ships no stub markers — check and add a `[[tool.mypy.overrides]]` entry for `modal.*`,
matching this project's existing pattern for other third-party packages lacking type stubs, if
needed.)

- [ ] **Step 4: Commit**

```bash
git add services/api/app/modal_app.py services/api/pyproject.toml
git commit -m "M7c: add Modal Function and Image definitions (not yet deployed)"
```

---

### Task 3: Wire the `modal` backend dispatch into `gpu_backend.py`

**Files:**
- Modify: `services/api/app/gpu_backend.py`
- Test: `services/api/tests/test_gpu_backend_modal_dispatch.py` (new)

**Interfaces:**
- Consumes: `app.modal_app`'s deployed Function names (Task 2) — `modal.Function.from_name("songbox-gpu", "run_separate")` etc.
- Produces: the real `"modal"` branch of `run_separate`/`run_transcribe`/`run_realign`/
  `run_package`, replacing Task 1's `NotImplementedError` placeholder branches.

**This task's dispatch LOGIC is fully unit-testable without credentials (by monkeypatching
`modal.Function.from_name` itself, never making a real network call). Whether the real deployed
Functions actually behave correctly end-to-end can only be confirmed in Task 4, against real
credentials.**

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_gpu_backend_modal_dispatch.py`:

```python
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.gpu_backend import run_package, run_realign, run_separate, run_transcribe


class _FakeModalFunction:
    def __init__(self, return_value: Any = None, error: Exception | None = None) -> None:
        self._return_value = return_value
        self._error = error
        self.calls: list[tuple[Any, ...]] = []

    def remote(self, *args: Any) -> Any:
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return self._return_value


def test_run_separate_dispatches_to_the_named_modal_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_fn = _FakeModalFunction(return_value={"vocals": b"a", "drums": b"b", "bass": b"c", "other": b"d"})
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    result = run_separate(b"audio-bytes", model_name="htdemucs", timeout_seconds=1800)

    assert result == {"vocals": b"a", "drums": b"b", "bass": b"c", "other": b"d"}
    from_name.assert_called_once_with("songbox-gpu", "run_separate")
    assert fake_fn.calls == [(b"audio-bytes", "htdemucs")]


def test_run_separate_maps_modal_function_timeout_to_backend_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modal.exception

    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_fn = _FakeModalFunction(error=modal.exception.FunctionTimeoutError("timed out"))
    monkeypatch.setattr("modal.Function.from_name", MagicMock(return_value=fake_fn))

    from app.gpu_backend import BackendTimeoutError

    with pytest.raises(BackendTimeoutError):
        run_separate(b"audio-bytes", model_name="htdemucs", timeout_seconds=1800)


def test_run_transcribe_dispatches_to_the_named_modal_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_result = object()
    fake_fn = _FakeModalFunction(return_value=fake_result)
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    result = run_transcribe(b"audio-bytes", model_size="tiny", timeout_seconds=1800)

    assert result is fake_result
    from_name.assert_called_once_with("songbox-gpu", "run_transcribe")
    assert fake_fn.calls == [(b"audio-bytes", "tiny")]


def test_run_realign_dispatches_to_the_named_modal_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_fn = _FakeModalFunction(return_value=[])
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    run_realign(b"audio-bytes", text="la la", timeout_seconds=1800)

    from_name.assert_called_once_with("songbox-gpu", "run_realign")
    assert fake_fn.calls == [(b"audio-bytes", "la la")]


def test_run_package_dispatches_to_the_named_modal_function_with_four_byte_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_result = object()
    fake_fn = _FakeModalFunction(return_value=fake_result)
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    result = run_package(
        vocals_bytes=b"v",
        drums_bytes=b"d",
        bass_bytes=b"b",
        other_bytes=b"o",
        pitch_model="tiny",
        timeout_seconds=3600,
    )

    assert result is fake_result
    from_name.assert_called_once_with("songbox-gpu", "run_package")
    assert fake_fn.calls == [(b"v", b"d", b"b", b"o", "tiny")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_gpu_backend_modal_dispatch.py -v`
Expected: FAIL — the `"modal"` branches still raise `NotImplementedError`.

- [ ] **Step 3: Replace the `NotImplementedError` branches in `app/gpu_backend.py`**

Add near the top of the file (inside a `TYPE_CHECKING` block is NOT sufficient here since `modal`
is used at runtime, not just for typing — but keep the actual `import modal` LAZY, inside each
function that needs it, so `local`-only usage never requires the `modal` package to be installed):

In `run_separate`, replace `raise NotImplementedError(...)` with:

```python
    if _active_backend() == "modal":
        return _run_separate_modal(audio_bytes, model_name=model_name)
```

and add:

```python
def _run_separate_modal(audio_bytes: bytes, *, model_name: str) -> dict[str, bytes]:
    import modal
    import modal.exception

    fn = modal.Function.from_name("songbox-gpu", "run_separate")
    try:
        return fn.remote(audio_bytes, model_name)  # type: ignore[no-any-return]
    except modal.exception.FunctionTimeoutError as exc:
        raise BackendTimeoutError(str(exc)) from exc
```

Apply the equivalent pattern to `run_transcribe`, `run_realign`, and `run_package` — each gets a
`_run_<stage>_modal(...)` helper that looks up `modal.Function.from_name("songbox-gpu",
"run_<stage>")`, calls `.remote(...)` with the same positional arguments Task 2's Modal Functions
expect (matching each Function's parameter order exactly — verify against `modal_app.py`), and
maps `modal.exception.FunctionTimeoutError` to `BackendTimeoutError`. Any other exception from
`.remote(...)` (a real Modal-side bug, an auth failure, the deployment not existing) is
deliberately NOT caught here — it should surface as a genuine unhandled 500, not be disguised as
`BackendBusyError`/`BackendTimeoutError`, since those specific types signal conditions a client
should retry on, and a misconfigured deployment is not one of those.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_gpu_backend_modal_dispatch.py -v`
Expected: PASS (5/5) — note these tests monkeypatch `modal.Function.from_name` and never make a
real network call, so they pass without any live Modal credentials.

- [ ] **Step 5: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions. (The full suite still runs entirely against `GPU_BACKEND=local`
by default — nothing in the default test run requires Modal credentials.)

- [ ] **Step 6: Commit**

```bash
git add services/api/app/gpu_backend.py services/api/tests/test_gpu_backend_modal_dispatch.py
git commit -m "M7c: wire the modal backend dispatch (unit-tested via mocked Function.from_name)"
```

---

### Task 4: Deploy, validate the sandbox for real, run the light load test

**This task requires real credentials that do not exist yet.** Before any step below can run, you
(the project owner) need to:

1. Create a Modal account at [modal.com](https://modal.com) (the Starter plan's $30/month free
   credit comfortably covers this task's ~$10 budget — no payment should be charged during this
   milestone's validation).
2. Run `modal setup` locally (or generate a token pair from the Modal dashboard's Settings ->
   API Tokens page) to get real `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` values.
3. Make those available to whatever environment runs the steps below — either via `modal setup`'s
   own local config (`~/.modal.toml`), or as `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` environment
   variables.

**Files:**
- Test: `services/api/tests/test_modal_sandbox_validation.py` (new, real-Modal-only — see its own
  skip condition below)
- Modify: `config/gpu_costs.yaml`
- Modify: `docs/BENCHMARKS.md`
- Modify: `docs/STATUS.md`
- Modify: `docs/PLAN.md` (open question 4)

**Interfaces:**
- Consumes: Task 2's deployed `songbox-gpu` Modal app, Task 3's `modal` backend dispatch.
- Produces: nothing consumed by a later task — this is the last task in this milestone.

- [ ] **Step 1: Deploy the Modal app**

Run: `cd services/api && modal deploy app/modal_app.py`
Expected: Modal's CLI reports a successful deployment, printing the deployed app's dashboard URL
and each Function's name. If this fails, the error will name what's missing (credentials, a
dependency that failed to install into the Image, etc.) — do not proceed past a failed deploy.

- [ ] **Step 2: Write and run the deliberate egress-probe validation (the one test that must run against the real sandbox)**

Create `services/api/tests/test_modal_sandbox_validation.py`:

```python
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MODAL_TOKEN_ID"),
    reason="requires real Modal credentials -- see M7c Task 4",
)


def test_block_network_true_actually_blocks_a_real_outbound_call() -> None:
    """The one test in this milestone that can only be run against the real deployed sandbox --
    proves block_network=True genuinely blocks traffic, not merely that it was left unconfigured
    (which would look identical from the outside if egress happened to succeed by accident)."""
    import modal

    blocked_fn = modal.Function.from_name("songbox-gpu", "run_separate")
    # A deliberately tiny, invalid input -- this call is expected to fail inside the container
    # (garbage bytes aren't a real WAV file), but the failure must come from separate_audio()
    # itself, never from a successful network call slipping through block_network=True. If this
    # somehow reaches example.com internally, that's a sandbox-configuration bug, not a WAV-parsing
    # bug -- inspect the real exception message/type to tell the two apart.
    with pytest.raises(Exception):  # noqa: B017 -- exact exception type depends on Demucs internals
        blocked_fn.remote(b"not a real wav file", "htdemucs")


def test_egress_probe_confirms_networking_works_when_not_blocked() -> None:
    """Confirms the OTHER test's negative result means something: this sibling function has
    block_network=False and must successfully reach a real public endpoint. If this one also
    failed, that would mean Modal itself has no outbound networking available at all (a Modal
    platform issue, not evidence block_network=True is doing anything), making the blocked test's
    result meaningless."""
    import modal

    probe_fn = modal.Function.from_name("songbox-gpu", "egress_probe")
    result = probe_fn.remote()
    assert "reached example.com" in result
```

Run: `cd services/api && MODAL_TOKEN_ID=<your-real-token-id> MODAL_TOKEN_SECRET=<your-real-token-secret> python -m pytest tests/test_modal_sandbox_validation.py -v`
Expected: 2/2 PASS. If `test_egress_probe_confirms_networking_works_when_not_blocked` fails,
something is wrong with Modal connectivity generally, not with `block_network` — fix that first.
If `test_block_network_true_actually_blocks_a_real_outbound_call` fails in a way that suggests the
call actually reached the network (inspect the real error), STOP — this means the sandbox claim is
false, and this is exactly the kind of finding this milestone exists to catch, not paper over.

- [ ] **Step 3: Run a real end-to-end track through the `modal` backend once**

Manually (or via a small one-off script) upload a real synthetic test track with `GPU_BACKEND=modal`
set, and run it through `/separate` -> `/transcribe` -> `/package`, confirming each stage produces
a correct, real result via the deployed Modal Functions — not a local approximation. Record the
real wall-clock duration for each stage.

- [ ] **Step 4: Run the light load test — 3-5 concurrent real jobs**

Write and run a small script (or a manually-invoked test) that fires 3-5 `/separate` requests
concurrently (e.g. via a `ThreadPoolExecutor` or several terminal tabs) against real, distinct
synthetic tracks with `GPU_BACKEND=modal`, confirming all complete successfully and independently
(no cross-job interference — unlike the `local` backend's single process-wide lock, Modal's
containers are genuinely isolated, so this should show real parallelism). Record the real,
measured cost for this batch from Modal's own dashboard/billing page.

- [ ] **Step 5: Record the real GPU pricing entry**

Modify `config/gpu_costs.yaml`, replacing `providers: []` with a real dated entry (fill in
`effective_date` with the actual date this deployment happened, not a placeholder):

```yaml
providers:
  - name: modal
    gpu_type: A10
    price_per_second_usd: 0.000306
    effective_date: "YYYY-MM-DD"
```

This is the only change needed for M7b's `job_cost.py` to start emitting real `estimated_cost_usd`
values instead of `null` — no code change required there.

- [ ] **Step 6: Record the real measured cost-per-track figure**

Read `docs/BENCHMARKS.md`'s existing format (it already has entries from M3/M4a's real
measurements — match that style). Add a new section reporting the real, measured per-track cost
from Step 3/4 (never an estimate) — e.g. "N tracks processed via the `modal` backend, total
measured cost $X.XX per Modal's billing dashboard, mean $Y.YY/track" — closing `docs/PLAN.md` open
question 4 with real data for the first time.

- [ ] **Step 7: Update `docs/PLAN.md`'s open question 4 and `docs/STATUS.md`**

In `docs/PLAN.md`, find open question 4 ("Cost per track end-to-end...") and replace its
`TODO: unmeasured until then` with the real figure from Step 6, referencing
`docs/BENCHMARKS.md`'s new section.

In `docs/STATUS.md`, add a "Done — M7c complete" entry (matching the format of the M7a/M7b entries
above it) covering: the interface restructuring (Task 1), the Modal deployment (Tasks 2-3), the
real sandbox validation result (Step 2 — state explicitly that `block_network=True` was proven to
actually block traffic, with the probe test as evidence), the real measured cost-per-track figure
(Step 6), and that `local` remains the default `GPU_BACKEND` for routine dev work.

- [ ] **Step 8: Commit**

```bash
git add services/api/tests/test_modal_sandbox_validation.py config/gpu_costs.yaml \
    docs/BENCHMARKS.md docs/STATUS.md docs/PLAN.md
git commit -m "M7c: deploy to Modal, validate the no-egress sandbox for real, record real cost data"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (bytes-based dispatch, no change to the four ML functions
themselves) — Task 1. Decision 2 (zero egress, not restricted egress) — reflected in
`modal_app.py`'s `block_network=True` on every real pipeline Function, explained in that file's own
docstring (Task 2). Decision 3 (`GPU_BACKEND` env var, `local` default) — Task 1's
`_active_backend()`. Decision 4 (real Modal A10 pricing closing M7b's cost gap) — Task 4 Step 5,
verified against real current `modal.com/pricing` data during planning, not fabricated. Decision 5
(sandbox validation is the actual point) — Task 4 Step 2's egress-probe test, the one test in this
whole plan that cannot be satisfied any other way than running against the real deployed sandbox.
Decision 6 (light load test, 3-5 concurrent) — Task 4 Step 4, matching the approved scope exactly
(not the heavier alternative). The design spec's "What M7c builds" list's 7 items map onto this
plan's 4 tasks: items 1-3 (interface, Modal definitions, route updates) are Tasks 1-3; items 4-7
(cost entry, validation tests, load test + BENCHMARKS.md, STATUS.md/PLAN.md updates) are Task 4.

**Placeholder scan:** No TBD/TODO in this plan's own instructions. Task 1's `NotImplementedError`
branches are real, correct, intentional runtime behavior for that point in the plan's sequence
(explicitly replaced by Task 3), not a plan-writing placeholder. Task 4's `<your-real-token-id>`
angle-bracket text is a literal shell-command placeholder for a secret the user must supply — this
is standard practice for documenting a command that needs a real credential, not a plan-content gap
(the surrounding instructions are fully concrete).

**Type consistency:** `run_separate(audio_bytes: bytes, *, model_name: str, timeout_seconds:
float) -> dict[str, bytes]` (Task 1) is called identically by Task 3's modal-dispatch tests and by
Task 1's own local-dispatch tests — same parameter names, same order. The four `_run_<stage>_modal`
helpers (Task 3) call `modal.Function.from_name("songbox-gpu", "run_<stage>")` — matching Task 2's
exact Function names in `modal_app.py` one-to-one (`run_separate`, `run_transcribe`, `run_realign`,
`run_package`), and the positional argument order each `.remote(...)` call passes matches each
Modal Function's real parameter order defined in Task 2 (verified by cross-reading both tasks'
code side by side while writing this plan).
