"""add bookmarked column to tracks

Revision ID: 0010_bookmarked
Revises: 0009
Create Date: 2026-09-01

Named "0010_bookmarked", not bare "0010" -- an unmerged sibling worktree
(m9-security-hardening) independently claimed revision id "0010" for its own
migration (0010_add_login_lockout_columns.py, also down_revision=0009) and
had already applied it to the shared dev Postgres instance this project's
worktrees all use by convention. A bare "0010" here would collide: alembic
identifies revisions by this string, not by file identity, so `alembic
upgrade head` against that shared database would see current_version ==
"0010" == target head and silently skip running this migration's upgrade()
entirely -- which is exactly what happened once, caught only because the
test suite failed with UndefinedColumn afterward. This file's own
directory listing still sorts right after 0009 (filename unchanged), only
the internal revision id was renamed to dodge the collision.

Whoever eventually merges m9-security-hardening will need to renumber ITS
0010 relative to whatever's on master at that point -- not resolved here,
just recorded so it isn't a surprise.

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_bookmarked"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("bookmarked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tracks", "bookmarked")
