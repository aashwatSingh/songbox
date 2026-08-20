from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db import db_session_for_tenant, get_engine


def test_can_connect_and_select_1() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_db_session_for_tenant_sets_readable_tenant_context() -> None:
    tenant_id = uuid.uuid4()
    session = db_session_for_tenant(tenant_id)
    try:
        result = session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == str(tenant_id)
    finally:
        session.close()
