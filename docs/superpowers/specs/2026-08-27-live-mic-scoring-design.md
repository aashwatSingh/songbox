# M6c: Live Mic Pitch Scoring — Design Spec

## Context

`docs/PLAN.md` scopes M6c as "live pitch detection in an AudioWorklet against a phone mic with
backing-track bleed (open question 3), scored against M6a's pitch contour, plus a calibration
flow." Open question 3 itself asks: "which algorithm survives a phone mic in a room with the
backing track playing out loud?" — genuinely open since the original brief, never answered with
real data.

Two things were verified before this design was written, not assumed:

- **Browser-native echo cancellation is real but not a solved answer for music.** `getUserMedia`'s
  `echoCancellation` constraint is standard and broadly supported, but MDN frames its documented
  behavior around WebRTC/system audio, and the more capable `echoCancellationMode` (`"all"` vs
  `"remote-only"`) is a Chrome-only, still-evolving proposal — not a settled, cross-browser
  guarantee that a page's own locally-played backing track gets suppressed from its own mic
  capture. It's a real mitigation to enable, not something to design around as a fix.
- **`AudioWorkletProcessor.process(inputs, outputs, parameters)` delivers exactly 128 sample
  frames per call** (verified against real MDN documentation, not assumed from familiarity) — far
  too short a window to run pitch detection directly (YIN needs at least ~2 periods of the lowest
  expected pitch; an 80Hz note needs ~551 samples at 44.1kHz). The worklet must accumulate samples
  into its own ring buffer across multiple `process()` calls and run pitch detection periodically,
  not per-block.

This milestone extends M6a's existing `/tracks/{id}/play` page rather than adding a new route —
it reuses the already-built synced-playback infrastructure (`StemPlayer`, the pitch-lane SVG, and
`findActivePitchFrameIndex`'s binary search) instead of duplicating any of it.

## Decision 1: bleed handling — mitigate and measure, don't assume solved

Per the approved scope: support both headphone and speaker users, degrading gracefully rather
than requiring headphones outright.

- Request the mic with `getUserMedia({audio: {echoCancellation: true, autoGainControl: false,
  noiseSuppression: false}})`. `echoCancellation: true` is enabled as a real, standard mitigation;
  `autoGainControl`/`noiseSuppression` are disabled because both actively distort the signal in
  ways that would corrupt pitch measurement (AGC changes level dynamically; noise suppression can
  attenuate or smear harmonic content pitch detection depends on).
- **Calibration, run once per scoring session, before singing starts:** play back ~4 seconds of
  the track's real accompaniment (via the same `StemPlayer`/decoded stem buffers M6a already
  loads) while asking the user to stay silent, and measure the mic's RMS energy during that
  window as a **bleed noise floor** specific to this track/room/device/volume combination.
- **During live scoring**, a pitch reading only counts toward the score if the mic's momentary RMS
  exceeds the calibrated floor by a margin (a tunable constant, not a validated psychoacoustic
  threshold — stated as such in code, matching M5's honesty pattern for its own tunable
  heuristics). Readings that don't clear it are excluded, not silently trusted as if they were
  clean signal. A headphone user's floor is near-zero, so nearly all their frames count.

This is a deliberate choice to answer "which algorithm survives bleed" with a **measured,
per-session confidence gate** rather than claim any specific DSP technique solves bleed outright —
no algorithm can perfectly separate a live human voice from correlated backing-track leakage
picked up by the same mic using only a handful of milliseconds of lookback, and pretending
otherwise would be exactly the kind of fabricated-confidence claim `CLAUDE.md`'s measurement
discipline forbids.

## Decision 2: real-time pitch detection — YIN in an AudioWorklet, no new dependency

A YIN-family autocorrelation pitch detector (the same class of algorithm real-time tuner apps
use), hand-written directly in a plain JS `AudioWorkletProcessor` — no new dependency, no model
download, matching this project's existing preference for direct implementations over frameworks.

Concretely: the worklet accumulates incoming 128-frame blocks into a ring buffer sized for a
~2048-sample analysis window (~46ms at 44.1kHz — long enough to resolve typical vocal
fundamentals down to ~80Hz, short enough to feel responsive). Every time the ring buffer advances
by a hop size (~512 samples, giving ~11.6ms between pitch estimates — finer time resolution than
the analysis window itself, standard practice for windowed pitch trackers), it runs YIN's
difference-function + cumulative-mean-normalized-difference steps over the current window,
finds the fundamental period via the standard absolute-threshold + parabolic-interpolation
refinement, and posts `{time: <worklet-clock seconds>, hz: <number | null>, rms: <number>}` back
to the main thread via `this.port.postMessage(...)` (`hz: null` when no clear periodicity is
found — e.g. during a breath or silence — mirroring `PitchFrame.hz`'s existing `number | null`
shape from M5/M6a's stored pitch data, for consistency).

## Decision 3: scoring — cents-based comparison against the stored contour, reusing M6a's binary search

Each live pitch reading is converted to cents relative to the currently-active target pitch frame
(`1200 * log2(liveHz / targetHz)`) — cents, not raw Hz, since pitch tolerance is logarithmic (a
semitone spans a much smaller Hz gap at low pitches than high ones; comparing raw Hz differences
would make the same actual tuning error look bigger or smaller depending on register). The
"currently active target frame" is found via the exact same `findActivePitchFrameIndex` binary
search `lib/player.ts` already implements for the pitch-lane playhead — no new lookup logic.

A live reading is scored as "on pitch" if its cents deviation falls within a tolerance band (a
tunable constant, explicitly commented as such — not a measured or validated threshold). Running
score = (frames scored on-pitch) / (frames counted at all, i.e. excluding both `hz: null` frames
and bleed-gated-out frames per Decision 1) — a plain percentage, not a weighted/curved scoring
formula (no basis exists yet to justify weighting one note or phrase over another).

## Decision 4: UI — extends the existing player page, doesn't replace it

An "Enable mic scoring" toggle added to `/tracks/{id}/play` (M6a's existing page). Clicking it
requests mic permission, then **starts playback immediately** (from the current position) rather
than a separate play-pause-then-play sequence: the first ~4 seconds of that playback session *are*
the calibration window (UI shows "stay quiet — calibrating..."), after which scoring activates and
listening continues uninterrupted — one continuous action, not three.

**Correction to this spec's original design**, found while writing the implementation plan: the
live pitch reading renders as a single moving **marker** (a small dot at the current instant),
not a second historical polyline. A live polyline would need a growing/sliding buffer of recent
(time, hz) points recomputed into a new points-string on every animation frame — exactly the
per-frame full-array-rebuild performance pattern M6a's own final review just found and fixed for
the *static* target polyline (which only needs to be memoized once, since it doesn't change
during playback). A live trace has no such fixed point to memoize against; a single marker avoids
reintroducing that cost class entirely, updates via plain `cx`/`cy` attribute changes (cheap), and
is still immediately legible — the user sees their current pitch dot relative to the stored target
line in real time, the same feedback a physical tuner or pitch app gives.

A running "X% on pitch" score displays alongside the existing playback controls. If the user
denies mic permission, or `AudioWorklet` init fails for any reason, the page falls back to exactly
M6a's existing playback-only experience — no error state blocking playback, no dead-end UI, since
mic scoring is additive, not required to use the player.

**Correction to this spec, found during the M6c final whole-branch review:** this decision's text
above says calibration "starts playback immediately (from the current position)." The actual
implementation, both before and after that review's fix round, calls `player.play(0)` — always
restarting from the beginning of the track, not wherever playback happened to be when the toggle
was clicked. The reviewer judged this the better design and confirmed it as the real, intentional
behavior (not an unnoticed deviation caught late): a calibration window measured from a fixed point
in the track is consistent every time, rather than one whose bleed characteristics vary depending
on which part of the song happens to be playing — a quiet intro vs. a loud chorus — when the user
enables mic scoring. This is what was actually built and approved through two rounds of task
review before the final review even started.

## What M6c builds

1. `apps/web/public/pitch-worklet.js` — the `AudioWorkletProcessor` subclass implementing YIN,
   registered via `registerProcessor(...)`, per Decision 2. Plain JS, no imports (worklet modules
   run in an isolated global scope separate from the app bundle).
2. `apps/web/lib/micScoring.ts` — main-thread logic: mic acquisition (`getUserMedia`), calibration
   (playing accompaniment silently-for-the-user, measuring RMS floor), the `AudioWorkletNode`
   wiring (loading the worklet module, connecting the mic `MediaStreamAudioSourceNode` into it,
   **never** connecting mic input to `audioContext.destination` — that would create audible
   feedback/howling through speakers, an explicit invariant worth stating plainly since it's easy
   to get wrong), and the cents-conversion/scoring math from Decision 3 (reusing
   `findActivePitchFrameIndex` from `lib/player.ts` — imported, not re-implemented).
3. Extensions to `apps/web/app/tracks/[id]/play/page.tsx`: the "Enable mic scoring" toggle,
   calibration UI state, the second pitch-lane polyline, and the running score display, per
   Decision 4.

No backend changes in this milestone — all processing is client-side; nothing is persisted (a
live scoring session's data isn't stored anywhere, matching that no requirement in `docs/PLAN.md`
asks for scoring history/leaderboards, which are explicitly out of scope for v0.1 per the original
brief).

## Testing strategy — and an honest limit on what can be verified without a human

Per the working agreement, this is UI/glue code (frontend, no backend changes), verified live
rather than test-first — matching M6a's precedent.

**What live browser verification (via the Browser pane tools, as used throughout M6a) can
actually confirm:** the worklet module loads without error, calibration runs and produces a
plausible RMS floor number, the mic-permission-denied and `AudioWorklet`-unsupported fallback
paths correctly leave the page in its ordinary M6a playback state, the live pitch trace renders
and updates during a real (or Chrome's fake-device-flag-driven) mic session, and the running score
updates sensibly. This proves the pipeline runs end-to-end.

**What it cannot confirm, and this needs to be said plainly:** whether the bleed-mitigation
strategy actually works well enough to be usable — i.e., a real answer to open question 3 — is not
something an automated/headless browser session can measure. That requires a real human singing
near real speakers with a real microphone, in a real room, which this controller cannot simulate.
Chrome's `--use-fake-device-for-media-stream` flag substitutes a synthetic fixed waveform for the
mic, useful only for confirming the *pipeline* runs, not for measuring real bleed survival.

Given `CLAUDE.md`'s "no fabricated accuracy... figure" rule, this milestone's `docs/BENCHMARKS.md`
entry will report the pipeline-mechanics verification honestly, and record real bleed-survival
characteristics as `TODO: unmeasured` unless the user is willing and able to do a real hands-on
test pass (singing along to a real track through real speakers, on a real device) during or after
implementation — which the plan will explicitly offer as a step, not silently skip.

## Out of scope for M6c

Scoring history/persistence, leaderboards, multiplayer/duet scoring, any backend change, any
change to M6a's core playback/lyrics/pitch-lane code beyond the additive extensions in Decision 4,
polyphonic/harmony detection (YIN assumes a single monophonic voice), any attempt at real acoustic
source separation of the mic signal (the calibration-floor gate is the only bleed mitigation
beyond browser-native AEC — a deliberate scope boundary, not an oversight), mobile-app-specific
mic handling (this is a browser feature, same v0.1 web-only scope as the rest of the project).
