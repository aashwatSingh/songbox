"""Measures real wall-clock separation time and GPU memory on this machine. Not a test --
run manually and paste its output into docs/BENCHMARKS.md. Uses a synthetic tone (not real
music) so it needs no rights clearance to run or share, per CLAUDE.md's rights-gate rules --
this only measures speed, not separation quality.
"""
from __future__ import annotations

import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import torch

from app.separation import separate_audio

BENCHMARK_DURATION_SECONDS = 180  # 3 minutes -- a realistic track length
BENCHMARK_RUNS = 3  # median of 3 runs -- a single one-shot run is too noisy under any system
# contention (background processes, thermal throttling, disk cache state) to trust as a
# reported number. See docs/BENCHMARKS.md's history: the first committed numbers here were a
# single run each, made while other test runs were happening concurrently on the same machine,
# and were off by 4.3x from a re-run in isolation.


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

        print(f"model={model_name}")
        elapsed_runs: list[float] = []
        for run_number in range(1, BENCHMARK_RUNS + 1):
            start = time.monotonic()
            stem_paths = separate_audio(tone_path, model_name=model_name)
            elapsed = time.monotonic() - start
            # separate_audio() writes its stems into a fresh tempfile.mkdtemp() directory every
            # call and never cleans it up itself (by design -- callers own the returned paths).
            # Clean it up immediately after each timed run so repeated benchmark runs don't
            # accumulate orphaned temp directories (each stem set is ~120 MiB).
            shutil.rmtree(next(iter(stem_paths.values())).parent, ignore_errors=True)

            elapsed_runs.append(elapsed)
            run_realtime_factor = BENCHMARK_DURATION_SECONDS / elapsed
            print(f"  run {run_number}/{BENCHMARK_RUNS}: wall clock {elapsed:.1f}s, "
                  f"realtime factor {run_realtime_factor:.2f}x")

        median_elapsed = statistics.median(elapsed_runs)
        median_realtime_factor = BENCHMARK_DURATION_SECONDS / median_elapsed
        print(f"  input duration: {BENCHMARK_DURATION_SECONDS}s")
        print(f"  median wall clock ({BENCHMARK_RUNS} runs): {median_elapsed:.1f}s")
        print(f"  median realtime factor: {median_realtime_factor:.2f}x")
        if torch.cuda.is_available():
            peak_mib = torch.cuda.max_memory_allocated() / (1024 * 1024)
            print(f"  peak GPU memory: {peak_mib:.0f} MiB")
            print(f"  device: {torch.cuda.get_device_name(0)}")
        else:
            print("  device: cpu (torch.cuda.is_available() was False)")


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "htdemucs"
    run_benchmark(model)
