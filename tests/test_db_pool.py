"""The deployed database is pooled per process (config/db_pool.py)."""
from __future__ import annotations

import environ
import pytest

from config import db_pool


@pytest.fixture
def env(monkeypatch):
    for name in ("DB_POOL", "DB_POOL_MIN", "DB_POOL_MAX", "DB_POOL_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    return environ.Env()


BASE = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "canopy_web",
                    "CONN_MAX_AGE": 0, "OPTIONS": {"sslmode": "require"}}}


def test_off_unless_turned_on(env):
    assert db_pool.apply(BASE, env) == BASE


def test_pooled_when_turned_on_with_modest_bounds(env, monkeypatch):
    monkeypatch.setenv("DB_POOL", "1")
    db = db_pool.apply(BASE, env)["default"]
    pool = db["OPTIONS"]["pool"]
    assert db["OPTIONS"]["sslmode"] == "require"
    assert db["CONN_MAX_AGE"] == 0
    assert pool["min_size"] == 2 and pool["max_size"] == 30
    assert pool["timeout"] == 10.0 and callable(pool["check"])


def test_env_overrides_and_off_switch(env, monkeypatch):
    monkeypatch.setenv("DB_POOL", "1")
    monkeypatch.setenv("DB_POOL_MAX", "12")
    assert db_pool.apply(BASE, env)["default"]["OPTIONS"]["pool"]["max_size"] == 12
    monkeypatch.setenv("DB_POOL", "0")
    assert db_pool.apply(BASE, env) == BASE


def test_django_builds_a_psycopg_pool_from_it(env, monkeypatch):
    """The options are ones psycopg_pool.ConnectionPool accepts (built closed, no connection)."""
    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.utils import ConnectionHandler
    from psycopg_pool import ConnectionPool

    monkeypatch.setenv("DB_POOL", "1")
    settings = ConnectionHandler(db_pool.apply(BASE, env)).settings
    wrapper = DatabaseWrapper(settings["default"], alias="pooltest")
    try:
        pool = wrapper.pool
        assert isinstance(pool, ConnectionPool)
        assert pool.max_size == 30 and pool.min_size == 2
    finally:
        DatabaseWrapper._connection_pools.pop("pooltest", None)
