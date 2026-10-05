"""Shared fixtures.

Everything in the suite runs on file-based SQLite by default. Tests marked
``requires_db`` need a real server and are skipped unless ``DATABASE_URL`` is
set; CI sets it for a PostgreSQL service container.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL_ENV = "DATABASE_URL"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{DATABASE_URL_ENV} is not set, so server-backed tests are skipped. "
            f"CI runs these against PostgreSQL."
        )
    return url


@pytest.fixture
def server_engine(database_url):
    """An engine against the real server, with a clean slate per test."""
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def make_tables(server_engine):
    """Create tables on the server and drop them again afterwards."""
    created: list[str] = []

    def _make(ddl: list[str], tables: list[str]):
        with server_engine.begin() as conn:
            for name in reversed(tables):
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
            for statement in ddl:
                conn.execute(text(statement))
        created.extend(tables)
        return server_engine

    yield _make

    with server_engine.begin() as conn:
        for name in reversed(created):
            conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
