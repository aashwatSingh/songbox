"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { getTranscription, realignTrack, type TranscriptionResponse } from "@/lib/api";

function BackToTracksLink() {
  return (
    <Link
      href="/tracks"
      className="mb-4 inline-block text-sm font-medium text-blue-600 hover:underline"
    >
      &larr; Back to tracks
    </Link>
  );
}

export default function TrackEditorPage(props: PageProps<"/tracks/[id]">) {
  const { id } = use(props.params);
  const [transcription, setTranscription] = useState<TranscriptionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wordTexts, setWordTexts] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getTranscription(id)
      .then((result) => {
        setTranscription(result);
        setWordTexts(result.words.map((w) => w.text ?? ""));
      })
      .catch((err: Error) => setError(err.message));
  }, [id]);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const result = await realignTrack(id, wordTexts.join(" "));
      setTranscription(result);
      setWordTexts(result.words.map((w) => w.text ?? ""));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (error && transcription === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <div className="mb-4 flex items-center gap-4">
          <BackToTracksLink />
          <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-blue-600 hover:underline">
            Play &rarr;
          </Link>
        </div>
        <p className="text-red-600">Could not load transcription: {error}</p>
      </main>
    );
  }
  if (transcription === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }

  if (!transcription.lyrics_display_allowed) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <div className="mb-4 flex items-center gap-4">
          <BackToTracksLink />
          <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-blue-600 hover:underline">
            Play &rarr;
          </Link>
        </div>
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="rounded bg-zinc-100 p-4 text-zinc-700">
          Lyric display isn&apos;t permitted for this track, so there&apos;s nothing to correct.
        </p>
      </main>
    );
  }

  if (transcription.language !== "en") {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <div className="mb-4 flex items-center gap-4">
          <BackToTracksLink />
          <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-blue-600 hover:underline">
            Play &rarr;
          </Link>
        </div>
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="rounded bg-zinc-100 p-4 text-zinc-700">
          Correction editing is English-only right now (detected language:{" "}
          {transcription.language}).
        </p>
        <ul className="mt-4 space-y-1">
          {transcription.words.map((word) => (
            <li key={word.idx} className="text-sm">
              {word.text ?? "(no text)"}{" "}
              <span className="text-zinc-400">
                {word.start_ms}ms - {word.end_ms}ms
              </span>
            </li>
          ))}
        </ul>
      </main>
    );
  }

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <div className="mb-4 flex items-center gap-4">
        <BackToTracksLink />
        <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-blue-600 hover:underline">
          Play &rarr;
        </Link>
      </div>
      <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
      <div className="flex flex-wrap gap-2 mb-6">
        {wordTexts.map((text, idx) => (
          <input
            key={idx}
            value={text}
            onChange={(e) => {
              const next = [...wordTexts];
              next[idx] = e.target.value;
              setWordTexts(next);
            }}
            className="border border-zinc-300 rounded px-2 py-1 text-sm w-24"
          />
        ))}
      </div>
      {wordTexts.length === 0 && (
        <p className="mb-4 text-sm text-zinc-500">
          No words to correct &mdash; this track has no detected speech.
        </p>
      )}
      <button
        onClick={handleSave}
        disabled={saving || wordTexts.length === 0}
        className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
      >
        {saving ? "Saving..." : "Save & re-align"}
      </button>
      {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
    </main>
  );
}
