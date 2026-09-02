from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Identity, get_identity
from app.db import get_db
from app.gate import FingerprintResolution
from app.models import FingerprintMatch, RightsDeclaration, Track

router = APIRouter()


class ReviewQueueItem(BaseModel):
    track_id: uuid.UUID
    status: str
    match_id: uuid.UUID
    resolution: str
    matched_release: str | None
    lane: str
    attestation_text: str
    user_id: uuid.UUID
    uploaded_at: datetime
    title: str | None
    artist: str | None


class ResolveReviewRequest(BaseModel):
    approve: bool


class ResolveReviewResponse(BaseModel):
    track_id: uuid.UUID
    status: str


@router.get("/review-queue", response_model=list[ReviewQueueItem])
def list_review_queue(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[ReviewQueueItem]:
    stmt = (
        select(Track, FingerprintMatch, RightsDeclaration)
        .join(FingerprintMatch, FingerprintMatch.track_id == Track.id)
        .join(RightsDeclaration, RightsDeclaration.id == Track.rights_declaration_id)
        .where(Track.status == "pending_review")
    )
    rows = db.execute(stmt).all()
    return [
        ReviewQueueItem(
            track_id=track.id,
            status=track.status,
            match_id=match.id,
            resolution=match.resolution,
            matched_release=match.matched_release,
            lane=declaration.lane,
            attestation_text=declaration.attestation_text,
            user_id=declaration.user_id,
            uploaded_at=declaration.created_at,
            title=track.title,
            artist=track.artist,
        )
        for track, match, declaration in rows
    ]


@router.post("/review-queue/{track_id}/resolve", response_model=ResolveReviewResponse)
def resolve_review(
    track_id: uuid.UUID,
    body: ResolveReviewRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> ResolveReviewResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "pending_review":
        raise HTTPException(
            status_code=409, detail=f"track is not pending review (status={track.status})"
        )

    match_stmt = (
        select(FingerprintMatch)
        .where(FingerprintMatch.track_id == track.id)
        .order_by(FingerprintMatch.id.desc())
    )
    match_row = db.execute(match_stmt).scalars().first()
    if match_row is not None:
        match_row.resolution = (
            FingerprintResolution.CONFIRMED if body.approve else FingerprintResolution.MISMATCH
        ).value
        match_row.reviewer_id = identity.user_id

    # "rejected" is a human-review outcome, not something the automated gate ever produces
    # itself (GateOutcome only has PASSED/HELD) -- it only exists from this endpoint.
    track.status = "passed" if body.approve else "rejected"

    # Commit before responding rather than leaving it to get_db's teardown, which FastAPI runs
    # AFTER the response is sent. The review console reloads the queue the moment this returns,
    # and without this that reload opens a new transaction that still sees the track as held --
    # so an approval that genuinely succeeded looks like it did nothing. Build the response body
    # first: commit expires the ORM attributes it reads.
    response = ResolveReviewResponse(track_id=track.id, status=track.status)
    db.commit()
    return response
