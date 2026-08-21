"""Measures real wall-clock separation time and GPU memory on this machine. Not a test --
run manually and paste its output into docs/BENCHMARKS.md. Uses a synthetic tone (not real
music) so it needs no rights clearance to run or share, per CLAUDE.md's rights-gate rules --
this only measures speed, not separation quality.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import torch

from app.separation import separate_audio

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
            f"sine=frequency=440:duration={BENCHMARK_DURATION_SECONDS}",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def run_benchmark(model_name: str) -> None:
    with TemporaryDirectory() as tmp_dir:
        tone_path = Path(tmp_dir) / "benchmark_tone.wav"
        _make_benchmark_tone(tone_path)

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        start = time.monotonic()
        separate_audio(tone_path, model_name=model_name)
        elapsed = time.monotonic() - start

        realtime_factor = BENCHMARK_DURATION_SECONDS / elapsed
        print(f"model={model_name}")
        print(f"  input duration: {BENCHMARK_DURATION_SECONDS}s")
        print(f"  wall clock: {elapsed:.1f}s")
        print(f"  realtime factor: {realtime_factor:.2f}x")
        if torch.cuda.is_available():
            peak_mib = torch.cuda.max_memory_allocated() / (1024 * 1024)
            print(f"  peak GPU memory: {peak_mib:.0f} MiB")
            print(f"  device: {torch.cuda.get_device_name(0)}")
        else:
            print("  device: cpu (torch.cuda.is_available() was False)")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "htdemucs"
    run_benchmark(model)
