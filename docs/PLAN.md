# Songbox — Plan

Karaoke generation platform: instrumental separation, word-level synced lyrics, pitch guide, synced
web player. Full spec context lives in the original build prompt (not checked in verbatim here — see
`docs/DECISIONS_LOG.md` for the environment-specific decisions made on top of it).

**Working agreement**, unchanged from the original brief:
1. Plan before code.
2. Test-first for the rights gate, the alignment engine, and the upload handler. UI and glue code are
   exempt.
3. ADRs for real forks, `docs/adr/NNNN-title.md` — Context / Decision / Consequences. Target: under 15.
4. Never fabricate a measurement — `TODO: unmeasured` until it's actually run.
5. Ask when ambiguity would cost more than two hours to unwind; otherwise pick the simpler option and
   log it in `docs/DECISIONS_LOG.md`.
6. Keep `docs/STATUS.md` current: done, in flight, blocked, next three actions.

## Architecture (as decided)

```
Next.js UI  →  FastAPI API (auth, rights gate, orchestration)
                    │  PASS only
             Object storage (private, per-tenant prefix, SSE)
                    │
             Worker pool (sandboxed, GPU where needed)
               1. probe + transcode      ffmpeg
               2. source separation      Demucs v4 (htdemucs_ft)
               3. transcription          Whisper large-v3 (on vocal stem)
               4. forced alignment       wav2vec2 → word timestamps
               5. pitch extraction       CREPE / torchcrepe
               6. structure analysis     beats, sections
               7. package                karaoke.json
                    │
             Player (Web Audio API, word highlight, pitch lane)
```

Stack: Next.js App Router + TypeScript (web) · Python 3.12 + FastAPI (API) · Python + PyTorch (workers)
· Postgres/Supabase with RLS · S3-compatible storage · Redis + RQ (not Celery — this workload doesn't
need Celery's surface area) · GPU workers behind a swappable backend interface (see ADR 0001). No
LangChain, no agent frameworks — direct library calls.

## Environment decisions made for this machine

- **Repo**: `Downloads\songbox`, fresh git repo (this one).
- **Local infra**: Docker Desktop on WSL2 — not yet installed, needed before M0's `docker compose up`
  works. See `docs/STATUS.md` for the install step.
- **GPU**: local NVIDIA GPU (CUDA 12.6 driver present) used for dev/test throughout M0–M6. Modal/RunPod
  wired up as the production backend in M7. See `docs/adr/0001-gpu-backend-abstraction.md`.
- **ffmpeg**: not yet installed on this machine — install before M2 (ingest hardening).

## Data flow clarification (not explicit in the original spec)

Presigned uploads go client → object storage directly, so raw audio never transits the Next.js tier.
The rights gate's Chromaprint fingerprint + ffprobe check therefore runs **server-side in the API
tier**, which fetches the just-uploaded object from private storage after the presigned upload
completes — not before, and not in the browser. This still satisfies "raw audio never transits the web
tier" (the web tier is Next.js, not the API), but the original spec's diagram doesn't spell out this
hop, so it's recorded here to avoid re-deriving it later.

## Milestones

Each ends with working, tested, committed code and an updated `STATUS.md`.

- **M0 — Skeleton (1 session).** Repo, Docker Compose (Postgres, Redis, MinIO), FastAPI health check,
  Next.js shell, CI with ruff + mypy strict + pytest.
- **M1 — Rights gate (2 sessions).** All three lanes, attestation records, Chromaprint fingerprinting,
  AcoustID lookup, hold-and-review flow.
  *Done when:* a known commercial recording uploaded under Lane A is held, and an original recording
  passes.
- **M2 — Hardened ingest (1 session).** Presigned upload, magic-byte validation, ffprobe gating,
  sandboxed transcode, all security-section limits enforced.
  *Done when:* a malformed-file test suite (truncated headers, wrong magic bytes,
  playlist-with-remote-URL, duration bomb) is fully rejected.
- **M3 — Separation (1 session).** Demucs on the local GPU backend, segmented, four stems stored,
  benchmarked (real numbers into `docs/BENCHMARKS.md`, not estimates).
- **M4 — Transcription + alignment (3 sessions, may run longer — see risk note below).** Whisper on the
  vocal stem, wav2vec2 forced alignment, word timings with confidence, the lyric correction editor,
  re-alignment on corrected text.
  *Done when:* measured word-onset error is within ±50ms median on a hand-labeled 10-track set built
  during this milestone.
- **M5 — Pitch + structure (1 session).** CREPE contour, beat grid, sections, `karaoke.json` v1 emitted
  and schema-validated.
- **M6 — Player (3+ sessions — see risk note).** Web Audio playback, word highlight, pitch lane, live
  mic pitch, transposition, stem mixer, calibration.
- **M7 — Harden and launch (2 sessions).** Retention purge, takedown endpoint, rate limits,
  observability, load test, **swap the GPU backend from local to Modal/RunPod and validate the
  no-egress sandbox for real** (this is the first point the sandbox claim is actually true, not just
  assumed).

**Out of scope for v0.1**: video export/rendering, mobile apps, social features, multiplayer,
scoring/leaderboards, a hosted song catalog, payments.

## Risk notes on the estimates above

- **M6** bundles two nontrivial R&D items into a 3-session estimate: SoundTouch/Rubber Band compiled to
  WASM for independent key/tempo control, and live pitch detection in an AudioWorklet that has to
  survive backing-track bleed into a phone mic. Expect this to run long; split it into sub-milestones
  once the WASM integration is attempted rather than letting it silently blow the estimate.
- **M7**'s backend swap (local → Modal/RunPod) is where the spec's "no network egress" sandbox
  guarantee is first actually tested. Local dev runs through M0–M6 do not validate that constraint —
  see `CLAUDE.md`.

## Open questions

Carried from the original brief, still genuinely open (answer with data, not opinion, when each
milestone reaches them):

1. Demucs quality/speed: is `htdemucs_ft` worth ~4× `htdemucs`'s inference time here? Needs a listening
   test, not a guess — do this in M3.
2. Non-English vocal alignment: what degrades, and what's the fallback? Needs at least one non-English
   track in the M4 eval set.
3. AcoustID false-negative rate on independent/unreleased music: is it acceptable given the gate exists
   to catch *commercial* leakage, not to be a complete catalog match? Measure during M1.
4. Live pitch detection under speaker/mic bleed: which algorithm survives a phone mic in a room with the
   backing track playing out loud? Open through M6.
5. Cost per track end-to-end, at what GPU instance size, and where that puts the price floor. Only
   answerable once M7's real cloud backend is wired up and benchmarked — `TODO: unmeasured` until then.

Resolved during this planning pass (see `docs/DECISIONS_LOG.md` for full reasoning):

6. Local infra without Docker installed → Docker Desktop on WSL2, install before M0.
7. Dev-time GPU strategy → local GPU through M0–M6, Modal/RunPod deferred to M7.
