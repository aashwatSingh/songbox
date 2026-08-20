"""enable row level security

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19

"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("licenses", "rights_declarations", "tracks", "fingerprint_matches")

APP_ROLE = "songbox_app"
# dev-only password, matches docker-compose.yml creds
APP_ROLE_PASSWORD = "songbox_app"


def upgrade() -> None:
    # songbox_app is intentionally NOT superuser and NOT bypassrls -- that's the entire point.
    # CREATE ROLE has no IF NOT EXISTS in Postgres, so guard it with a DO block for idempotency
    # (re-running migrations against an existing dev DB, or migrating a fresh one, both work).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}';
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE songbox TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Only revoke if the role exists (in case this migration is downgraded before an
    # earlier dev DB state that never had songbox_app).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                REVOKE SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} FROM {APP_ROLE};
                REVOKE USAGE ON SCHEMA public FROM {APP_ROLE};
                REVOKE CONNECT ON DATABASE songbox FROM {APP_ROLE};
            END IF;
        END
        $$;
        """
    )
    # The role itself is intentionally left in place on downgrade (DROP ROLE can fail if anything
    # else references it, and leaving an unprivileged, ungranted role around is harmless).
