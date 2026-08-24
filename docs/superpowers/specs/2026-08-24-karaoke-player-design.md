# M6a: Core Synced Player — Design Spec

## Context

`docs/PLAN.md` originally scoped M6 as one milestone: "Web Audio playback, word highlight, pitch lane,
live mic pitch, transposition, stem mixer, calibration" (3+ sessions, flagged as likely to run long —
the same shape of estimate that M4 blew through). During this brainstorm, the user approved splitting
M6 the same way M4 split into M4a/M4b: this spec covers **M6a — the core synced player** only.
Stem-volume mixing and key/tempo transposition become M6b; live mic pitch scoring and calibration
become M6c. Neither later milestone is scoped here.

M6a also closes M5's open question 10 (`docs/PLAN.md`): M5 built pitch/structure extraction as
write-only flat DB columns and explicitly deferred the read endpoint, `karaoke.json` assembly, and
schema validation to "whichever milestone actually consumes this data." That's M6a.

Two real gaps existed going into this design, both resolved below:
- **No endpoint has ever served audio bytes to a browser.** Uploads use presigned PUT to MinIO
  (`services/api/app/storage.py`); there has never been an equivalent GET path. Verified
  `minio-py`'s `Minio.presigned_get_object(bucket_name, object_name, expires=timedelta(...))` is the
  right primitive — confirmed against the installed client's real signature, not assumed.
- **No JSON Schema validation library was in this project.** Added `jsonschema` (MIT license,
  verified from the installed package's metadata: `python-jsonschema/jsonschema` on GitHub,
  `License-Expression: MIT`) — `jsonschema.validate(instance, schema)` raising
  `jsonschema.exceptions.ValidationError` on mismatch, confirmed against the real installed 4.26.0
  API, not assumed from familiarity.

## Decision 1: playback audio — client-side mix of three stems, no new persisted artifact

The player plays an "instrumental" (no vocals) while lyrics highlight and the pitch lane shows the
target contour. Rather than persisting the accompaniment file M5's `synthesize_accompaniment` already
computes as a transient temp-file artifact (which would reverse that milestone's explicit "never
written to MinIO" decision), the player fetches presigned GET URLs for the `drums`, `bass`, and
`other` stems individually and mixes them client-side: three `AudioBufferSourceNode`s, each routed
through its own `GainNode`, started at the same `AudioContext` time offset. `vocals` is not fetched in
M6a — no feature in this milestone needs it (adding it now would be speculative scope creep toward
M6b/M6c's "practice with vocals" or scoring features, which aren't designed yet).

This also means per-stem gain is already wired as three independent `GainNode`s by the time M6b (the
mixer milestone) starts — M6a just never exposes a UI control for them.

## Decision 2: `GET /tracks/{track_id}/package` — assembly + schema validation happen at read time

New endpoint on `services/api/app/routes/tracks.py`, following the exact gate/response conventions
already established by `get_transcription` in the same file:

- 404 if the track doesn't exist or belongs to another tenant (same as every other endpoint).
- 404 `"no karaoke package found for this track"` if no `KaraokePackage` row exists yet — **not** a
  new response envelope. This exactly mirrors `get_transcription`'s existing 404-when-absent
  convention, so the frontend's "not ready yet" state uses the same catch-a-404 pattern the
  correction editor (M4b) already uses for its own not-ready states, rather than inventing a second
  convention for the same kind of situation.
- Fetches the latest `KaraokePackage` row for the track (`order_by(created_at.desc()).limit(1)`,
  the same "latest wins" pattern already used for `Transcription`) — each `/package` call created an
  immutable new row (M5 decision), so "latest" is the current one.
- Assembles the row's flat columns into the nested `karaoke.json` v1 document (see Decision 3), then
  validates that document against a JSON Schema with `jsonschema.validate()` before returning it. A
  `ValidationError` here means the assembled document doesn't match its own declared schema — a
  genuine internal bug (data corruption, or the schema and the assembly code have drifted apart), not
  a client-input problem, so it maps to a 500 with a fixed generic detail string, never echoing
  the validation error's content (which could include a fragment of the stored words/lyrics data).
- This endpoint also mints the three presigned stem URLs (Decision 4) and returns them alongside the
  validated document, not inside it (see Decision 3 for why they're kept out of the versioned shape).

## Decision 3: `karaoke.json` v1 — nested document, assembled fresh, resolving M5's flat-vs-nested tension

M5's design spec showed an *illustrative* nested shape (`"pitch": {"model": ..., "hop_ms": ...,
"frames": [...]}`) but stored flat DB columns instead, explicitly leaving the reconciliation to
whichever milestone reads the data. M6a resolves it here: DB storage stays exactly as M5 built it
(no migration needed), and the nested `karaoke.json` shape is assembled fresh on every `GET`:

```json
{
  "schema_version": 1,
  "track_id": "...",
  "words": [
    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9}
  ],
  "pitch": {
    "model": "tiny",
    "hop_ms": 10,
    "frames": [
      {"time_ms": 0, "hz": 220.1, "confidence": 0.87}
    ]
  },
  "tempo_bpm": 128.4,
  "beats_ms": [0, 469, 938],
  "sections_ms": [0, 18200, 41500]
}
```

`hop_ms` is `packaging.py`'s `CREPE_HOP_MS` constant (10), not stored per-row today — it's a
module-level constant in the packaging code, so the assembly step reads it from there rather than
duplicating the number. If a future milestone ever makes the hop length configurable per package,
that's a real schema/migration change (per `CLAUDE.md`'s "any shape change needs a migration path"
rule) — out of scope here, where it's still a fixed constant.

The presigned stem URLs are deliberately **not** part of this document. They're ephemeral
(time-limited, session-specific accessors), not content — embedding them in the versioned
`karaoke.json` shape would mean the "same" document looks different on every fetch and can't be
validated against a stable notion of what the artifact *is*. The API response wraps both:

```json
{
  "karaoke": { /* the schema-validated v1 document above */ },
  "stem_urls": {"drums": "/tracks/{id}/stems/drums", "bass": "/tracks/{id}/stems/bass", "other": "/tracks/{id}/stems/other"}
}
```

(Field name `stem_urls` is kept even though these are now same-origin API paths rather than presigned
URLs — see Decision 4's correction — since the frontend still treats them identically: paths to
`fetch()` audio bytes from, resolved against the same `NEXT_PUBLIC_API_BASE_URL` every other
`apps/web/lib/api.ts` call already uses.)

The JSON Schema itself lives in a new small module, `services/api/app/karaoke_schema.py`, as a plain
Python dict (`KARAOKE_SCHEMA_V1`) — no separate `.json` asset file, since the schema is code-adjacent
to the one function that both produces and validates against it, and this repo doesn't otherwise ship
non-Python data files from `app/`.

## Decision 4 (corrected during plan-writing): stem audio proxied through FastAPI, not presigned MinIO URLs

**Correction to this spec's original design**, found while gathering context for the implementation
plan: the original text of this decision assumed presigned MinIO URLs were an already-proven pattern
in this codebase (extrapolating from the architecture doc's description of "presigned uploads").
Checking the actual code found that's false — `upload_track` takes a normal multipart POST through
the API; no presigned URL of any kind has ever been used anywhere in this project, and MinIO has no
browser CORS policy configured for `localhost:3000`. FastAPI, by contrast, already has working,
tested CORS for exactly that origin (`services/api/app/main.py`, added in M4b). Standing up new,
unverified MinIO CORS configuration for this milestone would be a real, unnecessary risk when a
lower-risk path — reusing infrastructure this project has already proven — is available. Confirmed
with the project owner before locking this in.

**Revised decision:** a new endpoint, `GET /tracks/{track_id}/stems/{stem_type}`, gated by the same
tenant-ownership check every other endpoint performs (404 before any storage call), restricted to
`stem_type in ("drums", "bass", "other")` (a 404 for `vocals` or any other value — M6a doesn't serve
vocals, per Decision 1), returns the raw audio bytes directly (`Response(content=data,
media_type="audio/wav")`, reusing `fetch_track_file()`'s existing full-bytes-in-memory pattern rather
than introducing a new streaming convention this codebase doesn't otherwise use). `GET /package`'s
response no longer mints or returns presigned URLs; instead it returns same-origin API paths the
browser can `fetch()` directly (already covered by the existing `CORSMiddleware` config), e.g.
`{"drums": "/tracks/{id}/stems/drums", ...}`. `CLAUDE.md`'s "never log raw audio... or signed URLs"
rule is satisfied more simply this way — there's no signed URL to accidentally log in the first
place, since the audio route requires the same per-request auth headers as every other endpoint
rather than embedding a bearer-token-like signature in the URL itself.

## Decision 5: frontend — `/tracks/[id]/play`, generate-on-demand, split-view layout

New Next.js page, following M4b's established `apps/web` conventions exactly (dev-only
`X-Dev-Tenant-Id`/`X-Dev-User-Id` identity headers via `apps/web/lib/api.ts`, `PageProps<'/tracks/
[id]/play'>` + `use(props.params)`, same fetch-wrapper pattern). Linked from both `/tracks` (the list
page) and `/tracks/[id]` (the correction editor page), so a track's package/player is reachable from
wherever the user already is.

Two states, checked in this order (mirroring the correction editor's own 404-then-degrade pattern):

1. **No package yet** (`GET /package` returned 404): show a "Generate karaoke package" button. Click
   → `POST /tracks/{id}/package` (already exists from M5) → on success, re-fetch `GET /package` and
   move to state 2. No polling loop needed since the POST call itself blocks until packaging
   completes or errors (same synchronous request/response shape as `/separate`, `/transcribe`,
   `/realign` already use).
2. **Ready**: the actual player — layout per the approved split-view mockup (lyrics strip on top,
   pitch-lane graph below, roughly equal visual weight). Playback controls: play/pause and a seek
   bar are the only controls in M6a (no mixer, no transposition — those are M6b). If every word's
   `text` is `null` (the lyrics-withheld case — directly readable from the fetched document itself,
   no second API call needed), the lyrics strip shows a brief "lyrics not available for this track"
   note instead of blank text, but everything else (audio, pitch lane, playback) works identically.

**Correction to this spec's original design**, found while gathering context for the implementation
plan: the original text described a third state, "non-English," reusing M4b's correction-editor
banner. That doesn't apply here — `language` is a `Transcription` field, never copied onto
`KaraokePackage`/`karaoke.json`, and nothing in the player's feature set (playback, word highlight,
pitch lane) depends on it; English-only was specifically a restriction on M4b's *text-editing* UI,
not on playback. Adding a second `GET /transcription` call solely to power a banner that gates
nothing here would be unjustified scope. Dropped.

**Sync mechanism:** on play, all three `AudioBufferSourceNode`s start at the same
`audioContext.currentTime + small offset` (so `start()` calls landing in the same JS tick stay
sample-aligned). A `requestAnimationFrame` loop reads `audioContext.currentTime` each frame,
binary-searches `words` for the currently-active word (by `start_ms`/`end_ms`) to drive the
highlight, and moves a playhead line across the pitch-lane SVG using the same current-time value
against `pitch.frames`. Seeking stops all three sources, recomputes each one's `start(0, offsetSeconds)`
call (Web Audio's buffer-offset start, not a naive stop/restart-from-zero), and restarts them
sample-aligned at the new position.

## What M6a builds

1. `services/api/app/karaoke_schema.py` — `KARAOKE_SCHEMA_V1` (JSON Schema dict, Draft 2020-12).
2. `GET /tracks/{track_id}/package` and `GET /tracks/{track_id}/stems/{stem_type}` on
   `services/api/app/routes/tracks.py` — assembly, validation, and stem-audio proxying, per
   Decisions 2-4.
3. `apps/web/app/tracks/[id]/play/page.tsx` — the player page, three states, per Decision 5.
4. `apps/web/lib/api.ts` additions: `getPackage()`, `generatePackage()` (thin wrappers matching the
   file's existing `getTranscription()`/`realignTrack()` pattern), plus new TS interfaces mirroring
   the `karaoke.json` v1 shape.
5. A small player module (`apps/web/lib/player.ts` or similar — exact file boundary decided in the
   plan) encapsulating the Web Audio setup/sync logic described in Decision 5, kept separate from the
   page component so the sync math is unit-testable independent of React rendering.
6. Links from `/tracks` and `/tracks/[id]` to the new `/tracks/[id]/play` route.

`pyproject.toml`: add `jsonschema>=4.26` to `dependencies`.

## Testing strategy

Per the working agreement (`docs/PLAN.md`: "test-first for the rights gate, the alignment engine, and
the upload handler; UI and glue code are exempt"), the backend endpoint is test-first; the player page
and Web Audio sync logic are UI/glue code, exempted the same way M4b's editor page was — verified via
a real live browser session instead (per this session's established personal-verification habit), not
skipped silently.

**Backend (`services/api/tests/test_tracks_package_get.py`, test-first):**
- Happy path: real `POST /package` first (using the existing real-pipeline test infra from M5's
  `test_tracks_package.py`), then `GET /package` returns 200 with a document that validates against
  `KARAOKE_SCHEMA_V1` (asserted directly in the test, not just trusted from the endpoint), plus three
  `stem_urls` keys; a follow-up `GET` on one of those paths returns 200 audio bytes.
- 404 when track doesn't exist / wrong tenant (existing pattern).
- 404 when no `KaraokePackage` row exists yet, with a non-invocation guard on the stem-lookup/serving
  code path (same monkeypatch-raises-if-called pattern used throughout this project).
- `GET /stems/{stem_type}` rejects `vocals` and any unrecognized stem type with 404 (M6a never serves
  vocals — Decision 1).
- Lyrics-withheld case: `words[*].text` are `null` in the assembled document too (this is the same
  invariant M5's Task 3 fix round added test coverage for at the DB layer — this test confirms it
  survives the read-time assembly step as well, since a careless assembly function could
  theoretically reintroduce the raw text from a joined table).
- "Latest package wins" when multiple `KaraokePackage` rows exist for one track (two real `/package`
  calls, assert `GET` returns the second one's data).

**Frontend (live browser verification, not automated):**
- Real upload → separate → transcribe → package → play flow end to end in a live browser session
  (`mcp__Claude_Browser__*` tools against the real dev server, per this session's established
  pattern), checking: audio actually plays, word highlight visibly tracks playback, pitch lane
  renders and its playhead moves, seeking works, the "generate" button appears/disappears correctly,
  and the lyrics-withheld/non-English banners still render correctly for those cases.
- No console errors during the full flow.

## Out of scope for M6a

Stem volume mixing UI, key/tempo transposition (WASM Rubber Band/SoundTouch), live mic pitch capture,
pitch-matching/scoring, calibration — all M6b/M6c, not designed here. Vocals playback/toggle. Any
change to M0-M5's existing code or schema (this milestone is additive only: one new column-free read
endpoint, one new frontend route). Mobile-specific player layout (out of scope for v0.1 per
`docs/PLAN.md`). Offline/downloadable playback.
