from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acoustid.client import AcoustIDClient, HTTPAcoustIDClient
from app.auth import Identity, get_identity
from app.db import get_db
from app.fingerprint import FingerprintError, fingerprint_audio
from app.gate import resolve_lane_outcome, resolve_lyrics_display_allowed
from app.gpu_backend import BackendBusyError, BackendTimeoutError, run_inference
from app.models import FingerprintMatch, License, RightsDeclaration, Stem, Track, Transcription
from app.separation import SeparationError, separate_audio
from app.storage import fetch_track_file, get_minio_client, save_track_file
from app.transcription import (
    DEFAULT_WHISPER_MODEL_SIZE,
    AlignmentError,
    TranscriptionError,
    align_words,
    run_transcription_and_alignment,
)
from app.validation import detect_audio_format

MAX_UPLOAD_BYTES = 150 * 1024 * 1024
ALLOWED_SEPARATION_MODELS = ("htdemucs", "htdemucs_ft")

# M2 capped tracks at 12 minutes (fingerprint.py's MAX_DURATION_SECONDS) and gave ffprobe/ffmpeg
# 30s subprocess timeouts. Demucs separation is much slower than an ffprobe call, and this
# machine's real measured rate (see docs/BENCHMARKS.md, median of 3 isolated runs) is CPU-only --
# 2.97x realtime for htdemucs, 0.71x for htdemucs_ft, meaning a full 12-minute track could take on
# the order of 4-17 minutes of wall clock on CPU depending on model. 30 minutes leaves comfortable
# headroom over the slower model's worst case without being unbounded; on a real GPU (once the
# CUDA torch build lands) this would be a small fraction of that. Revisit downward once GPU
# inference is the norm for this endpoint.
SEPARATION_TIMEOUT_SECONDS = 1800

ALLOWED_WHISPER_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")

# Whisper + wav2vec2 forced alignment is slower per second of audio than Demucs separation was
# (see docs/BENCHMARKS.md's M4 section) but shares the same "one heavy model at a time on this
# box" reasoning as SEPARATION_TIMEOUT_SECONDS above. Using the same 1800s bound until real
# numbers say otherwise.
TRANSCRIPTION_TIMEOUT_SECONDS = 1800


def _read_upload_capped(upload: UploadFile, limit: int) -> bytes:
    """Read an upload into memory, refusing anything larger than `limit` bytes.

    Starlette has already spooled the whole request body before this handler runs (to disk
    past its 1 MiB spool threshold), so `upload.file` is seekable and its size is available
    in O(1) -- measure first, then read once. An earlier version accumulated 1 MiB chunks
    with a running total instead; that was measurably worse, not better, because the chunk
    list and the joined result are both live at the join, peaking at ~2x the payload versus
    1x for a single read. Measuring up front rejects oversized input without allocating for
    it at all.

    This caps what WE buffer and process, not raw ingress -- the body already reached disk
    before we got here. A true ingress cap belongs at a reverse proxy or in ASGI middleware.
    """
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    if size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {limit // (1024 * 1024)} MiB upload limit",
        )
    return upload.file.read()


router = APIRouter()


def get_acoustid_client() -> AcoustIDClient:
    return HTTPAcoustIDClient()


class UploadResponse(BaseModel):
    track_id: uuid.UUID
    status: str
    reason: str


class TrackSummary(BaseModel):
    track_id: uuid.UUID
    status: str
    duration_seconds: float | None
    has_transcription: bool


@router.get("/tracks", response_model=list[TrackSummary])
def list_tracks(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[TrackSummary]:
    tracks = db.execute(
        select(Track).where(Track.tenant_id == identity.tenant_id)
    ).scalars().all()

    transcribed_track_ids = set(
        db.execute(
            select(Transcription.track_id)
            .where(Transcription.tenant_id == identity.tenant_id)
            .distinct()
        ).scalars().all()
    )

    return [
        TrackSummary(
            track_id=track.id,
            status=track.status,
            duration_seconds=(
                float(track.duration_seconds) if track.duration_seconds is not None else None
            ),
            has_transcription=track.id in transcribed_track_ids,
        )
        for track in tracks
    ]


@router.post("/tracks/upload", response_model=UploadResponse)
def upload_track(
    request: Request,
    file: UploadFile = File(...),
    lane: str = Form(...),
    attestation_text: str = Form(...),
    license_id: uuid.UUID | None = Form(default=None),
    pd_cc_source: str | None = Form(default=None),
    pd_cc_license: str | None = Form(default=None),
    attribution_string: str | None = Form(default=None),
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
    acoustid_client: AcoustIDClient = Depends(get_acoustid_client),
) -> UploadResponse:
    if lane not in ("A", "B", "C"):
        raise HTTPException(status_code=422, detail="lane must be one of A, B, C")

    if lane == "B" and license_id is None:
        raise HTTPException(status_code=422, detail="lane B requires license_id")

    data = _read_upload_capped(file, MAX_UPLOAD_BYTES)
    audio_format = detect_audio_format(data)
    if audio_format is None:
        raise HTTPException(status_code=422, detail="file does not match any accepted audio format")
    # delete=False + explicit close before fingerprinting: on Windows, NamedTemporaryFile
    # opens the file exclusively, so a subprocess (ffprobe/ffmpeg) can't open it while our
    # handle is still open. Close it first, then clean up manually in the finally block.
    # The suffix comes from the DETECTED format, never from file.filename -- a client-supplied
    # name can be over-long or contain characters the OS rejects, which raised an unhandled
    # OSError/FileNotFoundError here (500) before the try block's cleanup could apply. It also
    # kept the client in control of the extension hint ffmpeg uses to pick a demuxer.
    tmp = tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        try:
            fp = fingerprint_audio(Path(tmp.name))
        except FingerprintError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not fingerprint audio: {exc}"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    license_covers_recording: bool | None = None
    if lane == "B":
        license_row = db.get(License, license_id)
        if license_row is None or license_row.tenant_id != identity.tenant_id:
            raise HTTPException(status_code=422, detail="license_id not found for this tenant")
        license_covers_recording = license_row.covers_recording

    acoustid_result = acoustid_client.lookup(fp.value, fp.duration_seconds)
    decision = resolve_lane_outcome(lane, acoustid_result, license_covers_recording)

    minio_client = get_minio_client()
    storage_key = save_track_file(minio_client, identity.tenant_id, data)

    declaration = RightsDeclaration(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        lane=lane,
        attestation_text=attestation_text,
        ip_address=request.client.host if request.client else "unknown",
        created_at=datetime.now(UTC),
        license_id=license_id,
        pd_cc_source=pd_cc_source,
        pd_cc_license=pd_cc_license,
        attribution_string=attribution_string,
    )
    db.add(declaration)
    # Flush so the FK-dependent inserts below (track -> declaration, match -> track) are
    # guaranteed to run after their parent row exists -- without relationship() configured
    # between these models, SQLAlchemy's flush does not otherwise order INSERTs by FK.
    db.flush()

    track = Track(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        duration_seconds=fp.duration_seconds,
        fingerprint=fp.value,
        rights_declaration_id=declaration.id,
        status=decision.outcome.value,
        storage_key=storage_key,
    )
    db.add(track)
    db.flush()

    match_row = FingerprintMatch(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        acoustid_response={
            "error": acoustid_result.error,
            "matches": [
                {
                    "release_title": m.release_title,
                    "recording_id": m.recording_id,
                    "score": m.score,
                }
                for m in acoustid_result.matches
            ],
        },
        matched_release=(
            acoustid_result.matches[0].release_title if acoustid_result.matches else None
        ),
        resolution=decision.resolution.value,
    )
    db.add(match_row)

    return UploadResponse(track_id=track.id, status=track.status, reason=decision.reason)


class ConfirmAttestationRequest(BaseModel):
    release_name: str


class ConfirmAttestationResponse(BaseModel):
    track_id: uuid.UUID
    status: str


@router.post("/tracks/{track_id}/confirm-attestation", response_model=ConfirmAttestationResponse)
def confirm_attestation(
    track_id: uuid.UUID,
    body: ConfirmAttestationRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> ConfirmAttestationResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "pending_review":
        raise HTTPException(
            status_code=409, detail=f"track is not pending review (status={track.status})"
        )

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    if declaration is None or declaration.lane != "A":
        raise HTTPException(status_code=409, detail="confirm-attestation is only valid for lane A")

    # rights_declarations rows are immutable -- record the stronger, named-release attestation
    # as an ADDITIONAL row, never mutate the original in place and never repoint
    # track.rights_declaration_id at this new row. Both matter for the same reason: this is
    # evidence for a future human reviewer, not a self-service unlock. M1 has no reviewer/admin
    # role separation yet (the dev auth stub can't tell an uploader from a reviewer, or even
    # confirm the caller IS the original uploader), and the uploader can already read the exact
    # matched_release string off GET /review-queue and echo it straight back -- so no amount of
    # string-matching the submitted release_name could ever be a real security boundary, and
    # repointing the FK would let any same-tenant caller silently replace the attestation,
    # uploader identity, and timestamp the reviewer actually sees. track.status is therefore
    # left untouched here too; only a human calling POST /review-queue/{id}/resolve can clear
    # a hold, and that endpoint still reads the ORIGINAL declaration via the untouched FK.
    stronger = RightsDeclaration(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        lane="A",
        attestation_text=(
            f"Stronger attestation (Lane A hold): I confirm I hold the rights to the "
            f"specific release '{body.release_name}', which the fingerprint check matched."
        ),
        ip_address=request.client.host if request.client else "unknown",
        created_at=datetime.now(UTC),
        release_name=body.release_name,
    )
    db.add(stronger)

    return ConfirmAttestationResponse(track_id=track.id, status=track.status)


class StemInfo(BaseModel):
    stem_type: str
    storage_key: str


class SeparateRequest(BaseModel):
    model_name: str = "htdemucs"


class SeparateResponse(BaseModel):
    track_id: uuid.UUID
    stems: list[StemInfo]


@router.post("/tracks/{track_id}/separate", response_model=SeparateResponse)
def separate_track(
    track_id: uuid.UUID,
    body: SeparateRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> SeparateResponse:
    model_name = body.model_name if body is not None else "htdemucs"
    if model_name not in ALLOWED_SEPARATION_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"model_name must be one of {ALLOWED_SEPARATION_MODELS}",
        )

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    minio_client = get_minio_client()
    original_bytes = fetch_track_file(minio_client, track.storage_key)

    # Re-detect format from the stored bytes rather than trusting anything client-supplied --
    # same reasoning as upload_track: the suffix ffmpeg/Demucs use to pick a demuxer must come
    # from the actual bytes, never from an attacker-controlled name.
    audio_format = detect_audio_format(original_bytes)
    if audio_format is None:
        raise HTTPException(
            status_code=422, detail="stored file no longer matches any accepted audio format"
        )

    tmp = tempfile.NamedTemporaryFile(suffix=f".{audio_format}", delete=False)
    try:
        tmp.write(original_bytes)
        tmp.flush()
        tmp.close()

        try:
            stem_paths = run_inference(
                lambda: separate_audio(Path(tmp.name), model_name=model_name),
                timeout_seconds=SEPARATION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    stems: list[StemInfo] = []
    try:
        for stem_type, stem_path in stem_paths.items():
            stem_bytes = stem_path.read_bytes()
            storage_key = save_track_file(minio_client, identity.tenant_id, stem_bytes)
            db.add(
                Stem(
                    id=uuid.uuid4(),
                    tenant_id=identity.tenant_id,
                    track_id=track.id,
                    stem_type=stem_type,
                    storage_key=storage_key,
                    model_name=model_name,
                )
            )
            stems.append(StemInfo(stem_type=stem_type, storage_key=storage_key))
    finally:
        # Clean up the temp directory created by separate_audio() -- all stem files share
        # the same parent directory from tempfile.mkdtemp(). Get the parent of any stem path.
        stem_dir = next(iter(stem_paths.values())).parent
        shutil.rmtree(stem_dir, ignore_errors=True)

    return SeparateResponse(track_id=track.id, stems=stems)


class WordInfo(BaseModel):
    idx: int
    start_ms: int
    end_ms: int
    confidence: float
    text: str | None


class TranscribeRequest(BaseModel):
    model_size: str = DEFAULT_WHISPER_MODEL_SIZE


class TranscribeResponse(BaseModel):
    track_id: uuid.UUID
    language: str
    aligner: str
    lyrics_display_allowed: bool
    words: list[WordInfo]


def _transcription_to_response(transcription: Transcription) -> TranscribeResponse:
    # transcription.words is JSONB (Mapped[list[dict[str, object]]]) -- these casts are pure
    # type-narrowing for mypy, not runtime conversions: the dict shape is fixed at write time
    # in transcribe_track() below (idx/start_ms/end_ms int, confidence float, text str) and
    # round-trips through Postgres JSONB without type coercion.
    words = [
        WordInfo(
            idx=cast(int, w["idx"]),
            start_ms=cast(int, w["start_ms"]),
            end_ms=cast(int, w["end_ms"]),
            confidence=cast(float, w["confidence"]),
            text=cast(str, w["text"]) if transcription.lyrics_display_allowed else None,
        )
        for w in transcription.words
    ]
    return TranscribeResponse(
        track_id=transcription.track_id,
        language=transcription.language,
        aligner=transcription.aligner,
        lyrics_display_allowed=transcription.lyrics_display_allowed,
        words=words,
    )


@router.post("/tracks/{track_id}/transcribe", response_model=TranscribeResponse)
def transcribe_track(
    track_id: uuid.UUID,
    body: TranscribeRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    model_size = body.model_size if body is not None else DEFAULT_WHISPER_MODEL_SIZE
    if model_size not in ALLOWED_WHISPER_MODEL_SIZES:
        raise HTTPException(
            status_code=422,
            detail=f"model_size must be one of {ALLOWED_WHISPER_MODEL_SIZES}",
        )

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    # .limit(1) rather than a bare scalar_one_or_none(): /separate has no idempotency guard (a
    # known, deliberately-deferred M3 limitation -- no unique constraint stops it from being
    # called twice on the same track, which would write two full sets of 4 Stem rows), so more
    # than one "vocals" row can legitimately exist here. scalar_one_or_none() raises
    # MultipleResultsFound (an unhandled 500) in that case; Stem has no created_at column (also
    # out of scope to add here) to pick "the most recent" set by, so which of the resulting stem
    # sets gets used below is arbitrary -- a real fix belongs with /separate's idempotency gap,
    # not here. Same pattern as get_transcription's order_by(...).limit(1) further down.
    vocals_stem = db.execute(
        select(Stem).where(Stem.track_id == track.id, Stem.stem_type == "vocals").limit(1)
    ).scalar_one_or_none()
    if vocals_stem is None:
        raise HTTPException(
            status_code=409, detail="track has no vocals stem -- run /separate first"
        )

    minio_client = get_minio_client()
    vocal_bytes = fetch_track_file(minio_client, vocals_stem.storage_key)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(vocal_bytes)
        tmp.flush()
        tmp.close()
        try:
            result = run_inference(
                lambda: run_transcription_and_alignment(Path(tmp.name), model_size=model_size),
                timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except TranscriptionError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not transcribe audio: {exc}"
            ) from exc
        except AlignmentError as exc:
            # Deliberately NOT interpolating `exc` into the response -- CLAUDE.md forbids
            # logging raw lyrics, and an alignment failure can legitimately occur mid-transcript,
            # so its message must never be trusted to be content-free by construction the way
            # TranscriptionError's failures (which occur before any text exists) are.
            raise HTTPException(
                status_code=422, detail="could not align transcript to audio"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    if declaration is None:
        # Internal-consistency check, not a client-facing failure: a track that passed the rights
        # gate always has a rights_declaration_id pointing at a real row (upload_track() creates
        # both in the same transaction). An assert here would be stripped under python -O and,
        # if this were ever false, `declaration.lane` below would instead raise an unhandled
        # AttributeError with no useful message.
        raise RuntimeError(f"track {track.id} has no rights declaration -- data integrity error")
    license_covers_lyrics: bool | None = None
    if declaration.license_id is not None:
        license_row = db.get(License, declaration.license_id)
        license_covers_lyrics = license_row.covers_lyrics if license_row else None
    lyrics_display_allowed = resolve_lyrics_display_allowed(declaration.lane, license_covers_lyrics)

    words_json = [
        {
            "idx": w.idx,
            "text": w.text,
            "start_ms": w.start_ms,
            "end_ms": w.end_ms,
            "confidence": w.confidence,
        }
        for w in result.words
    ]
    transcription = Transcription(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        whisper_model=model_size,
        aligner=result.aligner,
        language=result.language,
        lyrics_display_allowed=lyrics_display_allowed,
        words=words_json,
        created_at=datetime.now(UTC),
    )
    db.add(transcription)
    db.flush()

    return _transcription_to_response(transcription)


@router.get("/tracks/{track_id}/transcription", response_model=TranscribeResponse)
def get_transcription(
    track_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")

    transcription = db.execute(
        select(Transcription)
        .where(Transcription.track_id == track.id)
        .order_by(Transcription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if transcription is None:
        raise HTTPException(status_code=404, detail="no transcription found for this track")

    return _transcription_to_response(transcription)


class RealignRequest(BaseModel):
    # Every other client-controlled input in this file is bounded (model_size/model_name by a
    # whitelist, uploads by MAX_UPLOAD_BYTES) -- this text was the one unbounded exception.
    # 5000 chars is a generous cap for song lyrics; align_words() would reject an empty/
    # whitespace string anyway (AlignmentError("cannot align empty text")), but that check runs
    # only after a MinIO fetch, temp file, lock acquisition, and model load, so it's re-checked
    # explicitly and early in realign_track() below.
    text: str = Field(min_length=1, max_length=5000)


@router.post("/tracks/{track_id}/realign", response_model=TranscribeResponse)
def realign_track(
    track_id: uuid.UUID,
    body: RealignRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    latest = db.execute(
        select(Transcription)
        .where(Transcription.track_id == track.id)
        .order_by(Transcription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(
            status_code=409, detail="no transcription found -- run /transcribe first"
        )
    # Deliberate, not a bug: this gate reads `latest.lyrics_display_allowed`, the PRIOR
    # transcription row's STORED value, while the value written to the NEW row further down is
    # freshly recomputed via resolve_lyrics_display_allowed() -- see that call site below for why
    # this asymmetry is safe by design rather than an oversight.
    if not latest.lyrics_display_allowed:
        raise HTTPException(
            status_code=409, detail="lyric correction is not available for this track"
        )
    if latest.language != "en":
        raise HTTPException(
            status_code=409,
            detail="correction/re-alignment is only available for English tracks",
        )

    vocals_stem = db.execute(
        select(Stem).where(Stem.track_id == track.id, Stem.stem_type == "vocals").limit(1)
    ).scalar_one_or_none()
    if vocals_stem is None:
        raise HTTPException(
            status_code=409, detail="track has no vocals stem -- run /separate first"
        )

    # Whitespace-only text passes Field(min_length=1) (a single space has length 1) but
    # align_words() would reject it anyway with AlignmentError("cannot align empty text") --
    # only after a MinIO fetch, temp file write, lock acquisition, and model load. Reject it here
    # instead, before any of that cost is spent.
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")

    minio_client = get_minio_client()
    vocal_bytes = fetch_track_file(minio_client, vocals_stem.storage_key)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(vocal_bytes)
        tmp.flush()
        tmp.close()
        try:
            words = run_inference(
                lambda: align_words(Path(tmp.name), body.text),
                timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except AlignmentError as exc:
            # Deliberately NOT interpolating `exc` -- same reasoning as /transcribe's mapping.
            raise HTTPException(
                status_code=422, detail="could not align transcript to audio"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    if declaration is None:
        raise RuntimeError(f"track {track.id} has no rights declaration -- data integrity error")
    license_covers_lyrics: bool | None = None
    if declaration.license_id is not None:
        license_row = db.get(License, declaration.license_id)
        license_covers_lyrics = license_row.covers_lyrics if license_row else None
    lyrics_display_allowed = resolve_lyrics_display_allowed(declaration.lane, license_covers_lyrics)

    words_json = [
        {
            "idx": w.idx,
            "text": w.text,
            "start_ms": w.start_ms,
            "end_ms": w.end_ms,
            "confidence": w.confidence,
        }
        for w in words
    ]
    transcription = Transcription(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        whisper_model="user-corrected",
        aligner="wav2vec2",
        language="en",
        lyrics_display_allowed=lyrics_display_allowed,
        words=words_json,
        created_at=datetime.now(UTC),
    )
    db.add(transcription)
    db.flush()

    return _transcription_to_response(transcription)
