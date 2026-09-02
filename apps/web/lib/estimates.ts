import type { PipelineStage } from "@/lib/pipeline";

/**
 * Per-stage runtime estimates for the local CPU backend.
 *
 * These are NOT guesses. Each stage is a two-point linear fit -- `fixedSeconds + perAudioSecond *
 * trackDuration` -- through timings measured on this machine on 2026-09-02, models already warm:
 *
 *   track length   separate   transcribe   package
 *   15.45s            7.0s         3.0s      4.0s
 *   232.0s           79.0s        47.0s     23.9s
 *
 * Predicted total for the 232s track is ~151s against ~150s measured. The separation rate
 * (0.33 s per audio-second, i.e. ~3x realtime) independently matches the htdemucs figure in
 * docs/BENCHMARKS.md, which is the main reason to trust the fit at all.
 *
 * Known limits, stated rather than papered over:
 *  - Two points per stage is a weak fit. It is fine for "about two minutes" and should not be
 *    read as precise.
 *  - Measured on ONE machine with a CPU backend. A different box, or the Modal GPU backend, will
 *    not match. Nothing here is used for billing or capacity planning -- only for a progress hint.
 *  - The FIRST run downloads model weights (Demucs from HuggingFace, Whisper from Systran), which
 *    added tens of seconds in the cold run measured this session. COLD_START_EXTRA_SECONDS is a
 *    deliberately rough allowance for that, not a fitted value.
 */
interface StageRate {
  fixedSeconds: number;
  perAudioSecond: number;
}

const STAGE_RATES: Record<PipelineStage, StageRate> = {
  separating: { fixedSeconds: 1.9, perAudioSecond: 0.333 },
  transcribing: { fixedSeconds: 0.0, perAudioSecond: 0.203 },
  packaging: { fixedSeconds: 2.6, perAudioSecond: 0.092 },
};

/** Rough allowance for the first-ever run, which downloads model weights before any work starts. */
export const COLD_START_EXTRA_SECONDS = 60;

export function estimateStageSeconds(stage: PipelineStage, trackDurationSeconds: number): number {
  const rate = STAGE_RATES[stage];
  return rate.fixedSeconds + rate.perAudioSecond * trackDurationSeconds;
}

export function estimateTotalSeconds(
  stages: readonly PipelineStage[],
  trackDurationSeconds: number,
): number {
  return stages.reduce((total, stage) => total + estimateStageSeconds(stage, trackDurationSeconds), 0);
}

/** "about 2 min", "about 45 sec" -- deliberately coarse, because the estimate is coarse. */
export function formatEstimate(seconds: number): string {
  if (seconds < 90) {
    return `about ${Math.max(5, Math.round(seconds / 5) * 5)} sec`;
  }
  return `about ${Math.round(seconds / 30) / 2} min`;
}

/** "0:07", "2:31" -- used for elapsed time, which is exact and needs no hedging. */
export function formatElapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${(whole % 60).toString().padStart(2, "0")}`;
}
