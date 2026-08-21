# Benchmarks

Real, measured numbers only. `TODO: unmeasured` for anything not actually run — never a
plausible-looking placeholder (per `CLAUDE.md`).

## M3: Source separation (Demucs)

Measured on: 2026-08-21, on this dev machine (CPU only — torch 2.13.0+cpu, no GPU available),
via `services/api/scripts/benchmark_separation.py` against a synthetic 3-minute 440Hz tone (not
real music — no rights clearance needed to run or share this number).

| Model | Wall clock (3min input) | Realtime factor | Peak GPU memory |
|---|---|---|---|
| `htdemucs` | 256.6s | 0.70x | N/A (CPU only) |
| `htdemucs_ft` | 459.9s | 0.39x | N/A (CPU only) |

Quality comparison between `htdemucs` and `htdemucs_ft`: `TODO: unmeasured` — needs a real
listening test with real songs and human judgment, out of scope for M3 (see
`docs/superpowers/specs/2026-08-21-source-separation-design.md`).

Note: this is measured against the `local` CPU backend (this dev machine), not the eventual
Modal/RunPod production backend — per `docs/adr/0001-gpu-backend-abstraction.md`, production
cost/speed figures are `TODO: unmeasured` until that backend exists in M7.
