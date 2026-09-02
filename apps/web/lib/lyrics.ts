import type { WordInfo } from "@/lib/api";

export interface LyricLine {
  words: WordInfo[];
  startMs: number;
  endMs: number;
}

/**
 * A pause long enough to read as a line break rather than ordinary word spacing. Rap delivery
 * leaves very little silence between words, so this alone would produce enormous lines -- hence
 * the word cap below.
 */
const DEFAULT_GAP_MS = 550;

/**
 * Hard ceiling on words per line. This is what actually does the work on dense vocals, where the
 * gap rule almost never fires. Seven is roughly what fits on one line at the player's text size
 * without wrapping on a narrow window, which is the point: a wrapped "line" is not a line.
 */
const DEFAULT_MAX_WORDS = 7;

/** Sentence-ending punctuation always ends a line, regardless of timing. */
const SENTENCE_END = /[.!?]"?$/;

/** A comma or similar is a weaker hint, used only to steer where a forced break lands. */
const CLAUSE_END = /[,;:]"?$/;
const CLAUSE_BONUS_MS = 120;

/** Never cut a line shorter than this, or the cap produces two-word fragments. */
const MIN_WORDS_BEFORE_BREAK = 3;

/**
 * Group word-level timings into readable karaoke lines.
 *
 * The player used to render every word into one wrapping flex container, so a full song arrived
 * as a solid paragraph. Lines are derived from the transcription's own word timings rather than
 * invented: a break happens at a real pause, at sentence-ending punctuation, or when a line hits
 * the width cap.
 *
 * Words whose text is null (lyrics withheld for rights reasons) still occupy their slot, so the
 * shape of the song stays visible even when the words cannot be shown.
 */
export function groupWordsIntoLines(
  words: readonly WordInfo[],
  options: { gapMs?: number; maxWords?: number } = {},
): LyricLine[] {
  const gapMs = options.gapMs ?? DEFAULT_GAP_MS;
  const maxWords = options.maxWords ?? DEFAULT_MAX_WORDS;

  const lines: LyricLine[] = [];
  let current: WordInfo[] = [];

  const flush = () => {
    if (current.length > 0) {
      lines.push({
        words: current,
        startMs: current[0].start_ms,
        endMs: current[current.length - 1].end_ms,
      });
      current = [];
    }
  };

  // When the length cap forces a break, cut at the BIGGEST pause in the line rather than exactly
  // at word N. Cutting blindly at the cap produced orphans like "on the long / way home" and
  // "the spring I / will carry every word"; splitting at the longest silence lands the break
  // where the singer actually breathes.
  const breakAtBestPause = (incomingGapMs: number) => {
    // Seed with the gap to the word about to be appended, scored as "break right here". Without
    // this the search could only see gaps already inside the line and would miss the common case
    // where the real breath is immediately after the last word -- which is exactly how
    // "on the long / way home" got split at "the".
    let bestIndex = current.length;
    let bestScore = incomingGapMs;
    for (let i = MIN_WORDS_BEFORE_BREAK; i < current.length; i += 1) {
      const gap = current[i].start_ms - current[i - 1].end_ms;
      const previousText = current[i - 1].text ?? "";
      const score = gap + (CLAUSE_END.test(previousText) ? CLAUSE_BONUS_MS : 0);
      if (score >= bestScore) {
        bestScore = score;
        bestIndex = i;
      }
    }
    if (bestIndex <= 0 || bestIndex >= current.length) {
      flush();
      return;
    }
    const remainder = current.slice(bestIndex);
    current = current.slice(0, bestIndex);
    flush();
    current = remainder;
  };

  for (const word of words) {
    const previous = current[current.length - 1];
    const pause = previous ? word.start_ms - previous.end_ms : 0;
    if (previous && pause >= gapMs) {
      flush();
    } else if (current.length >= maxWords) {
      breakAtBestPause(pause);
    }
    current.push(word);
    if (word.text !== null && SENTENCE_END.test(word.text)) {
      flush();
    }
  }
  flush();

  return lines;
}

/** Index of the line containing the currently-sung word, or -1. */
export function activeLineIndex(lines: readonly LyricLine[], activeWordIdx: number): number {
  if (activeWordIdx < 0) {
    return -1;
  }
  return lines.findIndex((line) => line.words.some((w) => w.idx === activeWordIdx));
}
