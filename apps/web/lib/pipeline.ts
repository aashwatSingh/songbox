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
  if (!track.has_stems) {
    onStageStart("separating");
    await separateTrack(track.track_id);
  }
  if (!track.has_transcription) {
    onStageStart("transcribing");
    await transcribeTrack(track.track_id);
  }
  onStageStart("packaging");
  await generatePackage(track.track_id);
}

export const PIPELINE_STAGE_LABELS: Record<PipelineStage, string> = {
  separating: "Separating stems…",
  transcribing: "Transcribing lyrics…",
  packaging: "Building package…",
};
