"""Test settings on PostgreSQL — for the suites that need what SQLite lacks
(pgvector: `tests/test_kb_vectors_pg.py`). CI's `Backend pytest (PostgreSQL)` job
points DATABASE_URL at a pgvector/pgvector service; locally:

    docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
    DATABASE_URL=postgres://postgres:postgres@localhost:5433/postgres \
      uv run pytest --ds=config.settings.pgtest tests/test_kb_vectors_pg.py

Not named test_*.py: pytest would collect it as a test module on every run.
"""
from .test import *  # noqa: F401, F403
from .base import env

DATABASES = {"default": env.db("DATABASE_URL")}
