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
    expected_result = {
        "vocals": b"a",
        "drums": b"b",
        "bass": b"c",
        "other": b"d",
    }
    fake_fn = _FakeModalFunction(return_value=expected_result)
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    result = run_separate(b"audio-bytes", model_name="htdemucs", timeout_seconds=1800)

    assert result == expected_result
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
