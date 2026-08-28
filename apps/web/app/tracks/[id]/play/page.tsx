"use client";

import Link from "next/link";
import { use, useEffect, useMemo, useRef, useState } from "react";
import {
  generatePackage,
  getDevIdentityHeaders,
  getPackage,
  stemUrl,
  type PackageResponse,
} from "@/lib/api";
import { StemPlayer, findActiveWordIndex, findActivePitchFrameIndex } from "@/lib/player";

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

async function decodeStem(context: AudioContext, path: string): Promise<AudioBuffer> {
  // Same-origin relative to API_BASE_URL, but still gated by the dev-auth-stub identity check
  // every other route requires -- apiFetch() attaches these headers automatically, but this is a
  // raw fetch() (binary audio, not JSON), so they're attached explicitly here.
  const response = await fetch(stemUrl(path), { headers: getDevIdentityHeaders() });
  if (!response.ok) {
    throw new Error(`could not fetch stem audio (${response.status})`);
  }
  const arrayBuffer = await response.arrayBuffer();
  return context.decodeAudioData(arrayBuffer);
}

export default function PlayerPage(props: PageProps<"/tracks/[id]/play">) {
  const { id } = use(props.params);
  const [pkg, setPkg] = useState<PackageResponse | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingAudio, setLoadingAudio] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);

  const playerRef = useRef<StemPlayer | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const durationSecondsRef = useRef(0);

  useEffect(() => {
    setNotReady(false);
    setError(null);
    getPackage(id)
      .then(setPkg)
      .catch((err: Error) => {
        if (err.message.toLowerCase().includes("no karaoke package")) {
          setNotReady(true);
        } else {
          setError(err.message);
        }
      });
  }, [id]);

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      audioContextRef.current?.close();
    };
  }, []);

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    try {
      await generatePackage(id);
      const result = await getPackage(id);
      setPkg(result);
      setNotReady(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setGenerating(false);
    }
  }

  // Fired by StemPlayer when a track reaches its natural end (as opposed to an explicit pause()
  // or seek() call). Stops the RAF loop so currentTimeMs doesn't keep growing past the track's
  // real duration, flips the Play/Pause button back to "Play", and clamps the displayed playhead
  // to the end of the track rather than wherever it happened to land.
  function handleTrackEnded() {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setIsPlaying(false);
    setCurrentTimeMs(durationSecondsRef.current * 1000);
  }

  async function ensurePlayerLoaded(current: PackageResponse): Promise<StemPlayer> {
    if (playerRef.current) return playerRef.current;
    setLoadingAudio(true);
    try {
      const context = new AudioContext();
      audioContextRef.current = context;
      const [drums, bass, other] = await Promise.all([
        decodeStem(context, current.stem_urls.drums),
        decodeStem(context, current.stem_urls.bass),
        decodeStem(context, current.stem_urls.other),
      ]);
      durationSecondsRef.current = Math.max(drums.duration, bass.duration, other.duration);
      const player = new StemPlayer(context, { drums, bass, other }, handleTrackEnded);
      playerRef.current = player;
      return player;
    } finally {
      setLoadingAudio(false);
    }
  }

  function tick() {
    const player = playerRef.current;
    if (player && player.isPlaying) {
      setCurrentTimeMs(player.currentTimeSeconds * 1000);
      rafRef.current = requestAnimationFrame(tick);
    }
  }

  async function handlePlayPause() {
    if (!pkg) return;
    setError(null);
    try {
      const player = await ensurePlayerLoaded(pkg);
      if (player.isPlaying) {
        player.pause();
        setIsPlaying(false);
        if (rafRef.current !== null) {
          cancelAnimationFrame(rafRef.current);
          rafRef.current = null;
        }
      } else {
        player.play(player.currentTimeSeconds);
        setIsPlaying(true);
        rafRef.current = requestAnimationFrame(tick);
      }
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function handleSeek(e: React.ChangeEvent<HTMLInputElement>) {
    const seconds = Number(e.target.value);
    playerRef.current?.seek(seconds);
    setCurrentTimeMs(seconds * 1000);
  }

  const activeWordIndex = useMemo(
    () => (pkg ? findActiveWordIndex(pkg.karaoke.words, currentTimeMs) : -1),
    [pkg, currentTimeMs]
  );
  const activeFrameIndex = useMemo(
    () => (pkg ? findActivePitchFrameIndex(pkg.karaoke.pitch.frames, currentTimeMs) : -1),
    [pkg, currentTimeMs]
  );

  if (error) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <BackToTracksLink />
        <p className="text-red-600">{error}</p>
      </main>
    );
  }

  if (notReady) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <BackToTracksLink />
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="mb-4 text-zinc-600">No karaoke package exists for this track yet.</p>
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
        >
          {generating ? "Generating..." : "Generate karaoke package"}
        </button>
      </main>
    );
  }

  if (pkg === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }

  const lyricsWithheld = pkg.karaoke.words.every((w) => w.text === null);
  const maxPitchHz = Math.max(1, ...pkg.karaoke.pitch.frames.map((f) => f.hz ?? 0));
  const durationMs = Math.max(1, durationSecondsRef.current * 1000);
  const playheadX = (currentTimeMs / durationMs) * 400;

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <BackToTracksLink />
      <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>

      {lyricsWithheld && (
        <p className="mb-4 rounded bg-zinc-100 p-3 text-sm text-zinc-600">
          Lyric display isn&apos;t permitted for this track &mdash; playing without lyrics.
        </p>
      )}

      <div className="rounded bg-zinc-950 p-4 mb-4">
        <div className="text-center text-lg mb-3 min-h-[1.75rem]">
          {pkg.karaoke.words.map((word, idx) => (
            <span
              key={word.idx}
              className={idx === activeWordIndex ? "text-blue-300 font-bold" : "text-zinc-500"}
            >
              {(word.text ?? "•") + " "}
            </span>
          ))}
        </div>
        <svg viewBox="0 0 400 60" className="w-full h-[60px] block">
          <polyline
            points={pkg.karaoke.pitch.frames
              .map((f) => {
                const x = (f.time_ms / durationMs) * 400;
                const y = f.hz === null ? 60 : 60 - (f.hz / maxPitchHz) * 55;
                return `${x},${y}`;
              })
              .join(" ")}
            fill="none"
            stroke="#8fd6ff"
            strokeWidth={2}
          />
          {activeFrameIndex >= 0 && (
            <line
              x1={playheadX}
              y1={0}
              x2={playheadX}
              y2={60}
              stroke="#fff"
              strokeWidth={1.5}
              opacity={0.6}
            />
          )}
        </svg>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handlePlayPause}
          disabled={loadingAudio}
          className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
        >
          {loadingAudio ? "Loading audio..." : isPlaying ? "Pause" : "Play"}
        </button>
        <input
          type="range"
          min={0}
          max={durationSecondsRef.current || 1}
          step={0.1}
          value={currentTimeMs / 1000}
          onChange={handleSeek}
          className="flex-1"
        />
      </div>
    </main>
  );
}
