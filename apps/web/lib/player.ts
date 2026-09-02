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

// Vendored, frozen snapshot of node_modules/@soundtouchjs/audio-worklet/.dist/soundtouch-processor.js
// at the exact version pinned in package.json (see the comment there). If that dependency version
// ever changes, this file must be re-copied from the new .dist/soundtouch-processor.js -- nothing
// automatically keeps the two in sync.
const SOUNDTOUCH_PROCESSOR_URL = "/soundtouch-processor.js";

// Measured real latency the SoundTouchNode adds to the signal path -- an OfflineAudioContext
// test (final M6b review) found ~132ms between when audio is scheduled (context.currentTime)
// and when it's actually audible, present even at default settings (100% tempo, 0 semitones),
// ranging ~119-151ms across different tempo/pitch settings. Not dynamically measured per-session
// (the node's own `metrics.framesBuffered` could do this more precisely, but that's asynchronous
// event-driven data, not available synchronously inside a getter) -- this is a fixed, honestly-
// approximate correction, not a precise one. Subtracted once here so every consumer of
// currentTimeSeconds (word highlighting, the pitch-lane playhead, M6c's mic-scoring target
// lookup) benefits automatically without needing to know the worklet has latency at all.
const SOUNDTOUCH_LATENCY_SECONDS = 0.132;

// -3.5 dB. Leaves room for three summed stems plus worklet overshoot before the limiter has to
// act at all; quiet enough to stop clipping, loud enough not to feel like a volume drop.
const MASTER_HEADROOM_GAIN = 0.67;

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
  private masterGain: GainNode | null = null;
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

    // Master bus: headroom, then a safety limiter, then out.
    //
    // The three playback stems sum to approximately the original recording minus vocals, and a
    // commercially mastered track already sits near 0 dBFS -- so summing them at unity clips, and
    // the time-stretch worklet's own overshoot makes it worse. That crunch is what "the audio
    // sounds off" actually is on a loud track. Backing off to -3.5 dB restores headroom, and the
    // compressor is configured as a brick-wall-ish limiter to catch whatever still peaks rather
    // than letting it wrap.
    const master = this.context.createGain();
    master.gain.value = MASTER_HEADROOM_GAIN;
    const limiter = this.context.createDynamicsCompressor();
    limiter.threshold.value = -1.5;
    limiter.knee.value = 0;
    limiter.ratio.value = 20;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.15;
    this.masterGain = master;
    this.soundTouchNode.connect(master);
    master.connect(limiter).connect(this.context.destination);
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
      // Both this AND soundTouchNode.playbackRate below are set on purpose -- it is the library's
      // documented pairing (see SoundTouchNode.d.ts's usage example). The buffer source does the
      // actual speed change by resampling, which drags pitch with it; telling the node the same
      // rate is how it knows how much pitch to compensate back out. Dropping either one leaves
      // tempo and pitch fighting each other.
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
    // Re-anchor to the RAW (uncompensated) scheduled position, not the public currentTimeSeconds
    // getter -- see rawScheduledPositionSeconds()'s comment for why. Using the compensated value
    // here would make the eventual resume's play(offsetSeconds) call start the source reading
    // ~132ms earlier than where it actually stopped, audibly replaying audio already heard.
    this.startedAtOffsetSeconds = this.rawScheduledPositionSeconds();
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
      // Re-anchor against the RAW (uncompensated) position -- see rawScheduledPositionSeconds()'s
      // comment. Re-anchoring against the public, already-compensated currentTimeSeconds here
      // would subtract SOUNDTOUCH_LATENCY_SECONDS a second time on top of the getter's own
      // subtraction on every subsequent read, permanently shifting the tracked position another
      // ~132ms behind the true audible position on every tempo change -- compounding without bound
      // across repeated drags of the Tempo slider, since each call's anchor is itself already
      // over-corrected from the previous call.
      const currentPosition = this.rawScheduledPositionSeconds();
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

  // The playhead position on the RAW, uncompensated "scheduled" timeline -- i.e. the sample
  // position the underlying AudioBufferSourceNodes are currently set to read from, matching
  // exactly what play()'s own offsetSeconds/source.start(0, offsetSeconds) call means. This is
  // the correct quantity for internal re-anchoring (pause()'s resume offset, setTempo()'s live
  // re-anchor): re-anchoring against the public currentTimeSeconds getter below (which subtracts
  // SOUNDTOUCH_LATENCY_SECONDS) would double-subtract the latency constant on every such call --
  // see pause()'s and setTempo()'s comments for the concrete failure this would cause.
  private rawScheduledPositionSeconds(): number {
    if (!this.playing) {
      return this.startedAtOffsetSeconds;
    }
    return (
      this.startedAtOffsetSeconds +
      (this.context.currentTime - this.startedAtContextTime) * this.tempoMultiplier
    );
  }

  get currentTimeSeconds(): number {
    if (!this.playing) {
      // Deliberately NOT latency-compensated -- this returns the same RAW value
      // rawScheduledPositionSeconds() would, because pause() (below) stores the raw position
      // here specifically so that handlePlayPause's resume call (`player.play(player
      // .currentTimeSeconds)` in page.tsx) restarts playback from the exact sample the source
      // nodes had reached, not from ~132ms earlier -- which would otherwise replay audio already
      // heard. Nothing in this codebase reads currentTimeSeconds while paused for any OTHER
      // purpose (the RAF display loop is cancelled on pause, so the UI's last-shown value is
      // whatever the compensated playing-branch below produced on the final frame before pause,
      // not a fresh paused-state read) -- if a future caller ever needs a compensated value while
      // paused, add a distinct accessor rather than compensating this branch, which would silently
      // reintroduce the resume-rewind bug this comment describes.
      return this.startedAtOffsetSeconds;
    }
    // Clamped so a position within the first ~132ms of playback reads as 0 rather than negative
    // -- downstream consumers (word/pitch-frame lookups, the displayed playhead) don't expect a
    // negative time. Compensation is applied exactly once, here -- the only place any caller
    // should read a latency-compensated value from. Internal re-anchoring uses
    // rawScheduledPositionSeconds() above instead, precisely to keep this a single, un-compounded
    // subtraction no matter how many times pause()/setTempo() run.
    return Math.max(0, this.rawScheduledPositionSeconds() - SOUNDTOUCH_LATENCY_SECONDS);
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
