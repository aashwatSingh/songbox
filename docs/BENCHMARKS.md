# Benchmarks

Real, measured numbers only. `TODO: unmeasured` for anything not actually run — never a
plausible-looking placeholder (per `CLAUDE.md`).

## M3: Source separation (Demucs)

Measured on: 2026-08-21, on this dev machine, via `services/api/scripts/benchmark_separation.py`
against a synthetic 3-minute 440Hz tone (not real music — no rights clearance needed to run or
share this number). Each model's numbers below are the **median of 3 runs**, run in isolation
(nothing else running concurrently on the machine) to avoid the noise a single one-shot run under
system contention produces — see the git history of this file for what that noise looked like.

This machine DOES have a GPU (NVIDIA GeForce RTX 4060 Laptop, confirmed via `nvidia-smi`). What's
actually missing is a CUDA-enabled torch build — only the CPU-only torch wheel (`torch
2.13.0+cpu`) is installed. `docs/PLAN.md`'s Task 2 Step 2 documents installing the CUDA build via
`--index-url https://download.pytorch.org/whl/cu121`; nobody has run that on this branch yet, so
these numbers are CPU-only, not "no GPU available."

| Model | Wall clock (3min input, median of 3) | Realtime factor | Peak GPU memory |
|---|---|---|---|
| `htdemucs` | 60.6s | 2.97x | `TODO: unmeasured` (CUDA torch build not installed) |
| `htdemucs_ft` | 252.9s | 0.71x | `TODO: unmeasured` (CUDA torch build not installed) |

Individual runs behind the medians above (`python scripts/benchmark_separation.py <model>`,
run in isolation — nothing else on the machine at the same time):

- `htdemucs`: 56.8s (3.17x), 60.6s (2.97x), 78.4s (2.29x)
- `htdemucs_ft`: 252.9s (0.71x), 267.8s (0.67x), 239.0s (0.75x)

GPU numbers: `TODO: unmeasured`. The design spec's point for this table was comparing against the
original "~1-2x realtime on GPU" claim; a CPU-only run cannot make that comparison, so this is
called out explicitly rather than silently omitted.

Each timed run includes `Separator` construction (model weight load off disk), not inference
alone — this matches the real per-request cost of `POST /tracks/{id}/separate`, which also builds
a fresh `Separator` per call, so the realtime factor above is an honest request-level number
rather than a pure-inference one.

Quality comparison between `htdemucs` and `htdemucs_ft`: `TODO: unmeasured` — needs a real
listening test with real songs and human judgment, out of scope for M3 (see
`docs/superpowers/specs/2026-08-21-source-separation-design.md`).

Note: this is measured against the `local` CPU backend (this dev machine), not the eventual
Modal/RunPod production backend — per `docs/adr/0001-gpu-backend-abstraction.md`, production
cost/speed figures are `TODO: unmeasured` until that backend exists in M7.

## M4: Alignment engine (Whisper + wav2vec2)

Measured on: 2026-08-22, on this dev machine (CPU-only — same `torch 2.13.0+cpu` build as M3's
table above), via the real command:

```
cd services/api
python scripts/eval_alignment.py base
```

against **JamendoLyrics Multi-Lang** (`jamendolyrics/jamendolyrics` on Hugging Face) — a
Creative-Commons benchmark with manually annotated word-level timings covering English, German,
French, and Spanish. Wall clock: approximately **2h49m** (process start 18:36:17, final output
flush 21:25:43, from the redirected stdout/stderr log timestamps) — run to completion, no subset
of the dataset and no early stop, per the design spec's warning about what happened when M3's
benchmark was measured under time pressure instead.

**Rights note (binding, not optional):** most JamendoLyrics tracks are CC BY-NC-ND / CC BY-NC-SA,
not rights-clean for this product's own Lane C — see `docs/superpowers/specs/2026-08-21-alignment-
engine-design.md`'s licensing correction. `scripts/eval_alignment.py` downloads each track's audio
to an ephemeral temp directory, runs the pipeline for scoring only, and deletes every derived
artifact (source audio, separated stems, alignment output) immediately after that track is scored,
inside a `finally` block. Any row whose `license_type` contains `"ND"` is skipped entirely before
any audio is even touched. Only the aggregate numbers below were ever committed — no audio, no
per-track derived file, from this dataset or otherwise.

Exact literal printed output:

```
Scored 40 tracks, skipped 39 ND-licensed tracks.

=== Aligned (wav2vec2 forced alignment against reference lyrics, English only) ===
  en: n=2188 words, median error=68.2ms, within 50ms=37.2%

=== Whisper-native (whisper_model_size='base', matched against reference via difflib) ===
  de: n=505 words, median error=124.4ms, within 50ms=18.2%
  en: n=1268 words, median error=95.3ms, within 50ms=31.2%
  es: n=1988 words, median error=137.2ms, within 50ms=18.8%
  fr: n=1955 words, median error=135.6ms, within 50ms=16.2%
  mean match rate across tracks: 50.8%
```

| Path | Language | n (words) | Median onset error | Within 50ms |
|---|---|---|---|---|
| Aligned (wav2vec2 forced alignment) | en | 2188 | 68.2ms | 37.2% |
| Whisper-native | de | 505 | 124.4ms | 18.2% |
| Whisper-native | en | 1268 | 95.3ms | 31.2% |
| Whisper-native | es | 1988 | 137.2ms | 18.8% |
| Whisper-native | fr | 1955 | 135.6ms | 16.2% |

Whisper-native mean match rate across tracks (difflib reconciliation of predicted vs. reference
words before scoring, so a low match rate can't hide inside an artificially good onset number):
**50.8%**.

**Against `docs/PLAN.md`'s M4 acceptance criterion** ("measured word-onset error is within ±50ms
median"): the primary, production-relevant number — aligned English, 68.2ms median, 37.2% within
50ms — does **not** meet that bar. This is the real, measured result against the real target; see
`docs/STATUS.md`'s M4a entry for how this is being carried forward.

Non-English languages were scored only through the whisper-native path (`align_words()` is not run
for them — see the script's docstring), since forced alignment against a non-English reference
requires a multilingual aligner, which is license-blocked for this commercial product per the
design spec (`torchaudio.pipelines.MMS_FA` is CC-BY-NC 4.0, non-commercial only). No language
produced zero scored words, so no cell above is `TODO: unmeasured`.

This is measured against the `local` CPU backend, same caveat as M3's table: not representative of
eventual Modal/RunPod production timing, which is `TODO: unmeasured` until M7.
