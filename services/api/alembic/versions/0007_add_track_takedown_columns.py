"""add track takedown columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("takedown_reason", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("takedown_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "takedown_at")
    op.drop_column("tracks", "takedown_reason")
