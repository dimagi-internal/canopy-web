"""The push trigger. The fleet's waiting set is a COUNT (open items per agent)
with no single "the fleet needs you now" event, so we snapshot the count per agent
and push only when it INCREASES. Tasks are the sole producer now — this file pins
the send/coalesce/prune mechanics on that producer. (That a task change marks its
agent dirty at all is pinned separately in test_push_items.py.)"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.agents import services as agent_services
from apps.agents.models import AgentTask
from apps.harness.models import Turn
from apps.push.models import AgentWaitingSnapshot, PushSubscription
from apps.workspaces.models import Workspace, WorkspaceMembership


_EXT = iter(range(1, 10_000))


def _ext() -> str:
    """A unique `ext_id` per task these tests create. Tasks are board cards and
    carry one; an ask raised through the service gets it for free."""
    return f"T{next(_EXT)}"


# transaction=True is load-bearing, not decorative: mark_dirty() relies on
# transaction.on_commit() to coalesce a batch into one push per agent. Under the
# plain @pytest.mark.django_db marker, pytest-django wraps each test in an outer
# atomic() that is rolled back at teardown, so on_commit callbacks are silently
# discarded and every push assertion would fail with call_count == 0.
pytestmark = pytest.mark.django_db(transaction=True)


# apps.push.services._dirty_set() lives on the DB connection; pytest is
# single-threaded, so tests share one connection -> one _push_dirty. Clear it
# around every test so a leaked id can't bleed between them.
@pytest.fixture(autouse=True)
def _reset_dirty_set():
    from apps.push import services

    services._dirty_set().clear()
    yield
    services._dirty_set().clear()


# send_to_user() short-circuits BEFORE _send_one when VAPID_PRIVATE_KEY is empty
# ("push not configured — stay silent"). CI has no .env, so set a dummy key here
# or every push-expecting assertion sees call_count == 0 for the wrong reason.
@pytest.fixture(autouse=True)
def _vapid_configured(settings):
    settings.VAPID_PRIVATE_KEY = "test-vapid-private-key"
    settings.VAPID_SUBJECT = "mailto:test@example.com"


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


# owner is load-bearing: refresh_agent_waiting pushes to agent.owner, and
# Agent.owner is nullable — an ownerless agent pushes to nobody, so every
# assertion below would pass vacuously without it.
@pytest.fixture()
def agent(workspace, user):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace, owner=user)


@pytest.fixture()
def sub(user):
    return PushSubscription.objects.create(
        user=user, endpoint="https://fcm.googleapis.com/fcm/send/AAA", p256dh="k", auth="a"
    )


def _item(agent, key, *, ask_kind=AgentTask.ASK_REVIEW):
    return AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=ask_kind, title=f"item {key}", origin=Turn.ORIGIN_API,
        idempotency_key=key,
    )


def test_a_new_open_item_pushes(agent, sub):
    with patch("apps.push.services._send_one") as send:
        _item(agent, "i1")
    assert send.call_count == 1
    assert AgentWaitingSnapshot.objects.get(agent=agent).waiting_count == 1


def test_clearing_an_item_does_not_push(agent, sub):
    """The count going DOWN must be silent — being buzzed when you clear something
    is the fastest way to make someone turn notifications off."""
    item = _item(agent, "i1")
    with patch("apps.push.services._send_one") as send:
        agent_services.dismiss_ask(item, by="jj@dimagi.com")
    assert send.call_count == 0
    assert AgentWaitingSnapshot.objects.get(agent=agent).waiting_count == 0


def test_an_unchanged_count_does_not_push(agent, sub):
    _item(agent, "i1")
    item2 = _item(agent, "i2")
    agent_services.dismiss_ask(item2, by="jj@dimagi.com")  # count now 1
    with patch("apps.push.services._send_one") as send:
        # touch a decided item — no change to the OPEN count
        item2.comment = "noted"
        item2.save(update_fields=["comment"])
    assert send.call_count == 0


def test_a_batch_of_items_pushes_once_per_agent_not_once_per_row(agent, sub):
    """THE storm case: a fleet audit raises many items in one call. create_items
    wraps the batch in one transaction, so on_commit coalesces to a single push."""
    with patch("apps.push.services._send_one") as send:
        agent_services.raise_asks(
            agent=agent,
            payloads=[{"title": f"a{i}", "idempotency_key": f"a{i}"} for i in range(10)],
        )
    assert send.call_count == 1
    assert AgentWaitingSnapshot.objects.get(agent=agent).waiting_count == 10


def test_a_rolled_back_transaction_does_not_wedge_push(agent, sub):
    """Django discards on_commit callbacks on rollback, but _dirty is a plain set
    and keeps its entries. A `if not _dirty` guard around the registration would
    never re-register after the first rollback, killing push process-wide. Pin it."""
    from django.db import transaction

    try:
        with transaction.atomic():
            _item(agent, "doomed")
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    with patch("apps.push.services._send_one") as send:
        _item(agent, "after")
    assert send.call_count == 1  # would be 0 if the registration were gated


def test_two_agents_in_one_transaction_push_once_each(agent, workspace, user, sub):
    from django.db import transaction

    hal = Agent.objects.create(slug="hal", name="Hal", workspace=workspace, owner=user)
    with patch("apps.push.services._send_one") as send:
        with transaction.atomic():
            _item(agent, "a1")
            _item(hal, "h1")
    assert send.call_count == 2


def test_a_user_with_no_subscription_gets_nothing(agent):
    with patch("apps.push.services._send_one") as send:
        _item(agent, "i1")
    assert send.call_count == 0
    # The snapshot still advances — otherwise the first push after subscribing
    # would fire for items that were already there.
    assert AgentWaitingSnapshot.objects.get(agent=agent).waiting_count == 1


def test_the_payload_carries_count_for_the_service_worker_badge(agent, sub):
    """SupervisorPage.setBadge only runs on mount, so a push arriving with the app
    closed must carry its own count for the SW's push listener (sw-push.js)."""
    with patch("apps.push.services._send_one") as send:
        _item(agent, "i1")
    assert send.call_count == 1
    _sub_arg, payload = send.call_args[0]
    assert payload["count"] == 1


def test_a_dead_subscription_is_pruned(agent, sub):
    """A subscription dies silently when the app is uninstalled: the push service
    starts returning 410 Gone. Prune on that signal, not on a timer."""
    from pywebpush import WebPushException

    class _Resp:
        status_code = 410

    with patch("apps.push.services._send_one", side_effect=WebPushException("gone", response=_Resp())):
        _item(agent, "i1")
    assert not PushSubscription.objects.filter(pk=sub.pk).exists()


def test_a_transient_send_failure_keeps_the_subscription(agent, sub):
    from pywebpush import WebPushException

    class _Resp:
        status_code = 503

    with patch("apps.push.services._send_one", side_effect=WebPushException("busy", response=_Resp())):
        _item(agent, "i1")
    sub.refresh_from_db()
    assert sub.failure_count == 1  # kept — a 503 is the service's problem, not ours


# ---- who hears about an agent with no owner -----------------------------------
# On labs six of eight agents have owner=None (2026-09-24), so pushing only to
# agent.owner meant "Hal needs you" went to nobody. The audience falls back to
# the agent's explicit admins, then the workspace's owners.


def _ownerless(workspace, slug="hal"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=workspace, owner=None)


def test_an_ownerless_agent_pushes_to_the_workspace_owner(workspace, user, sub):
    hal = _ownerless(workspace)
    with patch("apps.push.services._send_one") as send:
        _item(hal, "i1")
    assert send.call_count == 1
    assert send.call_args.args[0].user_id == user.pk


def test_an_explicit_admin_is_told_instead_of_every_workspace_owner(workspace, user, sub):
    from apps.agents.models import AgentAdmin

    admin = User.objects.create_user("ad", "ad@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=admin, workspace=workspace, role=WorkspaceMembership.EDITOR)
    PushSubscription.objects.create(user=admin, endpoint="https://fcm.googleapis.com/fcm/send/BBB",
                                    p256dh="k", auth="a")
    hal = _ownerless(workspace)
    AgentAdmin.objects.create(agent=hal, user=admin)
    with patch("apps.push.services._send_one") as send:
        _item(hal, "i1")
    assert [c.args[0].user_id for c in send.call_args_list] == [admin.pk]


def test_an_admin_who_left_the_workspace_is_not_told(workspace, user, sub):
    from apps.agents.models import AgentAdmin

    gone = User.objects.create_user("gone", "gone@dimagi.com", "pw")
    PushSubscription.objects.create(user=gone, endpoint="https://fcm.googleapis.com/fcm/send/CCC",
                                    p256dh="k", auth="a")
    hal = _ownerless(workspace)
    AgentAdmin.objects.create(agent=hal, user=gone)  # never a member
    with patch("apps.push.services._send_one") as send:
        _item(hal, "i1")
    assert [c.args[0].user_id for c in send.call_args_list] == [user.pk]  # falls to the ws owner


def test_a_sent_push_is_logged(agent, sub, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="apps.push.services"), \
            patch("apps.push.services._send_one"):
        _item(agent, "i1")
    assert any("push: sent" in r.getMessage() for r in caplog.records)
