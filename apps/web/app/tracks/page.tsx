"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { listTracks, logout, uploadTrack, type TrackSummary } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";

export default function TracksPage() {
  const router = useRouter();
  const { user, loading: authLoading, refresh } = useAuth();
  const [tracks, setTracks] = useState<TrackSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const reloadTracks = () => {
    listTracks()
      .then(setTracks)
      .catch((err: Error) => setError(err.message));
  };

  useEffect(() => {
    if (!authLoading && user === null) {
      router.push("/login");
    }
  }, [authLoading, user, router]);

  useEffect(() => {
    // Gated on `user` so this doesn't fire on every unauthenticated visit before the redirect
    // above has a chance to run -- without this, an unauthenticated visit to /tracks fired a
    // doomed, guaranteed-401 API call before redirecting to /login.
    if (user === null) {
      return;
    }
    reloadTracks();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reloadTracks is stable per render intent
  }, [user]);

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile) {
      return;
    }
    setUploadError(null);
    setUploading(true);
    try {
      await uploadTrack(uploadFile, "I made this recording");
      setUploadFile(null);
      reloadTracks();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const handleLogout = async () => {
    try {
      await logout();
    } catch {
      // A failed logout call shouldn't trap the user on the page -- fall through to navigate
      // away regardless.
    } finally {
      await refresh();
      router.push("/login");
    }
  };

  if (authLoading || user === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }
  if (error) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p className="text-red-600">Could not load tracks: {error}</p>
      </main>
    );
  }
  if (tracks === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading tracks...</p>
      </main>
    );
  }

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold">Tracks</h1>
        <button onClick={() => void handleLogout()} className="text-sm text-zinc-500 underline">
          Log out
        </button>
      </div>
      <form
        onSubmit={(e) => void handleUpload(e)}
        className="mb-8 flex flex-col gap-3 rounded border border-zinc-200 p-4"
      >
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">Upload a track</span>
          <input
            type="file"
            accept="audio/*"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            className="text-sm"
          />
        </label>
        {uploadError && <p className="text-red-600 text-sm">{uploadError}</p>}
        <button
          type="submit"
          disabled={!uploadFile || uploading}
          className="self-start rounded bg-zinc-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {uploading ? "Uploading..." : "Upload"}
        </button>
      </form>
      {tracks.length === 0 ? (
        <p className="text-zinc-500">No tracks yet.</p>
      ) : (
        <ul className="divide-y divide-zinc-200">
          {tracks.map((track) => (
            <li key={track.track_id} className="py-3 flex items-center justify-between gap-4">
              <div className="min-w-0">
                <p className="font-mono text-sm truncate">{track.track_id}</p>
                <p className="text-sm text-zinc-500">
                  status: {track.status}
                  {track.duration_seconds !== null &&
                    ` · ${track.duration_seconds.toFixed(1)}s`}
                </p>
              </div>
              {track.has_transcription ? (
                <div className="shrink-0 flex items-center gap-3">
                  <Link
                    href={`/tracks/${track.track_id}`}
                    className="text-sm font-medium text-blue-600 hover:underline"
                  >
                    Edit lyrics
                  </Link>
                  <Link
                    href={`/tracks/${track.track_id}/play`}
                    className="text-sm font-medium text-blue-600 hover:underline"
                  >
                    Play
                  </Link>
                </div>
              ) : (
                <span className="shrink-0 text-sm text-zinc-400">not transcribed yet</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
