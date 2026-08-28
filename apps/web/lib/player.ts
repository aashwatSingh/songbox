export interface PlayerWord {
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string | null;
}

export interface PlayerPitchFrame {
  time_ms: number;
  hz: number | null;
  confidence: number;
}

// Binary search for the last word whose start_ms is <= currentTimeMs. Returns -1 before the
// first word starts. Words are assumed sorted by start_ms (guaranteed by the alignment engine's
// output order -- M4a).
export function findActiveWordIndex(words: PlayerWord[], currentTimeMs: number): number {
  let lo = 0;
  let hi = words.length - 1;
  let result = -1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (words[mid].start_ms <= currentTimeMs) {
      result = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

// Same binary search shape for pitch frames (frames are emitted at a fixed hop_ms, but the
// search doesn't assume even spacing, matching findActiveWordIndex's approach for consistency).
export function findActivePitchFrameIndex(
  frames: PlayerPitchFrame[],
  currentTimeMs: number
): number {
  let lo = 0;
  let hi = frames.length - 1;
  let result = -1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (frames[mid].time_ms <= currentTimeMs) {
      result = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

export interface StemBuffers {
  drums: AudioBuffer;
  bass: AudioBuffer;
  other: AudioBuffer;
}

// Plays three stems sample-aligned via the Web Audio API, summed through independent GainNodes
// (left at gain=1 in M6a -- M6b's mixer milestone will expose these as user controls without
// needing to touch this class's playback logic).
export class StemPlayer {
  private context: AudioContext;
  private buffers: StemBuffers;
  private sources: AudioBufferSourceNode[] = [];
  private gains: GainNode[] = [];
  private startedAtContextTime = 0;
  private startedAtOffsetSeconds = 0;
  private playing = false;

  constructor(context: AudioContext, buffers: StemBuffers) {
    this.context = context;
    this.buffers = buffers;
  }

  play(offsetSeconds = 0): void {
    this.stopSources();
    const stems: (keyof StemBuffers)[] = ["drums", "bass", "other"];
    this.sources = [];
    this.gains = [];
    for (const stem of stems) {
      const source = this.context.createBufferSource();
      source.buffer = this.buffers[stem];
      const gain = this.context.createGain();
      gain.gain.value = 1;
      source.connect(gain).connect(this.context.destination);
      source.start(0, offsetSeconds);
      this.sources.push(source);
      this.gains.push(gain);
    }
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

  get currentTimeSeconds(): number {
    if (!this.playing) {
      return this.startedAtOffsetSeconds;
    }
    return this.startedAtOffsetSeconds + (this.context.currentTime - this.startedAtContextTime);
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  private stopSources(): void {
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
