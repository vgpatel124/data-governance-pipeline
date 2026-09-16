"""Shared test isolation.

Every test gets its own governed DuckDB file so the real data/governed.duckdb is
never touched and tests cannot see each other's governed rows.
"""
import pytest

from src.config.settings import settings


@pytest.fixture(autouse=True)
def isolated_governed_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_GOVERNED_PATH", str(tmp_path / "governed.duckdb"))
    yield
