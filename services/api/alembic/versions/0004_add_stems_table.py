"""add stems table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "songbox_app"


def upgrade() -> None:
    op.create_table(
        "stems",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "track_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracks.id"),
            nullable=False,
        ),
        sa.Column("stem_type", sa.String(length=10), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(length=20), nullable=False),
    )
    op.create_index("ix_stems_tenant_id", "stems", ["tenant_id"])
    op.create_index("ix_stems_track_id", "stems", ["track_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON stems TO {APP_ROLE}")
    op.execute("ALTER TABLE stems ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stems FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON stems
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON stems")
    op.execute("ALTER TABLE stems DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON stems FROM {APP_ROLE}")
    op.drop_index("ix_stems_track_id", table_name="stems")
    op.drop_index("ix_stems_tenant_id", table_name="stems")
    op.drop_table("stems")
