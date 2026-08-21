from __future__ import annotations

import shutil
import wave
from pathlib import Path

from app.separation import STEM_TYPES, separate_audio


def test_separate_audio_produces_all_four_stems_as_44100hz_stereo_wav(synthetic_wav: Path) -> None:
    stems = separate_audio(synthetic_wav)

    try:
        assert set(stems) == set(STEM_TYPES)
        for stem_type, stem_path in stems.items():
            assert stem_path.exists(), f"{stem_type} stem file was not written"
            with wave.open(str(stem_path), "rb") as wav_file:
                assert wav_file.getframerate() == 44100, f"{stem_type} stem is not 44.1kHz"
                assert wav_file.getnchannels() == 2, f"{stem_type} stem is not stereo"
    finally:
        # separate_audio() writes all stems into one shared temp dir (tempfile.mkdtemp) that
        # it does not clean up itself -- clean it up here so repeated test runs don't
        # accumulate orphaned temp directories.
        shutil.rmtree(next(iter(stems.values())).parent, ignore_errors=True)
