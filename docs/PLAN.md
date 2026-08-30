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
- **M4 — Transcription + alignment (3 sessions, may run longer — see risk note below).** Split into two
  sub-milestones during brainstorming, since the original scope bundled backend ML, an accuracy-
  measurement deliverable, and this repo's first real frontend surface into one unit:
  - **M4a — Alignment engine (done, see `docs/STATUS.md`).** Whisper on the vocal stem, wav2vec2
    forced alignment, word timings with confidence, and the eval harness that measures accuracy
    against a real public benchmark (JamendoLyrics Multi-Lang) instead of a hand-labeled set — a
    deliberate, documented deviation from the original "hand-labeled 10-track set" plan, made for
    independence/comparability, not convenience (see the design spec's licensing correction).
    *Done when:* measured word-onset error is within ±50ms median. **Measured, not met**: the real
    result is 68.2ms median / 37.2% of words within 50ms (2,188 words across the 7 non-ND English
    tracks; 40 tracks total were scored across all four languages: en=7, es=17, fr=12, de=4 — see
    `docs/BENCHMARKS.md`'s M4 section). Human decision: merge M4a as
    engineering-complete with this gap documented, not silently closed; closing it is real,
    open-ended follow-up work (candidates: a larger wav2vec2 variant, checking whether the
    separated vocal stem's audio quality degrades alignment precision vs. the original mix, or a
    systematic bias in the frame-to-millisecond conversion), tracked as open question 5 below —
    not a blocker for M4b or M5, but real work that should land before the player's word-highlight
    UX (M6) depends on tight timing.
  - **M4b — Lyric correction editor (done, see `docs/STATUS.md`).** The lyric correction editor
    UI and re-alignment on corrected text — this repo's first real frontend work (`apps/web` was
    still the unmodified Next.js starter as of M4a). `GET /tracks`, dev-only CORS, the gated
    `POST /tracks/{id}/realign` endpoint, an API client with dev-only client-side identity
    (explicitly not real auth — see open question 9), and a `/tracks` list page plus a
    `/tracks/[id]` correction editor with its three states (editable, locked for
    lyrics-not-allowed, locked for non-English). Verified with a real live browser session
    (upload→approve→separate→correct→re-align, both locked states, no console errors) as well as
    automated tests, per the working agreement's UI/glue-code exemption from test-first.
- **M5 — Pitch + structure (1 session).** CREPE contour, beat grid, sections, `karaoke.json` v1 emitted
  and schema-validated. **Narrowed during the approved design spec** to extraction-only, flat DB
  columns — the assembled `karaoke.json` document, its schema validation, and a read endpoint were
  deferred to M6, recorded as open question 10 below (see `docs/STATUS.md`'s M5 entry).
- **M6 — Player (3+ sessions — see risk note).** Split into three sub-milestones during
  brainstorming, since the original scope bundled the deferred read/assembly/validation path from
  M5, core Web Audio playback, and two nontrivial R&D items (WASM pitch/tempo shifting, live mic
  pitch detection) into one unit:
  - **M6a — Core synced player (done, see `docs/STATUS.md`).** `GET /tracks/{id}/package`
    (assembling M5's flat columns into the versioned `karaoke.json` v1 document and
    schema-validating it — closing the read-path part of open question 10 below) and
    `GET /tracks/{id}/stems/{stem_type}` (proxying stem audio through FastAPI rather than
    presigned MinIO URLs, a spec correction made during planning — commit `8e8ded0`), plus the
    `/tracks/{id}/play` page: Web Audio playback of the three non-vocal stems sample-aligned via
    `StemPlayer`, word-highlight lyrics, and an SVG pitch-lane visualization with a moving
    playhead.
  - **M6b — Stem mixer + transposition (done, see `docs/STATUS.md`).** Independent per-stem
    volume/mute controls on the `GainNode`s M6a's `StemPlayer` already creates, plus key/tempo
    shifting via `@soundtouchjs/audio-worklet`'s `SoundTouchNode` (the SoundTouch/Rubber
    Band-to-WASM R&D item the risk note below originally flagged) — mixer and transpose state
    persists across `play()`'s existing recreate-nodes-on-every-seek pattern, and tempo changes
    re-anchor `currentTimeSeconds` so word highlighting, the pitch-lane playhead, and M6c's mic-
    scoring target lookup all stay correctly synced regardless of tempo. Wired into the
    `/tracks/{id}/play` page via new Mixer and Transpose UI panels.
  - **M6c — Live mic pitch scoring + calibration (done, see `docs/STATUS.md`).** YIN pitch
    detection in an `AudioWorkletProcessor` (`apps/web/public/pitch-worklet.js`), wired into the
    `/tracks/{id}/play` page via `apps/web/lib/micScoring.ts`: a per-session bleed calibration
    (measures the mic's RMS floor with only the backing track playing, before scoring starts) and
    cents-based scoring against M6a's stored pitch contour. Real-world bleed survival (open
    question 3 below) is mechanically implemented but not yet measured against a real mic/speaker
    setup — `TODO: unmeasured`, tracked as a pending manual follow-up, not closed by this
    milestone.
- **M7 — Harden and launch (2 sessions).** Retention purge, takedown endpoint, rate limits,
  observability, load test, **swap the GPU backend from local to Modal/RunPod and validate the
  no-egress sandbox for real** (this is the first point the sandbox claim is actually true, not just
  assumed).

**Out of scope for v0.1**: video export/rendering, mobile apps, social features, multiplayer,
scoring/leaderboards, a hosted song catalog, payments.

## Risk notes on the estimates above

- **M6** originally bundled two nontrivial R&D items into a 3-session estimate: SoundTouch/Rubber Band
  compiled to WASM for independent key/tempo control, and live pitch detection in an AudioWorklet that
  has to survive backing-track bleed into a phone mic. This played out as anticipated: M6 was split
  into M6a/M6b/M6c (see the M6 milestone entry above) before the WASM/live-mic work was attempted, with
  M6a landing the read path and core Web Audio playback first and the two R&D items pushed to their own
  M6b/M6c sub-milestones rather than silently blowing the original estimate.
- **M7**'s backend swap (local → Modal/RunPod) is where the spec's "no network egress" sandbox
  guarantee is first actually tested. Local dev runs through M0–M6 do not validate that constraint —
  see `CLAUDE.md`.

## Open questions

Carried from the original brief, still genuinely open (answer with data, not opinion, when each
milestone reaches them):

1. Demucs quality/speed: is `htdemucs_ft` worth ~4× `htdemucs`'s inference time here? Needs a listening
   test, not a guess — do this in M3.
2. AcoustID false-negative rate on independent/unreleased music: is it acceptable given the gate exists
   to catch *commercial* leakage, not to be a complete catalog match? Measure during M1.
3. Live pitch detection under speaker/mic bleed: which algorithm survives a phone mic in a room with the
   backing track playing out loud? **M6c built the mechanism** (YIN pitch detection, an
   `echoCancellation`-enabled mic constraint, a per-session bleed-floor calibration measured with
   the backing track already playing, and an RMS gate excluding any frame that doesn't clear the
   calibrated floor) but has NOT measured real bleed survival -- that needs a human singing near
   real speakers, which no automated agent session can produce. Still open pending that manual
   test pass; `TODO: unmeasured`.
4. **Partially resolved in M7c.** GPU instance size: Modal `A10`, $0.000306/second (real, current
   pricing — `config/gpu_costs.yaml`). Real measured cost for a full pipeline run (a 3-second
   synthetic test track, not a real song): ≈$0.0134 — see `docs/BENCHMARKS.md`'s M7c section for
   the real per-stage timings and the light load test's concurrency evidence. What remains open:
   a real cost-per-track figure for an actual multi-minute song, since Demucs/Whisper/CREPE
   processing time scales with track length and this milestone only measured a 3-second synthetic
   tone — `TODO: unmeasured` until a real-length track is run through the same deployment.
5. **New in M4a.** Alignment accuracy is 68.2ms median (37.2% within 50ms), missing the ±50ms target —
   what closes the gap? Candidates, none yet tried: a larger wav2vec2 variant
   (`WAV2VEC2_ASR_LARGE_LV60K_960H`), checking whether the separated vocal stem's audio quality
   (post-Demucs artifacts) degrades alignment precision versus aligning against the original mix, or
   a systematic bias in the frame-to-millisecond conversion. Real data only, per usual — no guessing
   which fix will work before trying it. Not a blocker for M4b/M5; should land before M6's word-
   highlight UX depends on tight timing.

9. **New in M4b.** No milestone anywhere in this plan scopes real authentication. Every endpoint
   across M1-M4a and M4b's new ones (`GET /tracks`, `POST /tracks/{id}/realign`) still
   authenticates via the dev-only `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub introduced in M1
   (see `docs/STATUS.md`'s M1 "Deliberately deferred" section) -- there is still no real identity
   provider, session, or credential check anywhere in this codebase. M4b made this stub reachable
   from a browser for the first time: `apps/web/lib/api.ts`'s dev-only client-side identity
   generates a random tenant/user UUID pair on first load, stores it in `localStorage`, and sends
   it as those same two headers on every request -- previously only curl and pytest ever exercised
   this path. This is explicitly NOT real auth (documented as such at that call site) and changes
   nothing about the underlying gap: anyone who can reach the API can set those headers to any
   tenant ID they choose. When does a real milestone replace the stub, and what's the actual auth
   model (identity provider, tenant provisioning, migration path for existing dev-stub data)?
   Genuinely open -- not silently assumed solved.

10. **New in M5. Read-path resolved in M6a** -- `docs/PLAN.md`'s original M5 entry called for
    "`karaoke.json` v1 emitted and schema-validated," but the approved M5 design spec
    (`docs/superpowers/specs/2026-08-23-pitch-structure-design.md`) narrowed this to flat DB
    columns on a new `karaoke_packages` table -- no assembled `karaoke.json` JSON document, no
    JSON Schema validation, and no read (`GET`) endpoint. This was discussed with the project
    owner and decided, not silently narrowed: M5 stayed extraction-only (pitch contour, beat grid,
    section boundaries, all written as flat columns via `POST /tracks/{id}/package`), and M6a (see
    `docs/STATUS.md`) added `GET /tracks/{id}/package`, assembling the stored columns into the
    versioned `karaoke.json` v1 shape (`services/api/app/karaoke_schema.py`) and schema-validating
    it before the player consumes it. **That sub-question is resolved** -- the endpoint exists,
    `karaoke.json` v1 is assembled and validated on every read. All three M6 sub-milestones
    (M6a, M6b, M6c) are now done -- see `docs/STATUS.md`. Open question 10 itself is now fully
    resolved: there is no remaining "was the read path/mixer/scoring ever built" scope left
    under this question. What's left touching this same area is tracked elsewhere, not here:
    open question 3 (real mic bleed survival, mechanism built by M6c but genuinely unmeasured)
    and open question 5 (the ±50ms alignment accuracy gap, which the word-highlight/pitch-lookup
    consumers of `karaoke.json` still inherit).

Resolved during this planning pass (see `docs/DECISIONS_LOG.md` for full reasoning):

6. Local infra without Docker installed → Docker Desktop on WSL2, install before M0.
7. Dev-time GPU strategy → local GPU through M0–M6, Modal/RunPod deferred to M7.

Resolved with real data during a milestone:

8. Non-English vocal alignment: what degrades, and what's the fallback? Measured in M4a against
   JamendoLyrics Multi-Lang's German/Spanish/French tracks via the `whisper_native` fallback path
   (no forced-alignment model covers those languages — see the design spec's licensing-blocked-
   multilingual-aligner scope decision): median error 124.4ms (de) / 137.2ms (es) / 135.6ms (fr),
   all worse than English's already-missed 50ms target, with a ~51% word-match rate against
   reference lyrics via difflib reconciliation. The fallback works end to end but is measurably
   worse than the (also currently insufficient) English path — see `docs/BENCHMARKS.md`.
