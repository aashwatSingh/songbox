# M3: Source separation — design

Status: approved. Date: 2026-08-21.

## Context

M1 (rights gate) and M2 (hardened ingest) are done and merged. A track that passes the rights gate
sits in MinIO in whatever format it was uploaded in, untouched since upload. M3 is the first "worker
pool" stage in the original architecture — the pipeline's actual media processing begins here. Per
`docs/PLAN.md`: "Demucs on the local GPU backend, segmented, four stems stored, benchmarked."

Two scope decisions were made with the user before designing the rest:

1. **Synchronous endpoint, not an RQ job queue, for M3.** The project's stack decision is Redis+RQ
   for async jobs, and M3 is nominally the first "worker pool" milestone — but M1/M2's gate and
   upload logic already run synchronously inline in the API, and standing up real job orchestration
   (`workers/`, currently empty) before a second pipeline stage (M4 transcription) exists to reveal
   what that orchestration actually needs to handle would be premature. M3 builds a synchronous
   `POST /tracks/{id}/separate` endpoint that blocks until Demucs finishes, same shape as M1/M2's
   endpoints. RQ wiring is deferred, not abandoned — it becomes real work once there's an actual
   multi-stage pipeline to orchestrate.
2. **`htdemucs` (base model), not `htdemucs_ft`, is the default — and this is a real, data-driven
   decision, not a shortcut.** The original spec explicitly flags "is `htdemucs_ft` worth ~4x the
   inference time?" as an open question needing a listening test, not an opinion. `htdemucs` is
   the starting default so M3's own dev loop stays fast on this machine's 8GB laptop GPU. The model
   name is a parameter, not hardcoded, so `htdemucs_ft` can be selected later once real quality
   data exists. `docs/BENCHMARKS.md` gets real measured speed numbers for both models — speed is
   objectively measurable now, from this machine. Quality stays `TODO: unmeasured`; a listening
   test needs real songs and human judgment, which is out of scope for this milestone.

## Environment this runs on

- GPU: NVIDIA GeForce RTX 4060 Laptop GPU, 8188 MiB VRAM, driver 560.94, CUDA 12.6 (confirmed via
  `nvidia-smi`).
- `torch`/`torchaudio` are not yet installed. `demucs` pulls both in as transitive dependencies, and
  the CUDA-enabled build must be installed explicitly (matching this machine's CUDA 12.6 driver) —
  the default PyPI wheel `pip install torch` resolves is CPU-only, which would make separation
  silently fall back to CPU inference (per the original spec, 10-20x realtime on CPU vs 1-2x
  realtime on GPU — a large, unannounced regression if it happened by accident). The implementation
  plan installs from PyTorch's CUDA 12.1 wheel index (`cu121` — the closest published wheel channel
  to this machine's 12.6 driver; CUDA is backward compatible within a major version series, so a
  12.1-built wheel runs fine against a 12.6 driver).
- Per ADR 0001 (`docs/adr/0001-gpu-backend-abstraction.md`), this is the "local" GPU backend used
  for dev through M0-M6. It has no network-egress-denial sandbox — that guarantee is only real once
  M7 swaps to the Modal/RunPod backend. Nothing in M3 changes that; M3 does not add any new
  sandboxing.

## What M3 actually builds

### 1. `stems` table (new Alembic migration)

Already named in the original spec's full data model (`docs/PLAN.md` §7), never built. New migration
`0004_add_stems_table.py`:

- `id` (UUID, PK), `tenant_id` (UUID, unconstrained like every other M1 table — see M1's design spec
  for why), `track_id` (FK → `tracks.id`), `stem_type` (`String(10)`: `"vocals"` / `"drums"` /
  `"bass"` / `"other"`), `storage_key` (Text — bare `{tenant_id}/{uuid4()}` per M2's storage-key
  convention, no filename component), `model_name` (`String(20)`: `"htdemucs"` or `"htdemucs_ft"` —
  which variant actually produced this stem, so both can coexist if ever needed, and so the
  eventual model-choice decision has real per-row provenance instead of an assumption).
- RLS: same pattern as every other M1/M2 table — `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL
  SECURITY`, a `tenant_isolation` policy, and `GRANT` to the `songbox_app` role (M1's migration
  `0002` already created that role; this migration only grants it access to the new table).
- Index on `tenant_id` and on `track_id` (matches M2's migration `0003` precedent of indexing every
  FK/tenant column a route will actually filter or join on).

### 2. `POST /tracks/{track_id}/separate`

New route, same file-organization pattern as `tracks.py`'s existing endpoints (a new function in
`services/api/app/routes/tracks.py`, not a new router file — this is one more lifecycle action on a
track, matching how `confirm-attestation` sits alongside `upload` rather than getting its own
module).

1. Look up the track; 404 if missing or wrong tenant (same pattern as every other track-scoped
   endpoint).
2. **`track.status != "passed"` → 409.** This is the actual enforcement of CLAUDE.md's "nothing
   reaches a GPU without a rights-gate PASS" — the first place in the codebase that check is real,
   not just documented intent. A `pending_review` or `rejected` track cannot trigger separation.
3. Fetch the original file from MinIO via `track.storage_key` (same client `get_minio_client()`
   already used for saving).
4. Run separation (new `services/api/app/separation.py` module — see below).
5. Store each of the four output stems in MinIO (`save_track_file`-style bare-UUID keys, reusing
   the existing bucket).
6. Write four `Stem` rows.
7. Return `{track_id, stems: [{stem_type, storage_key}, ...]}`.

### 3. `services/api/app/separation.py` — the actual Demucs wrapper

A pure function, testable independently of the HTTP layer (matching `fingerprint.py`'s shape from
M1/M2): `separate_audio(path: Path, model_name: str = "htdemucs") -> dict[str, Path]` — runs Demucs
on the input file, returns a dict of `{"vocals": path, "drums": path, "bass": path, "other": path}`
pointing at temp WAV files. Internally:

- Loads the input via Demucs' own audio-loading (which shells out to ffmpeg for format handling —
  consistent with the rest of the codebase never hand-rolling audio decode).
- Runs `demucs.separate` in **segmented mode with crossfade** (Demucs' own `--segment` /overlap
  parameters), not a single whole-track pass — this is what bounds memory by segment length rather
  than track length, per the original spec, and matters concretely on an 8GB card for a 12-minute
  track (M2's own duration cap).
- Every output stem is asserted 44.1kHz stereo WAV before the function returns — the first real
  instance of CLAUDE.md's "all internal audio is 44.1kHz stereo WAV, assert at every stage
  boundary" rule actually being enforced in code, not just documented. Demucs' `htdemucs` family is
  natively trained on 44.1kHz stereo, so this is confirming an existing property, not force-
  resampling — but the assertion is what turns "should be true" into "is checked."
- Raises a new `SeparationError` (mirroring `FingerprintError`'s shape) on any failure — model load
  failure, malformed input, out-of-memory, etc.

### 4. `docs/BENCHMARKS.md` (new)

Real numbers, measured on this machine, for both `htdemucs` and `htdemucs_ft`: wall-clock separation
time for at least one real-length (multi-minute) audio sample, GPU memory used, and the resulting
realtime-factor (track duration / processing time) — directly comparable to the original spec's
"~1-2x realtime on GPU" claim, confirming or correcting it with this hardware's actual number rather
than repeating the spec's figure as fact. `TODO: unmeasured` for anything not actually run (per
CLAUDE.md's measurement-discipline rule) — in particular, quality comparison between the two models
stays `TODO: unmeasured` here.

## Testing strategy (test-first, per the working agreement)

1. `stems` table + migration + RLS test — same pattern as M1's Task 3 (`Base.metadata`-derived table
   list, so this new table is automatically covered by the existing tenant_id/RLS invariant tests
   without needing a hardcoded update to them).
2. `separate_audio` unit-tested directly against a synthetic fixture (a short, real sine-wave WAV
   generated via ffmpeg at test time, same pattern as every prior milestone's fixtures — Demucs will
   produce near-silent/noise-like stems for a pure tone, which is fine: the test asserts the
   *pipeline* runs and produces four correctly-shaped 44.1kHz stereo WAV files, not that the
   separation is perceptually good, which needs real music this milestone doesn't need).
3. `POST /tracks/{id}/separate` wired end-to-end: upload a track through M1/M2's existing flow,
   confirm it (or use a Lane A track with no fingerprint match, which auto-passes), call separate,
   assert four `Stem` rows exist with the right `stem_type`s and real MinIO objects behind each
   `storage_key`. Also test the gate: attempt separation on a `pending_review` track, assert 409 and
   confirm Demucs was never invoked (mirroring M2's rejection-stage-specificity fix — a 409 alone
   doesn't prove the GPU was never touched, so this test needs to prove it, not just check the
   status code).

## Out of scope for M3

RQ/async job queue (deferred, see Context above — a real, acknowledged deviation from the
architecture's eventual shape, not an oversight). Quality comparison between `htdemucs` and
`htdemucs_ft` (needs a real listening test with real songs — `TODO: unmeasured`). Container-level
GPU worker sandboxing (M7's job, per ADR 0001). Any consumer of the stems this milestone produces —
M4 (transcription) is what actually uses the isolated vocal stem; M3 only produces and stores it.
