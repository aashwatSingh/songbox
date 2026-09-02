import type { PipelineStage } from "@/lib/pipeline";

/**
 * Per-stage runtime estimates for the local GPU backend (CUDA, RTX 4060).
 *
 * Measured, not guessed -- each stage is a two-point linear fit
 * (`fixedSeconds + perAudioSecond * trackDuration`) through timings taken from the server's own
 * job_cost log, running the real pipeline end to end:
 *
 *   track length   separate   transcribe   package
 *   25.7s             4.3s         8.0s      9.4s
 *   231.8s           21.1s        69.6s     77.0s
 *
 * These replace a CPU-era fit. The machine now has a CUDA torch build, and the same 231.8s track
 * that took 432.6s to transcribe on CPU takes 69.6s -- so the old numbers over-estimated by
 * roughly 4-6x. An over-estimate is the safe direction (the bar finishes early rather than
 * looking hung), but it is still wrong, so it is refit rather than left.
 *
 * Known limits, stated rather than papered over:
 *  - Two points per stage is a weak fit; good for "about a minute", not for precision.
 *  - One machine, one GPU. A CPU-only box will be far slower than these numbers suggest -- the
 *    CPU-era fit for reference was separate 0.614, transcribe 1.871, package 0.147 s per
 *    audio-second.
 *  - Timings vary with machine load; these were taken with the pipeline as the only heavy work.
 *  - The FIRST run downloads model weights. COLD_START_EXTRA_SECONDS is a rough allowance for
 *    that, not a fitted value.
 */
interface StageRate {
  fixedSeconds: number;
  perAudioSecond: number;
}

const STAGE_RATES: Record<PipelineStage, StageRate> = {
  // (21.102 - 4.349) / (231.786 - 25.685) = 0.0813 s per audio-second; intercept 2.26
  separating: { fixedSeconds: 2.3, perAudioSecond: 0.081 },
  // (69.649 - 8.045) / 206.101 = 0.2989; intercept 0.37
  transcribing: { fixedSeconds: 0.4, perAudioSecond: 0.299 },
  // (77.011 - 9.422) / 206.101 = 0.3279; intercept 1.00. Package only became competitive after
  // torchcrepe got an explicit batch_size -- unbatched on GPU it measured 170.3s here.
  packaging: { fixedSeconds: 1.0, perAudioSecond: 0.328 },
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
