"""Deletes tracks that never passed the rights gate (status stays pending_review or rejected)
older than RETENTION_WINDOW_DAYS. Run manually or via an external OS-level scheduled task -- this
project has no in-process scheduler/cron infrastructure, and building one just for this script
would be new infrastructure this milestone doesn't otherwise need (see the design spec's
Decision 2).

RETENTION_WINDOW_DAYS is a policy choice, not a measured or validated number -- easy to change,
not backed by a real compliance review.

Run as: cd services/api && python -m scripts.purge_expired_tracks (NOT python
scripts/purge_expired_tracks.py -- that invocation cannot import app.*, since app isn't installed
as a package in this environment and the plain-script invocation doesn't add the project root to
sys.path).

The entire sweep below runs in a single transaction -- a failure partway through rolls back all
prior Track/RightsDeclaration deletions in that run (while their already-removed MinIO objects
stay removed, since object storage deletes aren't transactional -- see app/deletion.py), and a
very large backlog means one long-held transaction. A known characteristic at current scale, not
something fixed here.
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

            # Supplementary declarations (e.g. confirm-attestation's "stronger attestation" row)
            # link back to the track via RightsDeclaration.track_id, not via
            # Track.rights_declaration_id -- the FK points the OPPOSITE direction from the
            # original declaration, so these must be deleted before the Track row itself, not
            # after.
            supplementary = (
                session.execute(
                    select(RightsDeclaration).where(RightsDeclaration.track_id == track.id)
                )
                .scalars()
                .all()
            )
            for supp in supplementary:
                session.delete(supp)
            session.flush()

            # Without relationship() configured between these models, SQLAlchemy's flush does
            # not otherwise order DELETEs by FK -- the track row must be gone before the
            # original declaration it points to can be deleted (Track -> RightsDeclaration, the
            # opposite FK direction from the supplementary declarations above). Two separate
            # flushes are required here, not one: queuing both deletes and flushing once lets the
            # unit of work pick its own order (observed: alphabetically by table name, deleting
            # rights_declarations before tracks), which violates the FK the same way skipping the
            # flush entirely would.
            session.delete(track)
            session.flush()
            session.delete(declaration)
            session.flush()

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
