# M5: Pitch + structure — design

Status: approved. Date: 2026-08-23.

## Context

M0-M4b are done and merged to `master`. M3 produces four stems per rights-gate-passed track
(`vocals`, `drums`, `bass`, `other`); M4a/M4b produce `Transcription` rows with word-level timings and
text (subject to `lyrics_display_allowed`). `docs/PLAN.md`'s M5 entry: "CREPE contour, beat grid,
sections, `karaoke.json` v1 emitted and schema-validated."

No `karaoke.json` schema exists anywhere in this project yet — the pipeline diagram in `docs/PLAN.md`
names it as the final packaging stage before the player (M6), but nothing has defined its shape. This
spec defines it for the first time. `CLAUDE.md`'s rule — "`karaoke.json` is a versioned schema. Any
shape change needs a migration path, not a silent bump." — governs everything from here forward, so
the schema is versioned from row one even though there is nothing yet to migrate from.

Two libraries are verified (not assumed) before this spec commits to them, following this project's
established discipline of checking real API shapes and licenses before they land in a design (M4a's
`MMS_FA`/`WAV2VEC2_ASR_BASE_960H` and JamendoLyrics licensing corrections are the precedent):

- **`torchcrepe`** (pitch extraction) — MIT License, confirmed directly from the repository's `LICENSE`
  file. `torchcrepe.predict(audio, sample_rate, hop_length=None, fmin=50., fmax=MAX_FMAX,
  model='full', return_periodicity=False, batch_size=None, device='cpu', pad=True)` takes mono audio
  shaped `(1, samples)`, **resamples to its own internal 16kHz automatically** (unlike M4a's
  `align_words`, which had to resample by hand for wav2vec2), and returns pitch in Hz plus, when
  `return_periodicity=True`, a confidence score — both per-frame at the given `hop_length` (default
  10ms). Verified against the actual `torchcrepe/core.py` source, not the README's looser prose.
- **`librosa`** (beat/structure detection) — ISC License (permissive, MIT/BSD-2-equivalent), confirmed
  directly from the repository's `LICENSE.md`. `librosa.beat.beat_track(y=..., sr=..., units="time")`
  returns `(tempo_bpm, beat_times_seconds)` directly in seconds when `units="time"` is passed —
  avoiding a manual frame-to-time conversion step. `librosa.segment.agglomerative(chroma, k)` returns
  segment-boundary frame indices, but **requires the caller to specify `k`** (the number of segments to
  produce) — it does not discover the "right" number of sections from the audio. This constraint,
  verified against the real function signature, directly shapes the structure-detection design below.

## Scope decisions

### Decision 1: pitch runs on the vocals stem; beat/structure run on the accompaniment

Pitch is a singing guide — it needs the isolated vocal melody, the same reasoning M4a already applied
to transcription (`CLAUDE.md`: "Source separation always precedes transcription... Whisper runs on the
isolated vocal stem, never the full mix"). Beat/structure detection needs to sync against what the
karaoke *player* actually plays back during singing: the instrumental (drums+bass+other), not the
original full mix, which still contains the real vocals that get muted/replaced. Using the full mix for
beat tracking would mean detecting rhythm partly from audio the player never plays in that form.

M3 stores the three non-vocal stems separately; no combined "accompaniment" file exists yet. Building
one is real, new work: load `drums`/`bass`/`other` (all guaranteed 44.1kHz stereo per M3's invariant, so
sample-aligned summing is safe), sum them, and **peak-normalize the result** before writing it. Summing
three independent, separately-mastered stems can clip past ±1.0 amplitude; the normalization step is
documented in place so a future reader doesn't "simplify" it away and reintroduce audible distortion.
This accompaniment WAV is a transient artifact of the packaging step, not a new persisted stem — it is
not written to MinIO or given its own `Stem` row, matching how `align_words`'s 16kHz mono tensor in M4a
was a transient model input, not a new stored asset.

### Decision 2: unlabeled structural boundaries, not verse/chorus/bridge labels

Real semantic section labeling (verse/chorus/bridge/outro) needs a trained classifier this project does
not have, and building or training one is a separate ML project, not a sub-task of M5. `CLAUDE.md`'s
measurement-discipline rule — no fabricated accuracy — applies here exactly as it did to M4a's
non-English-alignment scope decision: guessing labels with a heuristic would produce confident, plausibly
wrong output, which is precisely what that rule exists to prevent.

M5 instead emits **unlabeled structural boundary timestamps** — real signal (`librosa.segment
.agglomerative`'s chroma-similarity clustering genuinely detects where a song's harmonic/timbral content
changes), described honestly (no "chorus"/"verse" label attached to any boundary, because no classifier
produced one).

This creates one more thing worth stating precisely, verified above: `agglomerative` requires a `k`
(segment count) as input — it does not choose k itself. There is no principled way to auto-select k
without more sophisticated novelty-curve methods this milestone does not build. **k is derived from
track duration via a fixed heuristic** (roughly one boundary per ~20 seconds, clamped to a sane range —
the exact numbers are an implementation-plan detail, not a design-level accuracy claim). This spec is
explicit that **boundary count is a tunable heuristic; boundary positions, given k, are what the
clustering genuinely detects from the audio.** Conflating those two would be exactly the kind of
unmeasured claim this project's review culture exists to catch — stating the distinction here means it
doesn't need to be re-derived or accidentally blurred later.

### Decision 3: `karaoke.json` lives in a new DB table as JSONB, not a MinIO file

A new `karaoke_packages` table, JSONB column, matching `transcriptions.words`'s existing precedent
exactly — same RLS pattern (`ENABLE`+`FORCE`, `tenant_isolation` policy, grant to `songbox_app`), same
reasoning: the document is small (word timings + a pitch contour + a beat grid — KB-scale JSON, not
MB-scale audio), consistent with every other JSONB-shaped document already in this schema, and it needs
no new storage subsystem. `Stem` files justified MinIO because they're genuinely large binary audio;
`karaoke.json` is neither large nor binary.

### Decision 4: CREPE defaults to `model='tiny'`, benchmarked against `'full'`

Same pattern as M3's `htdemucs`-before-`htdemucs_ft` and M4a's Whisper-`base`-before-larger-sizes: start
with the fast option so this milestone's own dev loop stays usable on this machine's CPU-only torch
build, keep `model` a parameter rather than hardcoding it, and put real measured speed/accuracy numbers
for both `tiny` and `full` into `docs/BENCHMARKS.md` before any decision to change the default. No
number is assumed ahead of actually running it.

## What M5 builds

### 1. Accompaniment synthesis

A small function (in a new `services/api/app/packaging.py`, alongside the pitch/structure logic below —
one module for the whole "assemble a karaoke package" concern, mirroring how `separation.py` and
`transcription.py` each own one pipeline stage) that loads the three non-vocal stems, sums them
sample-for-sample, peak-normalizes, and returns a transient WAV path. Consumed only by beat/structure
detection below; never persisted.

### 2. Pitch extraction

`extract_pitch(vocals_path: Path, model: str = "tiny") -> list[PitchFrame]`, where `PitchFrame` carries
`time_ms`, `hz` (or `null` when the frame is unvoiced), and `confidence`. Loads the vocals stem, builds
the mono `(1, samples)` tensor `torchcrepe.predict` requires (same mono-mixdown pattern as M4a's
`align_words` — reused, not reinvented), and calls `predict(..., model=model, return_periodicity=True)`.
Frames below a confidence threshold are marked unvoiced (`hz: null`) rather than assigned CREPE's raw
(unreliable, at low confidence) Hz estimate — the periodicity score decides this, not a guess.

### 3. Beat + structure detection

`extract_structure(accompaniment_path: Path) -> StructureResult`, where `StructureResult` carries
`tempo_bpm: float`, `beats_ms: list[int]` (from `librosa.beat.beat_track(..., units="time")`, converted
to milliseconds), and `sections_ms: list[int]` (boundary timestamps from `librosa.segment.agglomerative`
with the duration-derived `k` from Decision 2, converted to milliseconds via `librosa.frames_to_time`).

### 4. `karaoke_packages` table (migration 0006)

- `id`, `tenant_id`, `track_id` (FK to `tracks.id`)
- `schema_version` (integer, `1` for every row this milestone produces — baked in from the start per
  `CLAUDE.md`'s versioning rule)
- `words` (JSONB) — copied from the track's most recent `Transcription` row at packaging time, same
  `{idx, text, start_ms, end_ms, confidence}` shape, `text` nulled when `lyrics_display_allowed` is
  false (same rule M4a's `_transcription_to_response` already applies, now applied at write time too
  since this document may be read by an unauthenticated player context later in M6 — never embed
  lyric text this row shouldn't be allowed to carry)
- `pitch_model` (string — which CREPE variant produced this row, matching `Stem.model_name`'s and
  `Transcription.whisper_model`'s existing provenance-column precedent)
- `pitch` (JSONB — the `PitchFrame` list)
- `tempo_bpm` (float), `beats_ms` (JSONB array), `sections_ms` (JSONB array)
- `created_at`

A new package is a new row (immutable, matching `Transcription`'s pattern from M4b) — re-packaging
after a lyric correction never destroys a prior package. Which row a future player (M6) reads is a
"most recent by `created_at`" query, matching `get_transcription`'s existing pattern exactly.

RLS: `ENABLE`+`FORCE` row level security, `tenant_isolation` policy, grant to `songbox_app`, indexes on
`tenant_id` and `track_id` — the exact established pattern from every prior milestone's tables.

### 5. `POST /tracks/{track_id}/package`

Added to `services/api/app/routes/tracks.py`, following every prior endpoint's conventions exactly.
Gate order, all before any model call:

1. Track exists and belongs to this tenant, else 404.
2. `track.status == "passed"`, else 409.
3. All four stems (`vocals`, `drums`, `bass`, `other`) exist for this track, else 409 — the first place
   in this codebase that checks for *all four* stems rather than just `vocals`, since this endpoint is
   the first to need the instrumental as well as the vocal line.
4. A `Transcription` row exists for this track, else 409 — packaging needs word timings to embed.

Runs pitch extraction and beat/structure detection through `run_inference`/`TRANSCRIPTION_TIMEOUT
_SECONDS`-equivalent timeout bound (a new `PACKAGE_TIMEOUT_SECONDS` constant, same reasoning as the
existing timeout constants — CPU-bound inference needs a bound, and this endpoint runs two models, not
one, so it inherits the same "one heavy job at a time" lock as everything else routed through
`gpu_backend.run_inference`). Writes one new `KaraokePackage` row on success.

## Testing strategy

Per the working agreement (test-first for the rights gate, the alignment engine, and the upload
handler — this is the alignment/pitch engine's direct successor, so it gets the same treatment):

1. **Accompaniment synthesis** — a unit test proving three synthetic tone fixtures sum correctly and
   the peak-normalization keeps the result within ±1.0 (constructible without needing real stems).
2. **`extract_pitch`** — real `torchcrepe` run against the `synthetic_wav` fixture, proving the pipeline
   produces well-formed frames (real timestamps, real Hz-or-null values, real confidence scores) — not
   that a 440Hz sine tone's pitch is meaningfully "correct" for a singing-guide use case, mirroring
   every prior milestone's synthetic-fixture philosophy.
3. **`extract_structure`** — real `librosa` run against a synthesized accompaniment, proving real tempo
   estimation and a real, correctly-shaped set of section boundaries (not zero, not exceeding the
   track's duration).
4. **`POST /tracks/{id}/package`** — each gate rejection (not-passed, missing a stem, no transcription)
   with a test proving the pitch/structure functions were never invoked, matching the established
   monkeypatch-raises-if-called pattern. Happy path: real end-to-end run, a `KaraokePackage` row exists
   with `schema_version == 1` and well-formed `words`/`pitch`/`beats_ms`/`sections_ms`.
5. **`docs/BENCHMARKS.md`** — real `tiny` vs `full` CREPE speed/accuracy numbers, run to completion on
   this machine, following the exact same discipline M3's Demucs and M4a's alignment benchmarks
   established (a real script, real measured output, no invented numbers, `TODO: unmeasured` for
   anything not actually run).

## Out of scope for M5

Verse/chorus/bridge semantic section labels (Decision 2 — a real, tracked open question, not a solved
problem this milestone claims to have handled). The player itself, and anything that reads
`karaoke.json` (M6). Any change to M4a's or M4b's existing endpoints or tables. Live pitch detection
(that's the player's problem in M6, against a live mic — `docs/PLAN.md` open question 3, unrelated to
this milestone's offline pitch-guide extraction).
