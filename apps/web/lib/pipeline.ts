import {
  ApiError,
  generatePackage,
  listTracks,
  separateTrack,
  transcribeTrack,
  type TrackSummary,
} from "@/lib/api";

export type PipelineStage = "separating" | "transcribing" | "packaging";

/**
 * Runs whichever of separate/transcribe/package a track still needs, based on its real
 * has_stems/has_transcription state (not client-side memory of what already ran this session --
 * that would be lost on a page refresh). Never calls separate() if has_stems is already true:
 * The backend now enforces this too -- /separate returns existing stems instead of producing a
 * second set, and every pipeline stage refuses a concurrent run for the same track with a 409 --
 * so a duplicate chain wastes a request rather than corrupting data. Skipping finished stages here
 * is still what keeps a retry cheap and avoids that 409 in the first place.
 *
 * Callers should re-fetch the track's current TrackSummary (via listTracks()) before each retry
 * attempt, rather than reusing a stale snapshot, so a partially-completed chain resumes correctly.
 */
export async function runMissingPipelineStages(
  track: Pick<TrackSummary, "track_id" | "has_stems" | "has_transcription">,
  onStageStart: (stage: PipelineStage) => void,
): Promise<void> {
  for (const stage of pendingStages(track)) {
    onStageStart(stage);
    if (stage === "separating") {
      await runOrWaitForExisting(track.track_id, () => separateTrack(track.track_id), (t) => t.has_stems);
    } else if (stage === "transcribing") {
      await runOrWaitForExisting(
        track.track_id,
        () => transcribeTrack(track.track_id),
        (t) => t.has_transcription,
      );
    } else {
      await runOrWaitForExisting(track.track_id, () => generatePackage(track.track_id), null);
    }
  }
}

/** How often to re-check whether the job someone else started has finished. */
const ALREADY_RUNNING_POLL_MS = 4000;

/**
 * Run one stage; if the backend says a job is already in flight for this track, WAIT for it
 * instead of failing.
 *
 * The per-track advisory lock returns 409 to the second caller by design -- that is what stops
 * duplicate stem sets. But "another job is already running" is not a failure from the user's point
 * of view, it is a reason to keep waiting: a second tab, a double-click, or a page refresh
 * mid-chain would otherwise surface a hard red error while the work was completing perfectly well.
 *
 * `isDone` reads the track's real server-side state, so this waits on the OTHER request's actual
 * progress rather than a timer. Passing null means the stage has no has_* flag to watch (packaging
 * has none), in which case we wait for the lock to clear and retry the call once.
 */
async function runOrWaitForExisting(
  trackId: string,
  run: () => Promise<unknown>,
  isDone: ((track: TrackSummary) => boolean) | null,
): Promise<void> {
  try {
    await run();
    return;
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 409 || !err.message.includes("already running")) {
      throw err;
    }
  }

  // Someone else holds the lock. Poll until their job lands, then either accept their result or
  // -- for a stage with nothing to observe -- retry now that the lock is free.
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, ALREADY_RUNNING_POLL_MS));
    const current = (await listTracks()).find((t) => t.track_id === trackId);
    if (!current) {
      throw new Error("track disappeared while waiting for an in-flight job");
    }
    if (isDone === null) {
      try {
        await run();
        return;
      } catch (err) {
        if (err instanceof ApiError && err.status === 409 && err.message.includes("already running")) {
          continue;
        }
        throw err;
      }
    }
    if (isDone(current)) {
      return;
    }
  }
}

/**
 * Which stages this track still needs, in run order. Derived from the same real has_stems /
 * has_transcription state runMissingPipelineStages uses, so a progress bar built from this spans
 * exactly the work that is actually going to happen -- resuming a half-finished track shows a bar
 * for the remaining stages only, instead of pretending it is starting from scratch.
 */
export function pendingStages(
  track: Pick<TrackSummary, "has_stems" | "has_transcription">,
): PipelineStage[] {
  const stages: PipelineStage[] = [];
  if (!track.has_stems) {
    stages.push("separating");
  }
  if (!track.has_transcription) {
    stages.push("transcribing");
  }
  stages.push("packaging");
  return stages;
}

export const PIPELINE_STAGE_LABELS: Record<PipelineStage, string> = {
  separating: "Separating stems…",
  transcribing: "Transcribing lyrics…",
  packaging: "Building package…",
};
