from __future__ import annotations

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
from app.models import FingerprintMatch, License, RightsDeclaration, Track
from app.storage import get_minio_client, save_track_file

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

    data = file.file.read()
    # delete=False + explicit close before fingerprinting: on Windows, NamedTemporaryFile
    # opens the file exclusively, so a subprocess (ffprobe/ffmpeg) can't open it while our
    # handle is still open. Close it first, then clean up manually in the finally block.
    tmp = tempfile.NamedTemporaryFile(suffix=Path(file.filename or "upload").suffix, delete=False)
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
        if license_id is None:
            raise HTTPException(status_code=422, detail="lane B requires license_id")
        license_row = db.get(License, license_id)
        if license_row is None or license_row.tenant_id != identity.tenant_id:
            raise HTTPException(status_code=422, detail="license_id not found for this tenant")
        license_covers_recording = license_row.covers_recording

    acoustid_result = acoustid_client.lookup(fp.value, fp.duration_seconds)
    decision = resolve_lane_outcome(lane, acoustid_result, license_covers_recording)

    minio_client = get_minio_client()
    storage_key = save_track_file(minio_client, identity.tenant_id, file.filename or "upload", data)

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
        acoustid_response={"matched": acoustid_result.matched, "error": acoustid_result.error},
        matched_release=(
            acoustid_result.matches[0].release_title if acoustid_result.matches else None
        ),
        resolution=decision.resolution.value,
    )
    db.add(match_row)

    return UploadResponse(track_id=track.id, status=track.status, reason=decision.reason)
