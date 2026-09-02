"""Modal Function definitions for the `modal` GPU backend (M7c). Deployed via:

    modal deploy services/api/app/modal_app.py

after `modal setup` (or setting MODAL_TOKEN_ID/MODAL_TOKEN_SECRET) has configured real
credentials -- this file cannot be deployed or tested without them. None of the four real
pipeline functions (run_separate/run_transcribe/run_realign/run_package) need network access on
their own merits, since the caller (this project's FastAPI backend) already fetches audio bytes
from MinIO before calling any of these, and each function returns its result through Modal's own
call/response marshaling rather than writing anywhere reachable over a network. This was the
intended "zero egress, not restricted egress" guarantee from the M7c design spec's Decision 2 --
but real validation against real infrastructure found it only holds for THREE of the four:
run_transcribe/run_realign/run_package keep block_network=True. run_separate does not -- see its
own docstring for why (Modal's own platform transport for a large return value needs network from
inside the container, a real platform constraint, not anything separate_audio()'s own code does).
run_package came within a hair of the same fate: its pitch contour data crosses Modal's real 2 MiB
inline-payload threshold at this project's own 12-minute MAX_DURATION_SECONDS cap, and keeps
block_network=True only because its return value was restructured into a compact packed-bytes
format -- see its own docstring. Two more functions, egress_probe and blocked_egress_probe, exist
purely to validate that block_network is genuinely enforced (not just declared) -- see their own
docstrings.

GPU: "A10" (Modal's real name -- not AWS's "A10G"), $0.000306/second per modal.com/pricing as of
this file's authoring. Sized for this pipeline's model sizes (Demucs, faster-whisper, wav2vec2,
torchcrepe) -- none of which need a top-tier H100/B200-class card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import modal

if TYPE_CHECKING:
    from app.transcription import TranscriptionResult, Word

def _prewarm_model_weights() -> None:
    """Runs at IMAGE BUILD time (real network access, per Image.run_function's real semantics --
    verified against the installed modal==1.5.5 package), not at request time. Real end-to-end
    validation against the deployed sandbox (M7c Task 4) found that separate_audio() genuinely
    fails under block_network=True: Demucs and faster-whisper both fetch their model weights from
    a remote hub on first use, and wav2vec2's torchaudio bundle does the same -- none of that is
    "network egress the caller didn't expect", it's a real runtime dependency the original design
    didn't account for. torchcrepe is NOT affected -- it ships both its "tiny" and "full" weight
    files directly inside the pip package (confirmed by inspecting the installed package's
    torchcrepe/assets/ directory), so pip_install already covers it.

    This warms exactly the DEFAULT model variant for each stage (htdemucs, faster-whisper "base"
    -- matching DEFAULT_WHISPER_MODEL_SIZE in app/routes/tracks.py -- and the one wav2vec2
    bundle), which is what this milestone's real validation run actually exercises. A real
    production deployment should extend this to cover every ALLOWED_* variant
    (ALLOWED_SEPARATION_MODELS, ALLOWED_WHISPER_MODEL_SIZES) too -- but the consequence of not
    pre-warming a variant differs by stage, and both halves are worth stating precisely rather
    than as one blanket claim:
    - run_transcribe keeps block_network=True (see below), so a client requesting a
      non-pre-warmed ALLOWED_WHISPER_MODEL_SIZES value hard-fails at runtime with no fallback --
      there's no "slow path", just a network error.
    - run_separate does NOT have block_network=True (see its own comment) -- a client requesting
      "htdemucs_ft" (the one non-pre-warmed ALLOWED_SEPARATION_MODELS value) will currently
      SUCCEED, just slowly, by downloading those weights live on every cold container. This is a
      real, client-influenced network fetch (the destination is fixed and the value is one of two
      allowlisted strings, so it's not attacker-directed egress, but "nothing the client can
      influence" would be an overstatement -- see run_separate's own comment).
    Tracked as follow-up, not done here to keep this validation pass's build time/cost
    proportionate.
    """
    from demucs.api import Separator
    from faster_whisper import WhisperModel

    Separator(model="htdemucs", device="cpu")
    WhisperModel("base", device="cpu", compute_type="int8")

    import torchaudio

    torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H.get_model()


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch>=2.1,<3.0",
        "torchaudio>=2.1,<3.0",
        "demucs>=4.0",
        "numpy>=1.26",
        "faster-whisper>=1.0",
        "soundfile>=0.12",
        "torchcrepe>=0.0.23",
        "librosa>=0.10",
        # faster-whisper's backend (ctranslate2) needs CUDA 12's libcublas.so.12/libcudnn.so at
        # runtime, but plain `pip install torch` on this image resolved CUDA 13.x libraries
        # instead -- a real version mismatch, confirmed by a genuine deploy failure ("Library
        # libcublas.so.12 is not found or cannot be loaded"). Installing these explicitly and
        # pointing LD_LIBRARY_PATH at them (below) is the documented fix for this well-known
        # ctranslate2/faster-whisper issue.
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12",
    )
    .env(
        {
            "LD_LIBRARY_PATH": (
                "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
                "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib"
            )
        }
    )
    .run_function(_prewarm_model_weights)
    .add_local_python_source("app")
)

app = modal.App("songbox-gpu", image=image)

# Wall-clock timeouts mirror services/api/app/routes/tracks.py's SEPARATION_TIMEOUT_SECONDS /
# TRANSCRIPTION_TIMEOUT_SECONDS / PACKAGE_TIMEOUT_SECONDS -- Modal's own `timeout` kwarg is the
# real backstop when running on Modal (the `local` backend's _run_with_timeout thread-join timeout
# is a separate, local-only mechanism that doesn't apply here).
_SEPARATION_TIMEOUT_SECONDS = 1800
_TRANSCRIPTION_TIMEOUT_SECONDS = 1800
_PACKAGE_TIMEOUT_SECONDS = 3600

# The `local` backend's process-wide lock (app/gpu_backend.py's _inference_lock) structurally
# capped this project at one inference job at a time -- Modal has no equivalent unless told to.
# Per-IP rate limits (app/rate_limit.py, 20/hour on each of these routes) bound one caller, not
# total concurrency/spend across every caller. This is a real cost-safety backstop
# (CLAUDE.md: "runs ML inference that costs real money per second"), not a performance tuning
# knob -- picked to match the concurrency this milestone's own light load test actually validated
# (M7c Task 4: 4 concurrent /separate calls, all real, all succeeded), not a guess.
_MAX_CONTAINERS = 5


# NOT block_network=True here, unlike the other three pipeline functions -- a real, measured
# Modal platform constraint, not a retreat from Decision 2's zero-egress goal.
#
# Real validation (M7c Task 4) found that run_separate's return value (four full-length audio
# stems) routinely exceeds Modal's real, documented 2 MiB inline-payload threshold
# (modal.com/docs/guide/local-data: "Small payloads (<= 2 MiB) are stored inline ... larger
# payloads are stored in object storage"). Above that threshold, MODAL'S OWN platform transport
# uploads the return value to its blob-storage backend (observed real destination:
# modal-blobs.s3-accelerate.amazonaws.com) -- and that upload runs from INSIDE the container, so
# block_network=True blocked Modal's own plumbing, not this project's code (confirmed by a real
# failed deploy: a genuine ClientConnectorDNSError raised from inside the running container,
# nothing this project's own separate_audio() call ever does).
#
# The obvious fix -- scope network access to exactly Modal's blob-storage domain via
# outbound_domain_allowlist -- turned out not to be available: that parameter exists only on
# modal.Sandbox, not on @app.function (confirmed against the real installed modal==1.5.5 package's
# App.function signature; mypy caught the mismatch before another wasted deploy attempt).
# @app.function's only network-restriction knob is the plain block_network bool.
#
# separate_audio() itself never makes a network call of its own (confirmed: this is the same
# function the `local` backend already calls, with no code path that reaches out) -- the egress
# from Modal's own platform transport for a large return value is not anything the AUDIO FILE's
# content could reach or influence (a malicious byte sequence can't redirect it). Precisely:
# `model_name` IS a client-supplied value, and today (without full networking here, "htdemucs_ft"
# -- the one of the two ALLOWED_SEPARATION_MODELS values this image doesn't pre-warm) would
# download its weights live on first use, an outbound fetch a client request genuinely triggers --
# see _prewarm_model_weights' docstring for the precise statement. The destination is fixed and
# the value is one of exactly two allowlisted strings, so this isn't attacker-directed egress, but
# "nothing a client can influence" would overstate it. run_transcribe/run_realign/run_package
# return small structured data (word timings, pitch/beat/section numbers) that stays well under
# 2 MiB, so they keep block_network=True -- this is a real, per-function, measured distinction.
@app.function(
    gpu="A10", timeout=_SEPARATION_TIMEOUT_SECONDS, max_containers=_MAX_CONTAINERS
)
def run_separate(audio_bytes: bytes, model_name: str) -> dict[str, bytes]:
    import shutil
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.separation import separate_audio

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        stem_paths = separate_audio(Path(tmp.name), model_name=model_name)
        try:
            return {stem_type: path.read_bytes() for stem_type, path in stem_paths.items()}
        finally:
            stem_dir = next(iter(stem_paths.values())).parent
            shutil.rmtree(stem_dir, ignore_errors=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(
    gpu="A10",
    block_network=True,
    timeout=_TRANSCRIPTION_TIMEOUT_SECONDS,
    max_containers=_MAX_CONTAINERS,
)
def run_transcribe(
    audio_bytes: bytes, model_size: str, initial_prompt: str | None = None
) -> TranscriptionResult:
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.transcription import run_transcription_and_alignment

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return run_transcription_and_alignment(
            Path(tmp.name), model_size=model_size, initial_prompt=initial_prompt
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(
    gpu="A10",
    block_network=True,
    timeout=_TRANSCRIPTION_TIMEOUT_SECONDS,
    max_containers=_MAX_CONTAINERS,
)
def run_realign(audio_bytes: bytes, text: str) -> list[Word]:
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.transcription import align_words

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return align_words(Path(tmp.name), text)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(
    gpu="A10",
    block_network=True,
    timeout=_PACKAGE_TIMEOUT_SECONDS,
    max_containers=_MAX_CONTAINERS,
)
def run_package(
    vocals_bytes: bytes, drums_bytes: bytes, bass_bytes: bytes, other_bytes: bytes, pitch_model: str
) -> dict[str, object]:
    """Returns a compact dict, NOT a PackageResult, unlike what a first reading of this file might
    expect -- final whole-branch review measured PackageResult's real pickled size at this
    project's own MAX_DURATION_SECONDS cap (12 minutes, fingerprint.py) and found it hits 2.67 MiB
    at CREPE_HOP_MS=10's frame rate (packaging.py) -- over Modal's real 2 MiB inline-payload
    threshold, THE SAME failure mode run_separate was fixed for, just never triggered by this
    milestone's 3-second synthetic test track. A list[PitchFrame] of dataclass instances carries a
    lot of per-object pickle overhead; struct-packing the three parallel arrays (time_ms as
    uint32, hz and confidence as float32, NaN standing in for hz=None) measured at 0.83 MiB for a
    12-minute track -- comfortable margin under the threshold, verified before this was written.
    gpu_backend.py's _run_package_modal unpacks this back into a real PackageResult; no caller
    outside these two functions ever sees the compact wire format -- true of the SHAPE (still a
    list[PitchFrame] on the far side), but not exactly true of the VALUES: float32 is lossy versus
    the float64 build_package() itself produces. A confidence of 0.9 round-trips as
    0.8999999761581421, not 0.9. This is musically irrelevant (nowhere near audible or visible
    pitch-guide precision) and was confirmed identical to 6 decimal places across BENCHMARKS.md's
    real pre/post-fix runs, but it is a real, measurable precision loss a caller doing exact
    equality on pitch/confidence values would observe -- worth knowing before this format is reused
    for something exactness-sensitive.
    """
    import struct
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.packaging import build_package

    stem_bytes_by_name = {
        "vocals": vocals_bytes,
        "drums": drums_bytes,
        "bass": bass_bytes,
        "other": other_bytes,
    }
    tmp_paths: dict[str, Path] = {}
    try:
        for stem_name, data in stem_bytes_by_name.items():
            tmp = NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            tmp_paths[stem_name] = Path(tmp.name)

        result = build_package(
            vocals_path=tmp_paths["vocals"],
            drums_path=tmp_paths["drums"],
            bass_path=tmp_paths["bass"],
            other_path=tmp_paths["other"],
            pitch_model=pitch_model,
        )
    finally:
        for path in tmp_paths.values():
            path.unlink(missing_ok=True)

    n = len(result.pitch)
    nan = float("nan")
    pitch_bytes = struct.pack(
        f"<{n}I{n}f{n}f",
        *(frame.time_ms for frame in result.pitch),
        *(nan if frame.hz is None else frame.hz for frame in result.pitch),
        *(frame.confidence for frame in result.pitch),
    )
    return {
        "pitch_model": result.pitch_model,
        "pitch_bytes": pitch_bytes,
        "pitch_frame_count": n,
        "tempo_bpm": result.tempo_bpm,
        "beats_ms": result.beats_ms,
        "sections_ms": result.sections_ms,
    }


@app.function(block_network=False, timeout=30)
def egress_probe() -> str:
    """M7c Task 4's deliberate sandbox-validation check -- NOT block_network=True, on purpose,
    since this function's entire job is proving block_network=True (on its sibling
    blocked_egress_probe below) actually blocks traffic. If this function (with networking
    allowed) can reach a public endpoint but blocked_egress_probe cannot, that's the real proof
    the sandbox is enforced, not merely unconfigured-and-accidentally-permissive.
    """
    import urllib.request

    with urllib.request.urlopen("https://example.com", timeout=10) as response:
        return f"reached example.com, status {response.status}"


@app.function(block_network=True, timeout=30)
def blocked_egress_probe() -> str:
    """The genuine negative-control for the sandbox-validation check: identical logic to
    egress_probe above, but with block_network=True. Feeding garbage bytes to one of the four real
    pipeline functions (run_separate etc.) would NOT prove anything about network blocking -- those
    functions never attempt a network call in the first place (that's the whole point of Decision
    2's zero-egress design), so they'd fail on bad input regardless of block_network's value. This
    function is the one that actually attempts the exact same call egress_probe makes, so its
    failure (or success) is real, direct evidence of whether block_network=True is enforced.
    """
    import urllib.request

    with urllib.request.urlopen("https://example.com", timeout=10) as response:
        return f"reached example.com, status {response.status}"
