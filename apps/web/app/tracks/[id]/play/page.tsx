"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useEffect, useMemo, useRef, useState } from "react";
import {
  deleteTrack,
  generatePackage,
  getPackage,
  listTracks,
  stemUrl,
  toggleBookmark,
  transcribeTrack,
  type PackageResponse,
} from "@/lib/api";
import {
  StemPlayer,
  findActiveWordIndex,
  findActivePitchFrameIndex,
  type StemBuffers,
} from "@/lib/player";
import { pendingStages, runMissingPipelineStages, type PipelineStage } from "@/lib/pipeline";
import { estimateTotalSeconds, formatEstimate } from "@/lib/estimates";
import { PipelineProgress } from "@/components/PipelineProgress";
import { activeLineIndex, groupWordsIntoLines } from "@/lib/lyrics";
import {
  BLEED_FLOOR_MARGIN_RMS,
  PitchTracker,
  ScoreTracker,
  requestMicStream,
  measureBleedFloor,
} from "@/lib/micScoring";

function BackArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-4 w-4">
      <path d="M19 12H5M11 18l-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function PlayFilledIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className="h-5 w-5">
      <path d="M6 4l14 8-14 8V4z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className="h-5 w-5">
      <rect x="6" y="4" width="4" height="16" />
      <rect x="14" y="4" width="4" height="16" />
    </svg>
  );
}

function formatTime(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

async function decodeStem(context: AudioContext, path: string): Promise<AudioBuffer> {
  // Same-origin relative to API_BASE_URL. This is a raw fetch() (binary audio, not JSON), so it
  // doesn't go through apiFetch() -- it needs its own credentials: "include" so the httpOnly
  // session cookie travels with the request the same way apiFetch()'s calls do.
  const response = await fetch(stemUrl(path), { credentials: "include" });
  if (!response.ok) {
    throw new Error(`could not fetch stem audio (${response.status})`);
  }
  const arrayBuffer = await response.arrayBuffer();
  return context.decodeAudioData(arrayBuffer);
}

const CALIBRATION_DURATION_MS = 4000;
const STEM_ORDER: (keyof StemBuffers)[] = ["drums", "bass", "other"];

export default function PlayerPage(props: PageProps<"/tracks/[id]/play">) {
  const { id } = use(props.params);
  const router = useRouter();
  const [pkg, setPkg] = useState<PackageResponse | null>(null);
  // Cosmetic only (header title/artist) -- reuses the existing list endpoint rather than adding a
  // new single-track GET route just for this. Silently stays null on failure; the header falls
  // back to showing the track id, same as before this page had any title/artist display at all.
  // duration/has_stems/has_transcription come along so the Generate button can show an estimate
  // BEFORE it is pressed, and size that estimate to the stages this track actually still needs.
  const [trackMeta, setTrackMeta] = useState<
    {
      title: string | null;
      artist: string | null;
      bookmarked: boolean;
      duration_seconds: number | null;
      has_stems: boolean;
      has_transcription: boolean;
    } | null
  >(null);
  const [notReady, setNotReady] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [generatingStage, setGeneratingStage] = useState<PipelineStage | null>(null);
  // Which stages this run will actually execute, and when it began -- both needed to draw
  // a bar that spans the real remaining work rather than always assuming all three stages.
  const [generatingStages, setGeneratingStages] = useState<PipelineStage[]>([]);
  const [generatingStartedAt, setGeneratingStartedAt] = useState<number>(0);
  const [generatingDuration, setGeneratingDuration] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Separate from `error` above deliberately -- `error` drives an early return that replaces the
  // ENTIRE loaded player (see the `if (error)` branch below). Bookmark/Remove failures happen
  // from inside the already-loaded player and must not wipe it out from under the user; they're
  // shown inline near the buttons instead, same pattern as `micError` below.
  const [actionError, setActionError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [loadingAudio, setLoadingAudio] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);

  // Real decoded-audio duration, in seconds -- stays 0 until ensurePlayerLoaded() has actually
  // decoded the stems (i.e. the first Play click). Before that, rendering below falls back to
  // estimatedDurationSeconds (derived straight from the package's own data) instead of treating
  // an unset 0/1 as a real duration.
  const [durationSeconds, setDurationSeconds] = useState(0);

  const playerRef = useRef<StemPlayer | null>(null);
  // Memoizes the in-flight ensurePlayerLoaded() promise (not just the resolved player), so
  // overlapping callers -- e.g. handlePlayPause and handleEnableMicScoring, if a user clicks Play
  // while the mic-permission prompt is still open -- await the SAME decode instead of each racing
  // to create their own AudioContext/StemPlayer. playerRef.current is only ever assigned once, by
  // whichever call actually started the in-flight promise; every other concurrent caller just
  // awaits it.
  const playerLoadPromiseRef = useRef<Promise<StemPlayer> | null>(null);
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
  const [framesCounted, setFramesCounted] = useState(0);

  const pitchTrackerRef = useRef<PitchTracker | null>(null);
  const scoreTrackerRef = useRef<ScoreTracker | null>(null);
  const bleedFloorRef = useRef(0);
  // tick()'s recursive requestAnimationFrame(tick) call is pinned to the closure it was first
  // scheduled from -- it never sees a LATER render's micState value (the same staleness class
  // M6a's durationSecondsRef exists to avoid for handleTrackEnded). A ref, not React state, is
  // what tick() must read to know whether scoring is active right now.
  const micActiveRef = useRef(false);
  // tick()'s recursive requestAnimationFrame(tick) call is pinned to the closure it was first
  // scheduled from -- it would never see a LATER render's pitchSemitones state value if read
  // directly. A ref, not React state, is what tick() must read to know the current key shift
  // when computing the mic-scoring target pitch (the same staleness class micActiveRef and
  // durationSecondsRef already exist to solve elsewhere in this file).
  const pitchSemitonesRef = useRef(0);

  const [stemVolumes, setStemVolumesState] = useState<Record<keyof StemBuffers, number>>({
    drums: 1,
    bass: 1,
    other: 1,
  });
  const [stemMuted, setStemMutedState] = useState<Record<keyof StemBuffers, boolean>>({
    drums: false,
    bass: false,
    other: false,
  });
  const [tempoPercent, setTempoPercent] = useState(100);
  const [pitchSemitones, setPitchSemitonesState] = useState(0);

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
    let cancelled = false;
    listTracks()
      .then((tracks) => {
        if (cancelled) return;
        const match = tracks.find((t) => t.track_id === id);
        if (match) {
          setTrackMeta({
            title: match.title,
            artist: match.artist,
            bookmarked: match.bookmarked,
            duration_seconds: match.duration_seconds,
            has_stems: match.has_stems,
            has_transcription: match.has_transcription,
          });
        }
      })
      .catch(() => {
        // Non-fatal -- header just falls back to the track id.
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

  // Re-runs transcription and repackaging on a track that already has lyrics. Without this the
  // chain skips both stages (has_transcription is already true), so an improvement to the Whisper
  // settings could never reach a track that had been processed under the old ones.
  async function handleRegenerateLyrics() {
    setRegenerating(true);
    setActionError(null);
    try {
      await transcribeTrack(id);
      await generatePackage(id);
      setPkg(await getPackage(id));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not regenerate lyrics.");
    } finally {
      setRegenerating(false);
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setGeneratingStage(null);
    setError(null);
    try {
      // Fetches the track's REAL current has_stems/has_transcription rather than assuming this
      // is a fresh track -- this button also has to recover a track that already has some stages
      // done (e.g. stems exist but transcription failed), and must never re-call /separate in
      // that case (see runMissingPipelineStages' docstring for why that's not just a nicety).
      const tracks = await listTracks();
      const track = tracks.find((t) => t.track_id === id);
      if (!track) {
        throw new Error("track not found");
      }
      setGeneratingStages(pendingStages(track));
      setGeneratingDuration(track.duration_seconds);
      setGeneratingStartedAt(Date.now());
      await runMissingPipelineStages(track, setGeneratingStage);
      const result = await getPackage(id);
      setPkg(result);
      setNotReady(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setGenerating(false);
      setGeneratingStage(null);
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
    // A load is already in flight (e.g. handlePlayPause was clicked while
    // handleEnableMicScoring's own ensurePlayerLoaded() call is still awaiting mic permission, or
    // vice versa) -- await that SAME promise instead of starting a second AudioContext/decode.
    if (playerLoadPromiseRef.current) return playerLoadPromiseRef.current;

    setLoadingAudio(true);
    const loadPromise = (async () => {
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
        await player.init();
        // Seed the freshly-created player with whatever mixer/transpose values the user already
        // set via the UI before ever clicking Play -- otherwise those settings would exist only
        // in React state with nothing to apply them to until the next render, silently doing
        // nothing on this first play(). Safe to read current-render state here directly: unlike
        // tick()'s recursive requestAnimationFrame(tick) closure (which stays pinned to whichever
        // render scheduled it), ensurePlayerLoaded is invoked fresh from a user-initiated event
        // handler every time, so it always sees the latest render's state.
        for (const stem of STEM_ORDER) {
          player.setStemVolume(stem, stemVolumes[stem]);
          player.setStemMuted(stem, stemMuted[stem]);
        }
        player.setTempo(tempoPercent / 100);
        player.setPitchSemitones(pitchSemitones);
        playerRef.current = player;
        return player;
      } finally {
        setLoadingAudio(false);
      }
    })();
    playerLoadPromiseRef.current = loadPromise;
    try {
      return await loadPromise;
    } finally {
      // Left set on success (playerRef.current is now also set, so the fast-path check above
      // short-circuits future calls before this ref is even consulted) and cleared on failure, so
      // a later retry after a load error starts a fresh attempt rather than replaying the same
      // rejected promise forever.
      if (!playerRef.current) {
        playerLoadPromiseRef.current = null;
      }
    }
  }

  function tick() {
    const player = playerRef.current;
    if (player && player.isPlaying) {
      const nowMs = player.currentTimeSeconds * 1000;
      setCurrentTimeMs(nowMs);

      const tracker = pitchTrackerRef.current;
      if (tracker) {
        // Display always shows whatever the current value is -- no need to dedupe against RAF's
        // own cadence for a live number the eye reads continuously.
        setLiveHz(tracker.getLatestReading()?.hz ?? null);

        // Scoring must never double-count the same worklet reading, regardless of how RAF's
        // cadence (display-refresh-rate-dependent) happens to line up with the worklet's own
        // posting cadence (~10-12ms, fixed by its hop size) -- see
        // PitchTracker.getLatestReadingIfNew()'s own comment for what this does and doesn't
        // guarantee. Using plain getLatestReading() here would double-count on a fast (120Hz)
        // display; getLatestReadingIfNew() fixes that, though on a slow (60Hz) display some
        // worklet readings still simply never get polled for and are missed, not double-counted.
        const newReading = tracker.getLatestReadingIfNew();
        if (micActiveRef.current && newReading && pkg) {
          const frameIndex = findActivePitchFrameIndex(pkg.karaoke.pitch.frames, nowMs);
          const rawTargetHz = frameIndex >= 0 ? pkg.karaoke.pitch.frames[frameIndex].hz : null;
          // The stored pitch contour is untransposed, but the singer is hearing audio shifted by
          // the current Key setting (StemPlayer.setPitchSemitones, M6b) -- without this shift, a
          // singer perfectly in tune with what they actually hear would be scored against the
          // WRONG target whenever Key is non-zero, off by pitchSemitonesRef.current * 100 cents,
          // which is larger than ScoreTracker's +-50 cent tolerance for any non-zero semitone
          // setting. pitchSemitonesRef (not the pitchSemitones state) because tick()'s recursive
          // requestAnimationFrame(tick) closure is pinned to whichever render scheduled it and
          // would never see a later Key-slider change otherwise.
          const targetHz =
            rawTargetHz === null
              ? null
              : rawTargetHz * Math.pow(2, pitchSemitonesRef.current / 12);
          scoreTrackerRef.current?.recordFrame(
            newReading.hz,
            targetHz,
            newReading.rms,
            bleedFloorRef.current
          );
          setScorePercent(scoreTrackerRef.current?.percentOnPitch ?? 0);
          setFramesCounted(scoreTrackerRef.current?.framesCounted ?? 0);
        }
      }

      rafRef.current = requestAnimationFrame(tick);
    }
  }

  async function handleEnableMicScoring() {
    if (!pkg) return;
    setMicError(null);
    setMicState("requesting");
    // Tracked locally (not just via pitchTrackerRef) so the catch block can still release the
    // mic's tracks if a throw happens before a PitchTracker is even constructed -- e.g.
    // ensurePlayerLoaded() failing, or the audio-context-not-ready check below.
    let micStream: MediaStream | null = null;
    try {
      micStream = await requestMicStream();
      const player = await ensurePlayerLoaded(pkg);
      const context = audioContextRef.current;
      if (!context) throw new Error("audio context not ready");

      const tracker = new PitchTracker(context, micStream);
      // Assigned before init() (not after) so that if init() throws (e.g. the worklet module
      // 404s or fails to parse), the catch block below can still reach this tracker and stop it
      // -- which releases the mic's tracks via PitchTracker.stop(). Assigning only on success
      // left the mic stream unreachable from the catch block on this and every earlier throw.
      pitchTrackerRef.current = tracker;
      await tracker.init();

      setMicState("calibrating");
      player.play(0);
      setIsPlaying(true);
      // Don't start a second RAF chain if ordinary Play already has one running (rafRef.current
      // is non-null exactly when a tick() chain is scheduled -- see handlePlayPause/
      // handleTrackEnded, which are the only other places that set it, and both null it out
      // whenever playback stops). player.play(0) above still restarts the shared StemPlayer from
      // position 0 for calibration; the already-running tick() loop picks that up on its next
      // frame since it always reads playerRef.current/pitchTrackerRef.current fresh.
      if (rafRef.current === null) {
        rafRef.current = requestAnimationFrame(tick);
      }

      const floor = await measureBleedFloor(tracker, CALIBRATION_DURATION_MS);
      bleedFloorRef.current = floor + BLEED_FLOOR_MARGIN_RMS;
      scoreTrackerRef.current = new ScoreTracker();
      micActiveRef.current = true;
      setMicState("active");
    } catch (err) {
      setMicError((err as Error).message);
      setMicState("idle");
      micActiveRef.current = false;
      if (pitchTrackerRef.current) {
        // Owns the mic stream at this point -- stop() releases its tracks too.
        pitchTrackerRef.current.stop();
        pitchTrackerRef.current = null;
      } else {
        // No tracker was ever constructed (threw before that point, or requestMicStream() itself
        // threw and left micStream null) -- release the raw stream directly if we got one.
        micStream?.getTracks().forEach((track) => track.stop());
      }
    }
  }

  // The "Enable mic scoring" toggle's off switch -- the design spec calls this a toggle, but until
  // this fix round the only way to stop scoring and release the mic was to navigate away or reload
  // the page. Releases the mic (PitchTracker.stop() stops its tracks), drops both refs so a stale
  // pitch/score tracker can't be reached from tick() any more, and resets the score/live-Hz display so
  // a later re-enable doesn't show a stale number left over from the prior session. Playback itself
  // is left running -- disabling mic scoring shouldn't also stop the music.
  function handleDisableMicScoring() {
    pitchTrackerRef.current?.stop();
    pitchTrackerRef.current = null;
    scoreTrackerRef.current = null;
    micActiveRef.current = false;
    setMicState("idle");
    setScorePercent(0);
    setFramesCounted(0);
    setLiveHz(null);
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

  function handleStemVolumeChange(stem: keyof StemBuffers, value: number) {
    setStemVolumesState((prev) => ({ ...prev, [stem]: value }));
    playerRef.current?.setStemVolume(stem, value);
  }

  function handleStemMuteToggle(stem: keyof StemBuffers) {
    setStemMutedState((prev) => {
      const next = { ...prev, [stem]: !prev[stem] };
      playerRef.current?.setStemMuted(stem, next[stem]);
      return next;
    });
  }

  function handleTempoChange(percent: number) {
    setTempoPercent(percent);
    playerRef.current?.setTempo(percent / 100);
  }

  async function handleToggleBookmark() {
    setActionError(null);
    try {
      const result = await toggleBookmark(id);
      setTrackMeta((prev) => (prev ? { ...prev, bookmarked: result.bookmarked } : prev));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Bookmark failed.");
    }
  }

  async function handleRemove() {
    if (!window.confirm("Delete this track? This can't be undone.")) {
      return;
    }
    setActionError(null);
    try {
      await deleteTrack(id);
      router.push("/tracks");
    } catch (err) {
      setActionError((err as Error).message);
    }
  }

  function handlePitchChange(semitones: number) {
    setPitchSemitonesState(semitones);
    pitchSemitonesRef.current = semitones;
    playerRef.current?.setPitchSemitones(semitones);
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

  // Estimate shown before the button is pressed. Null until the track's duration is known --
  // guessing a number without one would be exactly the kind of plausible-looking placeholder
  // CLAUDE.md's measurement-discipline rule forbids.
  const upfrontEstimate =
    trackMeta && trackMeta.duration_seconds !== null
      ? estimateTotalSeconds(pendingStages(trackMeta), trackMeta.duration_seconds)
      : null;

  const headerTitle = trackMeta?.title ?? `Track ${id}`;
  const headerArtist = trackMeta?.artist;

  if (error) {
    return (
      <div className="min-h-screen">
        <header className="flex items-center gap-3 px-8 py-5 border-b border-surface-border">
          <Link href="/tracks" className="text-accent hover:text-accent-hover">
            <BackArrowIcon />
          </Link>
          <h1 className="text-lg font-bold">{headerTitle}</h1>
        </header>
        <main className="max-w-3xl mx-auto px-8 py-10">
          <p className="text-red-400">{error}</p>
        </main>
      </div>
    );
  }

  if (notReady) {
    return (
      <div className="min-h-screen">
        <header className="flex items-center gap-3 px-8 py-5 border-b border-surface-border">
          <Link href="/tracks" className="text-accent hover:text-accent-hover">
            <BackArrowIcon />
          </Link>
          <h1 className="text-lg font-bold">{headerTitle}</h1>
        </header>
        <main className="max-w-3xl mx-auto px-8 py-10">
          <p className="mb-4 text-muted">No karaoke package exists for this track yet.</p>
          {generating ? (
            <div className="max-w-md">
              <PipelineProgress
                stages={generatingStages}
                currentStage={generatingStage}
                trackDurationSeconds={generatingDuration}
                startedAt={generatingStartedAt}
              />
            </div>
          ) : (
            <>
              <button
                onClick={handleGenerate}
                className="rounded bg-accent px-4 py-2 text-white text-sm font-semibold hover:bg-accent-hover transition-colors"
              >
                Generate karaoke package
              </button>
              {upfrontEstimate !== null && (
                <p className="mt-2 text-xs text-muted">
                  Estimated {formatEstimate(upfrontEstimate)} on this machine.
                </p>
              )}
            </>
          )}
        </main>
      </div>
    );
  }

  if (pkg === null) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted">Loading...</p>
      </main>
    );
  }

  const lyricsWithheld = pkg.karaoke.words.every((w) => w.text === null);
  // Grouped from the transcription's own word timings -- see lib/lyrics.ts. Rendering every
  // word into one wrapping container turned a whole song into a solid paragraph.
  const lyricLines = groupWordsIntoLines(pkg.karaoke.words);
  // findActiveWordIndex returns an ARRAY POSITION, while the lines carry each word's own `idx`
  // field. Those happen to coincide today, but resolving the position to the real word makes the
  // comparison correct by construction rather than by coincidence.
  const activeWordId = pkg.karaoke.words[activeWordIndex]?.idx ?? -1;
  const currentLineIndex = activeLineIndex(lyricLines, activeWordId);
  const playheadX = (currentTimeMs / durationMs) * 400;

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between px-8 py-5 border-b border-surface-border">
        <div className="flex items-center gap-3">
          <Link href="/tracks" className="text-accent hover:text-accent-hover">
            <BackArrowIcon />
          </Link>
          <h1 className="text-lg font-bold">{headerTitle}</h1>
        </div>
        {headerArtist && <span className="text-sm text-muted">{headerArtist}</span>}
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10">
        {lyricsWithheld && (
          <p className="mb-4 rounded border border-surface-border bg-surface p-3 text-sm text-muted">
            Lyric display isn&apos;t permitted for this track &mdash; playing without lyrics.
          </p>
        )}

        <div className="rounded-lg border border-surface-border bg-surface p-5">
          <svg viewBox="0 0 400 60" className="w-full h-[70px] block">
            <polyline points={pitchPoints} fill="none" stroke="#e2431f" strokeWidth={2} />
            {activeFrameIndex >= 0 && (
              <line
                x1={playheadX}
                y1={0}
                x2={playheadX}
                y2={60}
                stroke="#f3efe7"
                strokeWidth={1.5}
                opacity={0.6}
              />
            )}
            {micState === "active" && liveHz !== null && (
              <circle cx={playheadX} cy={60 - (liveHz / maxPitchHz) * 55} r={4} fill="#f2582f" />
            )}
          </svg>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={handlePlayPause}
            disabled={loadingAudio || micState === "requesting" || micState === "calibrating"}
            className="flex items-center justify-center rounded bg-accent p-3 text-white disabled:opacity-50 hover:bg-accent-hover transition-colors"
          >
            {loadingAudio ? (
              <span className="text-xs px-1">...</span>
            ) : isPlaying ? (
              <PauseIcon />
            ) : (
              <PlayFilledIcon />
            )}
          </button>
          <input
            type="range"
            min={0}
            max={effectiveDurationSeconds || 1}
            step={0.1}
            value={currentTimeMs / 1000}
            onChange={handleSeek}
            disabled={loadingAudio || micState === "requesting" || micState === "calibrating"}
            className="flex-1 accent-[#e2431f]"
          />
          <span className="text-sm text-muted tabular-nums">
            {formatTime(currentTimeMs / 1000)} / {formatTime(effectiveDurationSeconds)}
          </span>
        </div>

        <div className="mt-6">
          {lyricLines.length > 0 && (
            <div className="mb-6 flex flex-col gap-3">
              {lyricLines.map((line, lineIdx) => {
                const isActiveLine = lineIdx === currentLineIndex;
                return (
                  <p
                    key={line.startMs}
                    className={`flex flex-wrap gap-x-2 leading-relaxed transition-all duration-200 ${
                      isActiveLine
                        ? "text-2xl text-foreground"
                        : "text-lg text-muted/60"
                    }`}
                  >
                    {line.words.map((word) => (
                      <span
                        key={word.idx}
                        className={
                          word.idx === activeWordId
                            ? "text-accent font-bold"
                            : undefined
                        }
                      >
                        {word.text ?? "•"}
                      </span>
                    ))}
                  </p>
                );
              })}
            </div>
          )}

          {micState === "idle" && (
            <button
              onClick={handleEnableMicScoring}
              className="rounded border border-accent px-4 py-2 text-accent text-sm font-medium hover:bg-surface-hover transition-colors"
            >
              Enable mic scoring
            </button>
          )}
          {micState === "requesting" && (
            <p className="text-sm text-muted">Requesting microphone access...</p>
          )}
          {micState === "calibrating" && (
            <p className="text-sm text-muted">Stay quiet -- calibrating...</p>
          )}
          {micState === "active" && (
            <div className="flex items-center gap-3">
              <p className="text-sm text-muted">
                Mic scoring active &mdash;{" "}
                {framesCounted === 0 ? "Listening..." : `${scorePercent.toFixed(0)}% on pitch`}
              </p>
              <button
                onClick={handleDisableMicScoring}
                className="rounded border border-surface-border px-3 py-1 text-muted text-xs font-medium hover:bg-surface-hover transition-colors"
              >
                Disable mic scoring
              </button>
            </div>
          )}
          {micError && <p className="mt-2 text-red-400 text-sm">{micError}</p>}
        </div>

        <div className="mt-8 border-t border-surface-border pt-6">
          <h2 className="text-sm font-bold mb-3">Mixer</h2>
          {STEM_ORDER.map((stem) => (
            <div key={stem} className="flex items-center gap-3 mb-2">
              <span className="w-16 text-sm capitalize text-muted">{stem}</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={stemVolumes[stem]}
                onChange={(e) => handleStemVolumeChange(stem, Number(e.target.value))}
                disabled={stemMuted[stem]}
                className="flex-1 accent-[#e2431f]"
              />
              <button
                onClick={() => handleStemMuteToggle(stem)}
                className={`rounded px-2 py-1 text-xs font-medium border transition-colors ${
                  stemMuted[stem]
                    ? "border-accent bg-accent text-white"
                    : "border-surface-border text-muted hover:bg-surface-hover"
                }`}
              >
                {stemMuted[stem] ? "Muted" : "Mute"}
              </button>
            </div>
          ))}
        </div>

        <div className="mt-6 border-t border-surface-border pt-6">
          <h2 className="text-sm font-bold mb-3">Transpose</h2>
          <div className="flex items-center gap-3 mb-2">
            <span className="w-16 text-sm text-muted">Key</span>
            <input
              type="range"
              min={-6}
              max={6}
              step={1}
              value={pitchSemitones}
              onChange={(e) => handlePitchChange(Number(e.target.value))}
              className="flex-1 accent-[#e2431f]"
            />
            <span className="w-12 text-sm text-right text-muted">
              {pitchSemitones > 0 ? `+${pitchSemitones}` : pitchSemitones}
            </span>
          </div>
          <div className="flex items-center gap-3">
            <span className="w-16 text-sm text-muted">Tempo</span>
            <input
              type="range"
              min={75}
              max={125}
              step={5}
              value={tempoPercent}
              onChange={(e) => handleTempoChange(Number(e.target.value))}
              className="flex-1 accent-[#e2431f]"
            />
            <span className="w-12 text-sm text-right text-muted">{tempoPercent}%</span>
          </div>
        </div>

        {pkg.karaoke.words.length > 0 && (
          <button
            onClick={() => void handleRegenerateLyrics()}
            disabled={regenerating}
            className="mt-2 rounded border border-surface-border px-3 py-1.5 text-xs font-medium text-muted disabled:opacity-50 hover:text-foreground hover:border-accent transition-colors"
          >
            {regenerating ? "Regenerating lyrics…" : "Regenerate lyrics"}
          </button>
        )}

        {actionError && <p className="mt-6 text-red-400 text-sm">{actionError}</p>}

        <div className="mt-8 flex items-center justify-between border-t border-surface-border pt-6">
          <div className="flex items-center gap-3">
            <button
              onClick={() => void handleToggleBookmark()}
              className={`rounded px-4 py-2 text-sm font-semibold border transition-colors ${
                trackMeta?.bookmarked
                  ? "border-accent bg-accent text-white"
                  : "border-accent text-accent hover:bg-surface-hover"
              }`}
            >
              {trackMeta?.bookmarked ? "Bookmarked" : "Bookmark"}
            </button>
            <button
              onClick={() => void handleRemove()}
              className="rounded border border-surface-border px-4 py-2 text-sm font-semibold hover:bg-surface-hover transition-colors"
            >
              Remove
            </button>
          </div>
          <Link href="/tracks" className="text-sm font-medium text-accent hover:underline">
            &larr; Back to library
          </Link>
        </div>
      </main>
    </div>
  );
}
