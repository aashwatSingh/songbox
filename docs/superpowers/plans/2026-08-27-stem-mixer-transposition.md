# M6b: Stem Mixer + Transposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live per-stem volume/mute controls and independent key/tempo transposition to the
existing `/tracks/{id}/play` player, using SoundTouchJS's `SoundTouchNode` `AudioWorklet`.

**Architecture:** Extend `StemPlayer` (in `apps/web/lib/player.ts`) with a single shared
`SoundTouchNode` downstream of all three stems, persistent mixer/transpose state that survives the
class's existing recreate-nodes-on-every-`play()` pattern, and a tempo-aware `currentTimeSeconds`.
Extend the player page with two new UI panels wired to the new `StemPlayer` methods. No backend
changes.

**Tech Stack:** `@soundtouchjs/audio-worklet` v2.1.1 (MPL-2.0 license, verified — real package
installed and inspected before writing this plan, not assumed from search results). Same Next.js
16 / React 19 / TypeScript stack as the rest of `apps/web`.

## Global Constraints

- Rubber Band is ruled out (GPL license, would require open-sourcing the app or a commercial
  license). Only `@soundtouchjs/audio-worklet` is used.
- One shared `SoundTouchNode` downstream of all three stems' `GainNode`s — never one per stem.
  Transposition shifts the whole mix uniformly.
- Tempo is set via each stem source's own `playbackRate` mirrored into
  `SoundTouchNode.playbackRate`; key transposition is `SoundTouchNode.pitchSemitones`, applied
  independently of tempo.
- `StemPlayer.currentTimeSeconds` must stay correct (return real song position, not wall-clock
  time) at any tempo — every consumer (word highlight, pitch-lane playhead, M6c's mic-scoring
  target lookup) depends on this without needing to know tempo exists.
- Mixer volume/mute and tempo/pitch settings must survive a seek or a pause/resume cycle —
  `StemPlayer.play()` recreates `GainNode`s on every call; the persistent state fields (not the
  node instances) are the source of truth, applied fresh each time nodes are (re)created.
- No backend changes, no new persistence (matches M6a's and M6c's same "nothing about a session is
  stored" scope decision) — mixer/transpose settings live only in page/player state, not
  `localStorage` or the API.
- This is UI/glue code (frontend, no backend touched) — exempt from test-first per the working
  agreement, verified live instead, per M6a's/M6c's established precedent.

---

### Task 1: `StemPlayer` — SoundTouchNode integration, persistent mixer/transpose state, tempo-aware timing

**Files:**
- Modify: `apps/web/lib/player.ts`
- Modify: `apps/web/package.json` (new dependency)
- Create: `apps/web/public/soundtouch-processor.js` (copied build artifact, see Step 1)

**Interfaces:**
- Consumes: `@soundtouchjs/audio-worklet`'s real, verified API — `SoundTouchNode extends
  AudioWorkletNode`, `SoundTouchNode.register(context: BaseAudioContext, processorUrl: string |
  URL): Promise<void>` (static), `new SoundTouchNode({context}: {context: BaseAudioContext})`,
  `.pitch`/`.pitchSemitones`/`.playbackRate` getters each returning a real `AudioParam` (set via
  `.value`).
- Produces: `StemPlayer.init(): Promise<void>` (new — must be called once, awaited, before the
  first `play()`), `StemPlayer.setStemVolume(stem: keyof StemBuffers, value: number): void`,
  `StemPlayer.setStemMuted(stem: keyof StemBuffers, muted: boolean): void`,
  `StemPlayer.setTempo(multiplier: number): void`, `StemPlayer.setPitchSemitones(semitones:
  number): void` — all consumed by Task 2. `StemPlayer`'s existing public surface
  (`play`/`pause`/`seek`/`currentTimeSeconds`/`isPlaying`) is unchanged in shape, only
  `currentTimeSeconds`'s and `play()`'s internals change.

- [ ] **Step 1: Install the dependency and copy the worklet processor asset**

Run: `cd apps/web && npm install @soundtouchjs/audio-worklet`

This installs `@soundtouchjs/audio-worklet` (verified real license: MPL-2.0, per
`node_modules/@soundtouchjs/audio-worklet/package.json`'s `"license"` field) at version `^2.1.1`.

The package's actual `AudioWorklet` processor module — the file that must be loaded via
`SoundTouchNode.register(context, url)` — is a self-contained, pre-bundled JS file (no external
imports; verified by reading its contents) at
`node_modules/@soundtouchjs/audio-worklet/.dist/soundtouch-processor.js`. Copy it into this
project's public static-asset directory, exactly matching how M6c's `pitch-worklet.js` is served:

Run: `cp apps/web/node_modules/@soundtouchjs/audio-worklet/.dist/soundtouch-processor.js apps/web/public/soundtouch-processor.js`

(If the installed version differs from `2.1.1` by the time this task runs and the file's real
location has moved, `find apps/web/node_modules/@soundtouchjs/audio-worklet -name
'soundtouch-processor.js'` will find it — the package's `package.json` `"exports"` field maps a
`"./processor"` subpath to this exact file, confirming this is the intended public entry point,
not an internal implementation detail.)

Confirm `apps/web/package.json`'s `dependencies` now includes `"@soundtouchjs/audio-worklet":
"^2.1.1"` (or whatever real version was installed).

- [ ] **Step 2: Extend `StemPlayer` with SoundTouchNode integration and persistent state**

In `apps/web/lib/player.ts`, add near the top of the file (after the existing type/function
exports, before `StemBuffers`):

```typescript
import { SoundTouchNode } from "@soundtouchjs/audio-worklet";

const SOUNDTOUCH_PROCESSOR_URL = "/soundtouch-processor.js";

// Fixed iteration/indexing order for the three stems -- used both when constructing fresh
// source/gain nodes in play() and when looking up which gain node a mixer change should apply to.
const STEM_ORDER: (keyof StemBuffers)[] = ["drums", "bass", "other"];
```

Replace the entire `StemPlayer` class with this extended version (the binary-search functions and
`StemBuffers` interface above it are unchanged):

```typescript
// Plays three stems sample-aligned via the Web Audio API, mixed through independent GainNodes
// (per-stem volume/mute, M6b) into a single shared SoundTouchNode (key/tempo transposition, M6b)
// before reaching the destination. Transposition shifts the whole mix uniformly -- one shared
// node, not one per stem, since shifting each stem's pitch independently would be musically
// meaningless.
export class StemPlayer {
  private context: AudioContext;
  private buffers: StemBuffers;
  private sources: AudioBufferSourceNode[] = [];
  private gains: GainNode[] = [];
  private soundTouchNode: SoundTouchNode | null = null;
  private startedAtContextTime = 0;
  private startedAtOffsetSeconds = 0;
  private playing = false;
  // Bumped every time sources are torn down (stopSources(), called from both pause()/seek() and
  // from play() itself before starting a new session). Each source's onended handler closes over
  // the token that was current when it started, so a manual stop (which bumps the token first)
  // is distinguishable from a natural end (which fires with the still-current token).
  private playToken = 0;
  private onEnded: (() => void) | null;

  // Persistent mixer/transpose state -- deliberately independent of any particular GainNode/
  // SoundTouchNode instance, since play() recreates sources and gains on every call (including
  // every seek and pause/resume). Without this, a mixer setting would silently reset the moment
  // the user touches the seek bar -- not a hypothetical, the normal expected user flow.
  private stemVolumes: Record<keyof StemBuffers, number> = { drums: 1, bass: 1, other: 1 };
  private stemMuted: Record<keyof StemBuffers, boolean> = {
    drums: false,
    bass: false,
    other: false,
  };
  private tempoMultiplier = 1;
  private pitchSemitones = 0;

  constructor(context: AudioContext, buffers: StemBuffers, onEnded?: () => void) {
    this.context = context;
    this.buffers = buffers;
    this.onEnded = onEnded ?? null;
  }

  // Must be called once, awaited, before the first play() call -- registers the SoundTouch
  // worklet module and constructs the single shared SoundTouchNode every stem mixes into.
  // Mirrors PitchTracker.init()'s established pattern from M6c (lib/micScoring.ts) for "async
  // worklet setup that must happen once after construction, before the class is otherwise used."
  async init(): Promise<void> {
    await SoundTouchNode.register(this.context, SOUNDTOUCH_PROCESSOR_URL);
    this.soundTouchNode = new SoundTouchNode({ context: this.context });
    this.soundTouchNode.connect(this.context.destination);
  }

  play(offsetSeconds = 0): void {
    if (!this.soundTouchNode) {
      throw new Error("StemPlayer.init() must be called and awaited before play()");
    }
    this.stopSources();
    const token = this.playToken;
    this.sources = [];
    this.gains = [];
    for (const stem of STEM_ORDER) {
      const source = this.context.createBufferSource();
      source.buffer = this.buffers[stem];
      source.playbackRate.value = this.tempoMultiplier;
      const gain = this.context.createGain();
      gain.gain.value = this.stemMuted[stem] ? 0 : this.stemVolumes[stem];
      source.connect(gain).connect(this.soundTouchNode);
      source.onended = () => this.handleSourceEnded(token);
      source.start(0, offsetSeconds);
      this.sources.push(source);
      this.gains.push(gain);
    }
    this.soundTouchNode.playbackRate.value = this.tempoMultiplier;
    this.soundTouchNode.pitchSemitones.value = this.pitchSemitones;
    this.startedAtContextTime = this.context.currentTime;
    this.startedAtOffsetSeconds = offsetSeconds;
    this.playing = true;
  }

  pause(): void {
    this.stopSources();
    this.startedAtOffsetSeconds = this.currentTimeSeconds;
    this.playing = false;
  }

  seek(offsetSeconds: number): void {
    if (this.playing) {
      this.play(offsetSeconds);
    } else {
      this.startedAtOffsetSeconds = offsetSeconds;
    }
  }

  setStemVolume(stem: keyof StemBuffers, value: number): void {
    this.stemVolumes[stem] = value;
    this.applyGain(stem);
  }

  setStemMuted(stem: keyof StemBuffers, muted: boolean): void {
    this.stemMuted[stem] = muted;
    this.applyGain(stem);
  }

  // Live tempo change -- re-anchors the position-tracking bookkeeping (the same mechanism seek()
  // already uses internally) so currentTimeSeconds stays correct across the change. No stop/
  // restart of the underlying source nodes is needed: playbackRate is a live-adjustable
  // AudioParam on an already-running node.
  setTempo(multiplier: number): void {
    if (this.playing) {
      const currentPosition = this.currentTimeSeconds;
      this.tempoMultiplier = multiplier;
      this.startedAtOffsetSeconds = currentPosition;
      this.startedAtContextTime = this.context.currentTime;
      for (const source of this.sources) {
        source.playbackRate.value = multiplier;
      }
      if (this.soundTouchNode) {
        this.soundTouchNode.playbackRate.value = multiplier;
      }
    } else {
      this.tempoMultiplier = multiplier;
    }
  }

  setPitchSemitones(semitones: number): void {
    this.pitchSemitones = semitones;
    if (this.soundTouchNode) {
      this.soundTouchNode.pitchSemitones.value = semitones;
    }
  }

  get currentTimeSeconds(): number {
    if (!this.playing) {
      return this.startedAtOffsetSeconds;
    }
    return (
      this.startedAtOffsetSeconds +
      (this.context.currentTime - this.startedAtContextTime) * this.tempoMultiplier
    );
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  // Fires once per play() session, on whichever of the three sources' onended callbacks lands
  // first. A stale token (this.playToken has since moved on, e.g. because pause()/seek() called
  // stopSources()) means this source was stopped manually, not a natural end -- ignore it. The
  // `playing` check additionally guards against this same session's other two sources firing
  // their (also-genuine) natural-end callbacks a few ms later, after the first one already
  // handled it.
  private handleSourceEnded(token: number): void {
    if (!this.playing || token !== this.playToken) return;
    this.playing = false;
    // A natural end means there's nothing left to resume -- the next play() call (e.g. from the
    // user clicking Play again) should restart from the top, not from wherever playback happened
    // to land.
    this.startedAtOffsetSeconds = 0;
    this.onEnded?.();
  }

  private applyGain(stem: keyof StemBuffers): void {
    const index = STEM_ORDER.indexOf(stem);
    const gain = this.gains[index];
    if (gain) {
      gain.gain.value = this.stemMuted[stem] ? 0 : this.stemVolumes[stem];
    }
  }

  private stopSources(): void {
    this.playToken += 1;
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // Already stopped or never started -- fine to ignore.
      }
    }
    this.sources = [];
  }
}
```

- [ ] **Step 3: Live browser verification via a temporary test hook**

`StemPlayer` has no automated test suite (UI/glue code, matching this exact file's established
precedent throughout M6a/M6c). Verify it live, using the same temporary-hook-then-fully-revert
technique the M6c final-review fix round used and had independently verified as a genuine,
non-fabricated testing approach (not a shortcut — real execution of real code, just driven from
the console instead of clicking through the UI, since Task 2 hasn't built the mixer/transpose UI
yet).

In `apps/web/app/tracks/[id]/play/page.tsx`, temporarily add (inside the `PlayerPage` component
body, anywhere reasonable — e.g. right after `ensurePlayerLoaded` is defined):

```tsx
  // TEMPORARY -- Task 1 verification only, remove before committing.
  useEffect(() => {
    (window as unknown as { __testPlayer?: unknown }).__testPlayer = { ensurePlayerLoaded, pkg };
  });
```

Start the dev server (`preview_start` with `{name: "songbox-api"}` then `{name: "songbox-web"}`),
navigate to a track's `/tracks/{id}/play` page with an existing package (reuse one from a prior
milestone's verification pass if still present in this worktree's dev database, or generate one
the same way prior tasks did). Using `javascript_tool`, run:

```javascript
const { ensurePlayerLoaded, pkg } = window.__testPlayer;
const player = await ensurePlayerLoaded(pkg);
player.play(0);
await new Promise((r) => setTimeout(r, 500));

const positionBeforeTempoChange = player.currentTimeSeconds;
player.setTempo(1.2);
await new Promise((r) => setTimeout(r, 1000));
const positionAfterTempoChange = player.currentTimeSeconds;
// Expected: positionAfterTempoChange - positionBeforeTempoChange is approximately 1.2 * 1.0
// (the ~1 real second waited, scaled by the new 1.2x tempo) -- confirms currentTimeSeconds
// re-anchored correctly across the live tempo change rather than jumping or drifting.

player.setPitchSemitones(3);
player.setStemVolume("drums", 0.3);
player.setStemMuted("bass", true);
await new Promise((r) => setTimeout(r, 500));
// Listen / take a screenshot-adjacent console read: confirm no errors, audio is still playing.

// The specific regression this milestone's Decision 4 exists to prevent:
player.seek(player.currentTimeSeconds); // seeking while playing calls play() again internally
await new Promise((r) => setTimeout(r, 200));
const drumsGainAfterSeek = player["gains"][0].gain.value; // drums is index 0 per STEM_ORDER
const bassMutedAfterSeek = player["gains"][1].gain.value; // bass is index 1, should still be 0 (muted)
JSON.stringify({
  positionBeforeTempoChange,
  positionAfterTempoChange,
  tempoDelta: positionAfterTempoChange - positionBeforeTempoChange,
  drumsGainAfterSeek, // expect ~0.3, NOT 1 -- confirms the mixer setting survived the seek
  bassMutedAfterSeek, // expect 0 -- confirms mute survived the seek
});
```

Expected: `tempoDelta` is close to `1.2` (± timing jitter from the `setTimeout` waits, which are
not frame-perfect); `drumsGainAfterSeek` is `0.3` (not `1`); `bassMutedAfterSeek` is `0`. If either
of the last two is wrong, Decision 4's fix (Step 2's `play()` reading persistent state instead of
hardcoding `gain.gain.value = 1`) has a bug — do not proceed to Task 2 until this passes for real.

Check `read_console_messages` with `onlyErrors: true` throughout — must be empty.

Once verification passes, **remove the temporary `useEffect`/`window.__testPlayer` hook** from
`page.tsx` before committing — confirm via `git diff` that no trace of it remains in what gets
committed.

- [ ] **Step 4: Run lint/typecheck/build**

Run: `cd apps/web && npm run lint && npx tsc --noEmit && npm run build`
Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add apps/web/lib/player.ts apps/web/package.json apps/web/package-lock.json \
    apps/web/public/soundtouch-processor.js
git commit -m "M6b: add SoundTouchNode integration and persistent mixer/transpose state to StemPlayer"
```

---

### Task 2: Mixer and Transpose UI panels on the player page

**Files:**
- Modify: `apps/web/app/tracks/[id]/play/page.tsx`

**Interfaces:**
- Consumes: `StemPlayer.init()`, `.setStemVolume(stem, value)`, `.setStemMuted(stem, muted)`,
  `.setTempo(multiplier)`, `.setPitchSemitones(semitones)` — all from Task 1.
- Produces: nothing new for later tasks (this is the last M6 sub-milestone's last task).

- [ ] **Step 1: Await `player.init()` in `ensurePlayerLoaded`, and seed it with current UI state**

In `ensurePlayerLoaded`'s player-construction block, change:

```tsx
        const player = new StemPlayer(context, { drums, bass, other }, handleTrackEnded);
        playerRef.current = player;
        return player;
```

to:

```tsx
        const player = new StemPlayer(context, { drums, bass, other }, handleTrackEnded);
        await player.init();
        // Seed the freshly-created player with whatever mixer/transpose values the user already
        // set via the UI before ever clicking Play -- otherwise those settings would exist only
        // in React state with nothing to apply them to until the next render, silently doing
        // nothing on this first play(). Safe to read current-render state here directly: unlike
        // tick()'s recursive requestAnimationFrame(tick) closure (which stays pinned to whichever
        // render scheduled it), ensurePlayerLoaded is invoked fresh from a user-initiated event
        // handler every time, so it always sees the latest render's state.
        for (const stem of STEM_ORDER) {
          player.setStemVolume(stem, stemVolumes[stem]);
          player.setStemMuted(stem, stemMuted[stem]);
        }
        player.setTempo(tempoPercent / 100);
        player.setPitchSemitones(pitchSemitones);
        playerRef.current = player;
        return player;
```

- [ ] **Step 2: New imports, constant, and state**

Change the `@/lib/player` import line from:

```tsx
import { StemPlayer, findActiveWordIndex, findActivePitchFrameIndex } from "@/lib/player";
```

to:

```tsx
import {
  StemPlayer,
  findActiveWordIndex,
  findActivePitchFrameIndex,
  type StemBuffers,
} from "@/lib/player";
```

Add a module-level constant near `CALIBRATION_DURATION_MS`:

```tsx
const STEM_ORDER: (keyof StemBuffers)[] = ["drums", "bass", "other"];
```

Add new state inside `PlayerPage`, alongside the existing `useState` declarations:

```tsx
  const [stemVolumes, setStemVolumesState] = useState<Record<keyof StemBuffers, number>>({
    drums: 1,
    bass: 1,
    other: 1,
  });
  const [stemMuted, setStemMutedState] = useState<Record<keyof StemBuffers, boolean>>({
    drums: false,
    bass: false,
    other: false,
  });
  const [tempoPercent, setTempoPercent] = useState(100);
  const [pitchSemitones, setPitchSemitonesState] = useState(0);
```

- [ ] **Step 3: New handlers**

Add near `handleSeek`:

```tsx
  function handleStemVolumeChange(stem: keyof StemBuffers, value: number) {
    setStemVolumesState((prev) => ({ ...prev, [stem]: value }));
    playerRef.current?.setStemVolume(stem, value);
  }

  function handleStemMuteToggle(stem: keyof StemBuffers) {
    setStemMutedState((prev) => {
      const next = { ...prev, [stem]: !prev[stem] };
      playerRef.current?.setStemMuted(stem, next[stem]);
      return next;
    });
  }

  function handleTempoChange(percent: number) {
    setTempoPercent(percent);
    playerRef.current?.setTempo(percent / 100);
  }

  function handlePitchChange(semitones: number) {
    setPitchSemitonesState(semitones);
    playerRef.current?.setPitchSemitones(semitones);
  }
```

- [ ] **Step 4: Render the Mixer and Transpose panels**

Add this block immediately after the existing mic-scoring `<div className="mt-4">...</div>` block
(the one containing the "Enable mic scoring"/"Disable mic scoring" UI), still inside `<main>`:

```tsx
      <div className="mt-6 border-t border-zinc-200 pt-4">
        <h2 className="text-sm font-semibold mb-2">Mixer</h2>
        {STEM_ORDER.map((stem) => (
          <div key={stem} className="flex items-center gap-3 mb-2">
            <span className="w-16 text-sm capitalize">{stem}</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={stemVolumes[stem]}
              onChange={(e) => handleStemVolumeChange(stem, Number(e.target.value))}
              disabled={stemMuted[stem]}
              className="flex-1"
            />
            <button
              onClick={() => handleStemMuteToggle(stem)}
              className={`rounded px-2 py-1 text-xs font-medium ${
                stemMuted[stem] ? "bg-red-100 text-red-700" : "bg-zinc-100 text-zinc-600"
              }`}
            >
              {stemMuted[stem] ? "Muted" : "Mute"}
            </button>
          </div>
        ))}
      </div>

      <div className="mt-4 border-t border-zinc-200 pt-4">
        <h2 className="text-sm font-semibold mb-2">Transpose</h2>
        <div className="flex items-center gap-3 mb-2">
          <span className="w-16 text-sm">Key</span>
          <input
            type="range"
            min={-6}
            max={6}
            step={1}
            value={pitchSemitones}
            onChange={(e) => handlePitchChange(Number(e.target.value))}
            className="flex-1"
          />
          <span className="w-12 text-sm text-right">
            {pitchSemitones > 0 ? `+${pitchSemitones}` : pitchSemitones}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className="w-16 text-sm">Tempo</span>
          <input
            type="range"
            min={75}
            max={125}
            step={5}
            value={tempoPercent}
            onChange={(e) => handleTempoChange(Number(e.target.value))}
            className="flex-1"
          />
          <span className="w-12 text-sm text-right">{tempoPercent}%</span>
        </div>
      </div>
```

- [ ] **Step 5: Live browser verification**

1. Start both dev servers (`preview_start` with `{name: "songbox-api"}` then
   `{name: "songbox-web"}`), navigate to a track's `/tracks/{id}/play` page with an existing
   package.
2. Confirm no regression: ordinary Play/Pause/seek, word highlight, pitch lane, and M6c's mic
   scoring toggle (permission-denial fallback path, same as every prior verification pass in this
   sandboxed environment) all still work exactly as before.
3. Confirm the Mixer and Transpose panels render below the existing controls.
4. Click Play. Drag the "drums" volume slider down; confirm (via `read_console_messages` for no
   errors, and if audio is actually audible in this environment, by ear) the mix audibly changes.
   Click "Mute" on "bass"; confirm the button's label/style changes to the muted state.
5. Drag the seek bar to a different position. Confirm the drums volume and bass mute settings from
   step 4 are still in effect after the seek (this is the exact regression Task 1's Decision 4 fix
   targets — check it explicitly here too, at the UI level, not just via Task 1's direct
   `StemPlayer` test).
6. Drag the "Tempo" slider to a non-100% value. Confirm: the displayed percentage updates, word
   highlighting and the pitch-lane playhead continue advancing at a visibly different rate matching
   the new tempo (not frozen, not jumping), and — if M6c's mic scoring was enabled in this same
   session — that its target-pitch lookup doesn't visibly break (the marker should still track
   against the correct point in the stored contour, not one that's drifted out of sync).
7. Drag the "Key" slider to a non-zero value; confirm no errors and (if audible in this
   environment) a perceptible pitch change independent of the tempo setting from step 6.
8. Check `read_console_messages` with `onlyErrors: true` across the whole flow — must be empty.

- [ ] **Step 6: Update `docs/STATUS.md` and `docs/PLAN.md`**

Following the exact pattern every prior M6 sub-milestone used: add an M6b completion entry to
`docs/STATUS.md` (what was built: `SoundTouchNode` integration, persistent mixer/transpose state
fixing the real play()-resets-everything bug, tempo-aware `currentTimeSeconds`, the Mixer/Transpose
UI panels) with what was live-verified (per Step 5) stated honestly — including that subjective
audio quality at extreme pitch/tempo settings was not evaluated by ear in this environment beyond
"no errors, audibly different," per the design spec's explicit scope note that no unearned quality
claim should be written. Update `docs/PLAN.md`'s M6 milestone entry to mark M6b done (all three M6
sub-milestones — M6a, M6b, M6c — now complete).

- [ ] **Step 7: Commit**

```bash
git add "apps/web/app/tracks/[id]/play/page.tsx" docs/STATUS.md docs/PLAN.md
git commit -m "M6b: add stem mixer and transpose UI panels to the player page"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (SoundTouchJS, one shared node) — covered in Task 1's `init()` and
the audio-graph wiring in `play()`. Decision 2 (tempo via mirrored `playbackRate`, pitch via
`pitchSemitones`) — covered in `play()`'s and `setTempo()`'s exact `AudioParam` assignments.
Decision 3 (tempo-aware `currentTimeSeconds`, re-anchoring) — covered in `setTempo()`'s
position-capture logic and the updated getter. Decision 4 (persistent state surviving `play()`
recreating nodes) — covered by the `stemVolumes`/`stemMuted`/`tempoMultiplier`/`pitchSemitones`
fields and `play()` reading them instead of hardcoding, plus Task 1 Step 3's explicit
seek-survives-mixer-setting verification. Decision 5 (UI panels extending the existing page) —
covered in Task 2.

**Placeholder scan:** No TBD/TODO in this plan's own instructions.

**Type consistency:** `StemPlayer.setStemVolume(stem: keyof StemBuffers, value: number)` and
`.setStemMuted(stem: keyof StemBuffers, muted: boolean)` (Task 1) match their call sites in Task
2's `handleStemVolumeChange`/`handleStemMuteToggle` exactly, including the shared `STEM_ORDER`
array (defined once in `player.ts`, exported implicitly via the `StemBuffers` type it's keyed on,
and re-declared identically in `page.tsx` since it's a plain literal array, not exported directly
from `player.ts` — both copies use the same three string literals in the same order, verified by
hand). `setTempo(multiplier: number)` takes a 0-1-scaled multiplier; `page.tsx`'s `tempoPercent`
state is a 0-100 UI value, correctly divided by 100 at every call site
(`handleTempoChange`/`ensurePlayerLoaded`'s seeding step) — checked both call sites use the same
conversion.
