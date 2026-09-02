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
import { PIPELINE_STAGE_LABELS, runMissingPipelineStages, type PipelineStage } from "@/lib/pipeline";

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
  const [uploadFile, setUploadFile] = useState<File | null>(null);
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
  const [dragActive, setDragActive] = useState(false);
  // The file input stays mounted at all times, even while the form is hidden. Browsers only honor
  // input.click() from inside a real user gesture, so "Upload track" has to reach an already-
  // mounted node synchronously in its own onClick -- mounting the input as a side effect of
  // opening the form and clicking it afterwards loses the gesture and the picker silently
  // never opens.
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Prefill the title from the filename so uploads stop defaulting to "Untitled", but never
  // overwrite something already typed.
  const selectFile = (file: File | null) => {
    setUploadFile(file);
    if (file) {
      setUploadError(null);
      setUploadTitle((current) => current || file.name.replace(/\.[^.]+$/, ""));
    }
  };

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
      await runMissingPipelineStages(track, setProcessingStage);
      setProcessingTrackId(null);
      setProcessingStage(null);
      reloadTracks();
    } catch (err) {
      setProcessingError(err instanceof Error ? err.message : "Processing failed.");
    }
  };

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile) {
      return;
    }
    setUploadError(null);
    setUploading(true);
    try {
      const result = await uploadTrack(
        uploadFile,
        "I made this recording",
        uploadTitle,
        uploadArtist
      );
      setUploadFile(null);
      setUploadTitle("");
      setUploadArtist("");
      setShowUpload(false);
      // Clear the input's own value too. Without this, re-picking the SAME file emits no change
      // event (the value is unchanged), so a retry after a failed upload silently selects nothing.
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      reloadTracks();
      // Only a "passed" upload has real content to process -- one held for manual review
      // (pending_review) has nothing for separate/transcribe/package to work with yet.
      if (result.status === "passed") {
        void runPipelineFor(result.track_id);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
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
        <Link href="/tracks" className="text-sm font-medium text-accent">
          Library
        </Link>
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
              <p className="text-sm text-muted">
                {processingStage ? PIPELINE_STAGE_LABELS[processingStage] : "Processing…"}
                {" "}(this can take a while on a real song)
              </p>
            )}
          </div>
        )}

        {/* Mounted unconditionally, outside the collapsible form -- see fileInputRef's note: the
            header button must be able to click it synchronously within its own user gesture. */}
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*"
          onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
          className="hidden"
        />

        {showUpload && (
          <form
            onSubmit={(e) => void handleUpload(e)}
            className="mt-6 flex flex-col gap-3 rounded-lg border border-surface-border bg-surface p-5 max-w-md"
          >
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
            <label className="flex flex-col gap-1">
              <span className="text-sm font-medium">Artist</span>
              <input
                type="text"
                value={uploadArtist}
                onChange={(e) => setUploadArtist(e.target.value)}
                placeholder="Artist name"
                className="rounded border border-surface-border bg-background px-3 py-2 text-sm"
              />
            </label>
            <div className="flex flex-col gap-1">
              <span className="text-sm font-medium">Audio file</span>
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
                  selectFile(e.dataTransfer.files?.[0] ?? null);
                }}
                className={`rounded border border-dashed px-4 py-6 text-sm transition-colors ${
                  dragActive
                    ? "border-accent bg-surface-hover text-foreground"
                    : "border-surface-border text-muted hover:border-accent hover:text-foreground"
                }`}
              >
                {uploadFile ? (
                  <span className="text-foreground font-medium">{uploadFile.name}</span>
                ) : (
                  <>
                    Choose a file<span className="text-muted"> or drag it here</span>
                  </>
                )}
              </button>
            </div>
            {uploadError && <p className="text-red-400 text-sm">{uploadError}</p>}
            <button
              type="submit"
              disabled={!uploadFile || uploading}
              className="self-start rounded bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50 hover:bg-accent-hover transition-colors"
            >
              {uploading ? "Uploading..." : "Upload"}
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
                  <div className="aspect-square bg-gradient-to-br from-[#2b3018] to-[#14150d] flex items-center justify-center text-[#9ba385]">
                    <MusicNoteIcon />
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
