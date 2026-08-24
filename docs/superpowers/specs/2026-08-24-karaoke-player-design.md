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
  "stem_urls": {"drums": "https://...", "bass": "https://...", "other": "https://..."}
}
```

The JSON Schema itself lives in a new small module, `services/api/app/karaoke_schema.py`, as a plain
Python dict (`KARAOKE_SCHEMA_V1`) — no separate `.json` asset file, since the schema is code-adjacent
to the one function that both produces and validates against it, and this repo doesn't otherwise ship
non-Python data files from `app/`.

## Decision 4: presigned URLs — short expiry, tenant-checked before minting, never logged

`CLAUDE.md` already states "never log raw audio, lyrics, or signed URLs" — this endpoint is the first
one to mint any, so that rule becomes load-bearing here for the first time, not just a standing
principle. The three URLs are minted only after the same tenant-ownership check every other endpoint
performs (404 before any storage call), using `Minio.presigned_get_object(bucket, storage_key,
expires=timedelta(hours=1))`. One hour comfortably covers a single listening session without the URL
becoming a long-lived credential. If a stem is somehow missing (shouldn't happen if a `KaraokePackage`
row exists, since packaging itself required all four stems — but not physically impossible if a stem
was deleted out-of-band), minting fails closed: the whole `GET /package` call raises 500 rather than
silently returning a response with fewer than three stem URLs.

## Decision 5: frontend — `/tracks/[id]/play`, generate-on-demand, split-view layout

New Next.js page, following M4b's established `apps/web` conventions exactly (dev-only
`X-Dev-Tenant-Id`/`X-Dev-User-Id` identity headers via `apps/web/lib/api.ts`, `PageProps<'/tracks/
[id]/play'>` + `use(props.params)`, same fetch-wrapper pattern). Linked from both `/tracks` (the list
page) and `/tracks/[id]` (the correction editor page), so a track's package/player is reachable from
wherever the user already is.

Three states, checked in this order (mirroring the correction editor's own 404-then-degrade pattern):

1. **No package yet** (`GET /package` returned 404): show a "Generate karaoke package" button. Click
   → `POST /tracks/{id}/package` (already exists from M5) → on success, re-fetch `GET /package` and
   move to state 3. No polling loop needed since the POST call itself blocks until packaging
   completes or errors (same synchronous request/response shape as `/separate`, `/transcribe`,
   `/realign` already use).
2. **Lyrics withheld or non-English** (visible once the package loads, from the same signal M4b's
   editor already surfaces): the player still plays audio and shows the pitch lane, but the lyrics
   strip shows the same "lyrics not available" / "non-English, editing disabled" banner styling M4b
   established, reusing rather than reinventing that UI.
3. **Ready**: the actual player — layout per the approved split-view mockup (lyrics strip on top,
   pitch-lane graph below, roughly equal visual weight). Playback controls: play/pause and a seek
   bar are the only controls in M6a (no mixer, no transposition — those are M6b).

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
2. `GET /tracks/{track_id}/package` on `services/api/app/routes/tracks.py` — assembly, validation,
   presigned URL minting, per Decisions 2-4.
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
  `stem_urls` keys.
- 404 when track doesn't exist / wrong tenant (existing pattern).
- 404 when no `KaraokePackage` row exists yet, with a "must not mint presigned URLs" non-invocation
  guard (same monkeypatch-raises-if-called pattern used throughout this project).
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
