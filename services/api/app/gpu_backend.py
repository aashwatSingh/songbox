from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.packaging import PackageResult
    from app.transcription import TranscriptionResult, Word

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
    audio_bytes: bytes,
    *,
    model_name: str,
    timeout_seconds: float,
    separate_audio_fn: Callable[..., dict[str, Path]] | None = None,
) -> dict[str, bytes]:
    """Runs Demucs source separation. Returns stem_type -> WAV bytes for all four stems.
    Dispatches to the `local` or `modal` backend based on the GPU_BACKEND environment variable
    (default "local").

    `separate_audio_fn` is an optional override for the underlying pipeline call, defaulting to
    the real `app.separation.separate_audio` when omitted -- every real caller (including the
    future Modal dispatch) omits it. It exists solely so `app/routes/tracks.py` can pass through
    its OWN module-level import of `separate_audio`: the route-level test suite
    (tests/test_tracks_separate.py) monkeypatches `app.routes.tracks.separate_audio` to prove a
    handler never reaches inference on a rejected request, and in one case (the 504-timeout test)
    to make a stand-in function actually run instead of the real one. A plain
    `from app.separation import separate_audio` done fresh inside this module on every call would
    never observe that patch -- it lives on a different module's namespace -- so tracks.py hands
    its own (possibly-patched) reference down explicitly instead.
    """
    if _active_backend() == "modal":
        return _run_separate_modal(audio_bytes, model_name=model_name)
    return _run_separate_local(
        audio_bytes,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        separate_audio_fn=separate_audio_fn,
    )


def _run_separate_local(
    audio_bytes: bytes,
    *,
    model_name: str,
    timeout_seconds: float,
    separate_audio_fn: Callable[..., dict[str, Path]] | None = None,
) -> dict[str, bytes]:
    if separate_audio_fn is not None:
        fn = separate_audio_fn
    else:
        from app.separation import separate_audio

        fn = separate_audio

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        stem_paths = _run_local(
            lambda: fn(Path(tmp.name), model_name=model_name),
            timeout_seconds=timeout_seconds,
        )
        try:
            return {stem_type: path.read_bytes() for stem_type, path in stem_paths.items()}
        finally:
            stem_dir = next(iter(stem_paths.values())).parent
            shutil.rmtree(stem_dir, ignore_errors=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _run_separate_modal(audio_bytes: bytes, *, model_name: str) -> dict[str, bytes]:
    import modal
    import modal.exception

    fn = modal.Function.from_name("songbox-gpu", "run_separate")
    try:
        return fn.remote(audio_bytes, model_name)  # type: ignore[no-any-return]
    except modal.exception.FunctionTimeoutError as exc:
        raise BackendTimeoutError(str(exc)) from exc


def run_transcribe(
    audio_bytes: bytes,
    *,
    model_size: str,
    timeout_seconds: float,
    initial_prompt: str | None = None,
    run_transcription_and_alignment_fn: Callable[..., TranscriptionResult] | None = None,
) -> TranscriptionResult:
    """See run_separate()'s docstring for why `run_transcription_and_alignment_fn` exists --
    same reasoning, this time for tests/test_tracks_transcribe.py's
    `app.routes.tracks.run_transcription_and_alignment` patches (one of which supplies a fake
    result outright, which only works if the patched callable is the one actually invoked).

    `initial_prompt` biases Whisper's decoding toward the track's own title/artist (see
    transcribe_audio's docstring for the measured win this produced on a real track) -- it never
    forces those words into the output, so an absent or wrong title/artist cannot corrupt an
    otherwise-correct transcript."""
    if _active_backend() == "modal":
        return _run_transcribe_modal(
            audio_bytes, model_size=model_size, initial_prompt=initial_prompt
        )

    if run_transcription_and_alignment_fn is not None:
        fn = run_transcription_and_alignment_fn
    else:
        from app.transcription import run_transcription_and_alignment

        fn = run_transcription_and_alignment

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        result: TranscriptionResult = _run_local(
            lambda: fn(Path(tmp.name), model_size=model_size, initial_prompt=initial_prompt),
            timeout_seconds=timeout_seconds,
        )
        return result
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _run_transcribe_modal(
    audio_bytes: bytes, *, model_size: str, initial_prompt: str | None = None
) -> TranscriptionResult:
    import modal
    import modal.exception

    fn = modal.Function.from_name("songbox-gpu", "run_transcribe")
    try:
        return fn.remote(audio_bytes, model_size, initial_prompt)  # type: ignore[no-any-return]
    except modal.exception.FunctionTimeoutError as exc:
        raise BackendTimeoutError(str(exc)) from exc


def run_realign(
    audio_bytes: bytes,
    *,
    text: str,
    timeout_seconds: float,
    align_words_fn: Callable[..., list[Word]] | None = None,
) -> list[Word]:
    """See run_separate()'s docstring for why `align_words_fn` exists -- same reasoning, this
    time for tests/test_tracks_realign.py's `app.routes.tracks.align_words` patches."""
    if _active_backend() == "modal":
        return _run_realign_modal(audio_bytes, text=text)

    if align_words_fn is not None:
        fn = align_words_fn
    else:
        from app.transcription import align_words

        fn = align_words

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return _run_local(
            lambda: fn(Path(tmp.name), text),
            timeout_seconds=timeout_seconds,
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _run_realign_modal(audio_bytes: bytes, *, text: str) -> list[Word]:
    import modal
    import modal.exception

    fn = modal.Function.from_name("songbox-gpu", "run_realign")
    try:
        return fn.remote(audio_bytes, text)  # type: ignore[no-any-return]
    except modal.exception.FunctionTimeoutError as exc:
        raise BackendTimeoutError(str(exc)) from exc


def run_package(
    vocals_bytes: bytes,
    drums_bytes: bytes,
    bass_bytes: bytes,
    other_bytes: bytes,
    *,
    pitch_model: str,
    timeout_seconds: float,
    build_package_fn: Callable[..., PackageResult] | None = None,
) -> PackageResult:
    """See run_separate()'s docstring for why `build_package_fn` exists -- same reasoning, this
    time for tests/test_tracks_package.py's `app.routes.tracks.build_package` patches."""
    if _active_backend() == "modal":
        return _run_package_modal(
            vocals_bytes, drums_bytes, bass_bytes, other_bytes, pitch_model=pitch_model
        )

    if build_package_fn is not None:
        fn = build_package_fn
    else:
        from app.packaging import build_package

        fn = build_package

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
            lambda: fn(
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


def _run_package_modal(
    vocals_bytes: bytes,
    drums_bytes: bytes,
    bass_bytes: bytes,
    other_bytes: bytes,
    *,
    pitch_model: str,
) -> PackageResult:
    """Unpacks the compact dict app.modal_app's run_package Function actually returns -- it does
    NOT return a PackageResult directly. Final whole-branch review measured a real PackageResult's
    pickled size at this project's own 12-minute MAX_DURATION_SECONDS cap: 2.67 MiB, over Modal's
    real 2 MiB inline-payload threshold (the exact failure mode run_separate was fixed for,
    documented in app/modal_app.py's run_separate comment -- see that comment for the full
    mechanism). run_package's Modal Function instead struct-packs the pitch contour as three
    parallel arrays (measured: 0.83 MiB at 12 minutes), which this function reassembles into a
    real PackageResult so every OTHER caller in this codebase keeps working with the same type
    build_package() has always returned. The reassembled TYPE is identical; the VALUES are not
    bit-for-bit -- hz and confidence cross the wire as float32, not build_package()'s native
    float64, so e.g. 0.9 comes back as 0.8999999761581421. Musically irrelevant, but real.
    """
    import math
    import struct

    import modal
    import modal.exception

    from app.packaging import PackageResult, PitchFrame

    fn = modal.Function.from_name("songbox-gpu", "run_package")
    try:
        raw = fn.remote(vocals_bytes, drums_bytes, bass_bytes, other_bytes, pitch_model)
    except modal.exception.FunctionTimeoutError as exc:
        raise BackendTimeoutError(str(exc)) from exc

    n = raw["pitch_frame_count"]
    unpacked = struct.unpack(f"<{n}I{n}f{n}f", raw["pitch_bytes"])
    time_ms_values = unpacked[:n]
    hz_values = unpacked[n : 2 * n]
    confidence_values = unpacked[2 * n : 3 * n]
    pitch = [
        PitchFrame(
            time_ms=t,
            hz=None if math.isnan(h) else h,
            confidence=c,
        )
        for t, h, c in zip(time_ms_values, hz_values, confidence_values, strict=True)
    ]
    return PackageResult(
        pitch_model=raw["pitch_model"],
        pitch=pitch,
        tempo_bpm=raw["tempo_bpm"],
        beats_ms=raw["beats_ms"],
        sections_ms=raw["sections_ms"],
    )
