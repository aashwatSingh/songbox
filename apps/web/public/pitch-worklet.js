// AudioWorkletProcessor implementing the YIN pitch-detection algorithm (de Cheveigne & Kawahara,
// 2002) over a rolling analysis window. Runs entirely inside the audio rendering thread -- no
// imports (worklet modules execute in an isolated global scope separate from the app's bundle),
// no dependency on anything outside this file.
//
// process() delivers exactly 128 sample frames per call (the fixed Web Audio render quantum) --
// far too short a window to resolve a vocal fundamental (an 80Hz note needs ~551 samples at
// 44.1kHz for even one period, and YIN needs at least two to find a match). This processor
// accumulates incoming blocks into its own ring buffer and only runs YIN once enough new samples
// have arrived to advance by one hop.

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
