"""add karaoke_packages table

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-23

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "karaoke_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("pitch_model", sa.String(length=20), nullable=False),
        sa.Column("pitch", postgresql.JSONB(), nullable=False),
        sa.Column("tempo_bpm", sa.Float(), nullable=False),
        sa.Column("beats_ms", postgresql.JSONB(), nullable=False),
        sa.Column("sections_ms", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_karaoke_packages_tenant_id", "karaoke_packages", ["tenant_id"])
    op.create_index("ix_karaoke_packages_track_id", "karaoke_packages", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON karaoke_packages TO {APP_ROLE}")
    op.execute("ALTER TABLE karaoke_packages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE karaoke_packages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON karaoke_packages
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON karaoke_packages")
    op.execute("ALTER TABLE karaoke_packages DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON karaoke_packages FROM {APP_ROLE}")
    op.drop_index("ix_karaoke_packages_track_id", table_name="karaoke_packages")
    op.drop_index("ix_karaoke_packages_tenant_id", table_name="karaoke_packages")
    op.drop_table("karaoke_packages")
