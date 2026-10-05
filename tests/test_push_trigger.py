"""Inbox items and the push machinery.

Inbox items (open asks, tasks parked on a person) are NOT on the supervisor
feed, and you are pushed only about the feed (Jonathan, 2026-10-05) — so an item
never pushes. Its agent's waiting COUNT is still snapshotted, coalesced to one
recompute per agent per transaction, because the live Inbox badge reads it.
(That a task change marks its agent dirty at all is pinned in
test_push_items.py; what DOES push is pinned in test_supervisor_feed.py.)

The send mechanics — prune a dead subscription, keep one on a transient
failure, log — and `agent_audience` are pinned here on `send_to_user` directly."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.agents import services as agent_services
from apps.agents.models import AgentTask
from apps.harness.models import Turn
from apps.push import services as push_services
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


def _count(agent) -> int:
    return AgentWaitingSnapshot.objects.get(agent=agent).waiting_count


def test_a_new_open_item_moves_the_badge_but_does_not_push(agent, sub):
    with patch("apps.push.services._send_one") as send:
        _item(agent, "i1")
    assert send.call_count == 0
    assert _count(agent) == 1


def test_clearing_an_item_lowers_the_count(agent, sub):
    item = _item(agent, "i1")
    agent_services.dismiss_ask(item, by="jj@dimagi.com")
    assert _count(agent) == 0


def test_a_batch_of_items_is_one_recompute_per_agent(agent, sub):
    with patch("apps.push.services.refresh_agent_waiting", wraps=push_services.refresh_agent_waiting) as refresh:
        agent_services.raise_asks(
            agent=agent,
            payloads=[{"title": f"a{i}", "idempotency_key": f"a{i}"} for i in range(10)],
        )
    assert refresh.call_count == 1
    assert _count(agent) == 10


def test_a_rolled_back_transaction_does_not_wedge_the_recompute(agent, sub):
    """Django discards on_commit callbacks on rollback, but _dirty is a plain set
    and keeps its entries. A `if not _dirty` guard around the registration would
    never re-register after the first rollback. Pin it."""
    from django.db import transaction

    try:
        with transaction.atomic():
            _item(agent, "doomed")
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    _item(agent, "after")
    assert _count(agent) == 1  # would be stale if the registration were gated


# ---- the send machinery --------------------------------------------------------


def test_the_payload_carries_count_for_the_service_worker_badge(user, sub):
    with patch("apps.push.services._send_one") as send:
        push_services.send_to_user(user, title="t", body="b", url="/supervisor", count=3)
    _sub_arg, payload = send.call_args[0]
    assert payload["count"] == 3


def test_a_user_with_no_subscription_gets_nothing(user):
    with patch("apps.push.services._send_one") as send:
        assert push_services.send_to_user(user, title="t", body="b", url="/") == 0
    assert send.call_count == 0


def test_a_dead_subscription_is_pruned(user, sub):
    """A subscription dies silently when the app is uninstalled: the push service
    starts returning 410 Gone. Prune on that signal, not on a timer."""
    from pywebpush import WebPushException

    class _Resp:
        status_code = 410

    with patch("apps.push.services._send_one", side_effect=WebPushException("gone", response=_Resp())):
        push_services.send_to_user(user, title="t", body="b", url="/")
    assert not PushSubscription.objects.filter(pk=sub.pk).exists()


def test_a_transient_send_failure_keeps_the_subscription(user, sub):
    from pywebpush import WebPushException

    class _Resp:
        status_code = 503

    with patch("apps.push.services._send_one", side_effect=WebPushException("busy", response=_Resp())):
        push_services.send_to_user(user, title="t", body="b", url="/")
    sub.refresh_from_db()
    assert sub.failure_count == 1  # kept — a 503 is the service's problem, not ours


def test_a_sent_push_is_logged(user, sub, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="apps.push.services"), \
            patch("apps.push.services._send_one"):
        push_services.send_to_user(user, title="t", body="b", url="/")
    assert any("push: sent" in r.getMessage() for r in caplog.records)


# ---- who hears about an agent -------------------------------------------------
# On labs six of eight agents have owner=None (2026-09-24), so pushing only to
# agent.owner meant nobody heard. The audience falls back to the agent's
# explicit admins, then the workspace's owners.


def _ownerless(workspace, slug="hal"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=workspace, owner=None)


def test_an_owned_agent_is_heard_by_its_owner(agent, user):
    assert push_services.agent_audience(agent) == [user]


def test_an_ownerless_agent_is_heard_by_the_workspace_owner(workspace, user):
    assert push_services.agent_audience(_ownerless(workspace)) == [user]


def test_an_explicit_admin_is_told_instead_of_every_workspace_owner(workspace, user):
    from apps.agents.models import AgentAdmin

    admin = User.objects.create_user("ad", "ad@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=admin, workspace=workspace, role=WorkspaceMembership.EDITOR)
    hal = _ownerless(workspace)
    AgentAdmin.objects.create(agent=hal, user=admin)
    assert push_services.agent_audience(hal) == [admin]


def test_an_admin_who_left_the_workspace_is_not_told(workspace, user):
    from apps.agents.models import AgentAdmin

    gone = User.objects.create_user("gone", "gone@dimagi.com", "pw")
    hal = _ownerless(workspace)
    AgentAdmin.objects.create(agent=hal, user=gone)  # never a member
    assert push_services.agent_audience(hal) == [user]  # falls to the ws owner
