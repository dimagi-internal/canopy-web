"""A slow knowledge-base call keeps only its own caller waiting.

Outside a Django request `thread_sensitive=True` is ONE thread per process, the one
MCP sign-in (apps/mcp/auth.py) and its audit write run on. A KB call there — a
reranked search, a first-call download, a long `sql` — held up every MCP request on
the instance; `off_the_shared_thread` runs it on a worker thread instead.
"""
from __future__ import annotations

import asyncio
import threading

from asgiref.sync import sync_to_async

from apps.mcp.projects import off_the_shared_thread


async def test_a_slow_kb_call_does_not_hold_up_the_shared_thread():
    started, release = threading.Event(), threading.Event()

    def slow_kb_call():
        started.set()
        release.wait(10)
        return "kb"

    call = asyncio.ensure_future(off_the_shared_thread(slow_kb_call)())
    await asyncio.to_thread(started.wait, 5)
    try:
        # What sign-in runs on: answered while the KB call is still going.
        signed_in = await asyncio.wait_for(sync_to_async(lambda: "signed in", thread_sensitive=True)(), 3)
    finally:
        release.set()
    assert signed_in == "signed in"
    assert await call == "kb"
