from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db import SessionLocal, db_session_for_tenant
from app.models import Base

RLS_TABLES = tuple(Base.metadata.tables.keys())


def test_every_table_has_row_level_security_enabled_and_forced() -> None:
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(:tables)"
            ),
            {"tables": list(RLS_TABLES)},
        ).all()
    finally:
        session.close()

    found = {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in rows}
    assert set(found) == set(RLS_TABLES)
    for table, (enabled, forced) in found.items():
        assert enabled, f"{table} does not have RLS enabled"
        err_msg = f"{table} does not FORCE RLS (owner would bypass policies)"
        assert forced, err_msg


def test_tenant_cannot_see_another_tenants_license_row() -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    session_b = db_session_for_tenant(tenant_b)
    license_id = uuid.uuid4()
    session_b.execute(
        text(
            "INSERT INTO licenses (id, tenant_id, reference, covers_recording, covers_lyrics) "
            "VALUES (:id, :tenant_id, 'ref', true, true)"
        ),
        {"id": license_id, "tenant_id": tenant_b},
    )
    session_b.commit()
    session_b.close()

    session_a = db_session_for_tenant(tenant_a)
    query = text("SELECT id FROM licenses WHERE id = :id")
    rows = session_a.execute(query, {"id": license_id}).all()
    session_a.close()
    assert rows == []

    session_b_read = db_session_for_tenant(tenant_b)
    rows_b = session_b_read.execute(
        text("SELECT id FROM licenses WHERE id = :id"), {"id": license_id}
    ).all()
    session_b_read.close()
    assert len(rows_b) == 1
