"use client";

import { useEffect, useState } from "react";
import {
  COLD_START_EXTRA_SECONDS,
  estimateStageSeconds,
  estimateTotalSeconds,
  formatElapsed,
  formatEstimate,
} from "@/lib/estimates";
import { PIPELINE_STAGE_LABELS, type PipelineStage } from "@/lib/pipeline";

/**
 * IMPORTANT: this is a TIME-BASED estimate, not real progress.
 *
 * Every pipeline stage is a single blocking HTTP request -- the backend has no job queue and
 * reports nothing while it works, so there is genuinely no completion percentage to read. The bar
 * fills against the measured estimates in lib/estimates.ts.
 *
 * Two consequences are handled deliberately rather than hidden:
 *  - The current stage's fill is capped below 100% until the request actually returns, so the bar
 *    can never sit at "done" while work is still running.
 *  - Once elapsed passes the estimate, the copy switches from a countdown to "still working" and
 *    says the job has not stalled, instead of showing a negative or frozen remaining time. It
 *    deliberately does NOT say "taking longer than expected": that phrasing made a healthy
 *    six-minute run look hung when the estimate was calibrated against the wrong model.
 */
const CURRENT_STAGE_FILL_CAP = 0.92;
const TICK_MS = 250;

interface PipelineProgressProps {
  stages: readonly PipelineStage[];
  currentStage: PipelineStage | null;
  trackDurationSeconds: number | null;
  startedAt: number;
}

export function PipelineProgress({
  stages,
  currentStage,
  trackDurationSeconds,
  startedAt,
}: PipelineProgressProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const elapsedSeconds = Math.max(0, (now - startedAt) / 1000);

  // Without a duration the estimate has no basis, so show elapsed time only rather than invent one.
  if (trackDurationSeconds === null || stages.length === 0) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm">
          {currentStage ? PIPELINE_STAGE_LABELS[currentStage] : "Starting…"}
        </p>
        <p className="text-xs text-muted">Elapsed {formatElapsed(elapsedSeconds)}</p>
      </div>
    );
  }

  const totalEstimate = estimateTotalSeconds(stages, trackDurationSeconds);
  const currentIndex = currentStage === null ? 0 : Math.max(0, stages.indexOf(currentStage));

  const completedEstimate = stages
    .slice(0, currentIndex)
    .reduce((sum, stage) => sum + estimateStageSeconds(stage, trackDurationSeconds), 0);
  const currentEstimate = estimateStageSeconds(stages[currentIndex], trackDurationSeconds);

  // Elapsed is measured across the whole chain, so subtract the stages already behind us to get
  // roughly how long the current one has been running.
  const currentElapsed = Math.max(0, elapsedSeconds - completedEstimate);
  const currentFill = Math.min(currentElapsed / Math.max(currentEstimate, 1), CURRENT_STAGE_FILL_CAP);
  const fraction = Math.min((completedEstimate + currentFill * currentEstimate) / totalEstimate, 1);

  const remaining = totalEstimate - elapsedSeconds;
  const overrun = remaining <= 0;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-sm">
          {currentStage ? PIPELINE_STAGE_LABELS[currentStage] : "Starting…"}
          <span className="ml-2 text-xs text-muted">
            step {currentIndex + 1} of {stages.length}
          </span>
        </p>
        <p className="text-xs text-muted tabular-nums">
          {formatElapsed(elapsedSeconds)}
          {overrun ? " · still working" : ` · ~${formatEstimate(remaining)} left`}
        </p>
      </div>

      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-surface-hover"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction * 100)}
        aria-label="Karaoke generation progress"
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-300 ease-linear"
          style={{ width: `${fraction * 100}%` }}
        />
      </div>

      {overrun ? (
        // Past the estimate, a countdown is worthless and "taking longer than expected" reads as a
        // hang -- which is exactly how a healthy six-minute run looked when the estimate was
        // calibrated to the wrong model. Say what is actually true: the job is still running,
        // nothing is stuck, and the estimate was the thing that was wrong.
        <p className="text-xs text-muted">
          Past the {formatEstimate(totalEstimate)} estimate, but still running &mdash; the job has
          not stalled. Long tracks and the larger transcription model can take several minutes.
        </p>
      ) : (
        <p className="text-xs text-muted">
          Estimated {formatEstimate(totalEstimate)} total. First run on a new machine takes up to
          ~{Math.round(COLD_START_EXTRA_SECONDS)}s longer while model weights download.
        </p>
      )}
    </div>
  );
}
