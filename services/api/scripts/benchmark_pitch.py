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
