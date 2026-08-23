"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listTracks, type TrackSummary } from "@/lib/api";

export default function TracksPage() {
  const [tracks, setTracks] = useState<TrackSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listTracks()
      .then(setTracks)
      .catch((err: Error) => setError(err.message));
  }, []);

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
      <h1 className="text-2xl font-semibold mb-6">Tracks</h1>
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
                <Link
                  href={`/tracks/${track.track_id}`}
                  className="shrink-0 text-sm font-medium text-blue-600 hover:underline"
                >
                  Edit lyrics
                </Link>
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
