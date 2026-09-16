"""Whose page may this caller see?

**The test that was missing.** Both page surfaces filtered `created_by = caller`
and every test made the caller and the session owner the same person — so the
suite was green while the feature could not work at all in production, where the
caller is the AGENT (holding its own PAT) and the owner is the human it is
talking to. Every test here makes those two DIFFERENT people, because that is
the only configuration that actually ships.

The failure was silent in the worst way: an agent with no page tools does not
error, it just answers without them.
"""

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import page_access
from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _world():
    """A human, the agent they chat with, and a bystander agent in the same tenant."""
    human = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=human)
    WorkspaceMembership.objects.create(user=human, workspace=ws, role=WorkspaceMembership.OWNER)

    ace_user = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws, user=ace_user)

    hal_user = User.objects.create_user("hal", "hal@dimagi-ai.com", "pw")
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, user=hal_user)

    session = Session.objects.create(workspace=ws, created_by=human, agent=ace, title="chat")
    return human, ace_user, hal_user, session


def _visible(user):
    return set(page_access.sessions_with_page_for(user).values_list("id", flat=True))


# --- the case that actually ships -------------------------------------------


def test_the_agent_can_see_the_page_of_the_person_it_is_talking_to():
    """THE regression. An agent authenticates to canopy's MCP as ITSELF, so the
    caller is never the session's creator — and the old `created_by` filter
    matched nothing, giving the agent no page tools and an empty page state."""
    _human, ace_user, _hal, session = _world()

    assert session.id in _visible(ace_user)


def test_the_human_can_still_see_their_own_page():
    human, _ace, _hal, session = _world()

    assert session.id in _visible(human)


# --- and nobody else --------------------------------------------------------


def test_another_agent_in_the_same_workspace_cannot():
    """Deliberately narrower than workspace membership. A co-tenant already
    cannot read the chat, and the page is more intimate than the chat — it is a
    live picture of somebody's screen."""
    _human, _ace, hal_user, session = _world()

    assert session.id not in _visible(hal_user)


def test_a_stranger_cannot():
    _human, _ace, _hal, session = _world()
    stranger = User.objects.create_user("nope", "nope@dimagi.com", "pw")

    assert session.id not in _visible(stranger)


def test_no_caller_matches_nothing_rather_than_everything():
    """The single most dangerous thing this predicate could get wrong.

    An empty `Q()` is the IDENTITY, so returning one for an absent caller would
    not narrow the query — it would widen it to every session on the
    deployment. `Q(pk__in=[])` is the safe direction.
    """
    _world()

    assert _visible(None) == set()
    assert _visible(object()) == set()


def test_an_agent_with_no_login_matches_nothing():
    """`Agent.user` is nullable, and a NULL must never match.

    This is the `workspace_id IS NULL means allow` shape the repo has already
    paid for: a nullable column used in an authorization predicate invites a leg
    where absence reads as permission.
    """
    human, _ace, _hal, _session = _world()
    ws = Workspace.objects.get(slug="w1")
    loginless = Agent.objects.create(slug="ghost", name="Ghost", workspace=ws, user=None)
    orphan = Session.objects.create(
        workspace=ws, created_by=human, agent=loginless, title="ghost chat"
    )

    # A user with no id of their own must not pick up the NULL-agent session.
    assert orphan.id not in _visible(User(pk=None))
    # And the human who OWNS it still sees it, by the other leg.
    assert orphan.id in _visible(human)


def test_an_agentless_session_is_visible_only_to_its_creator():
    """A repo chat has no agent, so only the `created_by` leg can match."""
    human, ace_user, _hal, _session = _world()
    ws = Workspace.objects.get(slug="w1")
    solo = Session.objects.create(workspace=ws, created_by=human, agent=None, title="solo")

    assert solo.id in _visible(human)
    assert solo.id not in _visible(ace_user)


def test_only_active_sessions_count():
    """An archived session is not a screen anyone is looking at."""
    _human, ace_user, _hal, session = _world()
    session.status = Session.ARCHIVED
    session.save(update_fields=["status"])

    assert session.id not in _visible(ace_user)


# --- through the surfaces that consume it -----------------------------------


def test_the_agent_sees_the_pages_state_through_the_mcp_tool():
    """Not the predicate in isolation — the function the tool actually calls."""
    from apps.canopy_sessions import page_state
    from apps.mcp.tools.page import _visible_pages

    _human, ace_user, _hal, session = _world()
    page_state.set_page_state(session, {"path": "/insights", "visible_ids": [1, 2]})

    pages = _visible_pages(ace_user.pk)

    assert [p["state"]["visible_ids"] for p in pages] == [[1, 2]]


def test_the_agent_sees_the_pages_actions_through_the_provider():
    from apps.canopy_sessions import page_actions
    from apps.mcp.page_tools import page_tool_specs

    _human, ace_user, _hal, session = _world()
    page_actions.set_declared_actions(session, [{"name": "dismissInsights"}])

    specs = page_tool_specs(ace_user)

    assert [name for name, _s, _spec in specs] == ["page_dismissInsights"]


def test_another_agent_gets_no_page_tools():
    from apps.canopy_sessions import page_actions
    from apps.mcp.page_tools import page_tool_specs

    _human, _ace, hal_user, session = _world()
    page_actions.set_declared_actions(session, [{"name": "dismissInsights"}])

    assert page_tool_specs(hal_user) == []
