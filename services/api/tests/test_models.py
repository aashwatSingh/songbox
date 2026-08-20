from __future__ import annotations

from sqlalchemy import inspect

from app.db import get_engine
from app.models import FingerprintMatch, License, RightsDeclaration, Track

EXPECTED_TABLES = {
    "licenses": License,
    "rights_declarations": RightsDeclaration,
    "tracks": Track,
    "fingerprint_matches": FingerprintMatch,
}


def test_all_expected_tables_exist_after_migration() -> None:
    inspector = inspect(get_engine())
    existing = set(inspector.get_table_names())
    for table_name in EXPECTED_TABLES:
        msg = f"{table_name} missing -- did you run `alembic upgrade head`?"
        assert table_name in existing, msg


def test_every_model_table_has_a_tenant_id_column() -> None:
    for table_name, model in EXPECTED_TABLES.items():
        columns = {c.name for c in model.__table__.columns}
        assert "tenant_id" in columns, f"{table_name} has no tenant_id column"
