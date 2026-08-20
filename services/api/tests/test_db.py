from __future__ import annotations

from sqlalchemy import text

from app.db import get_engine


def test_can_connect_and_select_1() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        assert result.scalar() == 1
