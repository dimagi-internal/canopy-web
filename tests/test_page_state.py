"""What the user is looking at, as state the agent can re-read.

The failure this exists to prevent is subtler than the one `test_page_actions`
covers, and worse for being invisible: an agent acting confidently on a view
that is no longer on screen. Page context used to be read once, at frame init,
and pasted onto the first message as prose — so filtering the page after opening
the chat left the agent describing, and acting on, a screen that had moved.

So these tests are mostly about FRESHNESS and BOUNDS: that a second declaration
wholly replaces the first rather than merging with it, that the version moves
forward so two snapshots can be told apart, and that a page trying to send its
rows instead of its selection is refused loudly at the point of the mistake.
"""

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import page_state
from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

#: The shape a page is supposed to send: what is selected, and where to read it
#: properly. Note what is NOT here — the insight text, project, category, age.
#: Those come from `list_insights`, under the caller's own permissions.
INSIGHTS_VIEW = {
    "surface": "the insights feed",
    "path": "/insights",
    "filters": {"category": "stale", "project": "commcare"},
    "visible_ids": [4471, 4472, 4480],
    "backing_tool": "list_insights",
}


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, title="chat")
    c = Client()
    c.force_login(user)
    return user, session, c


def _put(c, session, state):
    return c.put(
        f"/api/canopy-sessions/{session.id}/page-state",
        data={"state": state},
        content_type="application/json",
    )


# --- declaring the view ------------------------------------------------------


def test_a_page_declares_what_it_shows_and_the_agent_can_read_it_back():
    _user, session, c = _ctx()

    assert _put(c, session, INSIGHTS_VIEW).status_code == 200

    read = c.get(f"/api/canopy-sessions/{session.id}/page-state").json()
    assert read["state"]["visible_ids"] == [4471, 4472, 4480]
    # The backing tool is the whole DRY move: the page says which rows and which
    # tool resolves them, and the agent reads the rows through that tool rather
    # than trusting a copy the page serialised.
    assert read["state"]["backing_tool"] == "list_insights"


def test_a_second_declaration_replaces_the_first_rather_than_merging():
    """A page has ONE current view.

    Merging is the bug that makes stale state indistinguishable from fresh
    state: a filter left over from the view the user navigated away from would
    survive into a view that does not have it, and the agent would narrow by a
    filter nobody applied.
    """
    _user, session, c = _ctx()

    _put(c, session, INSIGHTS_VIEW)
    _put(c, session, {"surface": "the supervisor inbox", "path": "/supervisor"})

    state = c.get(f"/api/canopy-sessions/{session.id}/page-state").json()["state"]
    assert state["path"] == "/supervisor"
    assert "filters" not in state
    assert "visible_ids" not in state


def test_the_version_moves_forward_so_two_snapshots_can_be_told_apart():
    _user, session, c = _ctx()

    first = _put(c, session, INSIGHTS_VIEW).json()
    second = _put(c, session, {**INSIGHTS_VIEW, "visible_ids": [4471]}).json()

    assert second["version"] > first["version"]


def test_the_version_is_assigned_by_the_server_not_accepted_from_the_page():
    """A client that picks its own version numbers can silently go backwards."""
    _user, session, c = _ctx()

    _put(c, session, INSIGHTS_VIEW)
    out = _put(c, session, {**INSIGHTS_VIEW, "version": 9999}).json()

    assert out["version"] == 2


# --- the bound that enforces "selection, not data" ---------------------------


def test_a_page_sending_its_rows_instead_of_its_selection_is_refused():
    """The cap is a design guard, not a resource limit.

    A page that serialises the rows it displays has duplicated `list_insights`,
    can go stale between render and send, and has become a second place an ACL
    could be got wrong. Refusing at the point the mistake is made is the only
    moment anyone will notice.
    """
    _user, session, c = _ctx()
    fat = {
        "path": "/insights",
        "insights": [
            {"id": n, "text": "x" * 300, "project": "commcare", "category": "stale"}
            for n in range(200)
        ],
    }

    response = _put(c, session, fat)

    assert response.status_code == 422
    assert "too_large" in response.json()["detail"]
    # And it says what to do instead, because the author is reading this error
    # exactly once and will do whatever it tells them.
    assert "selection" in response.json()["detail"]


def test_the_cap_is_generous_for_the_shape_we_actually_want():
    """Several hundred ids must fit — otherwise the cap would push pages back
    toward sending less than the agent needs to act on a full screen."""
    _user, session, c = _ctx()

    response = _put(c, session, {**INSIGHTS_VIEW, "visible_ids": list(range(500))})

    assert response.status_code == 200


def test_a_refused_declaration_leaves_the_previous_view_intact():
    """A rejection must not blank the screen the agent already knew about."""
    _user, session, c = _ctx()
    _put(c, session, INSIGHTS_VIEW)

    _put(c, session, {"rows": [{"t": "x" * 400} for _ in range(100)]})

    state = c.get(f"/api/canopy-sessions/{session.id}/page-state").json()["state"]
    assert state["visible_ids"] == [4471, 4472, 4480]


# --- absence --------------------------------------------------------------


def test_no_page_attached_reads_as_empty_not_as_an_error():
    _user, session, c = _ctx()

    read = c.get(f"/api/canopy-sessions/{session.id}/page-state").json()

    assert read["state"] == {}
    assert read["version"] == 0


def test_clearing_is_how_a_page_detaches():
    _user, session, c = _ctx()
    _put(c, session, INSIGHTS_VIEW)

    session.refresh_from_db()
    page_state.clear_page_state(session)

    session.refresh_from_db()
    assert page_state.current_page_state(session) == {}


def test_another_users_session_is_not_readable():
    """The same gate every other by-id read uses. Stated here because page state
    describes a person's screen, which is about as personal as canopy data gets.
    """
    _user, session, _c = _ctx()
    stranger = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    other = Client()
    other.force_login(stranger)

    assert other.get(f"/api/canopy-sessions/{session.id}/page-state").status_code == 404


# --- the service's own contract ---------------------------------------------


def test_the_state_is_stored_as_given_apart_from_the_version():
    _user, session, _c = _ctx()

    stored = page_state.set_page_state(session, INSIGHTS_VIEW)

    assert json.loads(json.dumps(stored)) == {**INSIGHTS_VIEW, "version": 1}


def test_a_non_object_state_is_refused():
    _user, session, _c = _ctx()

    with pytest.raises(page_state.PageStateError) as exc:
        page_state.set_page_state(session, ["not", "an", "object"])

    assert exc.value.code == "bad_state"


# --- the agent's read path ---------------------------------------------------


def test_the_current_page_tool_is_registered_through_the_real_entry_point():
    """Not "the module has tests" — "the server actually serves it".

    `page_tools.py` once shipped with ten passing tests and was never imported
    by anything, so the feature did not exist. This asserts against the mounted
    FastMCP instance, which is the only thing that can be wrong in production.
    """
    import asyncio

    from apps.mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp._list_tools())}
    assert "current_page" in names


def test_the_tool_stays_out_of_the_namespace_reserved_for_host_actions():
    """`page_tools.TOOL_PREFIX` is reserved for tools a HOST declares at
    runtime, so that a page cannot shadow one of canopy's own. A static canopy
    tool inside that namespace inverts the same collision — a host action named
    `state` would be silently shadowed — and blurs a boundary meant to be sharp.
    """
    import asyncio

    from apps.mcp import page_tools
    from apps.mcp.server import mcp

    static = {t.name for t in asyncio.run(mcp._list_tools())}
    assert not any(n.startswith(page_tools.TOOL_PREFIX) for n in static)
