"""Approval cards: a proposal waiting on a person, delivered with everything needed to decide it,
decided through one door, with every copy brought up to date.

The card IS the board task (`agents.AgentTask`); these tests pin the contract around it:
which tasks are cards, who gets one, what it says, how it reaches them, what a decision does
(once), and that a decision made anywhere is shown everywhere.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import services
from apps.agents.models import Agent, AgentTask, AgentTaskHumanWhy
from apps.approvals import cards, deciders, delivery
from apps.approvals.decide import ApprovalError, decide
from apps.approvals.models import ApprovalDecision, ApprovalDelivery
from apps.harness.models import Turn
from apps.push.models import NotificationPreference
from apps.slack import approvals as slack_cards
from apps.slack.models import SlackUserLink
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user
from tests.fixtures.slack import ALICE, click

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def world(ws, hal, alice):
    """Alice (an owner, and the ALICE Slack user when `linked`) owns Hal; Bo is a viewer; Eve is outside."""
    WorkspaceMembership.objects.filter(user=alice, workspace=ws).update(role=WorkspaceMembership.OWNER)
    Agent.objects.filter(pk=hal.pk).update(owner=alice)
    hal.refresh_from_db()
    bo = a_user("bo@dimagi.com")
    wsvc.ensure_member(ws, bo, WorkspaceMembership.VIEWER)
    eve = a_user("eve@elsewhere.org")
    return {"ws": ws, "hal": hal, "alice": alice, "bo": bo, "eve": eve}


def proposal(world, **over):
    p = {"title": "Add Wouter to the connect workspace", "next_action": "Add wouter@dimagi.com as an admin",
         "plan": "1. Add the membership\n2. Tell Wouter", "rationale": "He asked in #canopy-support and is an admin there",
         "status": "suggested", "idempotency_key": "p1",
         "on_approve": [{"prompt": "Apply approved proposal"}]}
    p.update(over)
    return services.create_tasks(world["hal"], [p])[0]


def test_only_a_proposal_that_starts_work_is_a_card(world):
    from datetime import timedelta

    from django.utils import timezone

    assert cards.needs_decision(proposal(world))
    assert not cards.needs_decision(proposal(world, idempotency_key="p2", on_approve=[]))   # an ordinary idea
    # A plain open ask on the board is not a card: those do not push (owner decision, 2026-10-05).
    assert not cards.needs_decision(proposal(world, idempotency_key="p3", on_approve=[], ask_kind="review"))
    snoozed = proposal(world, idempotency_key="p4")
    AgentTask.objects.filter(pk=snoozed.pk).update(snoozed_until=timezone.now() + timedelta(days=1))
    snoozed.refresh_from_db()
    assert not cards.needs_decision(snoozed)


def test_a_card_says_everything_needed_to_decide(world):
    task = proposal(world)
    AgentTaskHumanWhy.objects.create(task=task, quote="can you add me as an admin on connect?",
                                     who_name="Wouter", who_email="wouter@dimagi.com",
                                     source_url="https://slack.example/p1")
    r = _client(world["alice"]).get(f"/api/approvals/{task.key}")
    assert r.status_code == 200, r.content
    card = r.json()
    assert card["state"] == "open" and card["can_decide"] is True
    assert card["agent_slug"] == "hal" and card["title"] == task.title
    assert card["evidence"] == [{"quote": "can you add me as an admin on connect?", "who": "Wouter",
                                 "source_url": "https://slack.example/p1", "said_at": None}]
    assert card["plan"] == ["Add the membership", "Tell Wouter"]
    assert card["will_run"] == [{"agent": "hal", "prompt": "Apply approved proposal"}]
    assert "Nothing runs" in card["if_declined"] and card["url"].endswith(f"/approvals/{task.key}")


def test_a_card_goes_to_the_accountable_people_in_order(world):
    task = proposal(world)
    assert deciders.for_task(task) == [world["alice"]]                      # the agent's owner
    AgentTask.objects.filter(pk=task.pk).update(waiting_on_user=world["bo"])
    task.refresh_from_db()
    assert deciders.for_task(task) == [world["bo"]]                         # a named person wins
    login = User.objects.create_user("hal-login", "hal@dimagi-ai.com", "pw")
    Agent.objects.filter(pk=world["hal"].pk).update(user=login, owner=None)
    AgentTask.objects.filter(pk=task.pk).update(waiting_on_user=None)
    task.refresh_from_db()
    owners = deciders.for_task(task)                                         # workspace owners; never the agent
    assert world["alice"] in owners and login not in owners and world["bo"] not in owners


@pytest.fixture
def pushes():
    with mock.patch("apps.approvals.channel_push.send_to_user", return_value=1) as m:
        yield m


def test_push_and_slack_reach_a_person_who_has_both_and_email_does_not(world, pushes, installation, slack, linked, mailoutbox):
    task = proposal(world)
    assert delivery.deliver(task.pk) == {"push": 1, "slack": 1, "email": 0}
    assert pushes.call_args.kwargs["urgency"] == "high" and "Add Wouter" in pushes.call_args.kwargs["title"]
    (post,) = [p for p in slack.said("chat.postMessage") if p["channel"] == ALICE]
    actions = [b for b in post["blocks"] if b["type"] == "actions"][0]["elements"]
    assert [a["action_id"] for a in actions[:2]] == [slack_cards.APPROVE, slack_cards.DECLINE]
    assert all(a["value"] == task.key for a in actions[:2])
    assert any("Add the membership" in str(b) for b in post["blocks"])      # the plan is IN the message
    assert not mailoutbox


def test_email_reaches_someone_neither_channel_does(world, pushes, mailoutbox):
    pushes.return_value = 0                                                  # no browser registered
    task = proposal(world)
    assert delivery.deliver(task.pk) == {"push": 0, "slack": 0, "email": 1}
    (mail,) = mailoutbox
    assert mail.to == ["alice@dimagi.com"] and "Approve?" in mail.subject
    html = mail.alternatives[0][0]
    assert f"/approvals/{task.key}?do=approve" in html and f"/approvals/{task.key}?do=decline" in html


def test_a_card_is_delivered_once_and_a_person_can_opt_out(world, pushes, mailoutbox):
    task = proposal(world)
    delivery.deliver(task.pk)
    delivery.deliver(task.pk)
    assert pushes.call_count == 1 and ApprovalDelivery.objects.filter(task=task).count() == 1
    other = proposal(world, idempotency_key="p9", title="Another")
    NotificationPreference.objects.create(user=world["alice"], approval_cards=False)
    assert delivery.deliver(other.pk) == {"push": 0, "slack": 0, "email": 0}
    assert not mailoutbox


def test_saving_a_new_card_delivers_it_on_commit(world, pushes, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        task = proposal(world)
    assert pushes.call_count == 1 and ApprovalDelivery.objects.filter(task=task).exists()


def test_the_backstop_delivers_a_card_that_was_missed_and_only_that(world, pushes):
    task = proposal(world)
    assert delivery.deliver_missed() == 1 and pushes.call_count == 1
    assert delivery.deliver_missed() == 0
    assert ApprovalDelivery.objects.filter(task=task).exists()


def test_approving_starts_the_work_once_and_the_second_click_says_who_got_there_first(world):
    task = proposal(world)
    first = _client(world["alice"]).post(f"/api/approvals/{task.key}/decide",
                                         {"decision": "approve", "comment": "yes"}, content_type="application/json")
    assert first.status_code == 200, first.content
    assert first.json()["state"] == "approved" and first.json()["decided"]["via"] == "web"
    assert Turn.objects.filter(raised_from_task_id=task.pk).count() == 1
    again = _client(world["bo"]).post(f"/api/approvals/{task.key}/decide", {"decision": "approve"},
                                      content_type="application/json")
    assert again.status_code == 409 and "Approved by" in again.json()["detail"]
    assert Turn.objects.filter(raised_from_task_id=task.pk).count() == 1


def test_declining_runs_nothing_and_says_so(world):
    task = proposal(world)
    r = _client(world["alice"]).post(f"/api/approvals/{task.key}/decide",
                                     {"decision": "decline", "comment": "not now"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["state"] == "declined"
    assert r.json()["decided"]["comment"] == "not now"
    assert not Turn.objects.filter(raised_from_task_id=task.pk).exists()


def test_a_viewer_may_decide_as_on_the_board_but_a_stranger_cannot_even_see_it(world):
    task = proposal(world)
    assert _client(world["eve"]).get(f"/api/approvals/{task.key}").status_code == 404
    assert _client(world["eve"]).post(f"/api/approvals/{task.key}/decide", {"decision": "approve"},
                                      content_type="application/json").status_code == 404
    with pytest.raises(ApprovalError) as e:
        decide(task, world["eve"], "approve")
    assert e.value.status == 404
    r = _client(world["bo"]).post(f"/api/approvals/{task.key}/decide", {"decision": "decline"},
                                  content_type="application/json")
    assert r.status_code == 200


def test_my_cards_lists_what_is_waiting_on_me_and_drops_it_once_decided(world):
    task = proposal(world)
    assert [c["key"] for c in _client(world["alice"]).get("/api/approvals/").json()] == [task.key]
    assert _client(world["eve"]).get("/api/approvals/").json() == []
    decide(task, world["alice"], "decline")
    assert _client(world["alice"]).get("/api/approvals/").json() == []


def test_a_slack_click_decides_the_card_and_rewrites_the_message(
        world, pushes, installation, slack, linked, django_capture_on_commit_callbacks):
    task = proposal(world)
    delivery.deliver(task.pk)
    sent = ApprovalDelivery.objects.get(task=task, channel="slack")
    with django_capture_on_commit_callbacks(execute=True):
        resp = click(slack_cards.APPROVE, task.key, user=ALICE, channel=sent.ref["channel"],
                     message_ts=sent.ref["ts"])
    assert resp.status_code == 200
    task.refresh_from_db()
    assert task.status == AgentTask.IN_PROGRESS
    assert ApprovalDecision.objects.get(task=task).via == "slack"
    (update,) = slack.said("chat.update")
    assert update["ts"] == sent.ref["ts"] and "Approved by" in update["text"] or "Approved by" in str(update["blocks"])
    assert not [b for b in update["blocks"] if b["type"] == "actions"]       # the buttons are gone


def test_a_click_on_a_message_canopy_did_not_send_for_this_card_decides_nothing(
        world, pushes, installation, slack, linked):
    task = proposal(world)
    delivery.deliver(task.pk)
    sent = ApprovalDelivery.objects.get(task=task, channel="slack")
    click(slack_cards.APPROVE, task.key, user=ALICE, channel=sent.ref["channel"], message_ts="1111.2222")
    task.refresh_from_db()
    assert task.status == AgentTask.SUGGESTED and not ApprovalDecision.objects.exists()
    assert slack.said("chat.postEphemeral")


def test_deciding_on_the_board_still_updates_every_copy(
        world, pushes, installation, slack, linked, django_capture_on_commit_callbacks):
    task = proposal(world)
    delivery.deliver(task.pk)
    with django_capture_on_commit_callbacks(execute=True):
        r = _client(world["alice"]).post(f"/api/agents/hal/tasks/{task.ref}/actions", {"action": "decline"},
                                         content_type="application/json")
    assert r.status_code == 200, r.content
    (update,) = slack.said("chat.update")
    assert "Declined by" in str(update["blocks"]) and "the board" in str(update["blocks"])
    assert ApprovalDelivery.objects.get(task=task, channel="slack").settled_at is not None
