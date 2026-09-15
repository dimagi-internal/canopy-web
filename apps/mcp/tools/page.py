"""The page the user is looking at, as an MCP tool the agent can re-read.

`page_actions`/`page_tools.py` already gave the agent what a page can DO. This
gives it what the page currently SHOWS — the other half of AG-UI's frontend
split, and the half that was missing.

**Why a tool and not a prompt preamble.** Page context used to be fetched once
when the widget's frame initialised, rendered to prose, and pasted onto the
first message. That reads the screen at the wrong moment (a user filters the
page *after* opening the chat at least as often as before), in the wrong form
(the WebMCP origin-trial ablation recovered the right next action 19/20 from a
bounded structured packet against 14/20 from a byte-matched prose summary), and
only once (by turn three the agent is reasoning about a page the user has left).

A tool inverts all three: the agent asks when it needs to know, and gets the
current view. "Close the ones I'm looking at" is answerable on turn nine.

**It returns a selection, not data.** The state names which rows are on screen
and which tool resolves them; the agent then calls THAT tool, so the rows arrive
through the ordinary path with the caller's own ACL applied. The page is a cache
and never the authority — the same rule contacts follow. `set_page_state`
enforces it with a size cap too small to hold the rows.
"""

from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


def _visible_pages(user_id: int) -> list[dict]:
    """Every attached page of this user's that declares a view, newest first.

    A user with two tabs open genuinely has two views, and which one they mean
    is not knowable here — so both are returned, each labelled with its session,
    rather than one being guessed at silently. `page_tools` makes the opposite
    call for ACTIONS because two tools with one name is unusable; reading is not
    subject to that constraint.
    """
    from apps.canopy_sessions.models import Session

    sessions = (
        Session.objects.filter(created_by_id=user_id, status=Session.ACTIVE)
        .exclude(page_state={})
        .order_by("-created_at")
    )
    out = []
    for session in sessions:
        state = dict(session.page_state or {})
        if not state:
            continue
        out.append(
            {
                "session_id": str(session.id),
                "state": state,
                # Surfaced rather than left inside `state` so a caller can order
                # or compare snapshots without knowing the page's own schema.
                "version": int(state.get("version") or 0),
            }
        )
    return out


# NOT named `page_something`: `page_tools.TOOL_PREFIX` reserves that namespace
# for tools a HOST declares at runtime, precisely so a page cannot shadow one of
# canopy's own. A static tool sitting inside that namespace inverts the same
# collision — a host action named `state` would be silently shadowed by this —
# and blurs a boundary that exists to be sharp.
@mcp.tool
async def current_page() -> list[dict]:
    """What the user is currently looking at, in their open canopy pages.

    Returns one entry per attached page, newest first, each with `session_id`,
    a monotonic `version`, and the page's own `state` object. An empty list
    means no page is attached — not that the user's screen is empty.

    A page's state describes its SELECTION: which filters are applied, which
    rows are visible (by id), and often a `backing_tool` naming the tool that
    resolves those ids. Read the rows with that tool rather than treating the
    ids' surrounding fields as authoritative; the state is a snapshot of a
    browser tab, while the tool returns live data under the caller's own
    permissions.

    Call this when a request is about "these", "the ones on screen", "what I'm
    looking at", or any scope narrower than everything the user can access.
    """
    user_id = current_user_id()
    pages = await sync_to_async(_visible_pages, thread_sensitive=True)(user_id)
    await write_audit(user_id=user_id, tool="current_page", args_summary=f"{len(pages)} page(s)")
    return pages
