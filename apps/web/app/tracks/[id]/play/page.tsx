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
import {
  BLEED_FLOOR_MARGIN_RMS,
  PitchTracker,
  ScoreTracker,
  requestMicStream,
  measureBleedFloor,
} from "@/lib/micScoring";

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

const CALIBRATION_DURATION_MS = 4000;

export default function PlayerPage(props: PageProps<"/tracks/[id]/play">) {
  const { id } = use(props.params);
  const [pkg, setPkg] = useState<PackageResponse | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingAudio, setLoadingAudio] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);

  // Real decoded-audio duration, in seconds -- stays 0 until ensurePlayerLoaded() has actually
  // decoded the stems (i.e. the first Play click). Before that, rendering below falls back to
  // estimatedDurationSeconds (derived straight from the package's own data) instead of treating
  // an unset 0/1 as a real duration.
  const [durationSeconds, setDurationSeconds] = useState(0);

  const playerRef = useRef<StemPlayer | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  // Mirrors the effective (real-or-estimated) duration for handleTrackEnded's imperative read.
  // Written directly during render (not from an effect) so it's always current by the time a
  // natural track-end can fire -- avoids handleTrackEnded's own closure only ever seeing the
  // duration value from whichever render happened to be active when ensurePlayerLoaded() ran.
  const durationSecondsRef = useRef(0);

  const [micState, setMicState] = useState<"idle" | "requesting" | "calibrating" | "active">(
    "idle"
  );
  const [micError, setMicError] = useState<string | null>(null);
  const [liveHz, setLiveHz] = useState<number | null>(null);
  const [scorePercent, setScorePercent] = useState(0);

  const pitchTrackerRef = useRef<PitchTracker | null>(null);
  const scoreTrackerRef = useRef<ScoreTracker | null>(null);
  const bleedFloorRef = useRef(0);
  // tick()'s recursive requestAnimationFrame(tick) call is pinned to the closure it was first
  // scheduled from -- it never sees a LATER render's micState value (the same staleness class
  // M6a's durationSecondsRef exists to avoid for handleTrackEnded). A ref, not React state, is
  // what tick() must read to know whether scoring is active right now.
  const micActiveRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    getPackage(id)
      .then((result) => {
        if (cancelled) return;
        setPkg(result);
        setNotReady(false);
        setError(null);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        if (err.message.toLowerCase().includes("no karaoke package")) {
          setNotReady(true);
          setError(null);
        } else {
          setError(err.message);
          setNotReady(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      pitchTrackerRef.current?.stop();
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
      setDurationSeconds(Math.max(drums.duration, bass.duration, other.duration));
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
      const nowMs = player.currentTimeSeconds * 1000;
      setCurrentTimeMs(nowMs);

      const tracker = pitchTrackerRef.current;
      if (tracker) {
        const reading = tracker.getLatestReading();
        setLiveHz(reading?.hz ?? null);

        if (micActiveRef.current && reading && pkg) {
          const frameIndex = findActivePitchFrameIndex(pkg.karaoke.pitch.frames, nowMs);
          const targetHz = frameIndex >= 0 ? pkg.karaoke.pitch.frames[frameIndex].hz : null;
          scoreTrackerRef.current?.recordFrame(
            reading.hz,
            targetHz,
            reading.rms,
            bleedFloorRef.current
          );
          setScorePercent(scoreTrackerRef.current?.percentOnPitch ?? 0);
        }
      }

      rafRef.current = requestAnimationFrame(tick);
    }
  }

  async function handleEnableMicScoring() {
    if (!pkg) return;
    setMicError(null);
    setMicState("requesting");
    try {
      const micStream = await requestMicStream();
      const player = await ensurePlayerLoaded(pkg);
      const context = audioContextRef.current;
      if (!context) throw new Error("audio context not ready");

      const tracker = new PitchTracker(context, micStream);
      await tracker.init();
      pitchTrackerRef.current = tracker;

      setMicState("calibrating");
      player.play(0);
      setIsPlaying(true);
      rafRef.current = requestAnimationFrame(tick);

      const floor = await measureBleedFloor(tracker, CALIBRATION_DURATION_MS);
      bleedFloorRef.current = floor + BLEED_FLOOR_MARGIN_RMS;
      scoreTrackerRef.current = new ScoreTracker();
      micActiveRef.current = true;
      setMicState("active");
    } catch (err) {
      setMicError((err as Error).message);
      setMicState("idle");
      micActiveRef.current = false;
      pitchTrackerRef.current?.stop();
      pitchTrackerRef.current = null;
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

  // Estimated duration derived straight from the package's own data (last pitch frame / last
  // word), used as the fallback before any real audio has been decoded -- fixes the bogus 1ms
  // duration the pitch lane and seek bar used to render against on first paint.
  const estimatedDurationSeconds = useMemo(() => {
    if (!pkg) return 1;
    const lastFrameMs = pkg.karaoke.pitch.frames.length
      ? pkg.karaoke.pitch.frames[pkg.karaoke.pitch.frames.length - 1].time_ms
      : 0;
    const lastWordMs = pkg.karaoke.words.length
      ? pkg.karaoke.words[pkg.karaoke.words.length - 1].end_ms
      : 0;
    const estimateMs = Math.max(lastFrameMs, lastWordMs);
    return estimateMs > 0 ? estimateMs / 1000 : 1;
  }, [pkg]);

  // Real decoded duration once known (from ensurePlayerLoaded), otherwise the estimate above.
  const effectiveDurationSeconds =
    durationSeconds > 0 ? durationSeconds : estimatedDurationSeconds;
  // Keep the imperative-read mirror current every render (see the ref's declaration comment).
  durationSecondsRef.current = effectiveDurationSeconds;
  const durationMs = effectiveDurationSeconds * 1000;

  // Math.max(1, ...frames.map(...)) throws RangeError: Maximum call stack size exceeded once the
  // spread exceeds V8's ~65,535 argument-count limit -- reachable on any track past ~11 minutes
  // (this pipeline's cap is 720s / ~72,000 CREPE frames at a 10ms hop). reduce() has no such
  // limit and is behaviorally identical, including the empty-array case (still returns 1).
  const maxPitchHz = useMemo(
    () => (pkg ? pkg.karaoke.pitch.frames.reduce((max, f) => Math.max(max, f.hz ?? 0), 1) : 1),
    [pkg]
  );

  // Memoized so this doesn't re-run 60x/sec during playback (the RAF loop drives currentTimeMs,
  // which is NOT a dependency here) -- only when the underlying frame data or duration changes,
  // which happens once, when the package loads / real duration becomes known.
  const pitchPoints = useMemo(() => {
    if (!pkg) return "";
    return pkg.karaoke.pitch.frames
      .map((f) => {
        const x = (f.time_ms / durationMs) * 400;
        const y = f.hz === null ? 60 : 60 - (f.hz / maxPitchHz) * 55;
        return `${x},${y}`;
      })
      .join(" ");
  }, [pkg, durationMs, maxPitchHz]);

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
            points={pitchPoints}
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
          {micState === "active" && liveHz !== null && (
            <circle
              cx={playheadX}
              cy={60 - (liveHz / maxPitchHz) * 55}
              r={4}
              fill="#ff6b6b"
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
          max={effectiveDurationSeconds || 1}
          step={0.1}
          value={currentTimeMs / 1000}
          onChange={handleSeek}
          className="flex-1"
        />
      </div>

      <div className="mt-4">
        {micState === "idle" && (
          <button
            onClick={handleEnableMicScoring}
            className="rounded border border-blue-600 px-4 py-2 text-blue-600 text-sm font-medium"
          >
            Enable mic scoring
          </button>
        )}
        {micState === "requesting" && (
          <p className="text-sm text-zinc-500">Requesting microphone access...</p>
        )}
        {micState === "calibrating" && (
          <p className="text-sm text-zinc-500">Stay quiet -- calibrating...</p>
        )}
        {micState === "active" && (
          <p className="text-sm text-zinc-600">
            Mic scoring active &mdash; {scorePercent.toFixed(0)}% on pitch
          </p>
        )}
        {micError && <p className="mt-2 text-red-600 text-sm">{micError}</p>}
      </div>
    </main>
  );
}
