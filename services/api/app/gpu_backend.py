from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

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
    cancelled from Python once started. Its eventual result is discarded. run_inference()'s
    `finally` releases the inference lock before that abandoned thread actually finishes, so a
    new job can start running concurrently with it -- inherited unchanged from M3's original
    design, not a new bug, just documented here now that the lock lives in this module."""


@dataclass
class _ThreadOutcome[T]:
    value: T | None = None
    error: BaseException | None = None
    completed: bool = False


def run_inference[T](fn: Callable[[], T], *, timeout_seconds: float) -> T:
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
    # can't infer that correlation from a plain bool flag (and a None check isn't a valid
    # narrowing here either, since T could legitimately be a type that allows None).
    return outcome.value  # type: ignore[return-value]
