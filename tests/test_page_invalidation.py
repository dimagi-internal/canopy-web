"""A page is told when the data it is showing changes.

The bug: "close the ones I'm looking at" did one of two things depending on
which tool the agent happened to pick — rows vanishing in front of you (a page
action), or the page going on displaying rows that no longer existed (a
server-side tool). And the same staleness arrives with no agent involved: the
fleet, a schedule, a second tab, a colleague.

These tests pin the GENERIC machinery (`apps.canopy_sessions.invalidation`):
who is told, when, and how often. They are anchored on the inbox's resource
(`item://`, raised by `apps/harness/signals.py`) because it is a real, live
consumer; the inbox-specific cases (a decision leaving the open set, a fleet
audit's batch) live in `test_item_invalidation`. Until 2026-10 the anchor was
the Insights feed, which was retired.

Two of these tests exist because `apps/push` documented traps in its own
docstrings that the first draft of this module walked straight into, and only a
test keeps them from being re-introduced by someone who reads the code and not
the comment.
"""

import uuid

import pytest
from django.contrib.auth.models import User
from django.db import transaction
from django.test import Client

from apps.agents.models import Agent, AgentTask
from apps.canopy_sessions import invalidation, page_state
from apps.canopy_sessions.models import Session
from apps.harness.signals import ITEM_RESOURCE
from apps.workspaces.models import Workspace, WorkspaceMembership

#: `transaction=True` because `on_commit` is the SUBJECT here. pytest-django's
#: default wraps each test in a transaction it rolls back, so commit hooks never
#: fire and every assertion below would pass or fail for a reason that has
#: nothing to do with the code. Slower, and the only faithful option.
pytestmark = pytest.mark.django_db(transaction=True)

_EXT = iter(range(1, 10_000))


@pytest.fixture
def sent(monkeypatch):
    """Everything published to a session group."""
    out = []
    # The dirty set lives on the CONNECTION and is not transactional: an earlier
    # test on this xdist worker that marked a resource inside a rolled-back
    # transaction leaves it there, and this test's first commit would flush it
    # (seen: a stale resource failing an unrelated test in the merge queue).
    invalidation._dirty_set().clear()
    monkeypatch.setattr(invalidation, "publish", lambda group, msg: out.append((group, msg)))
    return out


def _world():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, title="chat")
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    return user, ws, session, agent


def _showing(session, uri=ITEM_RESOURCE):
    page_state.set_page_state(session, {"path": "/w/w1/agents/echo/inbox", "resource": uri,
                                        "backing_tool": "list_tasks"})


def _an_item(agent, title="review the deploy"):
    return AgentTask.objects.create(
        agent=agent, ext_id=f"T{next(_EXT)}", title=title, origin="manual",
        ask_kind=AgentTask.ASK_REVIEW, idempotency_key=str(uuid.uuid4()),
    )


# --- the loop ----------------------------------------------------------------


def test_a_page_showing_the_inbox_is_told_when_an_item_changes(sent):
    _user, _ws, session, agent = _world()
    _showing(session)

    _an_item(agent)

    assert [m["type"] for _g, m in sent] == ["page.invalidate"]
    assert sent[0][1]["uri"] == ITEM_RESOURCE


def test_it_fires_on_deletion_too_which_is_the_case_that_actually_bit(sent):
    """"Close everything" can be a delete, and a delete is what left the page
    showing rows that no longer existed."""
    _user, _ws, session, agent = _world()
    item = _an_item(agent)
    _showing(session)
    sent.clear()

    item.delete()

    assert [m["uri"] for _g, m in sent] == [ITEM_RESOURCE]


def test_a_page_showing_something_else_is_not_disturbed(sent):
    _user, _ws, session, agent = _world()
    _showing(session, uri="walkthrough://")

    _an_item(agent)

    assert sent == []


def test_a_page_declaring_no_resource_is_not_notified(sent):
    """Silence, not a default-to-everyone. A page that never said what it shows
    cannot be told its data moved."""
    _user, _ws, session, agent = _world()
    page_state.set_page_state(session, {"path": "/w/w1/agents/echo/inbox"})

    _an_item(agent)

    assert sent == []


# --- the traps apps/push paid for --------------------------------------------


def test_a_bulk_change_sends_one_notification_not_one_per_row(sent):
    """A fleet audit writes hundreds of rows in one transaction. Coalescing is
    the difference between one refetch and a page refetching itself to death."""
    _user, _ws, session, agent = _world()
    _showing(session)
    sent.clear()

    with transaction.atomic():
        for n in range(25):
            _an_item(agent, title=f"item {n}")

    assert len(sent) == 1, f"expected one coalesced notification, got {len(sent)}"


def test_a_rolled_back_transaction_notifies_nothing(sent):
    """A notification for a write that never landed makes a correct page refetch
    into the same state — which looks like a bug and teaches people to distrust
    the signal."""
    _user, _ws, session, agent = _world()
    _showing(session)
    sent.clear()

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        _an_item(agent)
        raise Boom()

    assert sent == []


def test_invalidation_still_works_after_a_rollback(sent):
    """THE trap, documented in `apps/push/services.mark_dirty` and walked into by
    the first draft of this module.

    Django discards on_commit callbacks when a transaction rolls back, but the
    dirty set is not transactional and keeps its entries. A
    `if not _dirty_set(): register()` guard therefore sees a non-empty set
    forever after the first rollback, never registers again, and silently kills
    invalidation process-wide until restart. Registering unconditionally is what
    makes this test pass.
    """
    _user, _ws, session, agent = _world()
    _showing(session)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        _an_item(agent)
        raise Boom()
    sent.clear()

    _an_item(agent, title="after the rollback")

    assert len(sent) == 1, "invalidation died after a rollback — the guard is back"


def test_a_publish_failure_does_not_take_down_the_write(monkeypatch):
    """Best-effort by design: a page that misses a notification shows stale data
    until its next read, while an exception inside on_commit would fail the
    request that did the real work."""
    _user, _ws, session, agent = _world()
    _showing(session)

    def boom(*_a, **_k):
        raise RuntimeError("channel layer is down")

    monkeypatch.setattr(invalidation, "publish", boom)

    item = _an_item(agent)  # must not raise

    assert AgentTask.objects.filter(pk=item.pk).exists()


def test_only_active_sessions_are_notified(sent):
    _user, _ws, session, agent = _world()
    _showing(session)
    session.status = Session.ARCHIVED
    session.save(update_fields=["status"])
    sent.clear()

    _an_item(agent)

    assert sent == []


# --- the composition that replaced a page action -------------------------------


def test_the_server_side_decline_invalidates_the_page(sent):
    """A server tool (`act_on_task`, here through its REST route — the MCP tool
    IS that route) changes the row; the receiver on the row is what tells the
    page. Without it the tool would decline the task and leave the page
    displaying it, which is the exact bug page actions were invented to dodge."""
    user, _ws, session, agent = _world()
    item = _an_item(agent)
    _showing(session)
    sent.clear()

    client = Client()
    client.force_login(user)
    resp = client.post(f"/api/agents/{agent.slug}/tasks/{item.ext_id}/actions",
                       data={"action": "decline"}, content_type="application/json")

    assert resp.status_code == 200, resp.content
    assert [m["uri"] for _g, m in sent] == [ITEM_RESOURCE]
