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
  // The `time` of the last reading returned by getLatestReadingIfNew(), so repeated calls within
  // the same worklet hop (e.g. multiple RAF frames firing before the worklet posts again, on a
  // high-refresh-rate display) can be told apart from a genuinely new reading.
  private lastConsumedTime: number | null = null;

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

  // Like getLatestReading(), but returns null unless the current reading's `time` (the worklet's
  // own clock, distinct from the RAF display refresh cadence) differs from the last reading this
  // method itself returned. The worklet posts a new reading roughly every ~10-12ms, independent of
  // requestAnimationFrame's cadence -- on a 60Hz display (~16.7ms/frame) some readings would
  // otherwise never be consumed, and on a 120Hz display (~8.3ms/frame) the same reading would
  // otherwise be consumed twice. Callers that need "count each real pitch reading exactly once"
  // (e.g. ScoreTracker's frame count) should use this; getLatestReading() itself is unchanged for
  // callers that just want "whatever the most recent value is right now" (e.g. a live Hz display).
  getLatestReadingIfNew(): PitchReading | null {
    const reading = this.latestReading;
    if (reading === null) return null;
    if (reading.time === this.lastConsumedTime) return null;
    this.lastConsumedTime = reading.time;
    return reading;
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
