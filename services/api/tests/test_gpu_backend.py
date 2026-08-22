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
