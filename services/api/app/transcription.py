from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import soundfile as sf
import torch
import torchaudio
import torchaudio.functional as F
from faster_whisper import WhisperModel

ENGLISH_LANGUAGE_CODE = "en"
DEFAULT_WHISPER_MODEL_SIZE = "base"


class TranscriptionError(Exception):
    """Raised when Whisper cannot transcribe the given file. Never includes transcript text in
    its message (CLAUDE.md: never log raw lyrics) -- transcription failures are about the
    process, not about specific words, so this is naturally satisfied by describing the failure
    mode rather than any content."""


class AlignmentError(Exception):
    """Raised when forced alignment cannot align the given text against the given audio. Never
    includes word or transcript text in its message (CLAUDE.md: never log raw lyrics)."""


@dataclass(frozen=True)
class Word:
    idx: int
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    words: list[Word]  # Whisper-native word timings (word_timestamps=True)


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    aligner: str  # "wav2vec2" | "whisper_native"
    words: list[Word]


def transcribe_audio(path: Path, model_size: str = DEFAULT_WHISPER_MODEL_SIZE) -> Transcript:
    """Transcribe `path` with faster-whisper, requesting word-level timestamps directly from
    Whisper. Used as-is for non-English tracks (the "whisper_native" aligner path) and as the
    source text for English tracks, which then get forced-aligned by align_words() for tighter
    timing precision."""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:
        raise TranscriptionError(f"could not load whisper model {model_size!r}: {exc}") from exc

    try:
        segments, info = model.transcribe(str(path), word_timestamps=True)
        segment_list = list(segments)
    except Exception as exc:
        raise TranscriptionError(f"transcription failed: {exc}") from exc

    text_parts: list[str] = []
    words: list[Word] = []
    idx = 0
    for segment in segment_list:
        text_parts.append(segment.text.strip())
        if segment.words is None:
            raise TranscriptionError("word_timestamps=True but a segment had no words")
        for w in segment.words:
            words.append(
                Word(
                    idx=idx,
                    text=w.word.strip(),
                    start_ms=int(w.start * 1000),
                    end_ms=int(w.end * 1000),
                    confidence=w.probability,
                )
            )
            idx += 1

    # No speech detected is a legitimate empty result, not an error -- an instrumental-only
    # track, a near-silent vocals stem after separation, or (in tests) a synthetic tone are all
    # real, valid input. faster-whisper still identifies a language from an initial audio window
    # independent of segmentation/VAD (confirmed empirically: info.language is a valid non-empty
    # code even when zero segments are found), so there is nothing to raise here -- just return
    # the empty result and let the caller (run_transcription_and_alignment) decide what to do
    # with zero words.
    return Transcript(text=" ".join(text_parts).strip(), language=info.language, words=words)


_WAV2VEC2_BUNDLE = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H


def _load_waveform(path: Path) -> tuple[torch.Tensor, int]:
    # soundfile reads WAV directly via libsndfile -- no FFmpeg/TorchCodec needed. align_words()
    # only ever receives guaranteed-WAV input (the M3 vocals stem, already asserted 44.1kHz
    # stereo WAV at that stage boundary, or this module's own WAV test fixtures), never the
    # arbitrary-format uploads upload_track() has to handle, so torchaudio.load()'s newer
    # TorchCodec-backed default (torchaudio >= 2.9) -- which needs a full-shared FFmpeg build not
    # present on this machine -- is more machinery than this function actually needs. Do not
    # "fix" this back to torchaudio.load() without re-checking that FFmpeg/TorchCodec
    # constraint.
    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    # soundfile returns (samples, channels); the rest of this function expects torchaudio's
    # (channels, samples) convention.
    waveform = torch.from_numpy(data.T).contiguous()
    return waveform, sample_rate


def align_words(path: Path, text: str) -> list[Word]:
    """Force-align `text` (assumed correct) against the audio at `path` using the MIT-licensed
    English wav2vec2 ASR bundle. English only -- callers must route non-English tracks to
    transcribe_audio()'s own word timings instead (see run_transcription_and_alignment).

    Runs the acoustic forward pass over the whole clip in one call (not chunked) -- see this
    file's module-level design note in the plan for why that's correct and bounded. Word
    boundaries come from tokenizing per-word and regrouping by known word length, not from the
    bundle's '|' separator token.
    """
    words_text = text.split()
    if not words_text:
        raise AlignmentError("cannot align empty text")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = _WAV2VEC2_BUNDLE.get_model().to(device)
        model.eval()
    except Exception as exc:
        raise AlignmentError(f"could not load alignment model: {exc}") from exc

    labels = _WAV2VEC2_BUNDLE.get_labels()
    dictionary = {c: i for i, c in enumerate(labels)}

    tokens_per_word: list[list[int]] = []
    for word in words_text:
        word_tokens = [dictionary[c] for c in word.upper() if c in dictionary]
        if not word_tokens:
            raise AlignmentError("transcript contains a word with no alignable characters")
        tokens_per_word.append(word_tokens)
    flat_tokens = [t for word_tokens in tokens_per_word for t in word_tokens]

    try:
        waveform, sample_rate = _load_waveform(path)
    except Exception as exc:
        raise AlignmentError(f"could not load audio: {exc}") from exc
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sample_rate != _WAV2VEC2_BUNDLE.sample_rate:
        waveform = F.resample(waveform, sample_rate, _WAV2VEC2_BUNDLE.sample_rate)

    with torch.inference_mode():
        emission, _ = model(waveform.to(device))
        emission = torch.log_softmax(emission, dim=-1)

    targets = torch.tensor([flat_tokens], dtype=torch.int32, device=device)
    try:
        aligned_tokens, alignment_scores = F.forced_align(emission, targets, blank=0)
    except Exception as exc:
        raise AlignmentError(f"forced alignment failed: {exc}") from exc
    aligned_tokens, alignment_scores = aligned_tokens[0], alignment_scores[0].exp()

    token_spans = F.merge_tokens(aligned_tokens, alignment_scores)
    if len(token_spans) != len(flat_tokens):
        raise AlignmentError("alignment produced an unexpected number of token spans")

    word_spans = _unflatten(token_spans, [len(wt) for wt in tokens_per_word])

    num_frames = emission.shape[1]
    ratio = waveform.shape[1] / num_frames

    words: list[Word] = []
    for idx, (word_text, spans) in enumerate(zip(words_text, word_spans, strict=True)):
        start_sample = ratio * spans[0].start
        end_sample = ratio * spans[-1].end
        start_ms = int(start_sample / _WAV2VEC2_BUNDLE.sample_rate * 1000)
        end_ms = int(end_sample / _WAV2VEC2_BUNDLE.sample_rate * 1000)
        confidence = sum(s.score for s in spans) / len(spans)
        words.append(
            Word(idx=idx, text=word_text, start_ms=start_ms, end_ms=end_ms, confidence=confidence)
        )

    return words


def _unflatten[T](items: list[T], lengths: list[int]) -> list[list[T]]:
    result: list[list[T]] = []
    i = 0
    for length in lengths:
        result.append(items[i : i + length])
        i += length
    return result


def run_transcription_and_alignment(path: Path, model_size: str) -> TranscriptionResult:
    """Orchestrates the full stage: transcribe, then align English tracks against their own
    transcript for tighter word-onset precision; non-English tracks keep Whisper's own word
    timings, since the alignment model here only covers English (see the design spec's
    licensing-blocked-multilingual-aligner scope decision)."""
    transcript = transcribe_audio(path, model_size=model_size)
    if not transcript.words:
        # No speech detected at all -- aligning empty text is meaningless (align_words() would
        # just raise AlignmentError on it), and there is trivially nothing for wav2vec2 to have
        # changed, so "whisper_native" is the right label here just as it is for the
        # non-English/no-alignment-coverage case below.
        words = transcript.words
        aligner = "whisper_native"
    elif transcript.language == ENGLISH_LANGUAGE_CODE:
        words = align_words(path, transcript.text)
        aligner = "wav2vec2"
    else:
        words = transcript.words
        aligner = "whisper_native"
    return TranscriptionResult(
        text=transcript.text, language=transcript.language, aligner=aligner, words=words
    )
