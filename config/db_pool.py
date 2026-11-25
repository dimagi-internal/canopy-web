"""The deployed database connection: a psycopg pool per process, decided in one tested place.
Off unless the deployment sets `DB_POOL=1`.

A pool bounds how many threads hold a connection at once, so it is only safe while no thread
holds one idle for long. A request's thread keeps whatever connection it opened until Django
or `apps/common/thread_context.py` closes it at the request's end, so a request that queries
early and then streams for minutes holds a slot the whole time: MCP sign-in did exactly that
under a stateful Streamable-HTTP GET stream, and a few dozen agent sessions exhausted the pool.
Sign-in now closes its connection when it returns (`apps/mcp/auth.py`,
`apps/mcp/delegation.py`; `tests/test_db_pool_exhaustion_pg.py` reproduces the exhaustion).
Before turning it on, check what the process holds: `pg_stat_activity` for this database,
idle connections with an old `state_change`, should be few.

Under ASGI every request, WebSocket and MCP message runs its sync code on a thread of its own
(`ThreadSensitiveContext`), and Django connections are per thread. A persistent connection
(`CONN_MAX_AGE`) therefore never gets reused: it stays bound to a thread that exits. Django's
answer for ASGI is the backend's own pool: a connection is taken from the process's pool
when a thread first queries and handed back when Django closes it (request end, every
`database_sync_to_async` call, `apps/common/thread_context.py` at the end of a socket or MCP
message, each off-request tick), so a request no longer pays a fresh TLS + SCRAM handshake to RDS.

`CONN_MAX_AGE` stays 0: Django refuses a pool with persistent connections.

Sizing: the RDS instance is shared with connect-labs and ace-web. `max_size` bounds what one
web process can hold (a deploy briefly runs old and new tasks side by side, and a scaled
service runs several); a thread waiting longer than `timeout` for a free connection gets an
error instead of queueing forever. Without `DB_POOL=1` each thread opens its own connection
and closes it at request end.
"""
from __future__ import annotations


def pool_options(env) -> dict | None:
    """The `OPTIONS["pool"]` for the default database, or None for no pool."""
    if not env.bool("DB_POOL", default=False):
        return None
    from psycopg_pool import ConnectionPool  # noqa: PLC0415 — only a pooled deployment needs psycopg_pool

    return {
        "min_size": env.int("DB_POOL_MIN", default=2),
        "max_size": env.int("DB_POOL_MAX", default=30),
        # Seconds a thread waits for a free connection before an error.
        "timeout": env.float("DB_POOL_TIMEOUT", default=10.0),
        # Close connections idle this long, down to min_size; recycle any after max_lifetime.
        "max_idle": 300.0,
        "max_lifetime": 1800.0,
        # Check a connection before handing it out: RDS and the network drop idle ones.
        "check": ConnectionPool.check_connection,
    }


def apply(databases: dict, env) -> dict:
    """`databases` with the default database pooled (when enabled)."""
    options = pool_options(env)
    if options is None:
        return databases
    default = {**databases["default"]}
    default["CONN_MAX_AGE"] = 0
    default["OPTIONS"] = {**default.get("OPTIONS", {}), "pool": options}
    return {**databases, "default": default}
