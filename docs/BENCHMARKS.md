# Benchmarks

Real, measured numbers only. `TODO: unmeasured` for anything not actually run — never a
plausible-looking placeholder (per `CLAUDE.md`).

## M3: Source separation (Demucs)

Measured on: 2026-08-21, on this dev machine, via `services/api/scripts/benchmark_separation.py`
against a synthetic 3-minute 440Hz tone (not real music — no rights clearance needed to run or
share this number). Each model's numbers below are the **median of 3 runs**, run in isolation
(nothing else running concurrently on the machine) to avoid the noise a single one-shot run under
system contention produces — see the git history of this file for what that noise looked like.

This machine DOES have a GPU (NVIDIA GeForce RTX 4060 Laptop, confirmed via `nvidia-smi`). What's
actually missing is a CUDA-enabled torch build — only the CPU-only torch wheel (`torch
2.13.0+cpu`) is installed. `docs/PLAN.md`'s Task 2 Step 2 documents installing the CUDA build via
`--index-url https://download.pytorch.org/whl/cu121`; nobody has run that on this branch yet, so
these numbers are CPU-only, not "no GPU available."

| Model | Wall clock (3min input, median of 3) | Realtime factor | Peak GPU memory |
|---|---|---|---|
| `htdemucs` | 60.6s | 2.97x | `TODO: unmeasured` (CUDA torch build not installed) |
| `htdemucs_ft` | 252.9s | 0.71x | `TODO: unmeasured` (CUDA torch build not installed) |

Individual runs behind the medians above (`python scripts/benchmark_separation.py <model>`,
run in isolation — nothing else on the machine at the same time):

- `htdemucs`: 56.8s (3.17x), 60.6s (2.97x), 78.4s (2.29x)
- `htdemucs_ft`: 252.9s (0.71x), 267.8s (0.67x), 239.0s (0.75x)

GPU numbers: `TODO: unmeasured`. The design spec's point for this table was comparing against the
original "~1-2x realtime on GPU" claim; a CPU-only run cannot make that comparison, so this is
called out explicitly rather than silently omitted.

Quality comparison between `htdemucs` and `htdemucs_ft`: `TODO: unmeasured` — needs a real
listening test with real songs and human judgment, out of scope for M3 (see
`docs/superpowers/specs/2026-08-21-source-separation-design.md`).

Note: this is measured against the `local` CPU backend (this dev machine), not the eventual
Modal/RunPod production backend — per `docs/adr/0001-gpu-backend-abstraction.md`, production
cost/speed figures are `TODO: unmeasured` until that backend exists in M7.
