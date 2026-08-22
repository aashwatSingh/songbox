# M4a: Alignment engine (transcription + word-level forced alignment) — design

Status: approved. Date: 2026-08-21.

## Context

M0-M3 are done and merged. M3 produces four separated stems per rights-gate-passed track, stored in
MinIO with `stems` rows; the `vocals` stem is what this milestone consumes.

`docs/PLAN.md`'s M4 entry reads: "Whisper on the vocal stem, wav2vec2 forced alignment, word timings
with confidence, the lyric correction editor, re-alignment on corrected text. *Done when:* measured
word-onset error is within ±50ms median on a hand-labeled 10-track set built during this milestone."
PLAN.md already hedges the estimate as "3 sessions, may run longer."

### Scope decision: M4 is split into M4a and M4b

M4 as written bundles four separable things: backend ML (Whisper + wav2vec2), an accuracy-measurement
deliverable, this repository's first real frontend surface (the lyric correction editor — `apps/web`
is still the unmodified Next.js starter), and a re-alignment loop. That is not one milestone.

- **M4a (this spec)** — the alignment engine and the eval harness that proves its accuracy. The
  measurement stays welded to the thing it measures, because the ±50ms figure *is* M4's acceptance
  criterion and this project does not ship unmeasured claims.
- **M4b (later spec)** — the lyric correction editor UI and re-alignment on corrected text.

### Scope decision: the eval set is a public benchmark, not a self-labeled set

PLAN.md's "done when" specifies "a hand-labeled 10-track set built during this milestone." **This spec
deliberately deviates.** M4a measures against **JamendoLyrics Multi-Lang** (the maintained successor
to the deprecated `f90/jamendolyrics` repository), a Creative Commons benchmark with *manually*
annotated word-level timings covering English, German, French, and Spanish.

Three reasons this is better evidence than a self-made set, not merely cheaper:

1. **Independent ground truth.** Labels we produce ourselves, after seeing our own tool's output, are
   not independent of the tool. The published annotations are.
2. **It answers an open question for free.** `docs/PLAN.md` open question 2 ("non-English vocal
   alignment: what degrades, and what's the fallback?") requires at least one non-English track.
   JamendoLyrics Multi-Lang supplies three non-English languages.
3. **Comparability.** It is a standard benchmark, so our number can be sanity-checked against
   published results. M3's final review caught a committed benchmark that was 4.3x wrong; an
   externally comparable baseline is a direct guard against repeating that.

**Correction — this is not "rights-clean by construction."** An earlier version of this section
claimed the Creative Commons licensing made this dataset a straightforward Lane C fit. Checking the
dataset's actual `license_type` field before writing the eval task disproved that: most tracks are
**CC BY-NC-ND / CC BY-NC-SA** — non-commercial, and ND ("No Derivatives") specifically forbids
creating derivative works, which running Demucs separation and forced alignment on the audio is. This
is not the product's Lane C path and must never be treated as one — no track from this dataset may be
uploaded through `/tracks/upload` or stored via the product's own pipeline.

What actually makes this defensible is narrower and is now a hard constraint on the eval harness, not
an assumption: `scripts/eval_alignment.py` downloads audio to an ephemeral temp directory, runs the
pipeline for scoring only, and deletes every derived artifact (separated stems, alignment output,
the source audio itself) immediately after scoring a track — only the aggregate error numbers are
ever committed, to `docs/BENCHMARKS.md`. Nothing from this dataset is stored, served, or exposed
through the product. On top of that, any track whose `license_type` contains `"ND"` is skipped
entirely, so the No-Derivatives question doesn't arise even for the tracks the eval actually touches.
This is a real, human-made decision, not a default — recorded here and in `docs/STATUS.md`.

This is also distinct from `CLAUDE.md`'s "no paste-a-link, no arbitrary third-party media fetch" rule:
that rule governs the *product's* ingestion path exposed to users. `scripts/eval_alignment.py` is a
developer-run internal tool with no user-facing surface, a single pinned dataset name, and no ability
to fetch an arbitrary URL — not a fourth ingress lane.

### Scope decision: the multilingual aligner is license-blocked

`torchaudio.pipelines.MMS_FA` is the obvious forced-alignment choice — wav2vec2 trained on 1,130
languages, purpose-built for alignment. **It is published under CC-BY-NC 4.0: non-commercial use
only.** Songbox is a commercial product (`docs/PLAN.md` discusses price floors and B2B licensing
lanes). Shipping an NC-licensed model inside it would be a licensing violation, on a project whose
entire premise is rights-cleanliness.

M4a therefore uses `torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H`, published under the **MIT License**
("Originally published by the authors of *wav2vec 2.0* under MIT License and redistributed with the
same license"), trained on LibriSpeech 960h — which is English-only. Non-English tracks fall back to
Whisper's own word timestamps (Whisper is MIT, 99 languages), and the eval **measures** how much
worse that fallback is rather than guessing.

Commercially-licensed multilingual forced alignment is left as a genuine open question, recorded in
`docs/PLAN.md`, not quietly assumed solved.

### Scope decision: ASR runtime is faster-whisper, model size is a parameter

PLAN.md targets Whisper large-v3. This machine currently has only the CPU-only torch wheel installed
(see `docs/BENCHMARKS.md`), and large-v3 on CPU across a multi-song eval sweep is impractical. M4a
uses **faster-whisper** (CTranslate2, MIT), several times quicker than the reference implementation on
CPU, with **model size as a parameter** rather than hardcoded — the same pattern M3 used for
`htdemucs`/`htdemucs_ft`, and for the same reason: pick the default from measured data, keep the
larger model reachable. Real per-size numbers go into `docs/BENCHMARKS.md`.

Cost of this choice, stated plainly: it adds a second inference runtime (CTranslate2) alongside torch.

### Scope decision: the gpu_backend seam gets built now

`docs/adr/0001-gpu-backend-abstraction.md`'s M3 update deferred the backend interface with an explicit
trigger: "revisit once M4 adds a second GPU-calling stage." M4a is that trigger, and the stated reason
for waiting — only one call site to design the interface against — no longer holds.

M4a builds the thin interface the ADR describes and routes **both** M3's Demucs call and M4a's
Whisper/wav2vec2 calls through it. This keeps M7's local-to-Modal/RunPod swap a configuration change
rather than the rewrite the ADR exists to prevent. ADR-0001 is updated to record that its deferral
ended here.

## What M4a builds

### 1. `gpu_backend` interface

A small module (`services/api/app/gpu_backend.py`) defining how a pipeline stage requests inference,
with a `local` implementation that runs in-process on this machine, exactly as M3 does today. M3's
`separate_audio` call and M4a's transcription/alignment calls both go through it. No behavior change
for M3 — this is a seam, not a rewrite. The `modal`/`runpod` implementations remain M7's work and are
explicitly not built here.

The single process-wide inference lock and wall-clock timeout M3 added to `POST /tracks/{id}/separate`
move into this layer, because the constraint they express — "one heavy model at a time on this box" —
is a property of the backend, not of any one endpoint. Demucs and Whisper contend for the same CPU,
GPU, and memory, so they share one lock rather than each having their own.

### 2. `services/api/app/transcription.py`

Pure functions, testable without the HTTP layer, mirroring `separation.py`'s shape:

- `transcribe_audio(path, model_size) -> Transcript` — faster-whisper. Returns transcript text,
  detected language, timed segments, and (for the non-English path) Whisper-native word timings.
- `align_words(path, text) -> list[WordTiming]` — torchaudio forced alignment against the MIT English
  wav2vec2 bundle. Raises `AlignmentError` on failure, mirroring `SeparationError`.

Two implementation details that are not obvious and must not be "fixed" by a later reader:

**Chunk the acoustic forward pass, not the alignment.** The expensive part of alignment is the
wav2vec2 forward pass over the audio; the alignment search itself operates on the resulting emission
matrix, which is small (wav2vec2 emits roughly 50 frames per second, so a three-minute song is about
9,000 frames over a ~32-symbol vocabulary). So `align_words` runs the forward pass in bounded audio
chunks, concatenates the emission frames into one matrix, and then calls
`torchaudio.functional.forced_align` **once** over the complete emissions against the complete
tokenized text.

This matters for correctness, not just memory. `forced_align` is documented `batch_size==1` — one
sequence — which this satisfies exactly. Chunking the *alignment* instead would mean guessing which
words belong to which chunk, and would cut words at arbitrary boundaries. It also means `align_words`
takes plain text rather than pre-segmented text, so the identical code path serves all three callers:
Whisper's transcript, the eval's reference lyrics, and M4b's user-corrected lyrics.

**Resampling to 16kHz does not violate the 44.1kHz invariant.** wav2vec2 requires 16kHz mono.
`CLAUDE.md`'s rule ("all internal audio is 44.1kHz stereo WAV, assert at every stage boundary")
governs what is *stored* and what *crosses stage boundaries*. The 16kHz mono tensor is a transient
model input constructed inside `align_words` and never persisted, never returned, and never written to
storage. The vocal stem read from MinIO is asserted 44.1kHz stereo on the way in, as M3's outputs are
on the way out.

### 3. `transcriptions` table (migration 0005)

- `id` (UUID PK), `tenant_id`, `track_id` (FK to `tracks.id`)
- `whisper_model` — which faster-whisper size produced this row
- `aligner` — `"wav2vec2"` or `"whisper_native"`, so the non-English degradation is visible in the
  data rather than inferred
- `language` — Whisper's detected language code
- `lyrics_display_allowed` (bool) — see rights handling below
- `words` (JSONB) — array of `{idx, text, start_ms, end_ms, confidence}`
- `created_at` (timestamptz)

Words are JSONB rather than a separate `words` table: they are always read as a complete set, they
feed `karaoke.json` (a document) in M5, and a table would mean thousands of rows per track for an
access pattern that never queries an individual word. The tradeoff, stated: M4b's per-word edits
become a JSONB write rather than a row update — acceptable, since re-alignment rewrites the whole set
anyway.

`created_at` is included deliberately. M3's final review noted that provenance columns without a
timestamp cannot be ordered across re-runs, and re-transcribing at a different model size is an
expected operation here.

RLS follows the established pattern exactly (`ENABLE` + `FORCE` row level security, a
`tenant_isolation` policy, grants to `songbox_app` and not the superuser role), plus indexes on
`tenant_id` and `track_id`.

### 4. Rights: lyrics are gated at display, not at compute

`CLAUDE.md` tracks lyric rights separately from recording rights and states that missing lyric
clearance is "a supported degraded state (no lyric text rendered), not an error."

Transcription therefore always runs — word timings drive the karaoke highlight and feed M5's
packaging regardless of whether the text may be shown. A `lyrics_display_allowed` boolean is resolved
once, at transcription time, and stored on the row:

- Lane A (creator-owned) → allowed
- Lane C (public domain / Creative Commons) → allowed
- Lane B (licensed) → allowed only if the referenced license's `covers_lyrics` is true

The read endpoint returns word timings unconditionally and word `text` only when the flag is true.
This puts the rights decision at the display boundary, which is where `CLAUDE.md` puts it.

`CLAUDE.md`'s "never log raw audio, lyrics, or signed URLs" becomes concrete here: no transcript text
may appear in any error message, exception, or log line. This extends the genericized-error pattern
M2 established for ffmpeg failures.

### 5. `POST /tracks/{track_id}/transcribe`

Added to `services/api/app/routes/tracks.py`, following the conventions the existing endpoints set.

Ordering matters and is the point of the endpoint. **Before any model is loaded:**

1. Track exists and belongs to this tenant, else 404.
2. `track.status == "passed"`, else 409 — the rights gate, same as M3.
3. A `stems` row with `stem_type == "vocals"` exists for this track, else 409.

Check 3 is how `CLAUDE.md`'s "source separation always precedes transcription — Whisper runs on the
isolated vocal stem, never the full mix" becomes structural rather than aspirational: there is no code
path that reaches Whisper without a vocal stem row existing. Tests must prove the models were never
invoked for a rejected request, not merely that the status code was right — the pattern M3's review
confirmed actually works.

Failures map to status codes consistently with the existing endpoints: unknown model size → 422,
`AlignmentError`/transcription failure → 422, backend busy beyond the lock timeout → 503, wall-clock
timeout → 504.

`GET /tracks/{track_id}/transcription` returns the stored result, honoring `lyrics_display_allowed`.

### 6. `scripts/eval_alignment.py` and the M4 section of `docs/BENCHMARKS.md`

Runs the pipeline over JamendoLyrics Multi-Lang (loaded via the `datasets` library,
`jamendolyrics/jamendolyrics` on Hugging Face) and reports **median absolute word-onset error** and
**percentage of words within ±50ms**, broken out per language, into `docs/BENCHMARKS.md`. Real
measured numbers only; `TODO: unmeasured` for anything not actually run.

Per the licensing correction above, this script is held to two hard rules, not left to a future
reader's judgment: skip any row whose `license_type` field contains `"ND"` before processing it, and
delete every artifact derived from a track's audio (temp audio file, separated stems, alignment
output) immediately after that track is scored — nothing survives the run except the aggregate
numbers written to `docs/BENCHMARKS.md`.

The primary measurement force-aligns the **reference lyrics**, not Whisper's transcript. This is
deliberate and matters for three reasons: the ±50ms criterion is about aligner precision rather than
ASR word-error rate; force-aligning known-correct text is what makes the number comparable to
published baselines; and it is precisely the code path M4b's "user corrects the lyrics, then re-align"
feature will exercise, so the number describes a real production path.

End-to-end accuracy using Whisper's own transcript is reported as a secondary number, since that is
what a user gets before correcting anything.

The eval runs the real pipeline — separation, then transcription, then alignment — because the ±50ms
claim has to hold end to end, not just for alignment in isolation on a clean vocal track.

## Testing strategy (test-first, per the working agreement)

Real-model accuracy is proven by the eval harness, not by pytest. The test suite covers what pytest
can cover honestly and quickly:

1. **Alignment post-processing** against a hand-constructed log-probabilities tensor — deterministic,
   no model download. This covers the frame-index-to-milliseconds conversion and the word-boundary
   extraction (the MIT English bundle marks word boundaries with a `|` token), which is where an
   off-by-one is most likely to hide and where it would silently shift every reported onset.
2. **Gating and rights behavior** via monkeypatched models, following M3's proven pattern: a
   non-passed track and a track with no vocals stem must each return 409 *and* prove the model was
   never invoked; a Lane B track without `covers_lyrics` must store the transcription with
   `lyrics_display_allowed=False` and the read endpoint must omit text while still returning timings.
3. **`transcriptions` table and RLS**, covered automatically by the existing `Base.metadata`-derived
   invariant tests, as `stems` was in M3.
4. **One slow integration test** running the real models end to end on a short clip, proving the
   pipeline actually executes rather than only its mocked skeleton.

## Out of scope for M4a

The lyric correction editor and re-alignment on corrected text (M4b). `karaoke.json` packaging (M5).
The RQ/async job queue (still deliberately deferred; the endpoint stays synchronous, as M1-M3's do).
The `modal`/`runpod` backend implementations and the no-egress sandbox validation (M7, per ADR-0001).
Commercially-licensed multilingual forced alignment (a genuine open question, recorded rather than
assumed). Streaming or memory-optimized transfer of large audio, and per-tenant rate limiting, both
carried over as known limitations from M3.
