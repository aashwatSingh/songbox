"""add track_id to rights_declarations for supplementary attestations

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-29

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rights_declarations",
        sa.Column(
            "track_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tracks.id"), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("rights_declarations", "track_id")
