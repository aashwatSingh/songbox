from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml

# Overridable via GPU_COSTS_PATH -- the positional parents[3] walk (services/api/app/job_cost.py
# -> app/ -> api/ -> services/ -> repo root) only holds up in a source checkout. It would break
# silently under e.g. a non-editable install where this file lands in site-packages/. A broken
# path now safely degrades to a null cost (see track_job_cost's exception handling below) rather
# than crashing, but it should still be overridable and documented rather than a bare positional
# walk.
_GPU_COSTS_PATH = Path(
    os.environ.get(
        "GPU_COSTS_PATH",
        str(Path(__file__).resolve().parents[3] / "config" / "gpu_costs.yaml"),
    )
)
_job_cost_logger = logging.getLogger("songbox.job_cost")


def estimate_cost_usd(
    duration_seconds: float, *, gpu_costs_path: Path = _GPU_COSTS_PATH
) -> float | None:
    """Looks up the most recent (as of today, never a future-dated entry) price-per-second entry
    in gpu_costs.yaml and multiplies by duration_seconds. Returns None if `providers` is empty or
    no entry's effective_date has arrived yet -- never a fabricated number.

    There is no per-request GPU-backend-selection concept anywhere in this codebase yet: every
    job runs through app.gpu_backend.run_inference(), which is ADR-0001's `local` backend,
    hardcoded (the `modal`/`runpod` implementation doesn't exist until M7c). `local` execution
    runs on the developer's own machine and has no real per-second billing to attach a number to,
    regardless of what this file contains -- so this function has no backend parameter and
    doesn't need one. It always returns the single most-recently-dated applicable entry, since
    this project only ever has one meaningfully "current" price at a time.
    """
    data = yaml.safe_load(gpu_costs_path.read_text()) or {}
    providers = data.get("providers") or []
    if not providers:
        return None

    today = date.today()
    applicable = [p for p in providers if date.fromisoformat(str(p["effective_date"])) <= today]
    if not applicable:
        return None

    latest = max(applicable, key=lambda p: date.fromisoformat(str(p["effective_date"])))
    return float(latest["price_per_second_usd"]) * duration_seconds


@contextmanager
def track_job_cost(track_id: object, job_type: str) -> Iterator[None]:
    """Times the wrapped block and logs one job-cost line on exit -- even if the block raised,
    since the duration up to a failure is still real and worth recording (retries/failures are
    exactly the kind of waste this exists to give visibility into). Never logs anything about the
    track's content -- only its opaque id, the job type, real measured duration, and an estimated
    cost that is `null` until real GPU pricing data exists. A pricing-lookup failure (a malformed
    or missing gpu_costs.yaml) degrades to a null cost plus a warning -- it must never turn a
    completed GPU job into a 500, or mask an HTTPException already propagating through this block.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        duration_seconds = time.monotonic() - start
        try:
            cost = estimate_cost_usd(duration_seconds)
        except Exception:
            _job_cost_logger.warning("gpu_cost_lookup_failed", exc_info=True)
            cost = None
        _job_cost_logger.info(
            "gpu_job",
            extra={
                "track_id": str(track_id),
                "job_type": job_type,
                "duration_seconds": round(duration_seconds, 3),
                "estimated_cost_usd": cost,
            },
        )
