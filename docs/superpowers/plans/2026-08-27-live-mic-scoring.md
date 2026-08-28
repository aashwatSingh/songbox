# M6c: Live Mic Pitch Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in live mic pitch scoring to the existing `/tracks/{id}/play` page — a YIN
pitch-detection `AudioWorklet`, a per-session bleed calibration step, and a running on-pitch score
compared against the stored pitch contour.

**Architecture:** A standalone `AudioWorkletProcessor` (plain JS, no imports, runs in an isolated
audio-thread scope) does the real-time DSP. A main-thread `lib/micScoring.ts` module owns mic
acquisition, worklet wiring, calibration, and scoring math. The existing `/tracks/{id}/play` page
orchestrates both on top of its already-built `StemPlayer`/RAF-loop/pitch-lane infrastructure from
M6a — no new route, no backend changes.

**Tech Stack:** Browser-native `getUserMedia`, `AudioWorklet`, `AudioWorkletNode` — no new
dependency. Same Next.js 16 / React 19 / TypeScript stack as the rest of `apps/web`.

## Global Constraints

- Never connect the mic input to `audioContext.destination` — that would produce audible feedback
  through any open speakers. The mic → worklet graph exists only for analysis.
- `getUserMedia` constraints: `echoCancellation: true`, `autoGainControl: false`,
  `noiseSuppression: false` — AGC/noise-suppression actively distort the signal pitch detection
  depends on.
- The bleed-floor calibration measures real RMS energy on this specific track/room/device/volume
  combination every session — never a hardcoded or assumed noise-floor constant.
- A pitch reading only counts toward the score if its RMS clears the calibrated floor (plus a
  margin) — excluded frames are dropped from scoring entirely, never treated as "off-pitch."
- Cents, not raw Hz, for pitch comparison (`1200 * log2(liveHz / targetHz)`) — pitch tolerance is
  logarithmic.
- The on-pitch tolerance and bleed-floor margin are tunable constants, explicitly commented as
  such — never presented as measured or psychoacoustically validated values (`CLAUDE.md`'s
  measurement-discipline rule).
- Mic scoring is strictly additive: if mic permission is denied or worklet init fails for any
  reason, the page must fall back to exactly its existing M6a playback-only behavior — no error
  state blocks ordinary playback.
- No backend changes, no new persistence — nothing about a live scoring session is stored.
- This is UI/glue code (frontend, no backend touched) — exempt from test-first per the working
  agreement, verified live instead. Per the design spec's own honesty note: automated
  verification can prove the pipeline runs correctly (worklet loads, detects real frequencies,
  calibration/scoring math is correct), but **cannot** prove real-world bleed survival — that
  needs a human singing near real speakers, which this plan explicitly calls out as a follow-up
  step for the user, not something a subagent can complete unattended.

---

### Task 1: `apps/web/public/pitch-worklet.js` — YIN pitch detector

**Files:**
- Create: `apps/web/public/pitch-worklet.js`

**Interfaces:**
- Produces: a registered `AudioWorkletProcessor` named `"pitch-detector"`, loadable via
  `audioContext.audioWorklet.addModule("/pitch-worklet.js")` (Next.js serves `apps/web/public/*`
  at the site root) and instantiable via `new AudioWorkletNode(context, "pitch-detector")`. Once
  connected to an audio source, it posts `{time: number, hz: number | null, rms: number}` messages
  to its `port` roughly every ~11.6ms (never on a fixed wall-clock schedule — driven by the audio
  render thread's own block-processing cadence). Task 2 consumes this message shape exactly.

This file has no dependency on any other file in this plan and is fully verifiable on its own (a
worklet just needs *an* `AudioContext` and *an* audio source — it doesn't care whether that source
is a mic or an oscillator).

- [ ] **Step 1: Write the worklet**

Create `apps/web/public/pitch-worklet.js`:

```javascript
// AudioWorkletProcessor implementing the YIN pitch-detection algorithm (de Cheveigne & Kawahara,
// 2002) over a rolling analysis window. Runs entirely inside the audio rendering thread -- no
// imports (worklet modules execute in an isolated global scope separate from the app's bundle),
// no dependency on anything outside this file.
//
// process() delivers exactly 128 sample frames per call (the fixed Web Audio render quantum) --
// far too short a window to resolve a vocal fundamental (an 80Hz note needs ~551 samples at
// 44.1kHz just for two periods). This processor accumulates incoming blocks into its own ring
// buffer and only runs YIN once enough new samples have arrived to advance by one hop.

const ANALYSIS_WINDOW_SIZE = 2048; // ~46ms at 44.1kHz
const HOP_SIZE = 512; // ~11.6ms at 44.1kHz -- finer time resolution than the window itself
const YIN_THRESHOLD = 0.15; // standard YIN absolute-threshold default (de Cheveigne & Kawahara)
const MIN_HZ = 60; // below typical vocal range -- bounds the search space
const MAX_HZ = 1000; // above typical vocal range

class PitchDetectorProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ringBuffer = new Float32Array(ANALYSIS_WINDOW_SIZE);
    this.ringWritePos = 0;
    this.samplesSinceLastHop = 0;
    this.filled = false;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      this.ringBuffer[this.ringWritePos] = channel[i];
      this.ringWritePos = (this.ringWritePos + 1) % ANALYSIS_WINDOW_SIZE;
      this.samplesSinceLastHop++;
      if (this.ringWritePos === 0) this.filled = true;
    }

    if (this.filled && this.samplesSinceLastHop >= HOP_SIZE) {
      this.samplesSinceLastHop = 0;
      const frame = this.readOrderedFrame();
      const hz = this.detectPitch(frame);
      const rms = this.computeRms(frame);
      this.port.postMessage({ time: currentTime, hz, rms });
    }

    return true;
  }

  // Copies the ring buffer out in chronological order (oldest sample first) -- the buffer wraps
  // in place, so a straight read starting at the current write position gives the right order.
  readOrderedFrame() {
    const frame = new Float32Array(ANALYSIS_WINDOW_SIZE);
    for (let i = 0; i < ANALYSIS_WINDOW_SIZE; i++) {
      frame[i] = this.ringBuffer[(this.ringWritePos + i) % ANALYSIS_WINDOW_SIZE];
    }
    return frame;
  }

  computeRms(frame) {
    let sumSquares = 0;
    for (let i = 0; i < frame.length; i++) sumSquares += frame[i] * frame[i];
    return Math.sqrt(sumSquares / frame.length);
  }

  // YIN: difference function -> cumulative mean normalized difference -> absolute threshold ->
  // parabolic interpolation for sub-sample precision. Returns Hz, or null if no period found
  // within [MIN_HZ, MAX_HZ] clears the threshold. The diff/cmnd arrays are computed over the
  // full [1, maxPeriod] range (not starting at minPeriod) so the cumulative-mean normalization
  // matches the textbook formula exactly -- only the threshold *search* is bounded to
  // [minPeriod, maxPeriod], not the normalization itself.
  detectPitch(frame) {
    const minPeriod = Math.max(2, Math.floor(sampleRate / MAX_HZ));
    const maxPeriod = Math.min(Math.floor(sampleRate / MIN_HZ), Math.floor(frame.length / 2) - 1);

    const diff = new Float32Array(maxPeriod + 1);
    for (let tau = 1; tau <= maxPeriod; tau++) {
      let sum = 0;
      for (let i = 0; i < frame.length - tau; i++) {
        const delta = frame[i] - frame[i + tau];
        sum += delta * delta;
      }
      diff[tau] = sum;
    }

    const cmnd = new Float32Array(maxPeriod + 1);
    cmnd[0] = 1;
    let runningSum = 0;
    for (let tau = 1; tau <= maxPeriod; tau++) {
      runningSum += diff[tau];
      cmnd[tau] = runningSum === 0 ? 1 : (diff[tau] * tau) / runningSum;
    }

    let tauEstimate = -1;
    for (let tau = minPeriod; tau <= maxPeriod; tau++) {
      if (cmnd[tau] < YIN_THRESHOLD) {
        while (tau + 1 <= maxPeriod && cmnd[tau + 1] < cmnd[tau]) tau++;
        tauEstimate = tau;
        break;
      }
    }
    if (tauEstimate === -1) return null;

    // Parabolic interpolation around tauEstimate for sub-sample precision.
    let betterTau = tauEstimate;
    if (tauEstimate > 1 && tauEstimate < maxPeriod) {
      const s0 = cmnd[tauEstimate - 1];
      const s1 = cmnd[tauEstimate];
      const s2 = cmnd[tauEstimate + 1];
      const denominator = 2 * s1 - s2 - s0;
      if (denominator !== 0) {
        betterTau = tauEstimate + (s2 - s0) / (2 * denominator);
      }
    }

    return sampleRate / betterTau;
  }
}

registerProcessor("pitch-detector", PitchDetectorProcessor);
```

Note on cost: the diff/cmnd computation is O(maxPeriod × window) ≈ 735 × 2048 ≈ 1.5M float
operations, run once per ~11.6ms hop. This is expected to be comfortably real-time on typical
desktop/phone hardware, but hasn't been measured on real low-end devices — if Task 2's live
verification shows audio glitches or dropped frames, the first tuning knobs are `MAX_HZ` (lowering
it shrinks `maxPeriod`) and `HOP_SIZE` (raising it reduces how often YIN runs), not a rewrite.

- [ ] **Step 2: Verify the algorithm against a known frequency, live in a real browser**

This worklet has no automated test suite (plain JS served as a static asset, outside the
TypeScript/bundler pipeline — matching how Next.js worklet files are conventionally handled). This
step is the real verification: feed it a signal of *known* frequency and confirm it reports that
frequency back, which is a genuine correctness check, not just "does it load."

Start the dev server (`preview_start` with `{name: "songbox-web"}` — the API server isn't needed
for this step, the worklet has no backend dependency) and navigate to any page. Then use the
Browser pane's `javascript_tool` to run this verification script (this is inspection/debugging of
already-written source, not implementing UI changes with it):

```javascript
async function verifyPitchDetection(testHz) {
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("/pitch-worklet.js");
  const node = new AudioWorkletNode(ctx, "pitch-detector");
  const osc = ctx.createOscillator();
  osc.frequency.value = testHz;
  // Deliberately NOT connected to ctx.destination -- this is a silent correctness check, it
  // should not produce audible output.
  osc.connect(node);

  const readings = [];
  node.port.onmessage = (event) => readings.push(event.data);

  osc.start();
  await new Promise((resolve) => setTimeout(resolve, 500));
  osc.stop();
  node.disconnect();
  osc.disconnect();
  await ctx.close();

  // Skip the first couple of readings (ring buffer still filling / settling).
  const settled = readings.slice(3).map((r) => r.hz).filter((hz) => hz !== null);
  const avg = settled.reduce((sum, hz) => sum + hz, 0) / settled.length;
  return { testHz, readingCount: readings.length, settledCount: settled.length, avgHz: avg };
}

const result440 = await verifyPitchDetection(440);
const result220 = await verifyPitchDetection(220);
JSON.stringify({ result440, result220 });
```

Expected: `result440.avgHz` within roughly 1-2 Hz of 440 (a clean synthetic oscillator is an easy
case for YIN — this confirms the algorithm and the ring-buffer/hop bookkeeping are correct, not
that real-world vocal detection will be this precise). `result220.avgHz` within a similar margin
of 220 — confirms the detector isn't just coincidentally locking onto one frequency. Both
`settledCount` values should be greater than 0 (if `avgHz` is `NaN`, no readings cleared the YIN
threshold — a real bug to fix, not a value to explain away).

If the readings are off by an octave (e.g. `avgHz` near 880 or 220 for a 440 test), that's YIN's
well-known octave-error failure mode — check the `minPeriod`/`maxPeriod` bounds and the threshold
search logic before assuming the test itself is wrong.

- [ ] **Step 3: Commit**

```bash
git add apps/web/public/pitch-worklet.js
git commit -m "M6c: add YIN pitch-detection AudioWorklet"
```

---

### Task 2: `apps/web/lib/micScoring.ts` and the player page integration

**Files:**
- Create: `apps/web/lib/micScoring.ts`
- Modify: `apps/web/app/tracks/[id]/play/page.tsx`

**Interfaces:**
- Consumes: Task 1's `"pitch-detector"` worklet, loaded via
  `audioContext.audioWorklet.addModule("/pitch-worklet.js")`, posting `{time, hz, rms}` messages.
  Also consumes `findActivePitchFrameIndex` from `apps/web/lib/player.ts` (already exists, from
  M6a) to find the currently-active target pitch frame — no new lookup logic.
- Produces: `PitchTracker` class (`init()`, `getLatestReading()`, `collectReadings(durationMs)`,
  `stop()`), `requestMicStream()`, `measureBleedFloor(tracker, durationMs)`, `hzToCents(hz,
  referenceHz)`, `ScoreTracker` class (`recordFrame(liveHz, targetHz, rms, bleedFloor)`,
  `percentOnPitch` getter, `framesCounted` getter), `ON_PITCH_TOLERANCE_CENTS`,
  `BLEED_FLOOR_MARGIN_RMS` constants — all consumed by the page component in this same task.

- [ ] **Step 1: Write `apps/web/lib/micScoring.ts`**

```typescript
export interface PitchReading {
  time: number; // AudioContext-relative seconds, from the worklet's own clock
  hz: number | null;
  rms: number;
}

const PITCH_WORKLET_MODULE_URL = "/pitch-worklet.js";
const PITCH_WORKLET_PROCESSOR_NAME = "pitch-detector";

export async function requestMicStream(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      autoGainControl: false,
      noiseSuppression: false,
    },
  });
}

// Wraps the AudioWorklet-based pitch detector: loads the worklet module, wires the mic input
// into it (never connected to destination -- that would create audible feedback through any
// open speakers), and exposes the latest reading for polling from an existing render loop rather
// than introducing a second timing mechanism.
export class PitchTracker {
  private context: AudioContext;
  private micStream: MediaStream;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private latestReading: PitchReading | null = null;
  private collector: ((reading: PitchReading) => void) | null = null;

  constructor(context: AudioContext, micStream: MediaStream) {
    this.context = context;
    this.micStream = micStream;
  }

  async init(): Promise<void> {
    await this.context.audioWorklet.addModule(PITCH_WORKLET_MODULE_URL);
    this.sourceNode = this.context.createMediaStreamSource(this.micStream);
    this.workletNode = new AudioWorkletNode(this.context, PITCH_WORKLET_PROCESSOR_NAME);
    this.workletNode.port.onmessage = (event: MessageEvent<PitchReading>) => {
      this.latestReading = event.data;
      this.collector?.(event.data);
    };
    this.sourceNode.connect(this.workletNode);
  }

  getLatestReading(): PitchReading | null {
    return this.latestReading;
  }

  // Collects every reading that arrives over the next durationMs, for calibration. Temporarily
  // takes over the message handler and restores the normal one (which keeps updating
  // latestReading throughout, including during collection) once done.
  async collectReadings(durationMs: number): Promise<PitchReading[]> {
    const readings: PitchReading[] = [];
    this.collector = (reading) => readings.push(reading);
    await new Promise((resolve) => setTimeout(resolve, durationMs));
    this.collector = null;
    return readings;
  }

  stop(): void {
    this.sourceNode?.disconnect();
    this.workletNode?.disconnect();
    this.micStream.getTracks().forEach((track) => track.stop());
  }
}

// One-time-per-session calibration: caller is responsible for actually playing the track's
// accompaniment during this window (e.g. via StemPlayer.play()) and keeping the user informed to
// stay silent -- this function only measures. Returns the max RMS observed; the caller adds
// BLEED_FLOOR_MARGIN_RMS before using it as a live-scoring gate.
export async function measureBleedFloor(
  tracker: PitchTracker,
  durationMs: number
): Promise<number> {
  const readings = await tracker.collectReadings(durationMs);
  return readings.reduce((max, r) => Math.max(max, r.rms), 0);
}

// Cents: a logarithmic pitch-distance unit where 100 cents = one semitone, independent of
// register (unlike raw Hz, where the same musical error spans very different Hz gaps at low vs
// high pitches).
export function hzToCents(hz: number, referenceHz: number): number {
  return 1200 * Math.log2(hz / referenceHz);
}

// Tunable, not measured or psychoacoustically validated -- see the design spec's Decision 3.
// +/-50 cents (a quarter of a semitone) is a deliberately lenient starting point.
export const ON_PITCH_TOLERANCE_CENTS = 50;

// Added to the calibrated bleed floor before gating live readings -- also tunable, not measured.
// A small additive margin over the observed calibration-window max, so borderline readings right
// at the floor don't flicker between counted/excluded.
export const BLEED_FLOOR_MARGIN_RMS = 0.02;

// Accumulates a running on-pitch percentage across a scoring session. A frame is counted only if
// both a live and a target Hz are available and the live reading's RMS clears the calibrated
// bleed floor (already including the margin) -- frames that don't clear it are excluded entirely,
// never treated as off-pitch, since there's no way to know whether an excluded frame's pitch (if
// any) came from the singer or from bleed.
export class ScoreTracker {
  private countedFrames = 0;
  private onPitchFrames = 0;

  recordFrame(
    liveHz: number | null,
    targetHz: number | null,
    rms: number,
    bleedFloor: number
  ): void {
    if (liveHz === null || targetHz === null) return;
    if (rms < bleedFloor) return;
    this.countedFrames += 1;
    if (Math.abs(hzToCents(liveHz, targetHz)) <= ON_PITCH_TOLERANCE_CENTS) {
      this.onPitchFrames += 1;
    }
  }

  get percentOnPitch(): number {
    if (this.countedFrames === 0) return 0;
    return (this.onPitchFrames / this.countedFrames) * 100;
  }

  get framesCounted(): number {
    return this.countedFrames;
  }
}
```

- [ ] **Step 2: Integrate into `apps/web/app/tracks/[id]/play/page.tsx`**

The current file (post-M6a) has: imports, `BackToTracksLink`, `decodeStem`, and the
`PlayerPage` component with state for `pkg`/`notReady`/`generating`/`error`/`loadingAudio`/
`isPlaying`/`currentTimeMs`/`durationSeconds`, refs for `playerRef`/`audioContextRef`/`rafRef`/
`durationSecondsRef`, the package-fetch effect, an unmount-cleanup effect, `handleGenerate`,
`handleTrackEnded`, `ensurePlayerLoaded`, `tick`, `handlePlayPause`, `handleSeek`, memoized
`activeWordIndex`/`activeFrameIndex`/`estimatedDurationSeconds`/`maxPitchHz`/`pitchPoints`, then
the render tree with its `error`/`notReady`/`pkg===null` early returns and the main player UI.

Make these additions, keeping everything else in the file unchanged:

**2a. New imports.** Change the `@/lib/api` and `@/lib/player` import lines to also pull in
nothing new from those files (no changes needed there), and add a new import line for
`micScoring`:

```tsx
import {
  BLEED_FLOOR_MARGIN_RMS,
  PitchTracker,
  ScoreTracker,
  requestMicStream,
  measureBleedFloor,
} from "@/lib/micScoring";
```

**2b. New constant**, placed near the top of the file alongside the existing `decodeStem`
function (module scope, not inside the component):

```tsx
const CALIBRATION_DURATION_MS = 4000;
```

**2c. New state and refs**, added inside `PlayerPage` alongside the existing `useState`/`useRef`
declarations:

```tsx
  const [micState, setMicState] = useState<"idle" | "requesting" | "calibrating" | "active">(
    "idle"
  );
  const [micError, setMicError] = useState<string | null>(null);
  const [liveHz, setLiveHz] = useState<number | null>(null);
  const [scorePercent, setScorePercent] = useState(0);

  const pitchTrackerRef = useRef<PitchTracker | null>(null);
  const scoreTrackerRef = useRef<ScoreTracker | null>(null);
  const bleedFloorRef = useRef(0);
  // tick()'s recursive requestAnimationFrame(tick) call is pinned to the closure it was first
  // scheduled from -- it never sees a LATER render's micState value (the same staleness class
  // M6a's durationSecondsRef exists to avoid for handleTrackEnded). A ref, not React state, is
  // what tick() must read to know whether scoring is active right now.
  const micActiveRef = useRef(false);
```

**2d. Extend the unmount-cleanup effect** to also stop the pitch tracker. Replace:

```tsx
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      audioContextRef.current?.close();
    };
  }, []);
```

with:

```tsx
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      pitchTrackerRef.current?.stop();
      audioContextRef.current?.close();
    };
  }, []);
```

**2e. New handler**, added near `handlePlayPause`:

```tsx
  async function handleEnableMicScoring() {
    if (!pkg) return;
    setMicError(null);
    setMicState("requesting");
    try {
      const micStream = await requestMicStream();
      const player = await ensurePlayerLoaded(pkg);
      const context = audioContextRef.current;
      if (!context) throw new Error("audio context not ready");

      const tracker = new PitchTracker(context, micStream);
      await tracker.init();
      pitchTrackerRef.current = tracker;

      setMicState("calibrating");
      player.play(0);
      setIsPlaying(true);
      rafRef.current = requestAnimationFrame(tick);

      const floor = await measureBleedFloor(tracker, CALIBRATION_DURATION_MS);
      bleedFloorRef.current = floor + BLEED_FLOOR_MARGIN_RMS;
      scoreTrackerRef.current = new ScoreTracker();
      micActiveRef.current = true;
      setMicState("active");
    } catch (err) {
      setMicError((err as Error).message);
      setMicState("idle");
      micActiveRef.current = false;
      pitchTrackerRef.current?.stop();
      pitchTrackerRef.current = null;
    }
  }
```

**2f. Extend `tick()`** to poll the pitch tracker and feed the score tracker once scoring is
active. Replace:

```tsx
  function tick() {
    const player = playerRef.current;
    if (player && player.isPlaying) {
      setCurrentTimeMs(player.currentTimeSeconds * 1000);
      rafRef.current = requestAnimationFrame(tick);
    }
  }
```

with:

```tsx
  function tick() {
    const player = playerRef.current;
    if (player && player.isPlaying) {
      const nowMs = player.currentTimeSeconds * 1000;
      setCurrentTimeMs(nowMs);

      const tracker = pitchTrackerRef.current;
      if (tracker) {
        const reading = tracker.getLatestReading();
        setLiveHz(reading?.hz ?? null);

        if (micActiveRef.current && reading && pkg) {
          const frameIndex = findActivePitchFrameIndex(pkg.karaoke.pitch.frames, nowMs);
          const targetHz = frameIndex >= 0 ? pkg.karaoke.pitch.frames[frameIndex].hz : null;
          scoreTrackerRef.current?.recordFrame(
            reading.hz,
            targetHz,
            reading.rms,
            bleedFloorRef.current
          );
          setScorePercent(scoreTrackerRef.current?.percentOnPitch ?? 0);
        }
      }

      rafRef.current = requestAnimationFrame(tick);
    }
  }
```

(`findActivePitchFrameIndex` is already imported at the top of this file from M6a — no new import
needed for it.)

**2g. UI additions in the render tree.** Inside the main player return block (the one with the
lyrics strip and pitch-lane `<svg>`), add the live-pitch marker to the existing `<svg>` and the
mic-scoring controls below the existing play/seek row. The existing `<svg>` block:

```tsx
        <svg viewBox="0 0 400 60" className="w-full h-[60px] block">
          <polyline
            points={pitchPoints}
            fill="none"
            stroke="#8fd6ff"
            strokeWidth={2}
          />
          {activeFrameIndex >= 0 && (
            <line
              x1={playheadX}
              y1={0}
              x2={playheadX}
              y2={60}
              stroke="#fff"
              strokeWidth={1.5}
              opacity={0.6}
            />
          )}
        </svg>
```

becomes (adding the live-pitch marker as a third child):

```tsx
        <svg viewBox="0 0 400 60" className="w-full h-[60px] block">
          <polyline
            points={pitchPoints}
            fill="none"
            stroke="#8fd6ff"
            strokeWidth={2}
          />
          {activeFrameIndex >= 0 && (
            <line
              x1={playheadX}
              y1={0}
              x2={playheadX}
              y2={60}
              stroke="#fff"
              strokeWidth={1.5}
              opacity={0.6}
            />
          )}
          {micState === "active" && liveHz !== null && (
            <circle
              cx={playheadX}
              cy={60 - (liveHz / maxPitchHz) * 55}
              r={4}
              fill="#ff6b6b"
            />
          )}
        </svg>
```

And the existing play/seek controls block:

```tsx
      <div className="flex items-center gap-3">
        <button
          onClick={handlePlayPause}
          disabled={loadingAudio}
          className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
        >
          {loadingAudio ? "Loading audio..." : isPlaying ? "Pause" : "Play"}
        </button>
        <input
          type="range"
          min={0}
          max={effectiveDurationSeconds || 1}
          step={0.1}
          value={currentTimeMs / 1000}
          onChange={handleSeek}
          className="flex-1"
        />
      </div>
```

gets a new block appended immediately after it (still inside the same `<main>`):

```tsx
      <div className="mt-4">
        {micState === "idle" && (
          <button
            onClick={handleEnableMicScoring}
            className="rounded border border-blue-600 px-4 py-2 text-blue-600 text-sm font-medium"
          >
            Enable mic scoring
          </button>
        )}
        {micState === "requesting" && (
          <p className="text-sm text-zinc-500">Requesting microphone access...</p>
        )}
        {micState === "calibrating" && (
          <p className="text-sm text-zinc-500">Stay quiet -- calibrating...</p>
        )}
        {micState === "active" && (
          <p className="text-sm text-zinc-600">
            Mic scoring active &mdash; {scorePercent.toFixed(0)}% on pitch
          </p>
        )}
        {micError && <p className="mt-2 text-red-600 text-sm">{micError}</p>}
      </div>
```

- [ ] **Step 3: Live browser verification**

This is a mix of two things: mechanics that ARE verifiable through the Browser pane's tools, and a
real bleed-survival measurement that is NOT (it needs a human singing near real speakers, which no
automated browser session can do). Do both parts, and report both honestly and separately.

**Part A — mechanics (do this through the Browser pane tools):**

1. Start both dev servers (`preview_start` with `{name: "songbox-api"}` then
   `{name: "songbox-web"}`), navigate to a track's `/tracks/{id}/play` page that already has a
   generated package (reuse one from M6a's own verification passes if still present in this
   worktree's dev database, or generate one the same way M6a's task did).
2. Confirm the "Enable mic scoring" button renders in its idle state alongside the existing
   playback controls, and that ordinary playback (Play/Pause/seek, word highlight, pitch lane)
   still works exactly as it did before this task — this task must not regress M6a's existing
   behavior.
3. Click "Enable mic scoring." The browser's real mic-permission prompt is a browser-chrome-level
   UI element, not part of the page DOM — if the Browser pane's tools cannot interact with it
   (this is a real, expected limitation, not a bug to work around), the flow will hang at
   `micState === "requesting"` or the `getUserMedia` promise will reject. If it rejects, confirm
   `micError` renders a message and the rest of the page (ordinary playback) is completely
   unaffected — this proves the "additive, no dead-end" fallback requirement works, which is
   itself real, valuable verification even without a granted mic.
4. If mic access CAN be granted in this environment (e.g. the harness auto-grants it, or a fake
   device is configured), continue: confirm the "Stay quiet — calibrating..." message appears,
   playback audibly/visibly starts immediately (per this task's design — calibration IS the start
   of playback, not a separate step), and after ~4 seconds the state flips to "active" with a
   score percentage displayed. Confirm the live-pitch marker (red circle) appears on the pitch
   lane once mic scoring is active. Check `read_console_messages` with `onlyErrors: true` — must
   be empty.
5. Regardless of whether mic access was grantable, use `javascript_tool` to directly verify the
   scoring math with fabricated data (this doesn't need a real mic): in the browser console,
   `import` isn't available, but you can verify `hzToCents`/`ScoreTracker`'s *logic* is correct by
   reading the committed source in `apps/web/lib/micScoring.ts` and hand-checking a few cases
   against the formula (e.g. `hzToCents(440, 440)` should be `0`; `hzToCents(466.16, 440)` — a
   semitone up — should be very close to `100`), OR by temporarily pasting the two pure functions
   (`hzToCents`, and a `ScoreTracker`-equivalent inline object) into a `javascript_tool` call and
   running a few `recordFrame`/`percentOnPitch` assertions directly. Report which you did and the
   actual numbers observed.

**Part B — real bleed survival (cannot be automated, must be offered to the user):**

Write in your report, plainly: "Real-world bleed survival (open question 3 in `docs/PLAN.md`) has
NOT been measured by this task, and cannot be measured by an automated agent — it requires a human
singing near real speakers with a real microphone. This should be offered to the project owner as
a manual follow-up test: enable mic scoring on a real device, sing along at a normal speaker
volume, and see whether the calibration/gating produces a usable score or gets swamped by bleed."
Do not fabricate a bleed-survival number or claim this was tested if it wasn't genuinely tested by
a human. This is exactly what `CLAUDE.md`'s measurement-discipline rule and the design spec's own
"honest limit on what can be verified" section require.

- [ ] **Step 4: Update `docs/STATUS.md` and `docs/PLAN.md`**

Following the exact pattern M6a's own final-review fix round used (and M5, M4b before it): add an
M6c completion entry to `docs/STATUS.md` (what was built: YIN pitch-detection `AudioWorklet`,
mic-scoring toggle on the player page, per-session bleed calibration, cents-based scoring against
the stored contour) with an explicit, honest note that real-world bleed survival is
`TODO: unmeasured` pending a real hands-on test pass — do NOT invent a survival percentage or
claim the open question is closed. Update `docs/PLAN.md`'s M6 milestone entry (mark M6c done,
alongside the already-done M6a) and open question 3 (state what was built and that real
measurement is still pending, rather than marking it fully resolved).

- [ ] **Step 5: Commit**

```bash
git add apps/web/lib/micScoring.ts "apps/web/app/tracks/[id]/play/page.tsx" docs/STATUS.md docs/PLAN.md
git commit -m "M6c: add live mic pitch scoring to the player page"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (bleed mitigation via `echoCancellation` + calibration + confidence
gate) — covered in `requestMicStream`'s constraints and the `measureBleedFloor`/`ScoreTracker`
gating logic. Decision 2 (YIN in an `AudioWorkletProcessor`, ring-buffer accumulation since
`process()` only delivers 128 frames) — covered in Task 1's worklet. Decision 3 (cents-based
scoring, reusing `findActivePitchFrameIndex`) — covered in `hzToCents`/`ScoreTracker` and `tick()`'s
extension. Decision 4 as corrected (extends the existing page, live marker not a polyline,
calibration-as-start-of-playback) — covered in Task 2's page integration. The testing-strategy
section's "honest limit" is carried through verbatim into Task 2's Step 3 Part B, not softened.

**Placeholder scan:** No TBD/TODO in this plan's own instructions. The `TODO: unmeasured` language
in Task 2 Step 4 is the established, deliberate project pattern (matching every prior milestone's
honest-gap convention), not a plan gap.

**Bug caught during self-review:** the first draft of `tick()`'s extension read `micState`
(React state) directly to decide whether to score a frame. Since `tick()`'s own recursive
`requestAnimationFrame(tick)` call is pinned to the closure it was first scheduled from, it would
never observe `micState` flipping from `"calibrating"` to `"active"` later in the same session —
scoring would silently never activate. Fixed by introducing `micActiveRef` (a ref, always current
regardless of which `tick` closure is running) — exactly the same staleness class M6a's own
`durationSecondsRef` was introduced to solve for `handleTrackEnded`, applied here before an
implementer could hit it fresh.

**Type consistency:** `PitchReading` (`{time, hz, rms}`) matches exactly between the worklet's
`postMessage` payload (Task 1) and `PitchTracker`'s `MessageEvent<PitchReading>` handler (Task 2).
`ScoreTracker.recordFrame(liveHz: number | null, targetHz: number | null, rms: number, bleedFloor:
number)`'s signature matches its one call site in `tick()` exactly, including argument order.
`measureBleedFloor(tracker: PitchTracker, durationMs: number)`'s signature matches its call in
`handleEnableMicScoring`. `findActivePitchFrameIndex`'s existing M6a signature
(`frames: PlayerPitchFrame[], currentTimeMs: number`) is used unchanged in `tick()`'s extension —
no new wrapper needed since `PackageResponse["karaoke"]["pitch"]["frames"]`'s shape already
matches `PlayerPitchFrame` structurally (established in M6a's own type-consistency review).
