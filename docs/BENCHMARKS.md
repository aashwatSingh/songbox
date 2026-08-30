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
engine-design.md`'s licensing correction. `scripts/eval_alignment.py` calls `load_dataset(...)`
first, which downloads the **full dataset snapshot** into the local Hugging Face cache — all 79
tracks, including all 39 ND-licensed ones (confirmed: ~393MB sits in the HF cache after a run).
The ND check happens before any *processing* of a track's audio, not before it's downloaded: any
row whose `license_type` contains `"ND"` is skipped before separation, transcription, or alignment
ever run on it, so no derivative work is ever created from ND-licensed audio, which is what ND
actually forbids. Only a per-track *working copy* — the file copied out to a `songbox-eval-*` temp
directory for processing — is ephemeral, deleted immediately after that track is scored, inside a
`finally` block. Only the aggregate numbers below were ever committed — no per-track derived file,
from this dataset or otherwise.

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

## M5: Pitch extraction (torchcrepe)

Measured on: 2026-08-23, on this dev machine (CPU-only — same `torch 2.13.0+cpu` build as M3/M4's
tables above; `torch.cuda.is_available()` returns `False`), via the real commands:

```
cd services/api
python scripts/benchmark_pitch.py tiny
python scripts/benchmark_pitch.py full
```

against a synthetic 3-minute 220Hz sine tone generated by `ffmpeg` at 44.1kHz stereo (not real
singing — no rights clearance needed to run or share this number; see the script's own docstring).
This measures `app.packaging.extract_pitch`'s wall-clock speed only, not pitch accuracy, since a
sine tone has no meaningful pitch-detection ground truth beyond "constant 220Hz."

Exact literal printed output:

```
model=tiny
  input duration: 180s
  wall clock: 31.6s
  realtime factor: 5.69x
  frames produced: 18001

model=full
  input duration: 180s
  wall clock: 587.2s
  realtime factor: 0.31x
  frames produced: 18001
```

| Model | Wall clock (3min input) | Realtime factor | Frames produced |
|---|---|---|---|
| `tiny` | 31.6s | 5.69x | 18001 |
| `full` | 587.2s | 0.31x | 18001 |

Each is a single run, not a median of several like M3's table — noted here rather than silently
presented as more rigorous than it is. `tiny` runs well faster than realtime on CPU (5.69x); `full`
runs at less than a third of realtime (0.31x) on this CPU-only build, consistent with `full` being
the much larger of torchcrepe's two published model sizes. This is the basis for Task 3's default
of `tiny` for `POST /tracks/{id}/package`, with `full` available as an explicit, slower opt-in.

Pitch **accuracy** against real singing: `TODO: unmeasured`. No ground-truth vocal pitch dataset
(e.g., labeled monophonic singing with known f0 contours) is in scope for this milestone — this
section measures speed only, matching this project's discipline of never writing a
plausible-looking number that wasn't actually measured.

This is measured against the `local` CPU backend, same caveat as M3/M4's tables: not representative
of eventual Modal/RunPod production timing, which is `TODO: unmeasured` until M7.

## M6c: Pitch detection (YIN AudioWorklet)

Measured on: 2026-08-27, in a real live browser session (`javascript_tool` against a running
`AudioContext`), via the method in `docs/superpowers/plans/2026-08-27-live-mic-scoring.md`'s Task 1
Step 2: an `OscillatorNode` set to a known frequency is connected into the worklet
(`pitch-detector`, registered from `apps/web/public/pitch-worklet.js`) in place of a mic source, run
for long enough to collect several readings, and the settled readings' `hz` values (the first few
skipped while the ring buffer is still filling) are averaged.

| Test signal | Measured avgHz |
|---|---|
| 440Hz oscillator | ≈440.02 |
| 220Hz oscillator | ≈220.00 |

Both numbers were independently reproduced twice — once by Task 1's implementer during its own live
verification, and again by the task reviewer's independent re-run against the same method — with no
octave error (YIN's well-known failure mode, which would show up as ≈880 or ≈220 for the 440Hz case)
in either run.

**What this measures, and what it does not:** this is the worklet's algorithmic accuracy against a
clean, single-frequency synthetic signal with no noise, no harmonics beyond the oscillator's own,
and no backing-track bleed — the easy case for a YIN-family detector, and it says nothing about
real-world vocal pitch-tracking accuracy (a singing voice has vibrato, formants, and breath noise no
oscillator has) or about bleed survival (`docs/PLAN.md` open question 3, `apps/web/lib/
micScoring.ts`'s calibration/RMS-floor-gate mechanism). Both remain `TODO: unmeasured` — closing
them needs a real human voice and, for bleed, a real room with real speakers and a real microphone,
per this milestone's design spec and `docs/STATUS.md`'s M6c entry.

## M7c: Cloud GPU backend (Modal), real cost-per-track

Measured on: 2026-08-30, against the real deployed `songbox-gpu` Modal app (`services/api/app/
modal_app.py`), GPU type `A10` ($0.000306/second per `modal.com/pricing`, recorded in
`config/gpu_costs.yaml`). This is the first real measurement of production GPU cost this project
has ever had — every prior milestone's timing numbers (M3/M4/M5/M6c above) were measured against
the `local` CPU backend and explicitly marked not representative of Modal/RunPod production timing.

**Real, measured durations** (a single 3-second synthetic sine-tone test track, full pipeline via
`GPU_BACKEND=modal`):

| Stage | Duration | Real output |
|---|---|---|
| `/separate` | 13.8s | 4 real stems (vocals/drums/bass/other) |
| `/transcribe` | 8.9s | `language=en` (real faster-whisper + wav2vec2 output) |
| `/package` | 21.1s | `tempo_bpm=139.67` (real torchcrepe + librosa output) |

**Cost for that one full-pipeline run:** (13.8 + 8.9 + 21.1) × $0.000306/s ≈ **$0.0134**.

**Light load test** (per the approved scope — 3-5 real concurrent jobs, not a production-scale
test): 4 concurrent `/separate` calls against 4 distinct synthetic tracks, all succeeded (200).
Individual durations: 9.5s, 4.3s, 5.8s, 2.5s (sum: 22.1s of real GPU-compute-seconds, cost ≈
$0.0068 for this batch) — but the whole batch's **wall-clock** was only 9.5s, matching the single
slowest job rather than the sum of all four. This is real, direct evidence of genuine concurrency:
Modal's containers run independently in parallel, unlike the `local` backend's single
process-wide lock, which serializes every job onto one machine.

**What this measures, and what it does not:** these numbers are for a 3-second synthetic test
tone, not a real multi-minute song — Demucs/Whisper/CREPE processing time scales with track
length, so this is not a representative "cost per real track" figure, and this document will not
claim one until a real multi-minute track is actually measured. What these numbers *do* establish,
for the first time with real data: the pipeline genuinely works end-to-end on Modal's
infrastructure, genuinely runs multiple jobs in parallel, and costs a small fraction of a cent per
GPU-second — closing `docs/PLAN.md` open question 4's "cost per track" question is now blocked
only on running a real-length track through this same real deployment, not on any remaining
engineering work.

**A real architectural finding from this validation pass, not a hypothetical:** `run_separate`
cannot use `block_network=True` the way the other three pipeline functions do. Its return value
(four full-length audio stems) routinely exceeds Modal's real, documented 2 MiB inline-payload
threshold, and payloads above that threshold are transported through Modal's own blob-storage
backend — from inside the function's container, which `block_network=True` blocks (confirmed by a
real failed deploy: a genuine `ClientConnectorDNSError` reaching
`modal-blobs.s3-accelerate.amazonaws.com` from inside the container, not from `separate_audio()`'s
own code, which never makes a network call of its own). `run_transcribe`/`run_realign`/
`run_package` return small structured data well under 2 MiB and keep `block_network=True`. See
`app/modal_app.py`'s comment on `run_separate` for the full account.
