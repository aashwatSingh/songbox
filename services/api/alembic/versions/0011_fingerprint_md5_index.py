"""replace the full-value fingerprint btree index with an md5 expression index

Revision ID: 0011
Revises: 0010_bookmarked
Create Date: 2026-09-02

`ix_tracks_fingerprint` (added in 0003) indexed the ENTIRE chromaprint fingerprint text. A
chromaprint fingerprint grows with audio duration, so this worked only for very short clips and
made every real-length song impossible to upload:

    psycopg.errors.ProgramLimitExceeded: index row size 6416 exceeds btree version 4
    maximum 2704 for index "ix_tracks_fingerprint"

Postgres caps a btree index entry at roughly 1/3 of an 8KB page (2704 bytes). A ~4 minute track
fingerprints to ~6.2k characters, well past that, so INSERT INTO tracks failed outright. The short
synthetic clips used in tests and demos fingerprint to only a few hundred characters, which is
exactly why every test passed while every genuine upload 500'd.

The fix is the one Postgres's own error HINT recommends: index a fixed-width md5 of the value
instead. That keeps indexed exact-match lookups possible at 16 bytes per row regardless of track
length.

NOTE for anyone adding a fingerprint lookup later: this index only serves queries written against
the same expression, e.g.

    WHERE md5(fingerprint) = md5(:value)

A plain `WHERE fingerprint = :value` will NOT use it. No query in the codebase filters on
fingerprint today -- the original index was speculative -- so this preserves the intent rather
than silently dropping the capability.
"""

from alembic import op

revision = "0011"
down_revision = "0010_bookmarked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_tracks_fingerprint", table_name="tracks")
    op.execute("CREATE INDEX ix_tracks_fingerprint_md5 ON tracks (md5(fingerprint))")


def downgrade() -> None:
    op.execute("DROP INDEX ix_tracks_fingerprint_md5")
    # Recreating the original index can fail if any stored fingerprint is already too large to
    # index -- which is the whole reason this migration exists. That is correct behavior for a
    # downgrade: it refuses rather than silently losing rows.
    op.create_index("ix_tracks_fingerprint", "tracks", ["fingerprint"])
