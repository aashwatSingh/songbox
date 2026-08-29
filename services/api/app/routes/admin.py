from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_admin_key
from app.db import get_admin_db
from app.deletion import delete_track_content
from app.models import Track
from app.rate_limit import limiter


@limiter.limit("10/minute")
def _check_takedown_rate_limit(request: Request, response: Response) -> None:
    # With app/rate_limit.py's key_style="endpoint", this counter is scoped to this dependency
    # FUNCTION itself, not to whichever route pulls it in as a dependency. That's deliberate here
    # (it's what makes the takedown limit apply per-IP regardless of track_id -- see rate_limit.py's
    # comment), but it also means if a second admin route is ever added reusing this exact
    # dependency, it would share this one 10/minute bucket with the takedown route rather than
    # getting its own independent limit. A conservative default, not a bug -- give a future
    # sibling route its own `@limiter.limit(...)`-decorated dependency function if it needs an
    # independent bucket.
    return None


router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(_check_takedown_rate_limit), Depends(require_admin_key)],
)


class TakedownRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class TakedownResponse(BaseModel):
    track_id: uuid.UUID
    status: str
    takedown_reason: str
    takedown_at: datetime


@router.post(
    "/tracks/{track_id}/takedown",
    response_model=TakedownResponse,
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
