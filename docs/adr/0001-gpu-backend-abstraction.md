# 0001 — GPU backend abstraction: local dev, serverless prod

## Context

The worker pool's ML stages (Demucs, Whisper, wav2vec2 forced alignment, CREPE) need GPU compute. The
spec calls for serverless GPU (Modal or RunPod) in production so idle cost is zero, and for that
worker pool to run with no network egress except to object storage and the queue (spec §5) — a security
property that's meaningful in a shared cloud sandbox but doesn't map cleanly onto a personal dev
machine.

This machine has a local NVIDIA GPU (driver 560.94, CUDA 12.6) sitting idle. Requiring a Modal or RunPod
account, and paying per-second cloud GPU time, for every iteration of M0–M6 dev work would be slow and
costly for no accuracy benefit — the model outputs are the same either way.

## Decision

Define a small backend interface (`workers/gpu_backend.py` or equivalent) with two implementations:

- **`local`** — runs inference as a subprocess on this machine's CUDA GPU. Used as the default backend
  through M0–M6. Gets process-level resource limits (CPU/memory/wall-clock) but **not** the network-
  egress-denial sandbox the spec requires — there's no meaningful way to sandbox network access on a
  personal dev machine the way a cloud container platform can.
- **`modal`** / **`runpod`** — the real serverless backend, wired up in M7 and validated against the
  spec's full §5 requirements (no egress, seccomp, read-only root, dropped capabilities, hard job
  limits) at that point, not before.

Each pipeline stage calls the backend interface, not a specific provider's SDK directly, so swapping is
a config change, not a rewrite.

## Consequences

- Dev iteration through M0–M6 is fast and free, using hardware already available.
- The "no network egress" sandbox guarantee is **not proven** until M7. Any claim that the pipeline is
  hardened before that point is false — `CLAUDE.md` and `docs/PLAN.md` both call this out so it isn't
  forgotten or silently assumed.
- M7 gains a real task: swap the backend, then re-run the security-relevant parts of the malformed-file
  and abuse test suites against the actual cloud sandbox, not just against the local backend.
- Cost-per-track figures produced during M0–M6 (if any are measured against `local`) are **not**
  representative of production cost — production cost is `TODO: unmeasured` until M7's real backend is
  live and benchmarked.
