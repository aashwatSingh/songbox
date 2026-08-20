"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-08-19

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "licenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("covers_recording", sa.Boolean(), nullable=False),
        sa.Column("covers_lyrics", sa.Boolean(), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "rights_declarations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lane", sa.String(length=1), nullable=False),
        sa.Column("attestation_text", sa.Text(), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_name", sa.Text(), nullable=True),
        sa.Column(
            "license_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("licenses.id"),
            nullable=True,
        ),
        sa.Column("pd_cc_source", sa.Text(), nullable=True),
        sa.Column("pd_cc_license", sa.Text(), nullable=True),
        sa.Column("attribution_string", sa.Text(), nullable=True),
    )

    op.create_table(
        "tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "rights_declaration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rights_declarations.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
    )

    op.create_table(
        "fingerprint_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("acoustid_response", postgresql.JSONB(), nullable=False),
        sa.Column("matched_release", sa.Text(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("fingerprint_matches")
    op.drop_table("tracks")
    op.drop_table("rights_declarations")
    op.drop_table("licenses")
