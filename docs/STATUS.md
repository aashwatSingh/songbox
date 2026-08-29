# Status

Last updated: 2026-08-29.

## Done — M7b complete

M7b's scope (`docs/superpowers/specs/2026-08-29-rate-limits-observability-design.md`: per-IP rate
limiting, structured JSON request logging, and GPU job cost logging — the "rate limits + observability"
third of `docs/PLAN.md`'s M7, alongside M7a's retention/takedown and M7c's cloud GPU backend swap) is
built and verified end to end, across three tasks plus this final-review fix round:

What was built:
- **Structured JSON request logging** (`services/api/app/logging_config.py`'s `JSONFormatter` +
  `configure_logging()`, wired into a request-timing middleware in `app/main.py`) — one JSON log line
  per request (`method`, `path`, `status_code`, `duration_ms`, `tenant_id`, `client_ip`), never track
  content, per `CLAUDE.md`'s "never log raw audio, lyrics, or signed URLs" rule.
- **Per-IP rate limiting** (`slowapi` + Redis, `services/api/app/rate_limit.py`) on 5 routes:
  `POST /tracks/upload` (30/hour), the four GPU-costing routes `/separate`/`/transcribe`/`/realign`/
  `/package` (20/hour each), and `POST /admin/tracks/{id}/takedown` (10/minute, wired as a
  dependency ordered *before* `require_admin_key` so it throttles wrong-key brute-force attempts too,
  not just successful calls). Numbers are stated policy, not measured/tuned — there's no real traffic
  yet to tune against.
- **GPU job cost logging** (`services/api/app/job_cost.py`'s `track_job_cost` context manager, wrapping
  the four GPU-invoking route handlers' inference calls) — logs real measured `duration_seconds` and an
  `estimated_cost_usd` read from `config/gpu_costs.yaml`'s `providers` list, `null` until that stub is
  ever populated with real pricing (`CLAUDE.md`'s "no fabricated... figure" rule).

**New required environment variable: `REDIS_URL`** (defaults to `redis://localhost:6379`, matching the
`docker-compose.yml` Redis service already running for this project — nothing else in this codebase
used Redis before this milestone). This is now a **hard dependency for the entire test suite**, not
just the rate-limiting tests: `services/api/tests/conftest.py` has a new `autouse` fixture
(`_reset_rate_limits`) that calls `limiter.reset()` against the real Redis-backed counters before
*every* test, so the whole suite fails to even collect cleanly without a real running Redis instance.
CI's `api` job (`.github/workflows/ci.yml`) now starts a `redis:7-alpine` service alongside the
existing `postgres` one for exactly this reason.

**The final whole-branch review's fix round found one merge-blocking bug and three other real issues,
all fixed in one pass:**
- **Critical, the merge blocker — every rate limit was trivially bypassable.** `Limiter(...)` never set
  `key_style`, which defaults to `"url"` in the real installed `slowapi` package — this scopes each
  counter to the *literal* request path, including the `track_id` UUID embedded in it (e.g.
  `/tracks/<uuid>/separate`). Varying the `track_id` across requests gave each one its own fresh
  bucket, defeating every rate limit in this milestone, including the takedown endpoint's brute-force
  protection (M7a's whole reason for existing). Fixed with `key_style="endpoint"` on the `Limiter`
  constructor, which scopes each counter to the view/dependency function instead of the literal path.
  Two regression tests were added driving 21/11 requests with a *different* `track_id` each time
  (the exact coverage gap that let this ship — every prior test reused one fixed `fake_track_id`) —
  confirmed genuinely RED against the unfixed code (`assert 404 == 429`, `assert 401 == 429` on the
  final request) and GREEN after the fix.
- **Important — a GPU cost-lookup failure could turn a successful GPU job into a 500, or mask a real
  error.** `track_job_cost`'s `finally:` block called `estimate_cost_usd(...)` with no error handling;
  a missing `config/gpu_costs.yaml` or a malformed provider entry would crash `finally`, which could
  either 500 an otherwise-successful job or, if an `HTTPException` was already propagating through the
  `with` block, silently replace it with the cost-lookup crash. Fixed: the lookup is now wrapped so a
  pricing-data problem degrades to a `null` cost plus a `songbox.job_cost` warning log, never an
  exception — an observability shim must never change the outcome of the thing it's observing.
- **Important — `_GPU_COSTS_PATH`'s positional `parents[3]` path walk was fragile off the source
  tree.** Now overridable via an optional `GPU_COSTS_PATH` environment variable, falling back to the
  same positional walk when unset.
- **Important — `JSONFormatter` silently dropped tracebacks.** It's installed as the root logger's
  handler for the whole app, but never read `record.exc_info` — so any future `logger.exception(...)`
  call anywhere in the app (including third-party libraries like `slowapi`, which calls
  `self.logger.exception(...)` internally) would silently lose its traceback. Fixed to include a
  `formatException(record.exc_info)`-derived `exception` field whenever `exc_info` is present.

**Reverse-proxy caveat, recorded in `app/rate_limit.py`'s own comment for whenever this runs behind a
real proxy/load balancer (not yet — M7c):** uvicorn's default `proxy_headers=True` with
`forwarded_allow_ips` defaulting to `"127.0.0.1"` means `request.client.host` (what
`get_remote_address` reads) becomes the *proxy's* address for every request once a proxy is
introduced, silently turning every "per-IP" limit here into one shared global limit — and making the
access-log `client_ip` field constant and useless. Set `FORWARDED_ALLOW_IPS` to the proxy's real
address when that day comes.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Load testing and retuning the rate-limit numbers with real traffic data** — M7c, the first point
  real traffic-shaped data will exist.
- **A `/metrics` Prometheus endpoint** — nothing in this project's infrastructure consumes one yet.
- **Populating real GPU provider pricing into `config/gpu_costs.yaml`** — a data-entry task requiring
  real provider quotes, not an engineering task this milestone can do.
- **Solving `docs/PLAN.md` open question 9 (real auth)** — this milestone's per-IP keying is an
  explicit, honest workaround, not a resolution.

## Done — M7a complete

M7a's scope (`docs/superpowers/specs/2026-08-28-retention-takedown-design.md`: retention purge and
an admin-gated takedown endpoint, the "find and delete a track's data" half of `docs/PLAN.md`'s M7)
is built and verified end to end, across three tasks plus this final-review fix round:

What was built:
- **`services/api/app/deletion.py`'s `delete_track_content(session, track)`** — a shared deletion
  core reused by both features: deletes every row and object-storage blob a track owns
  (`FingerprintMatch`, `Stem` rows + their MinIO objects, `Transcription`, `KaraokePackage`, the
  original upload's MinIO object), leaving the `Track` row and its `RightsDeclaration` for the
  caller to handle, since retention purge (hard delete) and takedown (tombstone) want different
  endings.
- **`services/api/scripts/purge_expired_tracks.py`** — a standalone script (no in-process scheduler
  exists in this project) that hard-deletes tracks whose `status` is still `pending_review` or
  `rejected` — i.e. never passed the rights gate — older than `RETENTION_WINDOW_DAYS = 30`, a
  stated policy choice, not a measured or compliance-reviewed number. Must be invoked as `cd
  services/api && python -m scripts.purge_expired_tracks` — **not** `python
  scripts/purge_expired_tracks.py`, which cannot import `app.*` in this environment (no in-process
  scheduler is wired to run this automatically; an operator or an external OS-level scheduled task
  must trigger it).
- **`POST /admin/tracks/{track_id}/takedown`** (`services/api/app/routes/admin.py`) — gated by a
  new `X-Admin-Key` header (`app/auth.py`'s `require_admin_key`, constant-time comparison via
  `secrets.compare_digest`, fail-closed 500 if `ADMIN_API_KEY` is unset), tombstones a track
  (`status="taken_down"`, `takedown_reason`, `takedown_at`) while removing its content via the same
  `delete_track_content()` core. **A new required environment variable, `ADMIN_API_KEY`, is not
  present in `docker-compose.yml` or any `.env.example`** — an operator must set it themselves
  before this endpoint will accept any request at all.

118 tests pass; `ruff check .` and `mypy app` (strict) both clean in `services/api`.

A final whole-branch review found 8 real issues, all fixed in one pass:
- **The merge blocker — an orphaned-data correctness gap.** `confirm-attestation`'s "stronger
  attestation" `RightsDeclaration` row (Lane A holds) is deliberately never repointed at by
  `Track.rights_declaration_id` (see the comment in `confirm_attestation()`), which meant
  `purge_expired_tracks()` had no way to find it — it carries `attestation_text`/`user_id`/
  `ip_address` and was orphaned forever once its track was purged, in a milestone whose entire
  purpose is deleting exactly this kind of data. Fixed with a new, additive migration (`0008`)
  adding a nullable `RightsDeclaration.track_id` column, set only on supplementary declarations
  like this one; the purge script now finds and deletes them (before the `Track` row, since this FK
  points the opposite direction from the original declaration) alongside the original.
- **`require_admin_key` crashed with 500 on a non-ASCII `X-Admin-Key` header** — Starlette decodes
  headers as latin-1, and `secrets.compare_digest` raises `TypeError` on a non-ASCII `str`. Fixed by
  comparing `.encode()`d bytes instead of `str`.
- **The admin router's auth gate was attached per-route, not per-router** — `router =
  APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])` now gates by construction,
  so any future route added to this file is safe by default; the external path
  (`/admin/tracks/{track_id}/takedown`) is unchanged.
- **No test covered a takedown of a nonexistent track.** Added
  `test_takedown_404s_for_unknown_track` to `test_admin_takedown.py`, with the same
  non-invocation-guard pattern the file's other negative-path tests use.
- **Docs-only — the purge script's docstring didn't state its real invocation command**, and
  documenting it as every sibling script in `docs/BENCHMARKS.md` is documented
  (`python scripts/<name>.py`) actually fails. Fixed with an explicit `Run as:` line.
- **Docs-only — no STATUS.md entry for this milestone.** This entry.
- **Minor — `app/deletion.py` didn't state that object-storage deletes aren't transactional with
  the DB.** MinIO object deletion happens before the caller's transaction commits, so a later commit
  failure rolls back the DB rows but not the already-deleted storage objects — the correct
  fail-safe direction for a deletion feature, now stated explicitly in the docstring.
- **Minor — the purge script's docstring didn't state that the whole sweep runs in one
  transaction.** A failure partway through a run rolls back all prior deletions in that run (while
  their already-removed MinIO objects stay removed), and a very large backlog means one long-held
  transaction — noted as a known characteristic at current scale, not fixed here.

**A known, honest limitation, not a bug to fix in this milestone:** the takedown endpoint records
*why* and *when* a track was taken down, but not *who* performed it. `X-Admin-Key` is a single
shared static key (Decision 4 of the design spec) — there is no per-actor identity or audit log
behind it, only the same coarse yes/no gate every caller shares.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Rate limits and observability** (M7b) and **the cloud GPU backend swap + real no-egress sandbox
  validation + load test** (M7c) — the other two M7 sub-milestones.
- **Real production scheduling for the purge script** (a hosted cron, a scheduled serverless
  function) — deferred until this project has real deployment infrastructure at all.
- **Notifying a track's owner that their content was taken down** — a real product feature, not
  built here.
- **Retention policy for `passed` tracks or inactive accounts** — no real account-lifecycle
  infrastructure exists to hang that policy on yet.

## Done — M6b complete

M6b's scope (the stem mixer and key/tempo transposition, the last of the three M6 sub-milestones)
is built and verified end to end, across two tasks:

What was built:
- **`apps/web/lib/player.ts`'s `StemPlayer` extension** (Task 1) — real per-stem volume/mute
  control (`setStemVolume`, `setStemMuted`) on the `GainNode`s M6a's player already created, and
  independent key/tempo transposition via `@soundtouchjs/audio-worklet`'s `SoundTouchNode`
  (`init()`, awaited once before the first `play()`; `setTempo(multiplier)` mirrors the multiplier
  onto both each source's `playbackRate` and the shared `SoundTouchNode`'s `playbackRate`;
  `setPitchSemitones(semitones)` sets `SoundTouchNode.pitchSemitones` independently of tempo). The
  real fix in this task: mixer/transpose state (`stemVolumes`, `stemMuted`, `tempoMultiplier`,
  `pitchSemitones`) now lives on the `StemPlayer` instance itself, independent of any particular
  `GainNode`/`SoundTouchNode`, and `play()` reads it instead of hardcoding gain=1/tempo=1/pitch=0 —
  without this, any mixer or transpose setting would have silently reset the moment the user
  touched the seek bar, since `play()` recreates every source/gain node on every seek (the same
  pattern M6a's `StemPlayer` already had). `setTempo()` also re-anchors the position-tracking
  bookkeeping (`startedAtOffsetSeconds`/`startedAtContextTime`) so `currentTimeSeconds` stays
  continuous across a live tempo change, the same mechanism `seek()` already used internally.
- **`apps/web/app/tracks/[id]/play/page.tsx` integration** (Task 2) — a Mixer panel (per-stem
  volume slider + mute button for drums/bass/other) and a Transpose panel (Key slider, -6 to +6
  semitones; Tempo slider, 75% to 125%) added below the existing mic-scoring controls, wired to
  the four new `StemPlayer` methods via `handleStemVolumeChange`/`handleStemMuteToggle`/
  `handleTempoChange`/`handlePitchChange`. `ensurePlayerLoaded` now awaits `player.init()` right
  after construction and seeds the freshly-created player with whatever mixer/transpose values the
  user already touched via the UI before ever clicking Play — otherwise those settings would exist
  only in React state with nothing to apply them to until the next render.

`npm run build`, `npm run lint`, and `npx tsc --noEmit` all clean in `apps/web`.

Live-browser-verified against real running servers (a real generated 8-second test package, since
this track's synthetic stems have no lyric words — `words: []` — but do have a full-length pitch
contour, sufficient to exercise the pitch-lane playhead/tick() chain even without word-highlight
text to look at):
- Ordinary playback (play/pause/seek), the pitch-lane playhead, and the mic-scoring toggle all
  unregressed: enabling mic scoring hit the same `getUserMedia` permission gate as every prior
  verification pass in this sandboxed browser environment (mic access is blocked in the Browser
  pane's automated tooling), exercising the established fallback path — `micError` renders
  "Permission denied", the mic flow resets to idle, and the Mixer/Transpose panels remained fully
  functional and untouched by the failed mic flow. Tempo changes were **not** exercised
  simultaneously with mic scoring active, since mic access could not be granted in this
  environment at all — the same limitation M6c's own verification recorded.
- The Mixer and Transpose panels render below the existing controls, both in their default state
  and reflecting live changes (screenshot-confirmed: a lowered Drums slider, a red "Muted" Bass
  button, Key at "+6", Tempo at "110%").
- **The tempo-sync check** (the property `setTempo()`'s re-anchoring logic exists to guarantee,
  exercised here for the first time through the page's real `tick()`/RAF loop reading
  `player.currentTimeSeconds`, not just Task 1's direct `StemPlayer` console script): a
  high-frequency in-page sampling script (bypassing MCP round-trip latency, which is far too
  coarse for an 8-second track) measured the seek bar's displayed position against
  `performance.now()` across a tempo change from 100% to 75% and then to 125%, mid-playback.
  Measured results: at 100% tempo, position advanced 0.6s over 602ms real time (0.997x); at 75%,
  0.3s over 401ms (0.748x) then 0.3s over 401ms again (0.748x); at 125%, 0.5s over 400ms (1.250x)
  then 0.5s over 404ms (1.238x) — matching each set tempo almost exactly. Critically, position was
  identical in samples taken immediately before and immediately after each tempo change (e.g.
  0.9s both 2ms apart around the 75% change; 1.5s both 2ms apart around the 125% change) —
  confirming no discontinuity/jump at the moment of change. A second run confirmed mixer settings
  (drums volume 0.3, bass muted) and transpose settings (tempo 110%, key +3) set **before the
  first Play click** were correctly seeded into the freshly-constructed player (`ensurePlayerLoaded`
  Step 1) — the button transitioned from "Play" to "Pause" and position began advancing with zero
  console errors — and that the same drums/bass/tempo/key settings were still in effect,
  unchanged, immediately after seeking to a new position (the exact regression Task 1's persistent-
  state design targets, now confirmed at the UI-wiring level, not just via Task 1's direct
  `StemPlayer` test). The Key slider was also confirmed to change independently of Tempo (dragging
  Key from -4 to +6 left the Tempo value unchanged at 110 throughout).
- **Subjective audio quality was not evaluated by ear** — this is a headless/automated browser
  environment with no real audio output to listen to, so "audibly different" claims are not made
  here at all; verification relies entirely on the precise timing measurements above and the
  absence of runtime/console errors, per `CLAUDE.md`'s measurement-discipline rule against writing
  a plausible-looking but unmeasured claim.

**A real bug was found during this milestone's live verification, and fixed within the same
milestone (not left as follow-up work):** every direct navigation to `/tracks/{id}/play` — a hard
reload, a typed URL, a bookmark, a shared link, i.e. anything that isn't a client-side `<Link>`
transition from an already-loaded page in the same app — returned an **HTTP 500** from the Next.js
server. Root cause: `@soundtouchjs/audio-worklet` defines `class SoundTouchNode extends
AudioWorkletNode` at module top level, and `apps/web/lib/player.ts`'s top-level `import {
SoundTouchNode } from "@soundtouchjs/audio-worklet"` (added in this milestone's Task 1) pulled that
class declaration — which evaluates its `extends` expression immediately, even without ever
instantiating the class — into the module graph `page.tsx` imports. Next.js still evaluates a
`"use client"` page's module graph on the server for the initial HTML render, and `AudioWorkletNode`
doesn't exist in that server (Node.js) environment, so the import threw `ReferenceError:
AudioWorkletNode is not defined` during that render. **Verified in both dev (`next dev`) and a real
production build** (`next build && next start`, a real GET against the built server) — this was not
a dev-mode-only quirk. In this project's dev environment, Fast Refresh happens to recover the page
purely client-side after the crash (the client bundle loads separately and `AudioWorkletNode` does
exist in the browser), which is almost certainly why this went unnoticed through Task 1's own
verification (a direct `StemPlayer` console script, never a rendered page) and this milestone's own
client-side-navigation-based testing above — but the raw HTTP response for any hard/first visit was
still a 500, and Fast Refresh is a dev-only safety net with no production equivalent.

**Fix**: `apps/web/lib/player.ts`'s top-level value `import { SoundTouchNode } from
"@soundtouchjs/audio-worklet"` was replaced with a type-only `import type { SoundTouchNode } from
"@soundtouchjs/audio-worklet"` (erased at compile time, cannot execute the package's module code)
plus a dynamic `const { SoundTouchNode } = await import("@soundtouchjs/audio-worklet")` inside
`StemPlayer.init()` — the one method that only ever runs client-side, after user interaction,
never during SSR. Re-verified for real: a genuine `curl -i` against a real `next build && next
start` production server on `/tracks/{id}/play` returned `200 OK` (was `500`); a fresh-tab hard
browser navigation showed no error; the Mixer/Transpose feature itself was re-confirmed working
after the fix (Play, Tempo change, Key change, zero console errors).

**The final whole-branch review found two more real, cross-cutting bugs, both fixed in a
subsequent round:**
1. **`SoundTouchNode` adds ~132ms of unconditional playback latency** (measured via a real
   `OfflineAudioContext` test against the vendored worklet processor, present even at default
   settings) — `StemPlayer.currentTimeSeconds` reflected when audio was *scheduled*
   (`context.currentTime`), not when it was actually *heard*, silently leading word highlighting,
   the pitch-lane playhead, and M6c's mic-scoring target lookup by that amount. Fixed by
   subtracting a measured `SOUNDTOUCH_LATENCY_SECONDS = 0.132` constant once, inside
   `currentTimeSeconds`'s getter, so every consumer benefits automatically. The fix round caught
   and corrected a real bug in the review's own literal suggested code along the way: naively
   re-anchoring `pause()`/`setTempo()` against the newly-compensated public getter (rather than an
   internal uncompensated value) would have caused a permanent, compounding ~132ms position drift
   on every tempo change and an audible rewind on every pause/resume — independently verified
   (both the bug and the fix) by executing the real compiled class against a controlled fake clock
   and a real browser `AudioContext`.
2. **Mic-scoring's target pitch was never transposed** — a non-zero Key setting shifted the
   audible backing track but not the stored contour `tick()` compared the singer's live pitch
   against, so any Key ≠ 0 scored a perfectly in-tune singer near 0%. Fixed by multiplying the
   target Hz by `2^(pitchSemitones/12)` before scoring, read from a ref (not React state) for the
   same closure-staleness reason `micActiveRef`/`durationSecondsRef` already exist in this file.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Persisting mixer/transpose settings** across page loads or sessions — both live only in this
  page's React state for the current session, same as M6c's `ScoreTracker` percentage.
- **Tuning the Tempo/Key slider ranges** (75-125%, -6 to +6 semitones) against real usability
  feedback — both are the design spec's starting values, not measured/validated ones.
- **A dedicated route or persistence layer for mixer presets** — no new route was added; this
  milestone only extends the existing `/tracks/{id}/play` page.

## Done — M6c complete

M6c's scope (live mic pitch scoring, closing the mechanism half of `docs/PLAN.md` open question 3
against M6a's stored pitch contour) is built and verified for mechanics, across two tasks:

What was built:
- **`apps/web/public/pitch-worklet.js`** (Task 1) — a plain-JS `AudioWorkletProcessor` registered
  as `"pitch-detector"`, running the YIN pitch-detection algorithm on whatever audio source it's
  connected to and posting `{time, hz, rms}` messages via its `port` on a ring buffer accumulated
  across the 128-frame chunks `process()` delivers. Live-verified in Task 1 against real 440Hz/220Hz
  oscillators with sub-Hz accuracy.
- **`apps/web/lib/micScoring.ts`** (Task 2) — `requestMicStream()` (mic acquisition with
  `echoCancellation: true`, `autoGainControl`/`noiseSuppression` off so the calibration step
  measures a stable, uncompressed bleed signal); `PitchTracker` (loads the worklet module, wires
  the mic source into it — **never connected to `context.destination`**, so there is no audible
  feedback path through any open speakers — and exposes the latest reading for polling from the
  page's existing `requestAnimationFrame` loop rather than a second timing mechanism);
  `measureBleedFloor()` (a per-session calibration measuring the max RMS observed while the backing
  track plays and the singer is asked to stay quiet); `hzToCents()`/`ON_PITCH_TOLERANCE_CENTS`
  (±50 cents, a deliberately lenient, unvalidated starting tolerance); and `ScoreTracker`
  (accumulates a running on-pitch percentage, excluding — never scoring as "off-pitch" — any frame
  whose RMS doesn't clear the calibrated bleed floor plus `BLEED_FLOOR_MARGIN_RMS`, since an
  excluded frame's pitch, if any, can't be attributed to the singer vs. bleed).
- **`apps/web/app/tracks/[id]/play/page.tsx` integration** (Task 2) — an "Enable mic scoring"
  toggle (`idle` → `requesting` → `calibrating` → `active` states, with `micError` rendered
  additively alongside ordinary playback rather than as a dead end on denial/failure); calibration
  is the start of playback, not a separate step, per the design spec's corrected Decision 4; a live
  red-circle marker on the existing pitch-lane SVG once scoring is active; and a running on-pitch
  percentage. The RAF `tick()` loop reads a `micActiveRef` ref (not the `micState` React state
  value) to decide whether to record a scoring frame each animation frame — the same staleness
  class M6a's own `durationSecondsRef` was introduced to solve, since `tick()`'s recursive
  `requestAnimationFrame(tick)` call is pinned to the closure it was first scheduled from and would
  never observe a later render's `micState` flipping from `"calibrating"` to `"active"`.

`npm run lint`, `npx tsc --noEmit`, and `npm run build` all clean in `apps/web`.

Live-browser-verified for mechanics: ordinary M6a playback (play/pause/seek, word highlight, pitch
lane) confirmed unregressed; the "Enable mic scoring" button renders in its idle state; clicking it
in this sandboxed browser environment hit `getUserMedia`'s real permission gate (mic access is
blocked in the Browser pane's automated tooling — an environment limitation, not a bug), which
exercised and confirmed the intended fallback path: `micError` renders "Permission denied", the mic
flow cleanly resets to `idle` allowing retry, and ordinary playback continues completely unaffected
— no dead end, no console errors, no side effects on the rest of the page. Task 2's own pass could
not get past this permission gate, so at that point the calibrating→active transition, the
live-pitch marker, and the score-percentage readout were verified only via `hzToCents`'s and
`ScoreTracker`'s pure logic (pasted into a live `javascript_tool` console session and run against
fabricated readings): `hzToCents(440, 440) = 0`, `hzToCents(466.16, 440) ≈ 99.99` (a semitone),
`hzToCents(880, 440) = 1200` (an octave), and a `ScoreTracker` fed 6 frames (one exact match, one
~20 cents sharp, one ~204 cents sharp, one below the bleed floor, one with a null live Hz, one with
a null target Hz) correctly counted only the 3 frames with both a valid Hz pair and RMS above the
floor, scoring 2 of those 3 as on-pitch — 66.67%, exactly as the tolerance and gating logic
predict.

**The final whole-branch review's fix round went further**: rather than stop at the permission
gate, it monkey-patched `navigator.mediaDevices.getUserMedia` from the browser console (never in
committed source) to resolve with a real `MediaStream` produced by a real `OscillatorNode` through
a `MediaStreamAudioDestinationNode` — a fake *microphone*, but every downstream step is the real,
unmodified application code (`PitchTracker.init()`, the real worklet fetch and YIN algorithm, the
real `measureBleedFloor()`, `ScoreTracker`, and the real `tick()`/RAF loop). Against that fake
input, the calibrating→active transition, the "Listening..." → real-percentage transition, and the
disable-mic-scoring control were all observed live, end-to-end, not just by code inspection — see
the M6c final-review fix round's commit for the full account. This is real execution against a
synthetic signal, explicitly **not** a substitute for the real human-voice bleed-survival test
below, which remains untested.

**Real-world bleed survival — `docs/PLAN.md` open question 3 — remains genuinely unmeasured.**
This is stated plainly per `CLAUDE.md`'s measurement-discipline rule: it requires a human singing
near real speakers with a real microphone, which no automated browser session can produce. The
mechanism this milestone built (echoCancellation, per-session calibration, RMS-floor gating) is a
reasoned design, not a validated one. Recorded as a pending manual follow-up, not closed here: a
person should enable mic scoring on a real device, sing along at a normal speaker volume, and
report whether the resulting score is usable or gets swamped by bleed.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Persisting scoring sessions** — `ScoreTracker`'s running percentage lives only in page state for
  the current session; no score is written to the database or shown anywhere outside this page.
- **The stem mixer and transposition** (M6b, still not started) — untouched by this milestone.
- **Tuning `ON_PITCH_TOLERANCE_CENTS`/`BLEED_FLOOR_MARGIN_RMS` against real data** — both are
  deliberately unvalidated starting points (see `apps/web/lib/micScoring.ts`'s own comments), not
  measured constants; real tuning depends on the real bleed-survival test above.

## Done — M6a complete

M6a's scope (the core synced player — read endpoint, `karaoke.json` v1 assembly and schema
validation, and Web Audio playback with word-highlight lyrics and a pitch-lane visualization,
closing `docs/PLAN.md` open question 10) is built and verified end to end, across two tasks plus
this fix round:

What was built:
- **`GET /tracks/{track_id}/package`** (`services/api/app/routes/tracks.py`) — assembles the flat
  `karaoke_packages` columns M5 wrote into the versioned `karaoke.json` v1 document
  `docs/PLAN.md`'s original M5 wording called for and M5's design spec deliberately deferred to
  M6 (see `docs/STATUS.md`'s M5 entry and `docs/PLAN.md` open question 10). The assembled document
  is schema-validated (`services/api/app/karaoke_schema.py`) before being returned. Lyric text is
  nulled per the row's stored `lyrics_display_allowed`, same rule applied at write time in M5 and
  at read time everywhere else in this codebase. Returns 404 when no package exists yet for the
  track (the player's "not ready" state) and includes proxy URLs for each non-vocal stem rather
  than presigned MinIO URLs — a spec correction made during planning (commit `8e8ded0`) so stem
  audio stays behind the same dev-auth-stub identity gate as every other route, instead of a
  separately-scoped signed-URL trust boundary.
- **`GET /tracks/{track_id}/stems/{stem_type}`** — proxies stem audio bytes through FastAPI
  (drums/bass/other; vocals intentionally excluded, since the player never plays back the isolated
  vocal stem itself per the design spec) rather than redirecting to a presigned URL, for the same
  reason above.
- **`apps/web/lib/player.ts`** — `StemPlayer`, a small class playing the three non-vocal stems
  sample-aligned via the Web Audio API (independent `GainNode`s per stem, left at gain=1 in M6a —
  M6b's stem-mixer milestone will expose these as real controls without touching this class's
  playback logic), plus binary-search helpers (`findActiveWordIndex`,
  `findActivePitchFrameIndex`) locating the current word/pitch-frame for a given playhead time.
- **`apps/web/app/tracks/[id]/play/page.tsx`** — the synced player page: word-by-word lyric
  highlight (or a "lyric display isn't permitted" notice when `lyrics_display_allowed` is false,
  same degraded-state pattern as the editor), an SVG pitch-lane visualization with a moving
  playhead, and transport controls (play/pause/seek) driven by a `requestAnimationFrame` loop
  reading `StemPlayer.currentTimeSeconds`.

108 tests pass (up from M5's 101); `ruff check .` and `mypy app` (strict) both clean in
`services/api`. `npm run lint` and `npx tsc --noEmit` both clean in `apps/web`.

Verified with a real live browser session against real running servers, per this project's
established UI/glue-code exemption from test-first: play/pause/seek, word-highlight advancing,
pitch-lane playhead tracking, pause-then-resume preserving position, and natural track-end
clamping/resetting were all exercised against a real generated package.

A final whole-branch review found real issues across both already task-reviewed tasks, fixed in
two rounds:

Task 2's own fix round (commit `55d774a`), before the whole-branch review:
- **An unbounded playback loop** — the `requestAnimationFrame` tick continued past a track's
  natural end in some cases, growing `currentTimeMs` past the real duration instead of stopping.
  Fixed by having `StemPlayer` fire an explicit `onEnded` callback on natural completion
  (distinguished from a manual `pause()`/`seek()` stop via a bumped `playToken`, so a stale
  callback from a torn-down session can't fire spuriously), which the page uses to cancel the RAF
  loop, flip the Play/Pause button back to "Play", and clamp the displayed playhead to the track's
  real duration.
- **Silent stem-decode errors** — a failed `decodeAudioData()` or stem fetch previously left the
  page stuck on "Loading audio..." with no feedback. Fixed to surface the error message in the
  page's existing error state.

The final whole-branch review (this fix round) found 5 more real issues, all fixed in one pass:
- **Important — `npm run lint` was red**, a whole-branch regression every prior milestone kept
  clean (this file's M4b entry records `npm run lint` passing explicitly, and CI runs it as a
  gate). The package-fetch `useEffect` in `apps/web/app/tracks/[id]/play/page.tsx` called
  `setNotReady(false)`/`setError(null)` synchronously in the effect body before the async fetch,
  tripping `react-hooks/set-state-in-effect`. Restructured so both resets happen inside the async
  `.then()`/`.catch()` handlers instead, and added a `cancelled` guard so a stale response can't
  overwrite state if `id` changes before the fetch resolves — closing a latent race the original
  version had, not just the lint error.
- **Important — `Math.max(1, ...pkg.karaoke.pitch.frames.map(...))` throws `RangeError` on a
  long-enough track.** CREPE emits a pitch frame every 10ms (`CREPE_HOP_MS` in
  `services/api/app/packaging.py`), so this pipeline's 720-second duration cap
  (`MAX_DURATION_SECONDS` in `services/api/app/fingerprint.py`) produces roughly 72,000 frames --
  above V8's function-argument-spread limit, an uncaught render-time crash reachable on any track
  well within what this pipeline accepts. Replaced with `frames.reduce((max, f) => Math.max(max,
  f.hz ?? 0), 1)` -- identical behavior (including the empty-array case, still returning `1`), no
  argument-count limit. Live-verified in a real browser console: `Math.max(1,
  ...new Array(200000).fill(0))` throws `RangeError: Maximum call stack size exceeded` in this
  project's actual browser environment (the threshold observed was between 100,000 and 200,000
  elements, higher than the commonly-cited ~65,535 figure -- engine/stack-budget-dependent, not a
  fixed constant, but the failure mode is real), while the equivalent `reduce()` ran cleanly at
  1,000,000 elements.
- **Important — the pitch-lane polyline's `points` string was rebuilt on every render at ~60fps
  during playback**, since the RAF tick loop calls `setCurrentTimeMs` on every animation frame,
  and the map+join over the full frame array (up to ~72,000 iterations / ~800KB string at the
  duration cap) was inline in JSX with no memoization -- real, measurable playback jank on
  realistic track lengths. Wrapped in `useMemo` keyed on `[pkg, durationMs, maxPitchHz]`, none of
  which change during playback (only `currentTimeMs` does, and it is deliberately not a
  dependency) -- confirmed live that the pitch lane's `points` attribute and rendering stayed
  stable across playback frames while the playhead line still moved every frame as expected.
- **Important — the pitch lane and seek bar rendered against a bogus 1ms duration until the first
  Play click.** The real duration lived only in a `useRef` populated inside `ensurePlayerLoaded()`
  (which doesn't run until the user clicks Play), so every pre-Play render computed
  `durationMs = Math.max(1, 0 * 1000) = 1`, collapsing every pitch-lane x-coordinate and giving the
  seek bar a `max={1}`. There was also a correctness gap independent of the visible bug: reading a
  ref during render has no render-triggering guarantee, and the old code only happened to
  re-render correctly because `setLoadingAudio(false)` (a real state update) fired incidentally
  afterward in the same function. Fixed with two changes: converted the real duration to
  `useState`, set from `ensurePlayerLoaded()` once the stems are actually decoded; and added an
  `estimatedDurationSeconds` fallback (`useMemo` on `pkg`, taking the max of the last pitch
  frame's `time_ms` and the last word's `end_ms`, falling back to a 1-second default only if both
  arrays are empty) used whenever the real duration isn't known yet. Live-verified: on a real
  ~5-second test track, the seek bar's `max` and the pitch lane's x-coordinate span were both
  already correct (matching the track's real duration) on first paint, before any Play click.
- **Important, docs-only — no completion record for M6a.** This entry, plus updating
  `docs/PLAN.md`'s M6 entry to record the real M6a/M6b/M6c split and mark open question 10's
  read-path sub-question resolved.

Deliberately out of scope, matching the design spec's own scope decisions:
- **The stem mixer and transposition** (M6b) — the three non-vocal stems play at a fixed gain in
  M6a; independent volume/mute controls and key/tempo shifting are M6b's job.
- **Live mic pitch scoring and calibration** (M6c) — a real mic input pipeline, scoring against the
  pitch contour, and a calibration flow, none of which M6a touches.
- **Real authentication** — `docs/PLAN.md` open question 9, unchanged by this milestone; the
  player page's raw stem fetch explicitly documents that it's still gated by the same dev-auth-stub
  identity headers as every other route, not real auth.

## Done — M5 complete

M5's scope per `docs/superpowers/specs/2026-08-23-pitch-structure-design.md` (pitch extraction via
CREPE, beat/structure detection via librosa, and a new `POST /tracks/{track_id}/package` endpoint
that persists both) is built and verified end to end, across four tasks:

What was built:
- **`karaoke_packages` table** (migration 0006) — tenant-scoped like every other table in this
  schema (`ENABLE`+`FORCE` RLS, `tenant_isolation` policy, grant to `songbox_app`), storing
  `schema_version`, a copy of the track's most recent `Transcription.words` (text nulled per
  `lyrics_display_allowed`, same rule M4a's response layer already applies, now applied at write
  time too), `pitch_model`, `pitch` (JSONB pitch-frame list), `tempo_bpm`, `beats_ms`,
  `sections_ms`, and `created_at`. Immutable, append-only, same pattern as `Transcription`.
- **`services/api/app/packaging.py`** — `synthesize_accompaniment()` (sums the three non-vocal
  stems, peak-normalized, a transient artifact never persisted to MinIO), `extract_pitch()`
  (`torchcrepe.predict` against the isolated vocals stem, frames below
  `CREPE_CONFIDENCE_THRESHOLD` marked unvoiced), and `extract_structure()` (`librosa.beat
  .beat_track` for tempo/beats, falling back to `librosa.feature.rhythm.tempo` when no onset
  signal is found, plus `librosa.segment.agglomerative` chroma-similarity section boundaries at a
  duration-derived `k`).
- **`POST /tracks/{track_id}/package`** — gated on track status `passed`, all four stems present,
  and a transcription existing, all checked before any model call, each proven by a
  non-invocation test. Runs pitch extraction and structure detection through the same
  `run_inference`/timeout-bound pattern as `/separate` and `/transcribe`.
- **`services/api/scripts/benchmark_pitch.py`** + `docs/BENCHMARKS.md`'s M5 section — real,
  CPU-measured CREPE numbers for both `tiny` and `full` against a synthetic 3-minute 220Hz tone:
  `tiny` 31.6s wall clock (5.69x realtime), `full` 587.2s (0.31x realtime), both producing 18001
  frames. This is the basis for `tiny` as the default pitch model, with `full` as an explicit
  slower opt-in.

101 tests pass (up from M4b's 90); `ruff check .` and `mypy app` (strict) both clean.

A final whole-branch review found 12 real issues across the four already task-reviewed tasks, all
fixed in one pass:
- **Critical — `test_package_rejects_track_that_has_not_passed_the_gate` didn't actually test the
  rights gate.** It uploaded a track that PASSED the gate, then asserted 409 for a different reason
  (missing stems) — a near-duplicate of `test_package_rejects_track_missing_a_stem`, leaving the
  `status != "passed"` check in `package_track` with zero real test coverage on a GPU-facing
  endpoint. Rewritten to mirror `test_tracks_transcribe.py`'s equivalent test exactly: a
  `FixtureAcoustIDClient` seeded with a real matched fingerprint holds the track at
  `pending_review`, and 409 is asserted against that genuinely-not-passed track.
- **Important — `PACKAGE_TIMEOUT_SECONDS` (1800s) was provably too small for the `full` CREPE
  model.** The measured 0.31x realtime factor against the 720s max-duration cap works out to
  roughly 2323s worst case — guaranteed to time out under the old bound. Raised to 3600s, with the
  comment citing the real numbers.
- **Important — the tempo-fallback comment overstated what `librosa.feature.rhythm.tempo` measures**
  when the primary beat tracker finds no onset content at all: in that case the fallback is
  dominated by librosa's own internal tempo prior, not real signal. Comment corrected to say so;
  behavior unchanged (a schema/provenance-flag fix was considered and explicitly deferred as
  out of scope for this round).
- **Important — the beat grid had zero real test coverage.** Every test used the sustained-sine
  `synthetic_wav` fixture, so `librosa.beat.beat_track`'s primary onset-based path never actually
  ran anywhere in the suite. Added
  `test_extract_structure_finds_beats_from_real_transients` — a synthesized click track with real
  percussive bursts — asserting genuinely non-empty `beats_ms`.
- **Important, doc-only — the design spec's JSONB-vs-MinIO rationale ("KB-scale JSON") didn't
  survive Task 4's real benchmark**, which serializes to roughly 1MB per 3-minute track (~4MB at
  the 720s cap). Not a bug (Postgres JSONB TOAST handles it), but the size claim was corrected in
  the design spec with the real number, flagged for M6's read-path planning.
- **Important, decided/deferred — `docs/PLAN.md`'s original M5 wording called for an assembled,
  schema-validated `karaoke.json` document and a read endpoint; the approved spec narrowed this to
  flat DB columns, extraction-only.** Recorded as `docs/PLAN.md` open question 10: M6 owns the read
  endpoint, JSON assembly, and schema validation.
- **Minor — `CREPE_CONFIDENCE_THRESHOLD` had no comment** explaining it's an unvalidated
  voiced/unvoiced cutoff, not a tuned accuracy figure. Comment added.
- **Minor — `synthesize_accompaniment` never asserted its own output against the project's
  44.1kHz-stereo-WAV invariant**, only that its three inputs agreed with each other. Added a
  post-write structural check mirroring `separation.py`'s pattern, since this function produces a
  new audio artifact rather than only consuming already-guaranteed-WAV input.
- **Minor — `build_package` ran the slow CREPE stage before the cheap accompaniment-validation
  stage**, so a malformed-stems error only surfaced after paying the full pitch-extraction cost.
  Reordered so `synthesize_accompaniment`'s validation runs first.
- **Minor — `schema_version=1` was a bare literal** in `package_track`. Added a module-level
  `KARAOKE_SCHEMA_VERSION` constant.
- **Minor — no NaN guard on JSONB-bound floats.** A NaN `hz` or `tempo_bpm` would serialize as the
  bare token `NaN`, which Postgres `jsonb` rejects (an unhandled 500). Added guards in both
  `extract_pitch` and `extract_structure` treating a NaN the same as the existing "no value"
  case (`None` for `hz`, `0.0`/fallback-trigger for `tempo_bpm`).
- **Minor, doc-only — `docs/STATUS.md`/`docs/PLAN.md` still described M5 as not started.** This
  entry, plus `docs/PLAN.md` open question 10 above.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Verse/chorus/bridge semantic section labels** (Decision 2) — real signal, no semantic
  classifier this project has or is building.
- **The player, and anything that reads `karaoke.json`** (M6) — including the assembled JSON
  document, schema validation, and a `GET` endpoint; see `docs/PLAN.md` open question 10.
- **Live pitch detection** — the player's problem in M6 against a live mic, `docs/PLAN.md` open
  question 3.
- **Closing the ±50ms alignment accuracy gap** (`docs/PLAN.md` open question 5) — unrelated,
  separate work.

## Done — M4b complete

M4b's scope per `docs/superpowers/specs/2026-08-23-lyric-correction-editor-design.md` (the lyric
correction editor UI and re-alignment on corrected text) is built and verified end to end, across
four tasks:

What was built:
- **`GET /tracks`** (Task 1) — a new, per-tenant, unpaginated list endpoint
  (`services/api/app/routes/tracks.py`) returning `{track_id, status, duration_seconds,
  has_transcription}` for every track belonging to the calling tenant. `has_transcription` is
  computed with one extra query collecting distinct `track_id`s from `transcriptions` for this
  tenant, not an N+1 lookup per track. Alongside it, dev-only permissive CORS middleware
  (`services/api/app/main.py`) so the Next.js dev server (port 3000) can call this API (port 8000)
  cross-origin — localhost origins only, explicitly documented as dev-only, not a hardened policy.
- **`POST /tracks/{track_id}/realign`** (Task 2) — gated, all checks before any model call,
  mirroring `transcribe_track`'s and `separate_track`'s proven pattern: track exists and belongs
  to this tenant (404), `track.status == "passed"` (409 — the rights gate, same as every mutating
  track endpoint), a `Transcription` row exists (409 — nothing to correct until `/transcribe` has
  run once), that row's `lyrics_display_allowed` is `true` (409), that row's `language == "en"`
  (409 — forced alignment is English-only), and a `vocals` stem exists (409). On pass, the
  corrected text is forced-aligned via the existing `align_words()` and written as a new,
  immutable `Transcription` row (`whisper_model="user-corrected"`, `aligner="wav2vec2"`,
  `language="en"`) — never a mutation of the prior row, matching this schema's established
  append-only pattern for `RightsDeclaration` rows.
- **Frontend API client** (Task 3) — `apps/web/lib/api.ts`, the first real code in `apps/web`.
  Centralizes the base URL, response parsing, and dev-only client-side identity: a random
  `tenant_id`/`user_id` pair generated on first load and persisted in `localStorage`, sent as the
  same `X-Dev-Tenant-Id`/`X-Dev-User-Id` headers every other client of this API (curl, pytest) has
  always used. Explicitly documented at that call site as **not real authentication** — see
  `docs/PLAN.md`'s open question 9 below, added by this same review pass since the design spec's
  Decision 1 required the gap to be recorded there, not silently assumed solved. A `/tracks` list
  page (`apps/web/app/tracks/page.tsx`) renders every track's status, linking into the editor only
  for tracks with `has_transcription === true`.
- **`/tracks/[id]` editor page** (Task 4) — `apps/web/app/tracks/[id]/page.tsx`. Three states:
  editable text-only correction (one input per word, joined with spaces and posted to
  `/realign` on save, per Decision 3 — no manual timing-boundary dragging), a locked banner when
  `lyrics_display_allowed` is `false` (Decision 6), and a locked banner when the track's language
  isn't English (Decision 5), both showing the existing words/timings read-only rather than an
  edit form.

90 tests pass (up from M4a's 81); `ruff check .` and `mypy app` (strict) both clean in
`services/api`. `npm run build` and `npm run lint` both clean in `apps/web`.

Verified with a real live browser session against real running servers — not just automated
tests, since UI and glue code are exempt from test-first per the working agreement: a full
upload→approve→separate→correct→re-align round trip was run end to end, including both
locked-banner states (lyrics-not-allowed and non-English), with no console errors.

A final whole-branch review found 9 real issues, all fixed in one pass:
- **Important — `docs/PLAN.md` never recorded the real-auth open question the design spec's
  Decision 1 explicitly required** ("real auth stays a genuine, tracked gap ... not silently
  assumed solved"). Added as open question 9: no milestone anywhere in the plan scopes real auth,
  every endpoint still uses the M1 dev-only header stub, and M4b made that stub reachable from a
  browser for the first time without changing that fact.
- **Important — `docs/STATUS.md` and `docs/PLAN.md` still described M4b as not started** even
  though it had been built. Both updated — this entry, and `docs/PLAN.md`'s M4b milestone line.
- **Important — `POST /tracks/{id}/realign` was missing a test for `track.status != "passed"`**,
  the single most safety-relevant gate in this codebase per `CLAUDE.md` ("Nothing reaches a GPU
  without a rights-gate PASS"), even though the design spec's testing strategy named all four gate
  rejections. Added `test_realign_rejects_track_that_has_not_passed_the_gate` in
  `services/api/tests/test_tracks_realign.py`, mirroring the existing non-invocation pattern in
  that file and the analogous tests in `test_tracks_transcribe.py`/`test_tracks_separate.py`.
- **Important — no test covered the CORS middleware**, even though the design spec's testing
  strategy named it explicitly. Added `services/api/tests/test_cors.py`: a lightweight preflight
  check confirming `http://localhost:3000` gets the expected `Access-Control-Allow-Origin` header.
- **Important — `RealignRequest.text` was the only unbounded, unvalidated client-controlled input
  in `tracks.py`**, unlike every other input in the file (`model_size`/`model_name` whitelists,
  the upload size cap). Added `Field(min_length=1, max_length=5000)`, plus an early rejection for
  whitespace-only text before the MinIO fetch/temp-file/lock/model-load cost that `align_words()`
  would otherwise pay before rejecting the same input, with a test proving `align_words` is never
  invoked for that case.
- **Minor — `apps/web/app/page.tsx` was still the untouched `create-next-app` template with no
  link to `/tracks`.** Added a `next/link` to the feature.
- **Minor — the "Save & re-align" button on a zero-word English transcription was enabled and
  could only ever fail** (posting empty text gets a 422). Disabled when `wordTexts.length === 0`,
  with explanatory copy.
- **Minor — the lyrics-gate stored-vs-recomputed asymmetry in `realign_track`** (the gate reads
  the prior transcription row's stored `lyrics_display_allowed`, while the new row's value is
  freshly recomputed) had no comment explaining it was deliberate. Added one at the gate check.
- **Minor — the editor page had no way back to `/tracks` except the browser's back button.**
  Added a "&larr; Back to tracks" link near the top of each of its three content-rendering states.

Deliberately out of scope, matching the design spec's own scope decisions:
- **Manual timing adjustment.** A real, larger future feature (waveform rendering, drag
  interaction, a timing-conflict model) — Decision 3.
- **Real authentication.** A genuine, tracked gap, not solved here — Decision 1, now recorded as
  `docs/PLAN.md` open question 9.
- **Upload, separation-trigger, or transcription-trigger UI.** The editor only acts on tracks that
  already have a transcription; producing one stays an API-only operation for now.
- **`karaoke.json` packaging** (M5) and **closing the ±50ms accuracy gap**
  (`docs/PLAN.md` open question 5) — both real, separate pieces of work this milestone does not
  attempt.

## Done — M4a engineering complete, accuracy target not met

M4a's scope per `docs/superpowers/specs/2026-08-21-alignment-engine-design.md` (the alignment
engine — Whisper + wav2vec2 forced alignment — and the eval harness that proves its accuracy
against a real, independent benchmark) is built and measured end to end, across five tasks:

What was built:
- **`services/api/app/gpu_backend.py`** (Task 1) — the `local` backend's `run_inference()`
  interface `docs/adr/0001-gpu-backend-abstraction.md` describes: one process-wide inference job
  at a time, bounded by a caller-supplied wall-clock timeout. M3's `separate_audio()` call in
  `services/api/app/routes/tracks.py` was retrofitted through it (a genuine no-behavior-change
  refactor), and M4a's transcription/alignment calls use it from the start. This closes the
  ADR-0001 deferral M3 documented — that ADR now has an "M4a update" section recording it.
- **`transcriptions` table + lyric-rights resolution** (Task 2) — tenant-scoped like every other
  table in this schema, with the Lane B `covers_recording`/`covers_lyrics` distinction on
  `licenses` kept separate per `CLAUDE.md`: missing lyric clearance is a supported degraded state
  (no lyric text rendered, timings still kept), not an error.
- **`services/api/app/transcription.py`** (Task 3) — `transcribe_audio()` (faster-whisper) and
  `align_words()` (wav2vec2 forced alignment via the non-deprecated `torchaudio.functional.
  forced_align`/`merge_tokens`, `WAV2VEC2_ASR_BASE_960H`, MIT-licensed). Runs on the isolated vocal
  stem M3 produces, never the full mix, per `CLAUDE.md`.
- **`POST /tracks/{track_id}/transcribe` and `GET /tracks/{track_id}/transcription`** (Task 4) —
  gated on the track's rights-gate status being `passed` AND a vocals stem existing, both checked
  before any model load and both proven by non-invocation tests, following M3's exact proven
  gating pattern.
- **`services/api/scripts/eval_alignment.py`** (Task 5) — a real accuracy measurement against
  JamendoLyrics Multi-Lang, a Creative-Commons benchmark with independently, manually annotated
  word timings across English/German/French/Spanish — not a hand-labeled 10-track set built by
  this same team, which `docs/PLAN.md`'s original wording called for (see "Spec corrections"
  below).

81 tests pass; `ruff check .` and `mypy app` (strict) both clean.

**The measured result does not meet `docs/PLAN.md`'s own M4 acceptance criterion.** PLAN.md states:
"*Done when:* measured word-onset error is within ±50ms median." The real, measured number for the
primary production path — wav2vec2 forced alignment against known-correct reference lyrics,
English, `python scripts/eval_alignment.py base` run to completion against all 40 non-ND-licensed
JamendoLyrics tracks (en=7, es=17, fr=12, de=4 by language; the aligned-English measurement below
draws only from the 7 English tracks) — is a **68.2ms median error, 37.2% of words within 50ms**
(2,188 words across those 7 English tracks; full output and the whisper-native secondary numbers in
`docs/BENCHMARKS.md`'s M4 section). This is above the ±50ms target, not within it. Recording this
plainly, not glossing over it: the decision on whether to revisit the model/approach, adjust the
target, or something else has been made — see the "In flight" note below and
`docs/PLAN.md`'s M4a entry (commit `d3ff5ff`): merge M4a as engineering-complete with this gap
documented, tracked as `docs/PLAN.md` open question 5, not a merge blocker.

Two real spec corrections were made during this milestone, both worth recording precisely since
the second one was a mistake caught before it shipped, not a clean up-front decision:
- **The M4/M4b split.** `docs/PLAN.md`'s M4 as originally scoped bundles four separable things:
  backend ML (Whisper + wav2vec2), an accuracy-measurement deliverable, this repo's first real
  frontend surface (the lyric correction editor — `apps/web` is still the unmodified Next.js
  starter), and a re-alignment loop. `docs/superpowers/specs/2026-08-21-alignment-engine-design.md`
  split this into M4a (this milestone: the alignment engine + the eval harness that proves its
  accuracy — the measurement stays welded to the thing it measures, since the ±50ms figure *is*
  the acceptance criterion) and M4b (later: the lyric correction editor UI and re-alignment on
  corrected text).
- **The JamendoLyrics rights-claim correction.** An earlier version of the M4a design spec claimed
  the dataset's Creative Commons licensing made it a straightforward, rights-clean Lane C fit.
  Checking the dataset's actual `license_type` field before writing the eval task disproved that:
  most tracks are **CC BY-NC-ND / CC BY-NC-SA** — non-commercial, and ND ("No Derivatives")
  specifically forbids creating derivative works, which running Demucs separation and forced
  alignment on the audio is. This is not the product's Lane C path and no track from this dataset
  may ever be uploaded through `/tracks/upload` or stored via the product's own pipeline. The eval
  script was written to a hard constraint instead: `load_dataset()` downloads the full dataset
  snapshot into the local Hugging Face cache -- including the ND-licensed tracks' audio, since the
  license check can't happen before that download -- but no ND-licensed track's audio is ever
  *processed*: any `license_type` containing `"ND"` is skipped before separation, transcription, or
  alignment ever run on it, so no derivative work (which is what ND actually forbids) is created
  from it. Only a per-track *working copy* -- the file copied out to a `songbox-eval-*` temp
  directory for processing -- is ephemeral and deleted immediately after that track is scored, in a
  `finally` block; only the aggregate numbers in `docs/BENCHMARKS.md` are ever committed.

A real bug was found and fixed mid-Task-4: `transcribe_audio()` originally raised
`TranscriptionError("transcription produced no words")` whenever Whisper's `base` model detected
zero speech segments — including the legitimate case of an audio segment that genuinely has no
speech in it. This was wrong: "no speech detected" is a supported empty result, not an error
condition, consistent with how missing lyric-display rights is already handled as a degraded state
rather than a failure. Fixed in `services/api/app/transcription.py` (commit `b622e7d`) to return an
empty `TranscriptionResult` instead of raising, with two new tests covering the empty-result path
and the original happy-path test corrected to check shape rather than specific content.

Deliberately deferred, matching the design spec's own scope decisions:
- **M4b's lyric correction editor UI and re-alignment on corrected text.** No frontend work
  happened this milestone; `apps/web` is still the unmodified Next.js starter.
- **Commercially-licensed multilingual forced alignment.** `torchaudio.pipelines.MMS_FA` (the
  obvious choice — wav2vec2 trained on 1,130 languages) is CC-BY-NC 4.0, non-commercial-only, and
  Songbox is a commercial product. Non-English tracks are scored only through the whisper-native
  path in this milestone's eval, not through forced alignment. A real, open question for a later
  milestone: whether a genuinely commercially-licensed multilingual aligner exists, and if not,
  what the actual fallback story is for non-English lyrics.
- **The RQ/async job queue.** Transcription is a synchronous `POST /tracks/{id}/transcribe`, same
  shape as M3's separation endpoint — still deferred until a milestone's shape makes the real job-
  orchestration needs clear, per M3's own deferral reasoning.
- **Container-level GPU worker sandboxing.** Still M7's job per ADR-0001; the `local` gpu_backend
  built this milestone gets process-level resource limits and a caller-supplied timeout, not the
  network-egress-denial sandbox the spec's cloud backend requires.

## Done — M3 complete

M3's scope per `docs/PLAN.md` ("Demucs on the local GPU backend, segmented, four stems stored,
benchmarked — real numbers into `docs/BENCHMARKS.md`, not estimates") is met, and verified: real
CPU-measured speed numbers exist for both `htdemucs` and `htdemucs_ft`, and an end-to-end test
proves four stems are actually stored (see below).

What was built:
- `stems` table + RLS, tenant-scoped like every other rights-relevant table in this schema.
- `services/api/app/separation.py` — `separate_audio()`, a Demucs wrapper. Runs Demucs' segmented,
  overlap-crossfade mode (`split=True`, `overlap=0.25`) so memory is bounded by segment length, not
  track length; runs on GPU when available and falls back to CPU automatically otherwise. Per
  `CLAUDE.md`'s "44.1kHz stereo WAV asserted at every stage boundary" requirement, every stem is
  checked twice before the function returns: once against the model's declared sample
  rate/channel count and the in-memory tensor's channel count, and again structurally — each
  written WAV file is reopened with the stdlib `wave` module and its real on-disk
  framerate/channel count checked, raising `SeparationError` on any mismatch. `out_dir` is cleaned
  up on any failure partway through writing the four stems, so a mid-loop error doesn't leak a
  temp directory.
- `services/api/app/routes/tracks.py`'s `POST /tracks/{track_id}/separate` — gated on the track's
  rights-gate status being `passed` (409 otherwise, and a test proves `separate_audio` is never
  even called for a non-passed track, not just that the status code is right); re-detects the
  stored file's format from its actual bytes rather than trusting anything client-supplied, same
  reasoning as M2's upload path; stores all four resulting stems in MinIO and writes one `Stem` row
  each. Bounded by a 1800s (30 minute) wall-clock timeout and a process-wide `threading.Lock` so
  only one separation runs at a time in this single-process app — a second request either waits for
  the lock or gets a 503 if the wait itself times out, and a run that exceeds the wall-clock bound
  returns 504 (the background thread is left to finish on its own; CPU-bound torch inference can't
  be cancelled from Python once started).
- `services/api/scripts/benchmark_separation.py` + `docs/BENCHMARKS.md` — real, CPU-measured
  numbers for both models, median of 3 isolated runs each (a single one-shot run under system
  contention proved unreliable — see below): `htdemucs` 60.6s/2.97x realtime, `htdemucs_ft`
  252.9s/0.71x realtime, against a synthetic 3-minute 440Hz tone (no rights clearance needed).

56 tests pass (up from M2's 50); `ruff check .` and `mypy app` (strict) both clean.

A final whole-branch review (independently re-running things against a live Postgres/MinIO/ffmpeg
environment, not just reading the diff) found 11 real issues across the four already
task-reviewed tasks, all fixed in one pass:
- **Critical — the committed benchmark numbers didn't reproduce.** The original numbers (single
  one-shot run per model, made while other test runs were happening concurrently on the same
  machine) were off by 4.3x from a clean re-run (`htdemucs`: 256.6s/0.70x committed vs. 60.2s/2.99x
  re-run). Fixed by re-measuring properly: 3 runs per model in isolation, reporting the median.
  The benchmark script also leaked its `mkdtemp` stem directory on every call — two orphaned
  121 MiB temp directories from the original runs were found and removed; the script now cleans up
  after each timed run immediately.
- **Critical — CI gates were red.** A `ruff` line-length violation and a `mypy` `attr-defined`
  error (`demucs.api.save_audio` isn't actually exported from that module, only re-exported without
  being in `__all__`; the real home is `demucs.audio.save_audio`) in `separation.py`.
- **Important — the wall-clock timeout and concurrency lock above** (a real, human-made scope
  decision: build this now rather than defer it, unlike the RQ queue below).
- **Important — the end-to-end separation test never fetched stems back from MinIO**, only checked
  DB rows and the storage-key prefix — a regression (wrong bucket, zero-byte upload, wrong format)
  could have passed undetected. Fixed to fetch each stem's actual bytes and assert real 44.1kHz
  stereo WAV data, mirroring what `tests/test_separation.py` already asserts for `separate_audio()`
  directly.
- **Important — `docs/STATUS.md` and `docs/BENCHMARKS.md`'s "no GPU available" wording, and the
  ADR-0001 `gpu_backend` deferral, were all undocumented or wrong** — all three fixed as part of
  this same pass (see below).
- **Minor — the stem-writing loop's mid-failure cleanup and structural WAV assertion**, described
  above; a temp-dir leak in `tests/test_separation.py`; an unbounded `torch` version floor
  (`pyproject.toml` now pins `torch>=2.1,<3.0` / `torchaudio>=2.1,<3.0`).

Deliberately deferred, matching the design spec's own scope decisions
(`docs/superpowers/specs/2026-08-21-source-separation-design.md`):
- **RQ/async job queue.** M3 is a synchronous `POST /tracks/{id}/separate` that blocks until Demucs
  finishes, same shape as M1/M2's endpoints. Standing up real job orchestration (`workers/`,
  currently empty) before a second pipeline stage exists to reveal what that orchestration actually
  needs to handle would be premature — deferred until M4 (transcription) gives it a real second
  stage to orchestrate.
- **Quality comparison between `htdemucs` and `htdemucs_ft`.** Speed is now measured
  (`docs/BENCHMARKS.md`); quality needs a real listening test with real songs and human judgment,
  which stays `TODO: unmeasured` and out of scope for M3.
- **Container-level GPU worker sandboxing.** M7's job per ADR-0001 — M3 runs on the same
  unsandboxed local GPU backend M1/M2 always have.
- **The ADR-0001 `gpu_backend` interface itself.** M3 is the first pipeline stage with a GPU call,
  and it calls `demucs.api` directly from `services/api/app/routes/tracks.py` rather than through
  the swappable interface ADR-0001 describes. This is now written down as a deliberate,
  acknowledged deferral in `docs/adr/0001-gpu-backend-abstraction.md`'s "M3 update" — building the
  interface against a single call site risked guessing its real shape wrong; revisit once M4 adds a
  second GPU-calling stage.

Known limitations, not fixed in M3 (found during the final review's re-verification, judged too
small individually to block merge, but real):
- **A 504 (separation-timeout) response releases the lock while the runaway thread is still
  running**, since CPU-bound torch inference can't be cancelled from Python. Under sustained
  overload (only reachable past the 30-minute timeout, i.e. a machine already in trouble), a fresh
  request can acquire the lock and start a second concurrent Demucs run alongside the orphaned one,
  and the orphan's temp stem directory has nothing to clean it up. The real fix is the deliberately
  deferred RQ/worker queue above, which can actually track and cancel in-flight jobs; noting this
  now so it isn't rediscovered from scratch when that queue is built.
- **No test exercises the 503 (lock-busy) path or proves requests are genuinely serialized** — both
  were verified manually during the final review (a concurrent-request probe showed
  `max_concurrent=1` and the lock releasing correctly on both the exception and timeout paths), but
  no automated test would catch a regression that silently removed the lock.

## Done — M2 complete

M2's own "done when" criterion (`docs/PLAN.md`: a malformed-file test suite — truncated headers,
wrong magic bytes, a playlist referencing a remote URL, a duration bomb — fully rejected) is met and
actually verified, not just written — proven by `test_upload_rejects_truncated_header`,
`test_upload_rejects_wrong_magic_bytes`, `test_upload_rejects_playlist_with_remote_url`, and
`test_upload_rejects_duration_bomb` in `services/api/tests/test_tracks_upload.py`.

What was built:
- `services/api/app/validation.py` (new) — `detect_audio_format`, magic-byte detection for six
  accepted formats (WAV, FLAC, MP3, M4A, OGG, AIFF) by binary signature, never by client-supplied
  filename extension or `Content-Type`.
- `services/api/app/fingerprint.py` hardening — a 720-second (12-minute) duration cap, a 2-stream
  cap, 30-second timeouts on both the `ffprobe` and `ffmpeg` subprocess calls, and
  `-protocol_whitelist file` on both.
- `services/api/app/storage.py` — storage keys are now bare `f"{tenant_id}/{uuid4()}"`, with no
  client-supplied filename component at all (closes a gap M1's final review flagged and deferred
  here).
- A 150 MiB upload cap (`MAX_UPLOAD_BYTES` in `services/api/app/routes/tracks.py`). Starlette has
  already spooled the request body to disk before the handler runs, so the check seeks to the end of
  the spooled file to get its size in O(1), rejects with 413 before reading anything if it's over the
  limit, and only then does a single `.read()` — no chunk-and-join step, which an earlier version of
  this cap used and which actually peaked at ~2x the payload in memory, worse than the one-shot read
  it was meant to improve on. 150 MiB, not 100: a 12-minute 44.1kHz/16-bit stereo WAV — the max
  duration this pipeline accepts — is ~121 MiB, so a 100 MiB cap would reject legitimate
  maximum-length uploads.
- The wiring in `services/api/app/routes/tracks.py`: magic-byte check and size cap run before any
  subprocess is spawned, the temp file's extension is derived from the *detected* format rather than
  the client-supplied filename (a client-controlled filename previously reached the filesystem via
  `tempfile.NamedTemporaryFile`'s `suffix=`, and an over-long or OS-invalid one raised an unhandled
  500 before any cleanup could run — fixed in the final whole-branch review), and the lane-B
  `license_id` presence check now runs before fingerprinting since it needs no DB access.

50 tests pass (up from M1's 32); `ruff check .` and `mypy app` (strict) both clean.

Deliberately deferred (recorded here, as `docs/superpowers/specs/2026-08-20-hardened-ingest-design.md`
said this file would):
- **Presigned direct-to-storage upload.** NOT built. M2 keeps M1's single multipart-request shape;
  the original external build prompt called for a presign/finalize two-phase flow, but the user chose
  to keep the existing shape instead. A real, acknowledged deviation from the original spec, not an
  oversight.
- **Container-level worker sandboxing** — no network-egress denial, no seccomp, no read-only root.
  M2 only hardens the ffprobe/ffmpeg subprocess calls already living in `services/api` (argument
  arrays, protocol whitelist, timeouts). Real sandboxing is M7's job per `docs/adr/0001-gpu-backend-
  abstraction.md` (M3 added the first real GPU-calling stage, Demucs separation, but still runs it
  as a plain in-process call on the `local` dev backend, not a sandboxed worker). Per `CLAUDE.md`,
  that guarantee is validated against the real cloud (Modal/RunPod) backend, not the local dev GPU
  backend, which remains a plain subprocess/call with resource limits.
- **Loudness normalization to -14 LUFS and 44.1kHz-stereo internal-format normalization.** Neither is
  in `docs/PLAN.md`'s M2 scope or its "done when," even though the original spec's §4.1 lists both —
  deferred to whichever milestone first actually consumes a normalized format.

Forward note for M3: `CLAUDE.md` requires that all internal audio is 44.1kHz stereo WAV, asserted at
every stage boundary. M2 now accepts six different formats and stores each one un-normalized, exactly
as uploaded — and does not record anywhere which of the six a given stored object actually is. M3's
Demucs separation step will need to both normalize to 44.1kHz stereo WAV and add the assertion
`CLAUDE.md` requires, and until a format column is added to `tracks`, it will have to re-probe the
format from the stored object itself rather than reading it from a row.

## Done — M1 complete

All of M1's own "done when" criterion (`docs/PLAN.md`: "a known commercial recording uploaded under
Lane A is held, and an original recording passes") is met and actually verified, not just written —
proven by `test_lane_a_upload_of_known_commercial_fingerprint_is_held` and
`test_lane_a_upload_of_original_recording_passes` in `services/api/tests/test_tracks_upload.py`
(Task 9).

Built across 11 tasks, each test-first and reviewed:
- `services/api/app/models.py` + initial Alembic migration — `licenses`, `rights_declarations`,
  `tracks`, `fingerprint_matches` tables, every one carrying `tenant_id`.
- Row-level security enforced on all four rights-gate tables, with the app connecting as a
  non-superuser role (`songbox_app`) so RLS actually applies — `services/api/app/db.py`'s
  `db_session_for_tenant` sets `app.tenant_id` via `set_config` per-session.
- `services/api/app/auth.py` — dev auth stub (`X-Dev-Tenant-Id`/`X-Dev-User-Id` headers), wired into
  `get_db` so every request's queries are automatically tenant-scoped by RLS, not by manual
  `WHERE tenant_id = ...` filtering in route code.
- `services/api/app/acoustid/` — `AcoustIDClient` interface, real HTTP implementation, and a
  `FixtureAcoustIDClient` test double driven by `services/api/app/acoustid/fixtures.py`.
- `services/api/app/fingerprint.py` — Chromaprint fingerprinting via ffmpeg's built-in `chromaprint`
  muxer (`ffmpeg -f chromaprint`), not the separate `fpcalc` binary — one fewer external tool to
  install/pin/track CVEs on, per the design spec.
- `services/api/app/gate.py` — `resolve_lane_outcome`, the lane x match-result table: Lane A always
  holds on a match, Lane B holds unless the license on file covers the recording, Lane C always holds
  on a match (PD/CC claims need manual verification even though they might be legitimately public
  domain), no match always passes, and an AcoustID lookup error holds rather than passing silently.
- `services/api/app/storage.py` — MinIO wrapper for uploaded track files.
- `services/api/app/routes/tracks.py` — `POST /tracks/upload` (end-to-end: fingerprint, gate,
  store file, write declaration/track/match rows) and `POST /tracks/{id}/confirm-attestation`
  (Lane A's path to add a stronger, named-release attestation as evidence — a new immutable
  `rights_declarations` row, never a mutation of the original). It is deliberately NOT sufficient
  on its own to clear a hold — see "Reviewed — M1" below for why.
- `services/api/app/routes/review_queue.py` — `GET /review-queue` (lists tracks stuck in
  `pending_review`, tenant-scoped via RLS, with enough context — lane, attestation text, uploader,
  timestamp — to actually be reviewable) and `POST /review-queue/{id}/resolve` (a human reviewer
  approves -> `passed`, or rejects -> `rejected`). This is the ONLY endpoint that can move a track
  out of `pending_review` — `"rejected"` is a human-review-only status; the automated gate in
  `gate.py` never produces it itself, and neither does `confirm-attestation`.
- 32 tests across `services/api/tests/`, all passing; `ruff check .` and `mypy app` (strict) both clean.

Deliberately deferred (all listed under "Out of scope for M1" in
`docs/superpowers/specs/2026-08-19-rights-gate-design.md`, so none of this should come as a surprise
later):
- Real auth (the `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub stays until a real milestone replaces
  it).
- A real AcoustID API key (`HTTPAcoustIDClient` is implemented but untested against the live service;
  tests run against `FixtureAcoustIDClient`).
- Upload hardening — presigned uploads, magic-byte validation, ffprobe gating, sandboxed transcode —
  all explicitly M2's job.
- Rate-limiting / abuse-alerting logic.
- `key` and `tempo` track fields.
- Admin roles (the review-queue endpoints are gated only by the dev auth stub's identity, not by any
  reviewer/admin role check).
- The `jobs`, `stems`, `lyric_versions`, `word_timings`, `pitch_contours`, `takedowns` tables (later
  milestones).

## Reviewed — M1

Each of M1's 11 tasks went through a task-scoped implement-then-review gate (per
superpowers:subagent-driven-development), not just a single pass. Four real, reproducible bugs
surfaced and were fixed along the way — this wasn't a smooth, uninterrupted build, and that's the
point of the gate:

- **Task 1** — `db_session_for_tenant` originally used `SET LOCAL app.tenant_id = :tenant_id`.
  Postgres's `SET`/`SET LOCAL` grammar rejects bound parameters outright (`psycopg.errors.SyntaxError`
  on every call, reproduced live) — switched to `SELECT set_config('app.tenant_id', :tenant_id, true)`,
  which does accept one and has identical transaction-scoped semantics.
- **Task 2** — the implementer added unrequested `alembic/__init__.py` and
  `alembic/versions/__init__.py` files, and the first one shadowed the real third-party `alembic`
  package, breaking `python -m alembic upgrade head` — the exact command the plan documents. Both
  files removed; standard `alembic init` never generates them for this reason.
- **Task 3 (the significant one)** — discovered that RLS policies alone don't work: `songbox`
  (`POSTGRES_USER` in the official postgres image) is a genuine Postgres superuser, and superusers
  unconditionally bypass every RLS policy regardless of `FORCE ROW LEVEL SECURITY` — confirmed live
  (`rolsuper = t`). A restricted, non-superuser role (`songbox_app`) was added, created and granted
  table privileges by the RLS migration itself, with `db_session_for_tenant` connecting through it
  instead. The reviewer independently live-verified `current_user` via that function actually resolves
  to `songbox_app` with `rolsuper = False`, audited the exact grants (nothing broader than needed), and
  ran a full downgrade/upgrade cycle.
- **Task 9** — two bugs surfaced together in the endpoint that proves M1's own "done when" criterion:
  (1) `tempfile.NamedTemporaryFile`'s `with`-block form keeps its own handle open, which Windows won't
  let a second process (ffmpeg) also open — fixed with `delete=False` + explicit close + manual
  cleanup; (2) none of the SQLAlchemy models declare `relationship()`s, so nothing gives the ORM
  ordering guidance across FK-dependent inserts in one flush — a 3-model insert chain
  (declaration→track→match) tripped a real `ForeignKeyViolation` that a 2-model chain didn't, proving
  genuine order-dependence rather than a phantom problem. Fixed with explicit `db.flush()` calls,
  applied proactively to Task 10's near-identical insert-then-update pattern too, so it didn't recur.

Two environment issues (not code bugs) were also hit and fixed permanently rather than worked around
per-task: a native Windows PostgreSQL 18 service was silently shadowing Docker's Postgres container on
port 5432 for any `localhost` client (remapped the container to 5433); and this session's Bash tool had
snapshotted its `PATH` before ffmpeg was installed mid-session, so Bash-driven subagents couldn't find
it even though PowerShell could (fixed by placing the binaries in `C:\Users\aashw\bin`, already first on
Bash's `PATH`).

Every fix above was independently verified — either by the task reviewer re-running the failing
command live, or by the reviewer reproducing the bug from scratch in an isolated script before
confirming the fix — not accepted on the implementer's word alone.

### Final whole-branch review

After all 11 tasks passed their own task-scoped review, a final review of the whole branch together
(the kind of thing no single task's narrow diff can surface) found one Critical issue and 7 Important
ones. The Critical one took three rounds to actually close and is worth recording in full, because
each round's fix turned out to be insufficient in a way only live attack-style testing caught:

1. **Round 1 (the bug):** `confirm-attestation` unconditionally passed a held Lane A track on any
   `release_name` at all — verified live: self-confirming with `release_name="literally anything"`
   moved a track to `passed` and removed it from `/review-queue`, so no human ever saw it. This
   directly defeated `CLAUDE.md`'s "nothing reaches a GPU without a rights-gate PASS" invariant — the
   PASS was self-granted.
2. **Round 2 (insufficient fix):** added a `_release_names_reconcile` substring-matching check
   requiring the submitted name to textually overlap the AcoustID-matched release title. Re-review
   found this defeated by a single character (`release_name="a"` matches almost any title) — and,
   more fundamentally, that no string-matching approach could ever work as a security boundary here:
   M1 has no reviewer/admin role separation yet, so the uploader can read the exact `matched_release`
   value straight off `GET /review-queue` (same auth as their own upload) and echo it back.
3. **Round 3 (the actual fix):** removed the matching logic entirely. `confirm-attestation` now only
   *records* the stronger attestation as an additional immutable `rights_declarations` row — it never
   touches `track.status` or `FingerprintMatch.resolution`/`reviewer_id`. The only way a Lane A hold
   can be cleared is a human calling `POST /review-queue/{id}/resolve` (Task 11, unmodified). An
   independent re-review then live-attacked this version specifically (one-character names, the exact
   matched title echoed back, 5 repeated calls, empty/oversized/SQL-injection/mass-assignment payloads,
   cross-tenant attempts) and confirmed none of them move a track off `pending_review`, and traced
   every write to `Track.status` across the whole `routes/` package to confirm
   `review_queue.py`'s `resolve_review` is provably the only one. That same pass found one more real
   gap — `confirm-attestation` was still repointing `track.rights_declaration_id` at the new row,
   which let any same-tenant caller (not just the original uploader) silently replace the attestation
   text, uploader identity, and timestamp a future reviewer would see — fixed by leaving that FK alone;
   the evidence is still persisted, just not substituted into what the review queue shows.

The other 7 Important findings (`ffprobe` missing `-protocol_whitelist file` on one of two calls, the
AcoustID response being stored as a bare matched/error boolean instead of the real match data, the
review queue lacking enough context to actually review anything, the tenant_id/RLS tests being
hardcoded allowlists instead of deriving from `Base.metadata`, CI having no service containers to
actually run the suite against, and no indexes beyond primary keys) were fixed in one batch and
independently re-verified — including live DB reads of the new indexes and a live probe proving the
`Base.metadata`-derived tests actually catch an injected model missing `tenant_id`.

## Done — M0 complete

All of M0's own "done when" criteria are now met and actually verified, not just written:

- Repo scaffolded (`apps/web`, `services/api`, `workers`, `config`, `docs`, `docs/adr`).
- `CLAUDE.md`, `docs/PLAN.md`, `docs/DECISIONS_LOG.md`, `docs/adr/0001-gpu-backend-abstraction.md`.
- `services/api` — FastAPI health-check skeleton. `pytest`, `ruff check`, `mypy --strict` all pass.
- `apps/web` — Next.js 16 App Router + TypeScript + Tailwind shell (via `create-next-app`).
  `npm run lint` and `npm run build` both pass clean.
- `docker-compose.yml` — Postgres, Redis, MinIO. **Verified running**: `docker compose up -d` brought
  up all three containers, all report `(healthy)` after their healthchecks settled.
- `.github/workflows/ci.yml` — forbidden-deps check, API job (ruff/mypy/pytest, pip-cached), web job
  (lint/build, npm-cached). Not yet run on a remote — no GitHub remote configured yet (still open).
- `.pre-commit-config.yaml` — ruff + ruff-format on `services/api`, a local `mypy-api` hook that runs
  mypy through the project's own venv, and the forbidden-dependency guard. Not yet installed
  (`pre-commit install`) as a real git hook — still open, low priority.
- `scripts/check_forbidden_deps.py` — structurally parses manifests (pyproject.toml/
  requirements*.txt/package.json) and scans lockfiles for `yt-dlp`/`youtube-dl`/`pytube`. Regression
  tests at `scripts/tests/test_check_forbidden_deps.py`.
- `config/gpu_costs.yaml` — stub, `TODO: unmeasured`, no real pricing yet.
- **ffmpeg installed** (v9.0-full_build via winget, `Gyan.FFmpeg`) — confirmed `ffmpeg -version` and
  `ffprobe -version` both work. Compiled with `--enable-chromaprint` and `--enable-whisper`, useful
  for M1/M4 later.
- **Docker Desktop installed and running** (v4.87.0 via winget, `docker version` reports both client
  and server). All three Compose services confirmed healthy.

## Reviewed

Ran a full multi-angle code review (8 finder angles + verify pass) against the M0 skeleton. 6 confirmed
findings, all fixed:
- `scripts/check_forbidden_deps.py` — was a raw-text substring search (false-positived on comments
  mentioning banned names) that only checked manifests, not lockfiles (false-negative on transitive
  deps), and walked `node_modules`/`.venv` fully before filtering. Rewritten: structural parsing
  (tomllib/json/line-based) for manifests, substring scan for lockfiles (package-lock.json, yarn.lock,
  pnpm-lock.yaml, uv.lock, poetry.lock — safe there since they're machine-generated), and a pruning
  walk that never descends into ignored directories. Regression-tested (6 tests, stdlib unittest).
- `CLAUDE.md` — the tenant_id line falsely claimed a test currently enforces it; reworded to a
  forward-looking requirement.
- `.pre-commit-config.yaml` — the mypy hook used to pin its own independent FastAPI version, divergent
  from `services/api/pyproject.toml`. Replaced with a local hook (`scripts/run_mypy_api.py`) that runs
  mypy through the actual project venv, so pre-commit and CI type-check against identical installed
  dependencies.
- `.github/workflows/ci.yml` — added `cache: "pip"` to the api job's setup-python step, matching the
  web job's npm caching.
- All fixes verified together: forbidden-deps script clean on the real repo, its unit tests pass,
  `run_mypy_api.py` passes, API pytest/ruff/mypy all pass.

## In flight

- Nothing mid-work right now. M0, M1, M2, M3, M4a, M4b, M5, M6a, M6b, and M6c are all done — the
  full M6 milestone (all three sub-milestones) is complete. M4a's measured aligned-English accuracy
  (68.2ms median, 37.2% within 50ms) does not meet `docs/PLAN.md`'s ±50ms acceptance criterion —
  see M4a's entry below. That decision has been made, not left pending: merge M4a as
  engineering-complete with the gap documented, tracked as real follow-up work (`docs/PLAN.md`
  open question 5, commit `d3ff5ff`), not a merge blocker. Real authentication remains a genuine,
  tracked gap across the whole project — `docs/PLAN.md` open question 9. M6a closed the read-path
  sub-question of `docs/PLAN.md` open question 10 (the `GET /tracks/{id}/package` read endpoint,
  `karaoke.json` v1 assembly, and schema validation now exist). M6c built the
  live-mic-pitch-scoring mechanism (calibration, cents-based scoring against M6a's contour) but
  left `docs/PLAN.md` open question 3 (real bleed survival) genuinely unmeasured, pending a manual
  test pass with a real mic and speakers — `TODO: unmeasured`, not closed. M6b built the stem mixer
  and key/tempo transposition, closing the original M6 scope entirely.

## Blocked

- **No GitHub remote configured yet**, so `.github/workflows/ci.yml` has only been reasoned about, not
  actually run by GitHub Actions. Not blocking M4/M5/M6 work, only CI-on-push.

## Next three actions

1. Run the real manual bleed-survival test M6c's mechanism still needs: a human singing along on
   a real device with real speakers/mic, to actually measure `docs/PLAN.md` open question 3 rather
   than leave it `TODO: unmeasured`. Concrete checklist for the tester to report back, not just a
   general impression:
   - (a) The calibrated bleed-floor RMS value observed (visible via a temporary console log or
     debugger breakpoint on `bleedFloorRef.current` in `apps/web/app/tracks/[id]/play/page.tsx`
     right after calibration completes).
   - (b) `framesCounted` (same page, `scoreTrackerRef.current?.framesCounted`) after singing
     through roughly one verse — confirms frames are actually being counted, not just that the UI
     renders a plausible-looking percentage.
   - (c) Whether the score ever gets stuck showing "Listening..." (the M6c final-review fix for
     the zero-frames-counted display) rather than eventually producing a real percentage — stuck
     "Listening..." would mean the bleed floor came out too high for any live frame to clear it.
   - (d) Whether the live-pitch marker (the red dot on the pitch-lane SVG) stays visible and
     tracks roughly with the sung note, or disappears / renders out of the visible lane bounds.
   - (e) Whether the backing track's own audio audibly glitches, stutters, or drops out while mic
     scoring is active. This is a real, flagged-but-unmeasured risk from the M6c final review: the
     worklet's YIN computation is CPU-heavy and runs inside the same `AudioContext` as playback, so
     a real processing overrun could glitch the music itself, not just corrupt a pitch reading —
     this has never been exercised against a genuine simultaneous mic-capture-plus-playback session
     in this sandboxed environment, only against playback alone or a denied-permission mic flow.
2. Close `docs/PLAN.md` open question 5 (the ±50ms accuracy gap): try a larger wav2vec2 variant,
   check whether the separated vocal stem's audio quality degrades alignment precision versus the
   original mix, or investigate a systematic bias in the frame-to-millisecond conversion. Real work,
   not a merge blocker.
3. Configure a GitHub remote so `.github/workflows/ci.yml` actually runs (see "Blocked" above), and
   revisit `docs/PLAN.md` open question 9 (real authentication) whenever a milestone's scope can
   actually absorb it.
