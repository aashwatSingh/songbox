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

// Type-only import: erased at compile time, produces zero runtime require/import call. The
// runtime import is deferred into init() below -- see the comment there for why.
import type { SoundTouchNode } from "@soundtouchjs/audio-worklet";

const SOUNDTOUCH_PROCESSOR_URL = "/soundtouch-processor.js";

// Fixed iteration/indexing order for the three stems -- used both when constructing fresh
// source/gain nodes in play() and when looking up which gain node a mixer change should apply to.
const STEM_ORDER: (keyof StemBuffers)[] = ["drums", "bass", "other"];

export interface StemBuffers {
  drums: AudioBuffer;
  bass: AudioBuffer;
  other: AudioBuffer;
}

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
  //
  // The @soundtouchjs/audio-worklet import is dynamic (not a top-level static import) because its
  // module declares `class SoundTouchNode extends AudioWorkletNode` at module scope -- evaluating
  // that class declaration requires evaluating `AudioWorkletNode` immediately, and
  // AudioWorkletNode is a browser-only Web Audio API global that doesn't exist in Node.js. A
  // top-level static import would drag that evaluation into Next.js's server-side render of any
  // page that imports this module (even just for its types/helpers), crashing every direct/hard
  // navigation with a 500. init() only ever runs client-side, after user interaction, so deferring
  // the import here keeps the module evaluation out of SSR entirely.
  async init(): Promise<void> {
    const { SoundTouchNode } = await import("@soundtouchjs/audio-worklet");
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
