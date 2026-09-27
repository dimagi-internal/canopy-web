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

**Three ways of asking, one per kind of caller, narrowest first.** A session
driving a chat presents that chat's KEY (`X-Canopy-Chat-Key`) and gets that
chat's page — this is how an agent sees the screen of the person it is talking
to. A confined caller token names its own CONVERSATION and gets that. Anything
else is a USER asking about their own open tabs — several, if they have more
than one. An agent's login alone is the third case and matches nothing: it is
in every chat the agent is in, so it cannot say which screen is meant.

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
    from django.contrib.auth.models import User

    from apps.canopy_sessions.page_access import sessions_with_page_for

    # Resolved to the real user so the predicate can compare concrete ids —
    # `page_visible_q` matches NOTHING for a missing caller, which is the safe
    # direction and the one an id-only filter would have got wrong.
    caller = User.objects.filter(pk=user_id).first()
    sessions = sessions_with_page_for(caller).exclude(page_state={})
    return [_page_out(session) for session in sessions if session.page_state]


def _pages_of_turns(turn_ids: list[str]) -> list[dict]:
    """The attached pages of THIS confined conversation, newest first.

    A caller token's tools run as the caller, and a widget visitor is a contact
    with no canopy account — so `page_visible_q` matches nothing (correctly: it
    answers "whose pages may this USER see") and the page the host declared for
    this very conversation was invisible to the agent it was declared for. The
    symptom was an empty list while a screen full of rows sat in front of the
    person asking.

    So when the token names a conversation, that is what is answered about. It is
    NARROWER than the user-scoped path, not wider: one conversation, the token's
    own, minted by canopy for this turn — the same pinning `who_is_asking` uses
    (`turn_scope.TURN_PINNED`), for the same reason.
    """
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    session_ids = [
        sid
        for sid in Turn.objects.filter(pk__in=turn_ids).values_list("chat_session_id", flat=True)
        if sid
    ]
    sessions = (
        Session.objects.filter(pk__in=session_ids).exclude(page_state={}).order_by("-created_at")
    )
    return [_page_out(session) for session in sessions if session.page_state]


def _page_of_chat() -> list[dict] | None:
    """The page of the chat this request's chat key names — or None when the
    request carries no key, so the caller falls through to the older paths."""
    from apps.mcp.chat_scope import current_chat_session

    session = current_chat_session()
    if session is None:
        return None
    return [_page_out(session)] if session.page_state else []


def _page_out(session) -> dict:
    state = dict(session.page_state or {})
    return {
        "session_id": str(session.id),
        "state": state,
        # Surfaced rather than left inside `state` so a caller can order or
        # compare snapshots without knowing the page's own schema.
        "version": int(state.get("version") or 0),
    }


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

    In a confined caller's turn — a visitor talking to an embedded panel — this
    answers about THAT conversation's page, and nothing else.
    """
    from apps.mcp.turn_scope import caller_turn_ids

    user_id = current_user_id()
    # A confined caller's turn answers about its OWN conversation. Checked first
    # because such a caller may also be a canopy user, and in that turn the
    # question is "this screen", not "every page I have open elsewhere".
    # A session driving a chat names that chat with its key, and the answer is
    # that chat's page — not every chat its agent's login happens to be in.
    pages = await sync_to_async(_page_of_chat, thread_sensitive=True)()
    if pages is not None:
        await write_audit(user_id=user_id, tool="current_page",
                          args_summary=f"{len(pages)} page(s) on this chat (chat key)")
        return pages
    turn_ids = caller_turn_ids()
    if turn_ids is not None:
        pages = await sync_to_async(_pages_of_turns, thread_sensitive=True)(turn_ids)
        await write_audit(user_id=user_id, tool="current_page",
                          args_summary=f"{len(pages)} page(s) on this conversation")
        return pages
    pages = await sync_to_async(_visible_pages, thread_sensitive=True)(user_id)
    await write_audit(user_id=user_id, tool="current_page", args_summary=f"{len(pages)} page(s)")
    return pages
