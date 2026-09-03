"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  deleteTrack,
  listTracks,
  logout,
  toggleBookmark,
  uploadTrack,
  type TrackSummary,
} from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";
import { pendingStages, runMissingPipelineStages, type PipelineStage } from "@/lib/pipeline";
import { PipelineProgress } from "@/components/PipelineProgress";

function MusicNoteIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="h-8 w-8">
      <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="18" cy="16" r="3" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="h-3.5 w-3.5">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className="h-3.5 w-3.5">
      <path d="M6 4l14 8-14 8V4z" />
    </svg>
  );
}

function BookmarkIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={1.5}
      className="h-4 w-4"
    >
      <path d="M6 3h12v18l-6-4-6 4V3z" strokeLinejoin="round" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="h-4 w-4">
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="h-4 w-4">
      <path d="M12 16V4M7 9l5-5 5 5M4 20h16" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function formatDuration(seconds: number | null): string | null {
  if (seconds === null) {
    return null;
  }
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function TracksPage() {
  const router = useRouter();
  const { user, loading: authLoading, refresh } = useAuth();
  const [tracks, setTracks] = useState<TrackSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  // Multiple files, not one. The server processes a single heavy job at a time (one global
  // inference lock), so a batch is uploaded and processed SEQUENTIALLY -- firing them in parallel
  // would just queue on that lock while making failures harder to attribute to a file.
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadArtist, setUploadArtist] = useState("");
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [busyTrackId, setBusyTrackId] = useState<string | null>(null);
  // Tracks the auto-processing chain (separate -> transcribe -> package) that kicks off right
  // after a passed upload. processingTrackId is null when nothing is running.
  const [processingTrackId, setProcessingTrackId] = useState<string | null>(null);
  const [processingStage, setProcessingStage] = useState<PipelineStage | null>(null);
  const [processingError, setProcessingError] = useState<string | null>(null);
  // Stages this run will actually execute, the track's duration, and the start time -- the
  // three inputs PipelineProgress needs to size the bar to the real remaining work.
  const [processingStages, setProcessingStages] = useState<PipelineStage[]>([]);
  const [processingDuration, setProcessingDuration] = useState<number | null>(null);
  const [processingStartedAt, setProcessingStartedAt] = useState<number>(0);
  // Batch bookkeeping, shown as "song 2 of 5" alongside the per-track progress bar.
  const [batchTotal, setBatchTotal] = useState(0);
  const [batchDone, setBatchDone] = useState(0);
  const [batchLabel, setBatchLabel] = useState<string | null>(null);
  const [batchFailures, setBatchFailures] = useState<string[]>([]);
  // Tracks the user has ticked for a bulk "Generate lyrics" run.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [dragActive, setDragActive] = useState(false);
  // The file input stays mounted at all times, even while the form is hidden. Browsers only honor
  // input.click() from inside a real user gesture, so "Upload track" has to reach an already-
  // mounted node synchronously in its own onClick -- mounting the input as a side effect of
  // opening the form and clicking it afterwards loses the gesture and the picker silently
  // never opens.
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Prefill the title from the filename so uploads stop defaulting to "Untitled", but never
  // overwrite something already typed. With several files the per-file title is derived from each
  // filename instead, so the shared Title box only applies to a single-file upload.
  const selectFiles = (files: File[]) => {
    setUploadFiles(files);
    if (files.length > 0) {
      setUploadError(null);
      if (files.length === 1) {
        setUploadTitle((current) => current || files[0].name.replace(/\.[^.]+$/, ""));
      }
    }
  };

  const titleForFile = (file: File, index: number, total: number): string =>
    total === 1 && uploadTitle ? uploadTitle : file.name.replace(/\.[^.]+$/, "");

  const openFilePicker = () => {
    setShowUpload(true);
    fileInputRef.current?.click();
  };

  const reloadTracks = () => {
    listTracks()
      .then(setTracks)
      .catch((err: Error) => setError(`Could not load tracks: ${err.message}`));
  };

  useEffect(() => {
    if (!authLoading && user === null) {
      router.push("/login");
    }
  }, [authLoading, user, router]);

  useEffect(() => {
    if (user === null) {
      return;
    }
    reloadTracks();
  }, [user]);

  // Fetches the track's CURRENT has_stems/has_transcription (never trusts a stale snapshot) and
  // runs whichever pipeline stages it's still missing. Safe to call again after a failure -- it
  // re-checks real state each time, so it never re-runs a stage that already succeeded (see
  // runMissingPipelineStages' own docstring for why that matters specifically for /separate).
  const runPipelineFor = async (trackId: string) => {
    setProcessingTrackId(trackId);
    setProcessingError(null);
    try {
      const current = await listTracks();
      const track = current.find((t) => t.track_id === trackId);
      if (!track) {
        throw new Error("track not found");
      }
      setProcessingStages(pendingStages(track));
      setProcessingDuration(track.duration_seconds);
      setProcessingStartedAt(Date.now());
      await runMissingPipelineStages(track, setProcessingStage);
      setProcessingTrackId(null);
      setProcessingStage(null);
      reloadTracks();
    } catch (err) {
      setProcessingError(err instanceof Error ? err.message : "Processing failed.");
    }
  };

  /**
   * Uploads every selected file, then runs the pipeline for each that passed the rights gate.
   *
   * Strictly sequential, and deliberately so: the backend serializes heavy jobs behind one global
   * inference lock, so parallel requests would queue there anyway while making it much harder to
   * say which file failed. One slow file also must not abort the rest -- each is caught
   * individually and reported at the end, so a single bad file in a batch of ten does not cost
   * the other nine.
   */
  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (uploadFiles.length === 0) {
      return;
    }
    const files = uploadFiles;
    setUploadError(null);
    setUploading(true);
    setBatchFailures([]);
    setBatchTotal(files.length);
    setBatchDone(0);

    const uploaded: { id: string; label: string }[] = [];
    const failures: string[] = [];

    try {
      for (const [i, file] of files.entries()) {
        setBatchLabel(`Uploading ${file.name}`);
        try {
          const result = await uploadTrack(
            file,
            "I made this recording",
            titleForFile(file, i, files.length),
            uploadArtist
          );
          // Only a "passed" upload has real content to process -- one held for manual review
          // has nothing for separate/transcribe/package to work with yet.
          if (result.status === "passed") {
            uploaded.push({ id: result.track_id, label: file.name });
          } else {
            failures.push(`${file.name}: held for review (${result.status})`);
          }
        } catch (err) {
          failures.push(`${file.name}: ${err instanceof Error ? err.message : "upload failed"}`);
        }
        setBatchDone(i + 1);
      }

      setUploadFiles([]);
      setUploadTitle("");
      setUploadArtist("");
      setShowUpload(false);
      // Clear the input's own value too. Without this, re-picking the SAME file emits no change
      // event (the value is unchanged), so a retry after a failed upload silently selects nothing.
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      reloadTracks();
    } finally {
      setUploading(false);
    }

    if (uploaded.length > 0) {
      await runPipelineForMany(uploaded, failures);
    } else {
      setBatchTotal(0);
      setBatchLabel(null);
      setBatchFailures(failures);
    }
  };

  /**
   * Runs the full pipeline over several tracks, one after another.
   *
   * Shared by the multi-upload path and the "Generate lyrics for selected" button. Each track is
   * isolated: a failure is recorded against that track's name and the run continues, because the
   * alternative -- aborting a ten-song batch on song three -- wastes all the work still queued
   * behind it.
   */
  const runPipelineForMany = async (
    targets: { id: string; label: string }[],
    priorFailures: string[] = []
  ) => {
    const failures = [...priorFailures];
    setBatchTotal(targets.length);
    setBatchDone(0);
    setProcessingError(null);

    for (const [i, target] of targets.entries()) {
      setBatchLabel(target.label);
      setProcessingTrackId(target.id);
      try {
        // Re-read real state per track rather than trusting a snapshot taken before the batch
        // started -- earlier tracks in this same run change what later ones still need.
        const current = await listTracks();
        const track = current.find((t) => t.track_id === target.id);
        if (!track) {
          throw new Error("track not found");
        }
        setProcessingStages(pendingStages(track));
        setProcessingDuration(track.duration_seconds);
        setProcessingStartedAt(Date.now());
        await runMissingPipelineStages(track, setProcessingStage);
      } catch (err) {
        failures.push(
          `${target.label}: ${err instanceof Error ? err.message : "processing failed"}`
        );
      }
      setBatchDone(i + 1);
      setProcessingStage(null);
    }

    setProcessingTrackId(null);
    setBatchLabel(null);
    setBatchTotal(0);
    setBatchFailures(failures);
    setSelectedIds(new Set());
    reloadTracks();
  };

  const handleGenerateSelected = async () => {
    if (tracks === null || selectedIds.size === 0) {
      return;
    }
    const selected = tracks.filter((t) => selectedIds.has(t.track_id));
    // Skip anything that already has lyrics. pendingStages() always ends with "packaging", so a
    // fully-processed track would otherwise be re-packaged for nothing -- observed live producing
    // a second karaoke_packages row per track. The button says "generate lyrics", so a track that
    // already has them is a no-op worth reporting rather than minutes of redundant GPU work.
    const needsWork = selected.filter((t) => !t.has_transcription);
    const skipped = selected.filter((t) => t.has_transcription);

    if (needsWork.length === 0) {
      setBatchFailures(
        skipped.map((t) => `${t.title ?? "Untitled"}: already has lyrics, nothing to do`)
      );
      setSelectedIds(new Set());
      return;
    }

    await runPipelineForMany(
      needsWork.map((t) => ({ id: t.track_id, label: t.title ?? "Untitled" })),
      skipped.map((t) => `${t.title ?? "Untitled"}: already has lyrics, skipped`)
    );
  };

  /** Ticks every track that has no lyrics yet -- the whole point of a bulk generate. */
  const selectAllMissingLyrics = () => {
    if (tracks === null) {
      return;
    }
    setSelectedIds(new Set(tracks.filter((t) => !t.has_transcription).map((t) => t.track_id)));
  };

  const toggleSelected = (trackId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(trackId)) {
        next.delete(trackId);
      } else {
        next.add(trackId);
      }
      return next;
    });
  };

  const handleToggleBookmark = async (trackId: string) => {
    setBusyTrackId(trackId);
    try {
      await toggleBookmark(trackId);
      reloadTracks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bookmark failed.");
    } finally {
      setBusyTrackId(null);
    }
  };

  const handleDelete = async (trackId: string) => {
    if (!window.confirm("Delete this track? This can't be undone.")) {
      return;
    }
    setBusyTrackId(trackId);
    try {
      await deleteTrack(trackId);
      reloadTracks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed.");
    } finally {
      setBusyTrackId(null);
    }
  };

  const handleLogout = async () => {
    try {
      await logout();
    } finally {
      await refresh();
      router.push("/login");
    }
  };

  if (authLoading || user === null) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted">Loading...</p>
      </main>
    );
  }

  const bookmarkedCount = tracks?.filter((t) => t.bookmarked).length ?? 0;

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between px-8 py-5 border-b border-surface-border">
        <span className="text-xl font-bold tracking-tight">SongBox</span>
        <nav className="flex items-center gap-6">
          <Link href="/tracks" className="text-sm font-medium text-accent">
            Library
          </Link>
          <Link href="/review" className="text-sm font-medium text-muted hover:text-foreground">
            Review
          </Link>
        </nav>
        <div className="flex items-center gap-4">
          <button
            onClick={openFilePicker}
            className="flex items-center gap-2 rounded bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-hover transition-colors"
          >
            <UploadIcon />
            Upload track
          </button>
          <button onClick={() => void handleLogout()} className="text-sm text-muted hover:text-foreground">
            Log out
          </button>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-8 py-10">
        <h1 className="text-4xl font-extrabold tracking-tight">Your tracks</h1>
        <p className="mt-2 text-sm text-muted">
          {tracks?.length ?? 0} track{tracks?.length === 1 ? "" : "s"} · {bookmarkedCount} bookmarked
        </p>

        {processingTrackId && (
          <div className="mt-6 flex items-center justify-between gap-4 rounded-lg border border-surface-border bg-surface p-4 max-w-md">
            {processingError ? (
              <>
                <p className="text-sm text-red-400">{processingError}</p>
                <button
                  onClick={() => void runPipelineFor(processingTrackId)}
                  className="shrink-0 rounded border border-accent px-3 py-1.5 text-sm font-medium text-accent hover:bg-surface-hover transition-colors"
                >
                  Retry
                </button>
              </>
            ) : (
              <div className="w-full">
                {batchTotal > 1 && (
                  <p className="mb-2 text-xs font-medium text-muted">
                    Song {Math.min(batchDone + 1, batchTotal)} of {batchTotal}
                    {batchLabel && <span className="text-foreground"> &middot; {batchLabel}</span>}
                  </p>
                )}
                <PipelineProgress
                  stages={processingStages}
                  currentStage={processingStage}
                  trackDurationSeconds={processingDuration}
                  startedAt={processingStartedAt}
                />
              </div>
            )}
          </div>
        )}

        {/* Per-song failures from a batch. Shown after the run rather than aborting it: one bad
            file in a batch of ten must not cost the other nine. */}
        {batchFailures.length > 0 && (
          <div className="mt-4 rounded-lg border border-surface-border bg-surface p-4 max-w-md">
            <div className="flex items-start justify-between gap-4">
              <p className="text-sm font-medium text-red-400">
                {batchFailures.length} song{batchFailures.length === 1 ? "" : "s"} could not be
                processed
              </p>
              <button
                onClick={() => setBatchFailures([])}
                className="shrink-0 text-xs text-muted hover:text-foreground"
              >
                Dismiss
              </button>
            </div>
            <ul className="mt-2 flex flex-col gap-1">
              {batchFailures.map((f) => (
                <li key={f} className="text-xs text-muted">
                  {f}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Bulk actions appear only once something is ticked, so the normal library view stays
            uncluttered. Disabled while a run is in flight -- the backend serializes heavy jobs
            anyway, and starting a second batch mid-run would just pile up 409s. */}
        {tracks !== null && tracks.some((t) => !t.has_transcription) && selectedIds.size === 0 && (
          <button
            onClick={selectAllMissingLyrics}
            className="mt-4 rounded border border-surface-border px-3 py-1.5 text-xs font-medium text-muted hover:border-accent hover:text-foreground transition-colors"
          >
            Select all {tracks.filter((t) => !t.has_transcription).length} without lyrics
          </button>
        )}

        {selectedIds.size > 0 && (
          <div className="mt-4 flex items-center gap-3 rounded-lg border border-accent bg-surface p-4 max-w-md">
            <p className="text-sm">
              {selectedIds.size} selected
            </p>
            <button
              onClick={() => void handleGenerateSelected()}
              disabled={processingTrackId !== null || uploading}
              className="rounded bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50 hover:bg-accent-hover transition-colors"
            >
              Generate lyrics for selected
            </button>
            <button
              onClick={() => setSelectedIds(new Set())}
              className="text-xs text-muted hover:text-foreground"
            >
              Clear
            </button>
          </div>
        )}

        {/* Mounted unconditionally, outside the collapsible form -- see fileInputRef's note: the
            header button must be able to click it synchronously within its own user gesture. */}
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*"
          multiple
          onChange={(e) => selectFiles(Array.from(e.target.files ?? []))}
          className="hidden"
        />

        {showUpload && (
          <form
            onSubmit={(e) => void handleUpload(e)}
            className="mt-6 flex flex-col gap-3 rounded-lg border border-surface-border bg-surface p-5 max-w-md"
          >
            {uploadFiles.length > 1 ? (
              // A single Title box cannot name five different songs. With a batch, each track is
              // titled from its own filename instead -- the same rule the single-file path already
              // used as its default.
              <p className="rounded border border-surface-border bg-background px-3 py-2 text-sm text-muted">
                {uploadFiles.length} songs selected &mdash; each is titled from its filename.
              </p>
            ) : (
              <label className="flex flex-col gap-1">
                <span className="text-sm font-medium">Title</span>
                <input
                  type="text"
                  value={uploadTitle}
                  onChange={(e) => setUploadTitle(e.target.value)}
                  placeholder="Track title"
                  className="rounded border border-surface-border bg-background px-3 py-2 text-sm"
                />
              </label>
            )}
            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium">
                Artist{uploadFiles.length > 1 && " (applied to all)"}
              </span>
              <input
                type="text"
                value={uploadArtist}
                onChange={(e) => setUploadArtist(e.target.value)}
                placeholder="Artist name"
                className="rounded border border-surface-border bg-background px-3 py-2 text-sm"
              />
            </label>
            <div className="flex flex-col gap-1">
              <span className="text-sm font-medium">Audio files</span>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={() => setDragActive(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragActive(false);
                  selectFiles(Array.from(e.dataTransfer.files ?? []));
                }}
                className={`rounded border border-dashed px-4 py-6 text-sm transition-colors ${
                  dragActive
                    ? "border-accent bg-surface-hover text-foreground"
                    : "border-surface-border text-muted hover:border-accent hover:text-foreground"
                }`}
              >
                {uploadFiles.length === 1 ? (
                  <span className="text-foreground font-medium">{uploadFiles[0].name}</span>
                ) : uploadFiles.length > 1 ? (
                  <span className="text-foreground font-medium">
                    {uploadFiles.length} files selected
                  </span>
                ) : (
                  <>
                    Choose files<span className="text-muted"> or drag them here</span>
                  </>
                )}
              </button>
            </div>
            {uploadError && <p className="text-red-400 text-sm">{uploadError}</p>}
            <button
              type="submit"
              disabled={uploadFiles.length === 0 || uploading}
              className="self-start rounded bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50 hover:bg-accent-hover transition-colors"
            >
              {uploading
                ? batchTotal > 1
                  ? `Uploading ${batchDone + 1} of ${batchTotal}...`
                  : "Uploading..."
                : uploadFiles.length > 1
                  ? `Upload ${uploadFiles.length} songs`
                  : "Upload"}
            </button>
          </form>
        )}

        {error && <p className="mt-6 text-red-400 text-sm">{error}</p>}

        {tracks === null ? (
          <p className="mt-8 text-muted">Loading tracks...</p>
        ) : tracks.length === 0 ? (
          <p className="mt-8 text-muted">No tracks yet.</p>
        ) : (
          <div className="mt-8 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
            {tracks.map((track) => {
              const duration = formatDuration(track.duration_seconds);
              const busy = busyTrackId === track.track_id;
              return (
                <div
                  key={track.track_id}
                  className="rounded-lg border border-surface-border bg-surface overflow-hidden flex flex-col"
                >
                  <div className="relative aspect-square bg-gradient-to-br from-[#2b3018] to-[#14150d] flex items-center justify-center text-[#9ba385]">
                    <MusicNoteIcon />
                    {/* Selection for bulk lyric generation. Sits on the artwork so it is reachable
                        without adding a row to every card. */}
                    <label
                      className="absolute left-2 top-2 flex cursor-pointer items-center gap-1.5 rounded bg-background/80 px-2 py-1 text-xs"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds.has(track.track_id)}
                        onChange={() => toggleSelected(track.track_id)}
                        className="accent-[#e2431f]"
                        aria-label={`Select ${track.title ?? "Untitled"} for bulk generation`}
                      />
                      <span className="text-muted">Select</span>
                    </label>
                    {!track.has_transcription && (
                      <span className="absolute right-2 top-2 rounded bg-background/80 px-2 py-1 text-xs text-muted">
                        No lyrics
                      </span>
                    )}
                  </div>
                  <div className="p-4 flex flex-col gap-1 flex-1">
                    <span className="text-xs font-semibold tracking-wide text-accent uppercase">
                      Track
                    </span>
                    <h3 className="font-bold leading-tight truncate">
                      {track.title ?? "Untitled"}
                    </h3>
                    <p className="text-sm text-muted truncate">{track.artist ?? "Unknown artist"}</p>
                    {duration && (
                      <p className="mt-1 flex items-center gap-1 text-xs text-muted">
                        <ClockIcon />
                        {duration}
                      </p>
                    )}
                    <div className="mt-3 flex items-center gap-2">
                      <Link
                        href={`/tracks/${track.track_id}/play`}
                        className="flex-1 flex items-center justify-center gap-1.5 rounded border border-surface-border py-2 text-sm font-medium hover:bg-surface-hover transition-colors"
                      >
                        <PlayIcon />
                        View
                      </Link>
                      <button
                        onClick={() => void handleToggleBookmark(track.track_id)}
                        disabled={busy}
                        aria-label={track.bookmarked ? "Remove bookmark" : "Bookmark"}
                        className={`flex items-center justify-center rounded border p-2 transition-colors disabled:opacity-50 ${
                          track.bookmarked
                            ? "border-accent bg-accent text-white"
                            : "border-surface-border hover:bg-surface-hover"
                        }`}
                      >
                        <BookmarkIcon filled={track.bookmarked} />
                      </button>
                      <button
                        onClick={() => void handleDelete(track.track_id)}
                        disabled={busy}
                        aria-label="Delete track"
                        className="flex items-center justify-center rounded border border-surface-border p-2 hover:bg-surface-hover transition-colors disabled:opacity-50"
                      >
                        <TrashIcon />
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
