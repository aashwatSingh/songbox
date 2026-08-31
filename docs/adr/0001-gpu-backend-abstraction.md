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

### M3 update

M3 (source separation) is the first pipeline stage that actually makes a GPU call, and it does so by
calling `demucs.api` directly from `services/api/app/routes/tracks.py` (`separate_audio()` in
`services/api/app/separation.py`) rather than through the `workers/gpu_backend.py` interface this ADR
describes. This is a deliberate, acknowledged deferral, not an oversight: with only one call site, the
interface's real shape (what it needs to abstract over `local` vs. `modal`/`runpod`, what belongs in
the interface vs. in each stage) is a guess. M4 (transcription — Whisper) will add a second GPU-calling
stage; once two real call sites exist, the actual common shape will be clear enough to extract the
interface without guessing wrong. Revisit this deferral when M4 lands. Until then, `separate_audio()`
selects `cuda` vs. `cpu` directly via `torch.cuda.is_available()`, matching the `local` backend's
behavior described above but without going through a swappable interface.

### M4a update

The deferral above ended here, as planned. `services/api/app/gpu_backend.py` now provides the
`local` backend's `run_inference()` interface this ADR describes: one process-wide inference job
at a time, bounded by a caller-supplied wall-clock timeout. M3's `separate_audio()` call in
`services/api/app/routes/tracks.py` was retrofitted to go through it (Task 1 of
`docs/superpowers/plans/2026-08-21-alignment-engine.md`), and M4a's transcription/alignment calls
(Task 4 of the same plan) use it from the start. The `modal`/`runpod` implementations remain M7's
work.

### M7c update — superseded in part, real data replaces the untested §5 checklist

The `modal` implementation is real and deployed (`services/api/app/modal_app.py`, app
`songbox-gpu`). This section states what real validation actually found, replacing this ADR's
original §5 checklist ("no egress, seccomp, read-only root, dropped capabilities, hard job
limits") with what's actually true today, item by item:

- **No network egress:** true for three of the four real pipeline functions
  (`run_transcribe`/`run_realign`/`run_package`), each verified via a real deliberate probe (a
  sibling function running identical logic with `block_network` flipped genuinely failed with a
  real DNS resolution error; the unblocked sibling genuinely succeeded — proof, not assumption).
  **Not true for `run_separate`** — a real, measured Modal platform constraint (its return value
  routinely exceeds Modal's real 2 MiB inline-payload limit, and Modal's own blob-storage
  transport for payloads above that limit needs network access from inside the container). See
  `modal_app.py`'s comment on `run_separate` for the full account, and `docs/BENCHMARKS.md`'s M7c
  section for the measured numbers. This ADR's original wording assumed all four functions could
  get the same guarantee uniformly — real infrastructure proved that assumption wrong for one of
  them.
- **Seccomp (corrected after confirmation review — the original wording overstated what gVisor
  itself guarantees):** Modal's real, documented runtime is gVisor-based (Google's
  application-kernel container isolation, also used by Cloud Run). gVisor's own mechanism *is*
  syscall interception, so seccomp-style filtering is a direct property of running on it, given
  platform-wide for every Modal Function without a per-function toggle.
- **Dropped capabilities, non-root execution, read-only root filesystem:** these are ordinary
  container-runtime configuration, a *different* axis from gVisor's syscall-interception guarantee
  — gVisor being the sandbox doesn't by itself imply the container inside it drops capabilities or
  mounts its root read-only. Modal may configure these platform-wide, but this project has not
  verified that and there is no per-function knob to check it against. Correcting this ADR's
  earlier text, which credited gVisor with all five properties as one bundle — that conflated two
  independent claims and overstated the one that wasn't actually gVisor's to make.
- **Hard job limits:** real, and cost-motivated, not just a security checkbox —
  `_MAX_CONTAINERS = 5` caps concurrent instances per pipeline function (`modal_app.py`), and
  `timeout=` mirrors the same wall-clock caps `app/routes/tracks.py` already enforced for the
  `local` backend.
- **Malformed-file/abuse re-run, this ADR's own explicit M7 commitment (above):** partially done,
  not the full re-run originally promised. One domain exception class (`SeparationError`) was
  verified for real to survive Modal's remote-call exception marshaling as the same Python
  exception type (not a generic wrapper), meaning `app/routes/tracks.py`'s existing
  `except SeparationError` still maps it to a 422 under `GPU_BACKEND=modal`, exactly as it does
  under `local` — confirmed by a real call against the deployed function with deliberately invalid
  audio bytes. The sibling domain exceptions (`TranscriptionError`, `AlignmentError`,
  `AccompanimentError`, `PitchExtractionError`, `StructureExtractionError`) were not each
  individually verified the same way — they're structurally identical plain `Exception` subclasses
  imported identically on both sides via `.add_local_python_source("app")`, so the same
  class-identity-preserving mechanism applies to them too by the same reasoning, but that's
  inference from one verified case, not five separate confirmations. A genuinely malformed (not
  garbage, but format-valid-yet-broken) test file was not constructed and run through the full
  route layer end to end — that remains open, real follow-up work, not silently dropped.
