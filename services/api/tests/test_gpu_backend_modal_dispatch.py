from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("modal")  # optional dependency -- every test in this file mocks it

from app.gpu_backend import run_package, run_realign, run_separate, run_transcribe  # noqa: E402


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
    # Third positional arg is initial_prompt -- None here since the caller didn't supply one.
    assert fake_fn.calls == [(b"audio-bytes", "tiny", None)]


def test_run_transcribe_forwards_the_initial_prompt_to_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Biasing Whisper toward a track's own title/artist (see transcribe_audio's docstring for
    the measured win this produced) only works if the prompt actually reaches the remote call."""
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_fn = _FakeModalFunction(return_value=object())
    monkeypatch.setattr("modal.Function.from_name", MagicMock(return_value=fake_fn))

    run_transcribe(
        b"audio-bytes", model_size="tiny", timeout_seconds=1800, initial_prompt="Song, Artist"
    )

    assert fake_fn.calls == [(b"audio-bytes", "tiny", "Song, Artist")]


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
    """run_package's Modal Function does NOT return a PackageResult directly -- final whole-branch
    review found its pitch contour crosses Modal's real 2 MiB inline-payload threshold at this
    project's own 12-minute track cap, so app/modal_app.py's run_package struct-packs the pitch
    data into a compact dict instead (see its docstring), and gpu_backend.py's _run_package_modal
    unpacks it back into a real PackageResult. This test mocks the REAL wire shape -- a dict with
    struct-packed pitch_bytes -- not a bare object(), so it actually exercises the unpacking code,
    not just the dispatch call.
    """
    import struct

    from app.packaging import PackageResult

    monkeypatch.setenv("GPU_BACKEND", "modal")
    pitch_bytes = struct.pack("<2I2f2f", 0, 10, 220.5, float("nan"), 0.9, 0.0)
    fake_raw_result = {
        "pitch_model": "tiny",
        "pitch_bytes": pitch_bytes,
        "pitch_frame_count": 2,
        "tempo_bpm": 120.0,
        "beats_ms": [0, 500],
        "sections_ms": [0],
    }
    fake_fn = _FakeModalFunction(return_value=fake_raw_result)
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

    assert isinstance(result, PackageResult)
    assert result.pitch_model == "tiny"
    assert result.tempo_bpm == 120.0
    assert result.beats_ms == [0, 500]
    assert result.sections_ms == [0]
    assert len(result.pitch) == 2
    assert result.pitch[0].time_ms == 0
    assert result.pitch[0].hz == pytest.approx(220.5)
    assert result.pitch[0].confidence == pytest.approx(0.9)
    assert result.pitch[1].time_ms == 10
    assert result.pitch[1].hz is None  # NaN sentinel correctly converted back to None
    assert result.pitch[1].confidence == pytest.approx(0.0)
    from_name.assert_called_once_with("songbox-gpu", "run_package")
    assert fake_fn.calls == [(b"v", b"d", b"b", b"o", "tiny")]
