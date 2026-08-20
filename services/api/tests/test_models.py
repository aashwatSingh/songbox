from __future__ import annotations

from sqlalchemy import inspect

from app.db import get_engine
from app.models import Base


def test_every_registered_model_has_a_tenant_id_column() -> None:
    for table_name, table in Base.metadata.tables.items():
        columns = {c.name for c in table.columns}
        assert "tenant_id" in columns, f"{table_name} has no tenant_id column"


def test_every_registered_model_table_exists_in_the_database() -> None:
    inspector = inspect(get_engine())
    existing = set(inspector.get_table_names())
    registered = set(Base.metadata.tables.keys())
    missing = registered - existing
    assert not missing, f"{missing} missing from DB -- did you run `alembic upgrade head`?"
