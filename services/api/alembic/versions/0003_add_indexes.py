"""add indexes for tenant_id, status, fingerprint, and FK lookups

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20

"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = ("licenses", "rights_declarations", "tracks", "fingerprint_matches")


def upgrade() -> None:
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    op.create_index("ix_fingerprint_matches_track_id", "fingerprint_matches", ["track_id"])
    op.create_index("ix_tracks_status", "tracks", ["status"])
    op.create_index("ix_tracks_fingerprint", "tracks", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_tracks_fingerprint", table_name="tracks")
    op.drop_index("ix_tracks_status", table_name="tracks")
    op.drop_index("ix_fingerprint_matches_track_id", table_name="fingerprint_matches")
    for table in TABLES:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
