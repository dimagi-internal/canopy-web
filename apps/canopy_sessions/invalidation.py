"""Telling a page its data changed.

The other half of `page_state.py`. That module lets a page say what it is
showing; this one makes the saying *stay true*, by notifying every attached page
when the data behind it changes — **whoever changed it, through whatever door**.

**The bug.** Ask canopy "close everything" on `/insights` and what you see
depends on which tool the agent picks: `page_dismissInsights` makes the rows
vanish in front of you, `clear_insights` deletes them server-side and leaves the
page displaying twenty rows that no longer exist until you reload. Same sentence,
two outcomes. And the same staleness arrives with no agent involved at all — the
fleet dismissing an insight while you watch the feed, a scheduled turn, a second
tab, a colleague. The agent only made it easy to notice.

**The vocabulary is MCP's.** A resource URI (`insight://`), and a notification
carrying the URI and nothing else, after which the receiver re-reads. That is
`notifications/resources/updated` exactly, and re-reading rather than patching is
deliberate: the refetch goes through the normal tool where authorization applies,
where a diff would be a second source of truth for data the page already knows
how to load. We take the vocabulary rather than inventing a parallel one, so the
day FastMCP grows a server-side subscription API this module is the only thing
that changes.

**Raised at the MODEL layer, not per tool.** "Every mutating tool publishes an
invalidation" is N sites and it rots — `page_tools.py` shipped with ten passing
tests and no import; six tenancy predicates each grew their own
`NULL means allow` leg. Anything requiring every future author to remember will
be forgotten. One receiver per resource cannot be, and `apps/push/signals.py`
already proves the shape: one row, one receiver, `transaction.on_commit` to
coalesce a batch into a single notification.

See `docs/superpowers/specs/2026-09-16-page-invalidation-design.md`.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.realtime.groups import publish, session_group

from .models import Session

log = logging.getLogger(__name__)

#: The AG-UI `CUSTOM` event name a client sees. Namespaced under `canopy.`
#: like every other canopy-specific event (see `agui.CUSTOM_PREFIX`), because a
#: client reading the stream must be able to tell "the protocol says this" from
#: "canopy says this" — and because a future AG-UI event with the same idea must
#: not collide with ours.
EVENT = "canopy.page.invalidate"


def _dirty_set() -> set[str]:
    """The resources marked in THIS connection's current transaction.

    On the connection, not the module — copied deliberately from
    `apps/push/services._dirty_set`, whose docstring records why: connections
    are thread-local while a module global is not, so two concurrent requests
    shared one set and whichever committed first drained BOTH, flushing the
    other thread's work on a connection that could not yet see its uncommitted
    rows.
    """
    conn = transaction.get_connection()
    if not hasattr(conn, "_canopy_dirty_resources"):
        conn._canopy_dirty_resources = set()
    return conn._canopy_dirty_resources


def mark_dirty(uri: str) -> None:
    """Note that `uri` changed; notify once this transaction commits.

    Safe to call from a signal receiver on every row of a bulk write: a fleet
    audit writing 200 rows in one transaction produces ONE notification per
    resource, not 200.

    Registers the flush UNCONDITIONALLY. Do not add a `if not _dirty_set()`
    guard around the registration — `apps/push` documents exactly why, having
    paid for it: Django discards on_commit callbacks when a transaction rolls
    back, but this set is not transactional and keeps its entries, so the guard
    would see a non-empty set forever after the first rollback, never register
    again, and silently kill invalidation process-wide until restart. Redundant
    callbacks are free; the first to run drains the set and the rest no-op.
    """
    if not uri:
        return
    _dirty_set().add(uri)
    transaction.on_commit(_flush)


def _flush() -> None:
    dirty = _dirty_set()
    uris = set(dirty)
    dirty.clear()
    for uri in uris:
        try:
            notify_resource_changed(uri)
        except Exception:  # noqa: BLE001
            # Best-effort by design. A page that misses a notification shows
            # stale data until its next read; a notification that raises inside
            # `on_commit` would take down the request that did the real work,
            # which is strictly worse than the staleness it was trying to fix.
            log.warning("page invalidation failed for %s", uri, exc_info=True)


def sessions_showing(uri: str):
    """Active sessions whose attached page declares it is showing `uri`.

    Matched on the page's own declaration rather than on anything canopy infers,
    because the page is the only party that knows what it rendered. A session
    that declares no resource is not notified — silence is correct there, not a
    default-to-everyone.
    """
    return Session.objects.filter(
        status=Session.ACTIVE, page_state__resource=uri
    ).only("id")


def notify_resource_changed(uri: str) -> int:
    """Tell every page showing `uri` to re-read. Returns how many were told.

    The payload is the URI and nothing else, which is `resources/updated`'s own
    shape. A client that wants to know WHAT changed re-reads; a client that
    cannot be bothered is no worse off than before this existed.
    """
    sent = 0
    for session in sessions_showing(uri):
        publish(
            session_group(session.id),
            {"type": "page.invalidate", "uri": uri},
        )
        sent += 1
    return sent
