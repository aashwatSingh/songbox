"""Modal Function definitions for the `modal` GPU backend (M7c). Deployed via:

    modal deploy services/api/app/modal_app.py

after `modal setup` (or setting MODAL_TOKEN_ID/MODAL_TOKEN_SECRET) has configured real
credentials -- this file cannot be deployed or tested without them. Every function below is
decorated with block_network=True: none of them need network access, since the caller (this
project's FastAPI backend) already fetches audio bytes from MinIO before calling any of these, and
each function returns its result directly through Modal's own call/response marshaling rather than
writing anywhere reachable over a network. This is a stronger guarantee than the original spec's
"no egress except object storage and the queue" wording assumed was necessary (see the M7c design
spec's Decision 2) -- zero egress, not restricted egress.

GPU: "A10" (Modal's real name -- not AWS's "A10G"), $0.000306/second per modal.com/pricing as of
this file's authoring. Sized for this pipeline's model sizes (Demucs, faster-whisper, wav2vec2,
torchcrepe) -- none of which need a top-tier H100/B200-class card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import modal

if TYPE_CHECKING:
    from app.packaging import PackageResult
    from app.transcription import TranscriptionResult, Word

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
    )
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


@app.function(gpu="A10", block_network=True, timeout=_SEPARATION_TIMEOUT_SECONDS)
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


@app.function(gpu="A10", block_network=True, timeout=_TRANSCRIPTION_TIMEOUT_SECONDS)
def run_transcribe(audio_bytes: bytes, model_size: str) -> TranscriptionResult:
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    from app.transcription import run_transcription_and_alignment

    tmp = NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        return run_transcription_and_alignment(Path(tmp.name), model_size=model_size)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.function(gpu="A10", block_network=True, timeout=_TRANSCRIPTION_TIMEOUT_SECONDS)
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


@app.function(gpu="A10", block_network=True, timeout=_PACKAGE_TIMEOUT_SECONDS)
def run_package(
    vocals_bytes: bytes, drums_bytes: bytes, bass_bytes: bytes, other_bytes: bytes, pitch_model: str
) -> PackageResult:
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

        return build_package(
            vocals_path=tmp_paths["vocals"],
            drums_path=tmp_paths["drums"],
            bass_path=tmp_paths["bass"],
            other_path=tmp_paths["other"],
            pitch_model=pitch_model,
        )
    finally:
        for path in tmp_paths.values():
            path.unlink(missing_ok=True)


@app.function(block_network=False, timeout=30)
def egress_probe() -> str:
    """M7c Task 4's deliberate sandbox-validation check -- NOT block_network=True, on purpose,
    since this function's entire job is proving the OTHER functions' block_network=True actually
    blocks traffic. If this function (with networking allowed) can reach a public endpoint but the
    four block_network=True functions above cannot, that's the real proof the sandbox is enforced,
    not merely unconfigured-and-accidentally-permissive. Never deployed with block_network=True --
    that would defeat its purpose.
    """
    import urllib.request

    with urllib.request.urlopen("https://example.com", timeout=10) as response:
        return f"reached example.com, status {response.status}"
