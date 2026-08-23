# M4b: Lyric correction editor — design

Status: approved. Date: 2026-08-23.

## Context

M0-M4a are done and merged to `master`. M4a built the alignment engine (`POST /tracks/{id}/transcribe`,
`GET /tracks/{id}/transcription`) and left `docs/PLAN.md` open question 5 unresolved: measured
forced-alignment accuracy (68.2ms median) misses the milestone's own ±50ms target. That gap is real
follow-up work, tracked separately — not something M4b tries to fix.

`docs/superpowers/specs/2026-08-21-alignment-engine-design.md` scoped M4b as "the lyric correction
editor UI and re-alignment on corrected text," split out of the original M4 milestone specifically
because it's this repository's first real frontend surface: `apps/web` is still the unmodified
`create-next-app` starter (no components, no API client, no auth wiring) as of M4a.

Two facts about the current state of the project shape this spec directly, discovered while exploring
before design started:

1. **No real authentication exists anywhere in this project.** Every API endpoint authenticates via
   `X-Dev-Tenant-Id`/`X-Dev-User-Id` headers (`services/api/app/auth.py`) — a dev-only stub M1
   explicitly documented and every milestone since has used via curl/pytest, never from a browser.
   `docs/PLAN.md` has no milestone anywhere that replaces this with real auth. M4b is the first
   milestone where a human actually clicks through a browser session, so this can't be silently
   deferred again — it needs an explicit answer (see Decision 1).
2. **No track-listing endpoint exists.** Only `GET /tracks/{id}/transcription` (direct by ID) exists.
   There is no way to discover which tracks exist or what state they're in without already knowing a
   UUID from a prior curl/pytest session.

## Scope decisions

### Decision 1: dev-only identity, stored client-side

The editor generates (or reads, if already present) a `tenant_id`/`user_id` pair in the browser's
`localStorage` on first load, and sends them as the existing `X-Dev-Tenant-Id`/`X-Dev-User-Id` headers
on every API call — the same stub M1 established, now reachable from a browser instead of only
curl/pytest. **This is explicitly not real authentication** and must not be mistaken for it later —
real auth stays a genuine, tracked gap (recorded in `docs/PLAN.md`'s open questions, not silently
assumed solved), deferred until a milestone actually scopes it. Building real auth now would make M4b
an auth milestone wearing a different name; `docs/PLAN.md` never budgeted for that scope anywhere.

### Decision 2: a minimal track list, not URL-param-only access

`GET /tracks` (new, per-tenant, no filtering beyond that) plus a `/tracks` list page in the frontend,
so a user can actually reach a track's editor without needing a UUID from an external tool first. This
is real scope beyond "just the editor," but the alternative — an editor reachable only by pasting a
UUID into a URL — isn't something a person could use end to end.

`Track.title`/`Track.artist` are real, nullable columns, but nothing in the pipeline built so far ever
populates them (no metadata-extraction stage exists). The list therefore shows track IDs and pipeline
status, not song titles — this is the honest current state of the data, not a gap this spec fakes
around with placeholder titles.

### Decision 3: text-only correction, not manual timing adjustment

A user can fix a word's spelling; they cannot drag a word's timing boundaries on a waveform. This
matches exactly what the M4a design spec scoped for M4b ("the lyric correction editor UI and
re-alignment on corrected text") and keeps `align_words()` — already built in M4a specifically for
this — as the entire re-alignment mechanism: correcting text and re-running forced alignment
automatically re-derives every word's timing, so no separate timing-editing UI or data model is
needed. Manual timing nudging is a real, larger feature (waveform rendering, drag interaction, a
timing-conflict model for adjacent words) that may matter once it's known whether the 68.2ms accuracy
gap actually needs it — a decision for a later milestone, not this one.

### Decision 4: corrections are new, immutable `Transcription` rows

Matches the established pattern in this schema: `RightsDeclaration` rows are never mutated in place,
only added to, so a track's rights history is a real audit trail rather than a value that silently
changed underneath a reviewer. Corrections follow the same shape. `GET /tracks/{id}/transcription`
already selects the most recent row by `created_at`, so this requires zero changes to the read path —
a correction is simply a new row that becomes the new "most recent."

A corrected row's `whisper_model` field gets the sentinel string `"user-corrected"` rather than a real
model size, because Whisper was never re-run to produce it — claiming a model size here would be
exactly the kind of fabricated-provenance value `CLAUDE.md`'s measurement discipline forbids. `aligner`
stays `"wav2vec2"`, since that's what actually produced the new timings.

### Decision 5: non-English tracks are read-only in the editor

Forced alignment (`align_words()`) only covers English — the MIT-licensed `WAV2VEC2_ASR_BASE_960H`
model is English-only; M4a's design spec already established that a commercially-licensed multilingual
aligner doesn't exist as an option (`MMS_FA` is CC-BY-NC, non-commercial-only, and Songbox is a
commercial product). A non-English track's editor page shows its current words and timings but disables
the correction form, with a plain explanation. The rejected alternative — allowing text edits and
either fabricating timing precision for a language with no real aligner, or silently leaving stale
timings after a word-count-changing edit — was rejected specifically because it violates the same
no-fabricated-accuracy principle M4a was built around.

### Decision 6 (not asked, a direct consequence of an existing invariant): lyrics-withheld tracks are read-only too

`CLAUDE.md`: "Missing lyric clearance is a supported degraded state (no lyric text rendered), not an
error." A track whose `lyrics_display_allowed` is `false` already has its word `text` withheld from
every API response (`_transcription_to_response` nulls it). The editor cannot offer to "correct" text
it isn't permitted to display in the first place — this isn't a new design choice, it's the existing
invariant applied to a new surface. The editor shows a locked banner explaining why, same shape as the
non-English case.

## What M4b builds

### 1. `POST /tracks/{track_id}/realign`

Added to `services/api/app/routes/tracks.py`, following `separate_track`/`transcribe_track`'s exact
conventions. Request body: `{"text": string}` — the corrected full lyric text, reconstructed
client-side by joining the edited per-word text fields with spaces. Response: the same
`TranscribeResponse` shape `/transcribe` already returns.

Gating order, all before any model call — mirrors `transcribe_track`'s proven pattern, extended with
two new checks specific to correction:

1. Track exists and belongs to this tenant, else 404.
2. `track.status == "passed"`, else 409 (the rights gate, same as every mutating track endpoint).
3. A `Transcription` row exists for this track (most-recent lookup, same pattern as `GET
   .../transcription`), else 409 — nothing to correct until `/transcribe` has run once.
4. That row's `lyrics_display_allowed` is `true`, else 409 — Decision 6.
5. That row's `language == "en"`, else 409 — Decision 5. Enforced server-side, not only hidden in the
   UI: this codebase never trusts client-side-only gating for anything correctness-relevant (the
   `model_size`/`model_name` whitelists in `/transcribe` and `/separate` set the precedent).
6. The track's `vocals` stem exists (defensive — a `Transcription` row existing already implies this,
   but every other mutating endpoint in this file re-checks its own preconditions rather than trusting
   an earlier endpoint's checks stayed valid).

On pass: fetch the vocals stem's bytes, write to a temp file (identical tempfile/`finally: unlink`
pattern as `transcribe_track`), call `run_inference(lambda: align_words(path, corrected_text),
timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS)` — reusing the existing timeout constant, since
`align_words` alone is a strict subset of what `/transcribe` already bounds. `AlignmentError` maps to
422 with a fixed generic detail string, not the exception's own message (`CLAUDE.md`: never log raw
lyrics — same reasoning `/transcribe`'s `AlignmentError` mapping already established).

`lyrics_display_allowed` on the new row is recomputed via `resolve_lyrics_display_allowed`, not copied
from the prior row — mirrors `/transcribe`'s own defensive-recompute pattern (re-checking rather than
trusting a previously-computed value), consistent with `/separate` re-detecting audio format from
stored bytes rather than trusting anything client-supplied.

The new row's `whisper_model` is the sentinel string `"user-corrected"` and `aligner` is `"wav2vec2"`,
per Decision 4 — `language` is always `"en"` (the gate above already required it of the row being
corrected).

### 2. `GET /tracks`

New endpoint, tenant-scoped, no pagination or filtering (dev-tool scale). Returns a list of
`{track_id, status, duration_seconds, has_transcription}`. `has_transcription` is computed via a
second query collecting distinct `track_id`s from `transcriptions` for this tenant, not an N+1 lookup
per track.

### 3. CORS middleware

`services/api/app/main.py` currently has none. The Next.js dev server (port 3000) cannot call the
FastAPI backend (port 8000) cross-origin without it. Dev-only permissive configuration (localhost
origins only) — not a production CORS policy, and recorded as such so a later milestone doesn't mistake
this for a hardened setting.

### 4. Frontend (`apps/web`)

First real code in this app. Given identity lives in `localStorage` (Decision 1, client-side only),
all data-fetching is client-side — no mixed Server/Client Component data-fetching patterns to keep
straight while auth is a stub component. Two pages:

- **`/tracks`** — lists tracks with status; only rows where `has_transcription` is true link into the
  editor (others show their state as plain text, non-interactive — no upload/separate/transcribe
  triggers are built here, see Out of Scope).
- **`/tracks/[id]`** — the editor. Fetches `GET /tracks/{id}/transcription`. Three states:
  - `lyrics_display_allowed === false` → locked banner (Decision 6), no edit form.
  - `language !== "en"` → locked banner (Decision 5), no edit form, words/timings still shown read-only.
  - Otherwise → one editable text input per word (Decision 3's Option A), a "Save & re-align" button
    that joins the current field values with spaces and calls `POST /tracks/{id}/realign`, then
    re-renders with the response's fresh words/timings.

A small shared API client module (`apps/web/lib/api.ts` or similar) centralizes the base URL, the
localStorage-identity-header injection, and response parsing — every fetch call goes through it rather
than each page reimplementing header handling.

## Testing strategy

Per the working agreement (test-first for the rights gate, the alignment engine, and the upload
handler — UI and glue code are exempt): the two new backend pieces get real tests, following the
established monkeypatch-proves-non-invocation pattern for every gate check; the frontend does not, per
the explicit exemption.

1. **`POST /tracks/{id}/realign`** — happy path (English, lyrics-allowed track with an existing
   transcription: corrected text produces a new `Transcription` row with real re-aligned timings, `GET
   .../transcription` reflects it). Each gate rejection (no transcription yet, `lyrics_display_allowed
   == false`, `language != "en"`, `status != "passed"`) with a test proving `align_words` was never
   invoked for the rejected cases, matching `/transcribe`'s and `/separate`'s proven pattern.
2. **`GET /tracks`** — returns only the calling tenant's tracks (RLS-adjacent correctness check, not a
   new RLS policy since `tracks` already has one); `has_transcription` is accurate for both a track
   with and without a `Transcription` row.
3. **CORS middleware** — a lightweight test confirming the configured localhost origins get the
   expected headers, not a broad security test (this is dev-only configuration, not a hardened policy
   under test).

## Out of scope for M4b

Manual timing adjustment (Decision 3 — a real, larger future feature). Real authentication (Decision 1
— a genuine, tracked gap, not solved here). Upload, separation-trigger, or transcription-trigger UI —
the editor only acts on tracks that already have a transcription; producing one stays an API-only
operation for now. `karaoke.json` packaging (M5). The RQ/async job queue and container-level GPU
sandboxing (both still deferred per M3/M4a's existing reasoning, unaffected by this milestone). Closing
the ±50ms accuracy gap (`docs/PLAN.md` open question 5) — a real, separate piece of work this milestone
does not attempt.
