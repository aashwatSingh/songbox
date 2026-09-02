import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError, type TrackSummary } from "@/lib/api";

const listTracks = vi.fn<() => Promise<TrackSummary[]>>();
const separateTrack = vi.fn();
const transcribeTrack = vi.fn();
const generatePackage = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listTracks: () => listTracks(),
    separateTrack: (trackId: string) => separateTrack(trackId),
    transcribeTrack: (trackId: string) => transcribeTrack(trackId),
    generatePackage: (trackId: string) => generatePackage(trackId),
  };
});

// Imported after the mock so runMissingPipelineStages sees the mocked functions above.
const { runMissingPipelineStages, pendingStages } = await import("@/lib/pipeline");

function track(overrides: Partial<TrackSummary> = {}): TrackSummary {
  return {
    track_id: "t1",
    status: "passed",
    title: null,
    artist: null,
    duration_seconds: 60,
    has_stems: false,
    has_transcription: false,
    bookmarked: false,
    ...overrides,
  };
}

const ALREADY_RUNNING = new ApiError(409, "another job is already running for this track; wait");

beforeEach(() => {
  vi.useFakeTimers();
  listTracks.mockReset();
  separateTrack.mockReset();
  transcribeTrack.mockReset();
  generatePackage.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("pendingStages", () => {
  test("lists only the stages a track still needs", () => {
    expect(pendingStages(track({ has_stems: true, has_transcription: true }))).toEqual([
      "packaging",
    ]);
    expect(pendingStages(track({ has_stems: false, has_transcription: false }))).toEqual([
      "separating",
      "transcribing",
      "packaging",
    ]);
  });
});

describe("runMissingPipelineStages against a 409 from a concurrent run", () => {
  test("waits for a concurrent /separate rather than failing", async () => {
    separateTrack.mockRejectedValueOnce(ALREADY_RUNNING);
    transcribeTrack.mockResolvedValue({});
    generatePackage.mockResolvedValue({});
    // First poll: still running. Second poll: the OTHER caller's job has landed.
    listTracks
      .mockResolvedValueOnce([track({ has_stems: false })])
      .mockResolvedValueOnce([track({ has_stems: true })]);

    const stages: string[] = [];
    const promise = runMissingPipelineStages(track(), (s) => stages.push(s));

    await vi.advanceTimersByTimeAsync(4000);
    await vi.advanceTimersByTimeAsync(4000);
    await promise;

    // The stage that hit the 409 is never retried directly -- its result comes from observing
    // has_stems flip to true, which is the whole point: retrying would be the exact double-run
    // /separate's own idempotency guard and this lock exist to prevent.
    expect(separateTrack).toHaveBeenCalledTimes(1);
    expect(stages).toEqual(["separating", "transcribing", "packaging"]);
  });

  test("does not treat a 409 that isn't the lock message as retryable", async () => {
    const rateLimited = new ApiError(409, "some unrelated conflict");
    separateTrack.mockRejectedValueOnce(rateLimited);

    await expect(runMissingPipelineStages(track(), () => {})).rejects.toBe(rateLimited);
    expect(listTracks).not.toHaveBeenCalled();
  });

  test("does not treat a non-409 ApiError as retryable", async () => {
    const serverError = new ApiError(500, "internal error");
    separateTrack.mockRejectedValueOnce(serverError);

    await expect(runMissingPipelineStages(track(), () => {})).rejects.toBe(serverError);
  });

  test("packaging has no has_* flag to watch, so it retries the call once the lock clears", async () => {
    const t = track({ has_stems: true, has_transcription: true });
    generatePackage.mockRejectedValueOnce(ALREADY_RUNNING).mockResolvedValueOnce({});
    listTracks.mockResolvedValueOnce([t]);

    const promise = runMissingPipelineStages(t, () => {});
    await vi.advanceTimersByTimeAsync(4000);
    await promise;

    expect(generatePackage).toHaveBeenCalledTimes(2);
  });

  test("propagates a real failure surfaced while waiting on a retried call", async () => {
    const t = track({ has_stems: true, has_transcription: true });
    const realFailure = new Error("packaging genuinely failed");
    generatePackage.mockRejectedValueOnce(ALREADY_RUNNING).mockRejectedValueOnce(realFailure);
    listTracks.mockResolvedValueOnce([t]);

    const promise = runMissingPipelineStages(t, () => {});
    await vi.advanceTimersByTimeAsync(4000);

    await expect(promise).rejects.toBe(realFailure);
  });

  test("throws if the track disappears while waiting", async () => {
    separateTrack.mockRejectedValueOnce(ALREADY_RUNNING);
    listTracks.mockResolvedValueOnce([]); // track_id no longer present

    const promise = runMissingPipelineStages(track(), () => {});
    await vi.advanceTimersByTimeAsync(4000);

    await expect(promise).rejects.toThrow(/disappeared/);
  });
});
