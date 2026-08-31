"""add users and sessions tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-31

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # citext gives case-insensitive email uniqueness ("Foo@x.com" and "foo@x.com" collide) without
    # application-layer lowercasing -- ships in postgres:16-alpine's bundled contrib extensions, no
    # extra image/dependency needed.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Deliberately NOT enabling row-level security here, unlike migration 0002's TABLES tuple.
    # users/sessions are the identity substrate RLS depends on (how a request's tenant is
    # discovered in the first place), not tenant content -- the same category of exception
    # app/db.py's get_admin_db() already documents for cross-tenant operations. A session lookup
    # in app/auth.py's get_identity() uses the unrestricted `songbox` role directly.
    #
    # Also deliberately NOT granting the restricted songbox_app role access to these tables --
    # only the unrestricted `songbox` role (used by app/db.py's SessionLocal, same as
    # get_admin_db()) can read/write them. app/auth.py's session lookup must use SessionLocal, not
    # AppSessionLocal, or every request would fail with a permissions error.


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_table("users")
    # citext extension intentionally left installed on downgrade -- DROP EXTENSION could affect
    # other objects, and an unused extension installed is harmless (same reasoning migration 0002
    # uses for leaving the songbox_app role in place on downgrade).
