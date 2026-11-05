"""One connection's slow sync call no longer stalls every other connection.

Without a ThreadSensitiveContext, every `thread_sensitive=True` call in a Channels consumer or an
MCP handler ran on ONE process-wide thread: two sockets each making a 0.4 s call took 0.8 s.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from asgiref.sync import sync_to_async

from apps.common import thread_context


def _slow(seen):
    seen.append(threading.get_ident())
    time.sleep(0.4)


async def _app(scope, receive, send):
    await sync_to_async(_slow, thread_sensitive=True)(scope["seen"])
    await sync_to_async(_slow, thread_sensitive=True)(scope["seen2"])


async def _two_connections(wrapped):
    a = {"type": "websocket", "seen": [], "seen2": []}
    b = {"type": "websocket", "seen": [], "seen2": []}
    started = time.monotonic()
    await asyncio.gather(wrapped(a, None, None), wrapped(b, None, None))
    return time.monotonic() - started, a, b


@pytest.fixture
def no_db_close(monkeypatch):
    calls = []

    async def fake_close():
        calls.append(threading.get_ident())
    monkeypatch.setattr(thread_context, "close_thread_connections", fake_close)
    return calls


def test_two_sockets_run_side_by_side(monkeypatch, no_db_close):
    monkeypatch.setenv("CANOPY_PER_CONNECTION_THREADS", "1")
    elapsed, a, b = asyncio.run(_two_connections(thread_context.PerConnectionThread(_app)))
    assert elapsed < 1.2, elapsed                          # 2 calls each, in parallel: ~0.8 s
    assert a["seen"][0] != b["seen"][0]                     # different threads
    assert a["seen"][0] == a["seen2"][0]                    # one thread for a socket's whole life


def test_switch_restores_the_shared_thread(monkeypatch, no_db_close):
    monkeypatch.setenv("CANOPY_PER_CONNECTION_THREADS", "0")
    elapsed, a, b = asyncio.run(_two_connections(thread_context.PerConnectionThread(_app)))
    assert elapsed >= 1.5, elapsed                          # 4 calls in series
    assert a["seen"][0] == b["seen"][0]


def test_connections_are_closed_on_the_way_out(monkeypatch, no_db_close):
    monkeypatch.setenv("CANOPY_PER_CONNECTION_THREADS", "1")
    asyncio.run(_two_connections(thread_context.PerConnectionThread(_app)))
    assert len(no_db_close) == 2


def test_lifespan_scopes_pass_straight_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
    asyncio.run(thread_context.PerConnectionThread(app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_mcp_messages_each_get_a_thread(monkeypatch, no_db_close):
    monkeypatch.setenv("CANOPY_PER_CONNECTION_THREADS", "1")
    mw = thread_context.mcp_middleware()

    async def call_next(ctx):
        await sync_to_async(_slow, thread_sensitive=True)(ctx)
        return "ok"

    async def go():
        a, b = [], []
        started = time.monotonic()
        res = await asyncio.gather(mw.on_message(a, call_next), mw.on_message(b, call_next))
        return time.monotonic() - started, res, a, b

    elapsed, res, a, b = asyncio.run(go())
    assert res == ["ok", "ok"] and elapsed < 0.75 and a[0] != b[0]


def test_close_thread_connections_closes_django_connections(monkeypatch):
    from django.db import connections

    closed = []
    monkeypatch.setattr(connections, "close_all", lambda: closed.append(threading.get_ident()))
    asyncio.run(thread_context.close_thread_connections())
    assert len(closed) == 1
