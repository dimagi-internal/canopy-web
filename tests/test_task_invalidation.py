"""The inbox is told when the inbox moves.

The inbox-specific sibling of `test_page_invalidation`, which pins the generic
machinery on this same resource. It exists because
`InboxSection` stopped serialising its rows into page state: the page now sends
ids and a `backing_tool`, which is only better than the old shape if something
tells the page its set changed. Otherwise the page holds ids of items that were
closed ten minutes ago and re-reads them forever. (The resource is still
`item://` — the page-state URI the frontend subscribes to.)
"""

import uuid

import pytest
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from apps.agents.models import Agent, AgentTask
from apps.canopy_sessions import invalidation, page_state
from apps.canopy_sessions.models import Session
from apps.harness.signals import ITEM_RESOURCE
from apps.workspaces.models import Workspace, WorkspaceMembership

_EXT = iter(range(1, 10_000))


def _ext() -> str:
    """A unique `ext_id` per task these tests create. Tasks are board cards and
    carry one; an ask raised through the service gets it for free."""
    return f"T{next(_EXT)}"


#: `transaction=True` because `on_commit` is the subject — see the same note in
#: `test_page_invalidation`.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sent(monkeypatch):
    out = []
    # The dirty set lives on the CONNECTION and is not transactional: an earlier
    # test on this xdist worker that marked a resource inside a rolled-back
    # transaction leaves it there, and this test's first commit would flush it
    # (seen: a stale resource from another test failing this one in the merge queue).
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
    page_state.set_page_state(
        session, {"path": "/w/w1/agents/echo", "resource": uri, "backing_tool": "list_tasks"}
    )


def _item(agent, title="review the deploy", **kw):
    return AgentTask.objects.create(agent=agent, ext_id=_ext(), title=title, origin="manual", idempotency_key=str(uuid.uuid4()), **kw
    )


def test_a_page_showing_the_inbox_is_told_when_a_task_arrives(sent):
    _user, _ws, session, agent = _world()
    _showing(session)

    _item(agent)

    assert [m["uri"] for _g, m in sent] == [ITEM_RESOURCE]


def test_closing_an_ask_notifies_even_though_it_leaves_the_open_set(sent):
    """The case a `state=OPEN` filter would have dropped — and the one that
    matters most, because it is how a row LEAVES the page."""
    _user, _ws, session, agent = _world()
    item = _item(agent)
    _showing(session)
    sent.clear()

    item.ask_closed_at = timezone.now()
    item.save(update_fields=["ask_closed_at"])

    assert [m["uri"] for _g, m in sent] == [ITEM_RESOURCE]


def test_a_fleet_audits_batch_sends_one_notification(sent):
    """`create_tasks` commits a whole audit in one transaction. Without
    coalescing, a 25-task batch is 25 refetches of the same list."""
    _user, _ws, session, agent = _world()
    _showing(session)
    sent.clear()

    with transaction.atomic():
        for n in range(25):
            _item(agent, title=f"item {n}")

    assert len(sent) == 1


def test_a_page_showing_another_resource_is_not_disturbed_by_a_task(sent):
    _user, _ws, session, agent = _world()
    _showing(session, uri="walkthrough://")

    _item(agent)

    assert sent == []


def test_the_receiver_is_connected_through_the_app_registry():
    """Not "the module has tests" — "Django actually calls it"."""
    from django.db.models.signals import post_save

    names = []
    for entry in post_save.receivers:
        ref = entry[1]
        fn = ref() if callable(ref) and hasattr(ref, "__callback__") else ref
        if fn is not None:
            names.append(getattr(fn, "__name__", ""))
    assert "_item_changed" in names
