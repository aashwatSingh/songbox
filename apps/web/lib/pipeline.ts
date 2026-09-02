import { generatePackage, separateTrack, transcribeTrack, type TrackSummary } from "@/lib/api";

export type PipelineStage = "separating" | "transcribing" | "packaging";

/**
 * Runs whichever of separate/transcribe/package a track still needs, based on its real
 * has_stems/has_transcription state (not client-side memory of what already ran this session --
 * that would be lost on a page refresh). Never calls separate() if has_stems is already true:
 * POST /tracks/{id}/separate has no idempotency guard on the backend (a second call writes a
 * second full set of Stem rows, and which set later stages use becomes arbitrary), so retrying
 * a failed chain must always re-check real state, never blindly restart from the top.
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
      await separateTrack(track.track_id);
    } else if (stage === "transcribing") {
      await transcribeTrack(track.track_id);
    } else {
      await generatePackage(track.track_id);
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
