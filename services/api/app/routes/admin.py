from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_admin_key
from app.db import get_admin_db
from app.deletion import delete_track_content
from app.models import Track

router = APIRouter()


class TakedownRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class TakedownResponse(BaseModel):
    track_id: uuid.UUID
    status: str
    takedown_reason: str
    takedown_at: datetime


@router.post(
    "/admin/tracks/{track_id}/takedown",
    response_model=TakedownResponse,
    dependencies=[Depends(require_admin_key)],
)
def takedown_track(
    track_id: uuid.UUID,
    body: TakedownRequest,
    db: Session = Depends(get_admin_db),
) -> TakedownResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="track not found")

    delete_track_content(db, track)
    track.status = "taken_down"
    track.takedown_reason = body.reason
    track.takedown_at = datetime.now(UTC)
    db.flush()

    return TakedownResponse(
        track_id=track.id,
        status=track.status,
        takedown_reason=track.takedown_reason,
        takedown_at=track.takedown_at,
    )
