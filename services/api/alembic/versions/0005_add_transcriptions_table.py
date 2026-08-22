"""add transcriptions table

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "transcriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("whisper_model", sa.String(length=20), nullable=False),
        sa.Column("aligner", sa.String(length=20), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("lyrics_display_allowed", sa.Boolean(), nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transcriptions_tenant_id", "transcriptions", ["tenant_id"])
    op.create_index("ix_transcriptions_track_id", "transcriptions", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON transcriptions TO {APP_ROLE}")
    op.execute("ALTER TABLE transcriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transcriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON transcriptions
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON transcriptions")
    op.execute("ALTER TABLE transcriptions DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON transcriptions FROM {APP_ROLE}")
    op.drop_index("ix_transcriptions_track_id", table_name="transcriptions")
    op.drop_index("ix_transcriptions_tenant_id", table_name="transcriptions")
    op.drop_table("transcriptions")
