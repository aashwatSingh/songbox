"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";
import { getTranscription, realignTrack, type TranscriptionResponse } from "@/lib/api";
import { useAuth } from "@/lib/AuthContext";

function PageHeader({ id }: { id: string }) {
  return (
    <div className="mb-6 flex items-center gap-4">
      <Link href="/tracks" className="text-sm font-medium text-accent hover:underline">
        &larr; Back to tracks
      </Link>
      <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-accent hover:underline">
        Play &rarr;
      </Link>
    </div>
  );
}

export default function TrackEditorPage(props: PageProps<"/tracks/[id]">) {
  const { id } = use(props.params);
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && user === null) {
      router.push("/login");
    }
  }, [authLoading, user, router]);
  const [transcription, setTranscription] = useState<TranscriptionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wordTexts, setWordTexts] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  // Free-text entry for the case where automatic transcription found nothing at all (an
  // instrumental track, or audio Whisper genuinely couldn't get anything from) -- the word-by-word
  // editor below has no way to add words that were never detected in the first place, so that path
  // is structurally a dead end for this case. The honest fix for "we couldn't transcribe it" is
  // letting the uploader (who has the rights to the recording) supply their own lyrics -- not
  // fetching someone else's copyrighted transcription from a third party.
  const [freeTextLyrics, setFreeTextLyrics] = useState("");
  const [savingFreeText, setSavingFreeText] = useState(false);

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

  async function handleSaveFreeText() {
    if (!freeTextLyrics.trim()) {
      return;
    }
    setSavingFreeText(true);
    setError(null);
    try {
      const result = await realignTrack(id, freeTextLyrics.trim());
      setTranscription(result);
      setWordTexts(result.words.map((w) => w.text ?? ""));
      setFreeTextLyrics("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSavingFreeText(false);
    }
  }

  if (authLoading || user === null) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted">Loading...</p>
      </main>
    );
  }

  if (error && transcription === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <PageHeader id={id} />
        <p className="text-red-400">Could not load transcription: {error}</p>
      </main>
    );
  }
  if (transcription === null) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted">Loading...</p>
      </main>
    );
  }

  if (!transcription.lyrics_display_allowed) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <PageHeader id={id} />
        <h1 className="text-2xl font-bold mb-4">Track {id}</h1>
        <p className="rounded border border-surface-border bg-surface p-4 text-muted">
          Lyric display isn&apos;t permitted for this track, so there&apos;s nothing to correct.
        </p>
      </main>
    );
  }

  if (transcription.language !== "en") {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <PageHeader id={id} />
        <h1 className="text-2xl font-bold mb-4">Track {id}</h1>
        <p className="rounded border border-surface-border bg-surface p-4 text-muted">
          Correction editing is English-only right now (detected language:{" "}
          {transcription.language}).
        </p>
        <ul className="mt-4 space-y-1">
          {transcription.words.map((word) => (
            <li key={word.idx} className="text-sm">
              {word.text ?? "(no text)"}{" "}
              <span className="text-muted">
                {word.start_ms}ms - {word.end_ms}ms
              </span>
            </li>
          ))}
        </ul>
      </main>
    );
  }

  if (wordTexts.length === 0) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <PageHeader id={id} />
        <h1 className="text-2xl font-bold mb-4">Track {id}</h1>
        <p className="mb-4 text-sm text-muted">
          No lyrics were automatically detected for this track &mdash; it may be instrumental, or
          the audio just didn&apos;t have enough for the transcriber to work with. If you have the
          rights to this recording, you can type or paste the lyrics yourself below.
        </p>
        <textarea
          value={freeTextLyrics}
          onChange={(e) => setFreeTextLyrics(e.target.value)}
          rows={8}
          placeholder="Paste or type the lyrics here…"
          className="w-full rounded border border-surface-border bg-surface px-3 py-2 text-sm focus:outline-none focus:border-accent"
        />
        <button
          onClick={() => void handleSaveFreeText()}
          disabled={savingFreeText || !freeTextLyrics.trim()}
          className="mt-3 rounded bg-accent px-4 py-2 text-white text-sm font-semibold disabled:opacity-50 hover:bg-accent-hover transition-colors"
        >
          {savingFreeText ? "Saving..." : "Save lyrics"}
        </button>
        {error && <p className="mt-4 text-red-400 text-sm">{error}</p>}
      </main>
    );
  }

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <PageHeader id={id} />
      <h1 className="text-2xl font-bold mb-4">Track {id}</h1>
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
            className="rounded border border-surface-border bg-surface px-2 py-1 text-sm w-24 focus:outline-none focus:border-accent"
          />
        ))}
      </div>
      <button
        onClick={handleSave}
        disabled={saving}
        className="rounded bg-accent px-4 py-2 text-white text-sm font-semibold disabled:opacity-50 hover:bg-accent-hover transition-colors"
      >
        {saving ? "Saving..." : "Save & re-align"}
      </button>
      {error && <p className="mt-4 text-red-400 text-sm">{error}</p>}
    </main>
  );
}
