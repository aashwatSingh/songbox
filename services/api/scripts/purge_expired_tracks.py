"""Deletes tracks that never passed the rights gate (status stays pending_review or rejected)
older than RETENTION_WINDOW_DAYS. Run manually or via an external OS-level scheduled task -- this
project has no in-process scheduler/cron infrastructure, and building one just for this script
would be new infrastructure this milestone doesn't otherwise need (see the design spec's
Decision 2).

RETENTION_WINDOW_DAYS is a policy choice, not a measured or validated number -- easy to change,
not backed by a real compliance review.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.deletion import delete_track_content
from app.models import RightsDeclaration, Track

RETENTION_WINDOW_DAYS = 30


def purge_expired_tracks() -> int:
    """Returns the number of tracks purged."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_WINDOW_DAYS)
    session = SessionLocal()
    purged = 0
    try:
        stmt = (
            select(Track, RightsDeclaration)
            .join(RightsDeclaration, RightsDeclaration.id == Track.rights_declaration_id)
            .where(
                Track.status.in_(("pending_review", "rejected")),
                RightsDeclaration.created_at < cutoff,
            )
        )
        rows = session.execute(stmt).all()
        for track, declaration in rows:
            delete_track_content(session, track)
            session.delete(track)
            session.flush()  # Ensure track is deleted before we try to delete the declaration.
            session.delete(declaration)
            purged += 1
        session.commit()
        return purged
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    count = purge_expired_tracks()
    # Never logs attestation text, audio, or lyrics -- only a count.
    print(
        f"Purged {count} expired track(s) (status pending_review/rejected, "
        f"older than {RETENTION_WINDOW_DAYS} days)"
    )
    sys.exit(0)
