import { describe, expect, test } from "vitest";
import { activeLineIndex, groupWordsIntoLines } from "@/lib/lyrics";
import type { WordInfo } from "@/lib/api";

function word(idx: number, text: string | null, start: number, end: number): WordInfo {
  return { idx, text, start_ms: start, end_ms: end, confidence: 1 };
}

describe("groupWordsIntoLines", () => {
  test("breaks on a real pause between words", () => {
    const lines = groupWordsIntoLines([
      word(0, "one", 0, 100),
      word(1, "two", 120, 200),
      // 800ms of silence -- a line break, not word spacing
      word(2, "three", 1000, 1100),
    ]);

    expect(lines.map((l) => l.words.map((w) => w.text))).toEqual([["one", "two"], ["three"]]);
  });

  test("caps line length even when words run together", () => {
    // Dense delivery with no pauses at all: the gap rule never fires, so without the word cap
    // this returns one enormous line -- the "reading an essay" bug.
    const words = Array.from({ length: 20 }, (_, i) => word(i, `w${i}`, i * 100, i * 100 + 90));
    const lines = groupWordsIntoLines(words, { maxWords: 7 });

    // Assert the invariant, not an exact line count: no line over the cap, nothing lost or
    // duplicated, and no stray one-word orphan. Where exactly the break lands depends on which
    // pause scores highest, which is the point of the pause-aware break.
    expect(lines.length).toBeGreaterThan(1);
    for (const line of lines.slice(0, -1)) {
      expect(line.words.length).toBeLessThanOrEqual(7);
      expect(line.words.length).toBeGreaterThan(1);
    }
    expect(lines.flatMap((l) => l.words.map((w) => w.idx))).toEqual(words.map((w) => w.idx));
  });

  test("forced breaks land on the longest pause, not on the word count", () => {
    // A clear breath after "long" -- the break belongs there, not three words later at the cap.
    const lines = groupWordsIntoLines(
      [
        word(0, "counting", 0, 200),
        word(1, "every", 210, 400),
        word(2, "street", 410, 600),
        word(3, "light", 610, 800),
        word(4, "on", 810, 900),
        word(5, "the", 910, 1000),
        word(6, "long", 1010, 1200),
        // 300ms breath -- under the hard gap threshold, but the best break available
        word(7, "way", 1500, 1700),
        word(8, "home", 1710, 1900),
      ],
      { maxWords: 7, gapMs: 550 },
    );

    expect(lines[0].words[lines[0].words.length - 1].text).toBe("long");
    expect(lines[1].words.map((w) => w.text)).toEqual(["way", "home"]);
  });

  test("ends a line at sentence-ending punctuation", () => {
    const lines = groupWordsIntoLines([
      word(0, "stop", 0, 100),
      word(1, "here.", 110, 200),
      word(2, "next", 210, 300),
    ]);

    expect(lines.map((l) => l.words.length)).toEqual([2, 1]);
  });

  test("keeps withheld words as placeholders instead of dropping them", () => {
    const lines = groupWordsIntoLines([word(0, null, 0, 100), word(1, null, 110, 200)]);

    expect(lines).toHaveLength(1);
    expect(lines[0].words).toHaveLength(2);
  });

  test("carries real start and end times for each line", () => {
    const lines = groupWordsIntoLines([
      word(0, "a", 500, 600),
      word(1, "b", 620, 900),
      word(2, "c", 2000, 2100),
    ]);

    expect(lines[0]).toMatchObject({ startMs: 500, endMs: 900 });
    expect(lines[1]).toMatchObject({ startMs: 2000, endMs: 2100 });
  });

  test("returns nothing for no words", () => {
    expect(groupWordsIntoLines([])).toEqual([]);
  });
});

describe("activeLineIndex", () => {
  test("finds the line holding the active word by its id, not its position", () => {
    // idx values deliberately do NOT match array positions here.
    const lines = groupWordsIntoLines([
      word(10, "one", 0, 100),
      word(11, "two.", 110, 200),
      word(12, "three", 900, 1000),
    ]);

    expect(activeLineIndex(lines, 12)).toBe(1);
    expect(activeLineIndex(lines, 10)).toBe(0);
  });

  test("returns -1 when nothing is active", () => {
    const lines = groupWordsIntoLines([word(0, "x", 0, 100)]);
    expect(activeLineIndex(lines, -1)).toBe(-1);
  });
});
