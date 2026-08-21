from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.acoustid.client import AcoustIDClient, HTTPAcoustIDClient
from app.auth import Identity, get_identity
from app.db import get_db
from app.fingerprint import FingerprintError, fingerprint_audio
from app.gate import resolve_lane_outcome
from app.models import FingerprintMatch, License, RightsDeclaration, Stem, Track
from app.separation import SeparationError, separate_audio
from app.storage import fetch_track_file, get_minio_client, save_track_file
from app.validation import detect_audio_format

MAX_UPLOAD_BYTES = 150 * 1024 * 1024
ALLOWED_SEPARATION_MODELS = ("htdemucs", "htdemucs_ft")


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
            stem_paths = separate_audio(Path(tmp.name), model_name=model_name)
        except SeparationError as exc:
            raise HTTPException(
                status_code=422, detail=f"could not separate audio: {exc}"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    stems: list[StemInfo] = []
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

    return SeparateResponse(track_id=track.id, stems=stems)
