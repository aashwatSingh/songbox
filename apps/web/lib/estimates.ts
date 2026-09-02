import type { PipelineStage } from "@/lib/pipeline";

/**
 * Per-stage runtime estimates for the local CPU backend.
 *
 * Measured, not guessed -- each stage is a two-point linear fit
 * (`fixedSeconds + perAudioSecond * trackDuration`) through timings taken on this machine:
 *
 *   track length   separate   transcribe   package
 *   25.7s            21.8s        46.8s     15.7s
 *   231.9s          148.5s*      432.6s     46.1s*
 *
 * The two transcribe figures and the whole 25.7s row are clean. The starred 231.9s separate and
 * package figures were taken while another job was competing for the CPU, so they are high and the
 * two rates derived from them are conservative -- those stages will tend to finish early. Marked
 * rather than silently trusted; re-measure them on an idle machine to tighten the fit.
 *
 * The transcribe numbers are the important ones. An earlier version of this file was fitted
 * against the `base` Whisper model and never re-measured after the default moved to `medium` to
 * fix a real mistranscription. `medium` is ~9x slower: the 232s track was predicted at 47s and
 * actually took 432.6s (the server's own job_cost log). The bar therefore blew past its estimate
 * about a minute into a perfectly healthy six-minute run and displayed "taking longer than
 * expected" for the rest of it, which read as a hang. These figures come from real runs under the
 * current defaults.
 *
 * Known limits, stated rather than papered over:
 *  - Two points per stage is a weak fit; good for "about six minutes", not for precision.
 *  - One machine, CPU only. There is an idle RTX 4060 in this box -- installing a CUDA torch build
 *    would invalidate every number here (in the good direction) and require a re-measure.
 *  - Timings vary with machine load. Several measurements this session were thrown off by other
 *    jobs running concurrently; these were taken with the pipeline as the only heavy work.
 *  - The FIRST run downloads model weights. COLD_START_EXTRA_SECONDS is a rough allowance for
 *    that, not a fitted value.
 */
interface StageRate {
  fixedSeconds: number;
  perAudioSecond: number;
}

const STAGE_RATES: Record<PipelineStage, StageRate> = {
  // (148.5 - 21.8) / (231.9 - 25.7) = 0.614 s per audio-second; intercept 21.8 - 0.614*25.7 = 6.0
  separating: { fixedSeconds: 6.0, perAudioSecond: 0.614 },
  // (432.6 - 46.8) / (231.9 - 25.7) = 1.871; intercept 46.8 - 1.871*25.7 = -1.3, clamped to 0
  transcribing: { fixedSeconds: 0.0, perAudioSecond: 1.871 },
  // (46.1 - 15.7) / (231.9 - 25.7) = 0.147; intercept 15.7 - 0.147*25.7 = 11.9
  packaging: { fixedSeconds: 11.9, perAudioSecond: 0.147 },
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
