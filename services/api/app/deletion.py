from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FingerprintMatch, KaraokePackage, Stem, Track, Transcription
from app.storage import delete_track_file, get_minio_client


def delete_track_content(session: Session, track: Track) -> None:
    """Deletes every row and object-storage blob a track owns -- FingerprintMatch, Stem (+ each
    stem's MinIO object), Transcription, KaraokePackage, and the original upload's MinIO object.
    Does NOT delete the Track row itself or its RightsDeclaration -- callers decide that part,
    since retention purge (hard delete) and takedown (tombstone) want different endings.

    Retention-purged tracks never had a Stem/Transcription/KaraokePackage row in the first place
    (those pipeline stages only run after the rights gate passes) -- for them, these queries are
    cheap no-ops, not dead code. Reusing one function for both cases is simpler than maintaining
    two purpose-built deletion paths that would drift apart over time.
    """
    minio_client = get_minio_client()

    stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
    for stem in stems:
        delete_track_file(minio_client, stem.storage_key)
        session.delete(stem)

    for match in (
        session.execute(select(FingerprintMatch).where(FingerprintMatch.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(match)

    for transcription in (
        session.execute(select(Transcription).where(Transcription.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(transcription)

    for package in (
        session.execute(select(KaraokePackage).where(KaraokePackage.track_id == track.id))
        .scalars()
        .all()
    ):
        session.delete(package)

    delete_track_file(minio_client, track.storage_key)
