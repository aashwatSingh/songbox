"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { listReviewQueue, resolveReview, type ReviewQueueItem } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";

function formatUploaded(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export default function ReviewPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [items, setItems] = useState<ReviewQueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyTrackId, setBusyTrackId] = useState<string | null>(null);

  const reload = () => {
    listReviewQueue()
      .then(setItems)
      .catch((err: Error) => setError(`Could not load the review queue: ${err.message}`));
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
    reload();
  }, [user]);

  const handleResolve = async (trackId: string, approve: boolean) => {
    setBusyTrackId(trackId);
    setError(null);
    try {
      await resolveReview(trackId, approve);
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record that decision.");
    } finally {
      setBusyTrackId(null);
    }
  };

  if (authLoading || user === null) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted">Loading...</p>
      </main>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between px-8 py-5 border-b border-surface-border">
        <span className="text-xl font-bold tracking-tight">SongBox</span>
        <nav className="flex items-center gap-6">
          <Link href="/tracks" className="text-sm font-medium text-muted hover:text-foreground">
            Library
          </Link>
          <Link href="/review" className="text-sm font-medium text-accent">
            Review
          </Link>
        </nav>
        <span className="text-sm text-muted">{user.email}</span>
      </header>

      <main className="max-w-4xl mx-auto px-8 py-10">
        <h1 className="text-4xl font-bold tracking-tight">Rights review</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted">
          Tracks the gate held instead of passing. Nothing here has reached the processing
          pipeline: separation, transcription and packaging all refuse a track that has not
          passed. Approving records your decision against the upload&apos;s original attestation,
          which is kept immutable.
        </p>

        {error && <p className="mt-6 text-red-400 text-sm">{error}</p>}

        {items === null ? (
          <p className="mt-8 text-muted">Loading…</p>
        ) : items.length === 0 ? (
          <p className="mt-8 text-muted">Nothing is waiting for review.</p>
        ) : (
          <ul className="mt-8 flex flex-col gap-4">
            {items.map((item) => (
              <li
                key={item.track_id}
                className="rounded-lg border border-surface-border bg-surface p-5"
              >
                <div className="flex items-start justify-between gap-6">
                  <div className="min-w-0">
                    <p className="font-semibold">{item.title ?? "Untitled"}</p>
                    <p className="text-sm text-muted">{item.artist ?? "Unknown artist"}</p>
                    <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                      <dt className="text-muted">Lane</dt>
                      <dd>{item.lane}</dd>
                      <dt className="text-muted">Resolution</dt>
                      <dd>{item.resolution}</dd>
                      <dt className="text-muted">Matched release</dt>
                      <dd>{item.matched_release ?? "none (lookup did not return a match)"}</dd>
                      <dt className="text-muted">Uploaded</dt>
                      <dd>{formatUploaded(item.uploaded_at)}</dd>
                      <dt className="text-muted">Attestation</dt>
                      <dd className="break-words">{item.attestation_text}</dd>
                    </dl>
                  </div>
                  <div className="flex shrink-0 flex-col gap-2">
                    <button
                      onClick={() => void handleResolve(item.track_id, true)}
                      disabled={busyTrackId === item.track_id}
                      className="rounded bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50 hover:bg-accent-hover transition-colors"
                    >
                      {busyTrackId === item.track_id ? "Saving…" : "Approve"}
                    </button>
                    <button
                      onClick={() => void handleResolve(item.track_id, false)}
                      disabled={busyTrackId === item.track_id}
                      className="rounded border border-surface-border px-4 py-2 text-sm font-medium text-muted disabled:opacity-50 hover:text-foreground hover:border-accent transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
