# M6b: Stem Mixer + Transposition — Design Spec

## Context

`docs/PLAN.md` scopes M6b as "independent per-stem volume/mute controls (the `GainNode`s M6a's
`StemPlayer` already creates, currently fixed at gain=1, are built to take real control input
without needing rework), plus key/tempo shifting — the SoundTouch/Rubber Band-to-WASM R&D item the
risk note below originally flagged." This is the last of the three M6 sub-milestones (after M6a's
core player and M6c's live mic scoring), completing the split the project made when the original
M6 estimate ("3+ sessions... expect this to run long") bundled too much into one unit.

Two things were verified before this design was written, not assumed:

- **Rubber Band Library is GPL-licensed.** Using it would require either open-sourcing the whole
  Songbox application or purchasing a commercial license from its author — a real blocker for a
  project that has never made an open-source decision. Ruled out.
- **SoundTouchJS is a pure JavaScript library (no WASM compilation step needed at all), LGPL-2.1 /
  MPL-2.0 licensed** (both safe for closed-source use), actively maintained, and already ships as a
  ready-made `AudioWorklet` (`SoundTouchNode`, exposing `pitch`/`pitchSemitones`/`playbackRate` as
  standard Web Audio `AudioParam`s). What the project's own risk notes assumed was a "compile a C++
  library to WASM" R&D problem turns out to be "wire up an existing, well-packaged npm dependency."
  This materially lowers this milestone's risk relative to the original estimate.

This milestone extends M6a's/M6c's existing `/tracks/{id}/play` page rather than adding a new
route, and touches `apps/web/lib/player.ts`'s `StemPlayer` class directly (the only file in this
codebase that owns the Web Audio playback graph).

## Decision 1: dependency — `soundtouchjs`, one shared `SoundTouchNode`, not one per stem

Add `soundtouchjs` (or its scoped `@soundtouchjs/audio-worklet` package, whichever the
implementation plan confirms is the current recommended entry point once its real API is verified
against installed source — not assumed from search results alone) as a new frontend dependency.

The audio graph inserts **one shared `SoundTouchNode`** downstream of all three stems' existing
`GainNode`s, not three independent instances:

```
drums source -> gain_drums -\
bass source  -> gain_bass   -+-> (Web Audio sums multiple inputs automatically) -> SoundTouchNode -> destination
other source -> gain_other  -/
```

Key transposition shifts the whole mix uniformly — shifting each stem's pitch independently would
be musically meaningless (a bassline transposed differently from the drums would immediately sound
wrong). One shared node also means one worklet instance's CPU cost, not three.

## Decision 2: tempo = mirrored `playbackRate`, pitch = `pitchSemitones` layered on top

Tempo changes are NOT done via `SoundTouchNode`'s own resampling in isolation. Each stem's
`AudioBufferSourceNode.playbackRate` is set to the tempo multiplier (speeding up or slowing down
buffer readout — which naturally raises or lowers pitch too, exactly like a phonograph), and
`SoundTouchNode.playbackRate` is mirrored to the same value, telling the pitch-correction stage to
cancel out that accidental pitch shift, leaving only the intended tempo change. An independent key
transposition is then layered on top via `SoundTouchNode.pitchSemitones`, which is applied
regardless of what `playbackRate` is doing. This is the standard technique for tempo-independent
pitch shifting (and vice versa) and is why a phase-vocoder-class library is needed at all — naive
resampling alone can't decouple the two.

Both `playbackRate` and `pitchSemitones` are standard `AudioParam`s, live-adjustable on
already-running nodes — no stop/restart of the underlying source nodes is needed to change tempo
or key mid-playback, only a live parameter update plus the position-bookkeeping correction in
Decision 3.

## Decision 3: "song time" vs. wall-clock time — the real synchronization problem this milestone introduces

Today, `StemPlayer.currentTimeSeconds` is `startedAtOffsetSeconds + (context.currentTime -
startedAtContextTime)` — a direct 1:1 mapping from elapsed wall-clock time to song position, valid
only because playback has always run at exactly 1x. Once tempo is adjustable, that mapping breaks:
at a 1.2x tempo, one real second advances the song by 1.2 song-seconds. Every consumer that
currently reads `currentTimeSeconds` and treats it as song position — the word-highlight lookup,
the pitch-lane playhead, and (from M6c) the mic-scoring target-pitch lookup — would silently
compare against the wrong point in the stored contour/lyrics the moment tempo isn't 1x.

**Resolution:** a live tempo change is treated as a lightweight internal seek, reusing the exact
re-anchoring pattern `seek()` already has — capture the current song position (computed under the
*old* tempo), update the stored tempo multiplier, reset `startedAtContextTime`/
`startedAtOffsetSeconds` to that captured position, and *then* apply the new tempo to the live
`AudioParam`s. `currentTimeSeconds`'s formula becomes `startedAtOffsetSeconds + (context.currentTime
- startedAtContextTime) * tempoMultiplier`. Nothing downstream (word highlight, pitch-lane
playhead, mic-scoring lookup) needs to change at all — they all already consume
`currentTimeSeconds`/`player.currentTimeSeconds`-derived milliseconds as "song position," which
this fix keeps true regardless of tempo. This is a `StemPlayer`-internal correction, not a
cross-cutting change to `page.tsx` or `micScoring.ts`.

## Decision 4: mixer/transposition state must survive `play()` recreating the audio graph

**A real bug this design catches before it's built**, not a hypothetical: `StemPlayer.play()`
currently constructs fresh `AudioBufferSourceNode`s and `GainNode`s from scratch on *every* call —
including every seek and every pause/resume cycle (`seek()` while playing calls `play()` again
internally; `pause()`/subsequent `play()` don't reuse the prior `GainNode`s either). Every fresh
`GainNode` is hardcoded to `gain.gain.value = 1`. If a user set a mixer volume or mute and then
seeks or pauses/resumes, their setting would silently reset to full volume with no code change
needed to reproduce it — this is not an edge case, it's the normal, expected user flow (nobody sets
a mixer level and then never touches the seek bar again).

**Resolution:** `StemPlayer` gains persistent state fields — `stemVolumes: {drums, bass, other}`,
`stemMuted: {drums, bass, other}`, `tempoMultiplier`, `pitchSemitones` — that live independently of
any particular `GainNode`/`SoundTouchNode` instance. `play()` reads this persistent state when
constructing new nodes (instead of hardcoding `gain.gain.value = 1`), and the new mixer/transpose
setter methods update both the persistent state *and* the live `AudioParam`s on whatever nodes are
currently active (so a change while playing takes effect immediately, without waiting for the next
`play()` call).

## Decision 5: UI — extends the existing player page, doesn't replace it

Two new panels on `/tracks/{id}/play`, below the existing playback controls (and above or beside
M6c's mic-scoring controls, whichever reads more naturally once laid out — a mockup-level detail
left to the plan, not a design fork worth a mockup session): a **Mixer** panel (one row per stem —
`drums`/`bass`/`other` — each with a volume slider and a mute toggle) and a **Transpose** panel (a
key slider in semitones, e.g. −6 to +6, and a tempo slider as a percentage, e.g. 75%–125%). Both
panels are visible and usable regardless of whether mic scoring (M6c) is enabled — this milestone
doesn't gate on or interact with mic-scoring state, it only extends the shared `StemPlayer`
instance both features already read from.

## What M6b builds

1. Add `soundtouchjs` (or the confirmed-correct scoped package) as an `apps/web` dependency.
2. Extend `apps/web/lib/player.ts`'s `StemPlayer`: the shared `SoundTouchNode` in the audio graph,
   persistent mixer/transpose state, new setter methods (`setStemVolume(stem, value)`,
   `setStemMuted(stem, muted)`, `setTempo(multiplier)`, `setPitchSemitones(semitones)`), and the
   tempo-aware `currentTimeSeconds` correction from Decision 3.
3. Extend `apps/web/app/tracks/[id]/play/page.tsx`: the Mixer and Transpose UI panels, wired to the
   new `StemPlayer` methods.

No backend changes — this milestone, like M6c, is entirely frontend.

## Testing strategy

Per the working agreement, this is UI/glue code (frontend, no backend touched), verified live per
M6a's and M6c's established precedent, not test-first.

**What live browser verification can confirm:** `soundtouchjs` loads and processes audio without
error; volume/mute sliders audibly and visibly work; a mixer setting survives a seek or
pause/resume (the specific regression Decision 4 exists to prevent — this must be explicitly
checked, not assumed fixed just because the code changed); tempo and pitch sliders produce an
audible effect; word highlighting and the pitch-lane playhead stay visually synced to the audio at
a non-1x tempo (the specific correctness property Decision 3 exists to guarantee); no console
errors across the whole flow.

**What it cannot confirm:** subjective audio quality of the pitch/tempo shifting at extreme
settings (a real product-quality question, but not something an automated check can score) — this
is a reasonable manual follow-up for the user to judge, not a blocking requirement for this
milestone the way M6c's real bleed-survival test was a blocking open question. No fabricated
quality claim (e.g. "sounds natural up to ±6 semitones") will be written anywhere without the user
actually having listened and said so.

## Out of scope for M6b

Any change to the rights gate, transcription, alignment, pitch/structure extraction, or the
backend generally. Any interaction with or change to M6c's mic-scoring code beyond both features
continuing to read from the same shared `StemPlayer`/`currentTimeSeconds`. Persisting a user's
mixer/transpose preferences across page loads (no localStorage, no backend write — matches M6c's
same "nothing about a session is stored" scope decision). Per-stem key/tempo (only a
whole-mix-uniform transposition, per Decision 1). Formant correction (SoundTouchJS exposes this as
an optional feature per its own docs; not requested, not built here — a candidate for a later
polish pass if the plain pitch-shift sounds unnatural at larger shifts, but not assumed necessary
without having listened first).
