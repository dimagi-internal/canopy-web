"""Reproduces how MCP sign-in exhausted a psycopg pool, and pins the fix.

A psycopg pool bounds how many threads may hold a connection at once. Under ASGI each HTTP
request runs its sync code on a thread of its own (`ThreadSensitiveContext`), and that thread
keeps whatever connection it opened until something closes it. MCP sign-in
(`apps/mcp/auth.py`) and the DPoP gate (`apps/mcp/delegation.py`) ran their lookups with a
plain `sync_to_async`, which never closes. A stateful Streamable-HTTP GET stream then held
that request's thread, and its connection, idle for the life of the stream. A few dozen
agent sessions were a few dozen pool slots gone, and every other request waited out the
pool timeout.

The fix closes the connection when the lookup returns (`database_sync_to_async`). The load
test below holds `max_size` long-lived "streams" open, then asks for one more connection:
with the plain wrapper the pool is exhausted; with the closing one it is not.

PostgreSQL only (a psycopg pool); CI runs it in `Backend pytest (PostgreSQL)`.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from channels.db import database_sync_to_async
from django.db import OperationalError, connection, connections

from tests._source import ROOT

pg = pytest.mark.skipif(connection.vendor != "postgresql", reason="a psycopg pool needs PostgreSQL")
ALIAS = "pool_load"
MAX = 3


@pytest.fixture(scope="module")
def pooled():
    base = connections["default"].settings_dict
    connections.settings[ALIAS] = {**base, "CONN_MAX_AGE": 0, "TEST": {"MIRROR": "default"},
                                   "OPTIONS": {**base.get("OPTIONS", {}),
                                               "pool": {"min_size": 0, "max_size": MAX, "timeout": 1.0}}}
    yield ALIAS
    from django.db.backends.postgresql.base import DatabaseWrapper

    pool = DatabaseWrapper._connection_pools.pop(ALIAS, None)
    if pool is not None:
        pool.close()
    try:
        del connections[ALIAS]
    except (AttributeError, KeyError):
        pass
    connections.settings.pop(ALIAS, None)


def _lookup():
    """What a sign-in lookup does to the connection: open it and query."""
    with connections[ALIAS].cursor() as cur:
        cur.execute("SELECT 1")
        return cur.fetchone()[0]


def _run(wrap) -> str:
    """Hold MAX streams open, each having signed in with `wrap`, then ask for one more."""
    async def stream(started, release):
        async with ThreadSensitiveContext():
            await wrap(_lookup)()
            started.release()
            await release.wait()  # the response streams; the request's thread lives on
            await sync_to_async(connections.close_all)()  # thread_context.PerConnectionThread at the end

    async def main():
        started, release = asyncio.Semaphore(0), asyncio.Event()
        streams = [asyncio.create_task(stream(started, release)) for _ in range(MAX)]
        for _ in range(MAX):
            done, _ = await asyncio.wait([asyncio.ensure_future(started.acquire())] + streams,
                                         return_when=asyncio.FIRST_COMPLETED, timeout=20)
            for task in streams:
                if task.done() and task.exception() is not None:
                    raise task.exception()
        try:
            async with ThreadSensitiveContext():
                await database_sync_to_async(_lookup)()
            return "served"
        except OperationalError as exc:
            return f"exhausted: {exc}"
        finally:
            release.set()
            await asyncio.gather(*streams)

    # On a loop of its own, as uvicorn runs one: under `async_to_sync` every thread-sensitive
    # call would run on the test's own thread instead of each context's.
    out: list = []

    def run():
        try:
            out.append(asyncio.run(main()))
        except BaseException as exc:  # noqa: BLE001 — handed back to the test thread
            out.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(60)
    assert out, "the load test did not finish"
    if isinstance(out[0], BaseException):
        raise out[0]
    return out[0]


@pg
@pytest.mark.django_db(transaction=True, databases="__all__")
def test_a_plain_sync_to_async_sign_in_exhausts_the_pool(pooled):
    out = _run(lambda fn: sync_to_async(fn, thread_sensitive=True))
    assert out.startswith("exhausted"), out


@pg
@pytest.mark.django_db(transaction=True, databases="__all__")
def test_a_closing_sign_in_leaves_the_pool_free(pooled):
    assert _run(database_sync_to_async) == "served"


def test_mcp_sign_in_and_the_dpop_gate_close_their_connection():
    for rel in ("apps/mcp/auth.py", "apps/mcp/delegation.py"):
        src = (ROOT / rel).read_text()
        assert "database_sync_to_async(" in src, rel
        assert "sync_to_async(" not in src.replace("database_sync_to_async(", ""), rel
