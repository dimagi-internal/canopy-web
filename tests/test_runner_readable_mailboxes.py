"""Which mailboxes a runner can READ is reported by the runner, not configured.

A laptop runner used to read only the mailboxes in a hand-kept `mailboxes` map in
~/.canopy/runner.json. The owner did not know the map existed, a box with `{}`
there was still rung by the push doorbell (which only knew agent assignments),
and it silently ignored every ring (canopy-web#1087). Now the runner probes —
a gog token for the address that answers a one-message search — and reports the
readable list on its heartbeat. The doorbell rings only boxes that can read the
address, the mailbox page shows who can, and a runner that has never reported
(older code) is UNKNOWN and keeps being rung, exactly as before.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.inbound import services
from apps.inbound.models import InboundMailbox, InboundPushConfig
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ADDRESS = "eva@dimagi-ai.com"


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="eva", name="Eva", workspace=workspace)


@pytest.fixture()
def mailbox(agent):
    return InboundMailbox.objects.create(address=ADDRESS, agent=agent)


def _runner(owner, workspace, name="jj-mbp-cdp", **kw):
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, owner=owner, workspace=workspace,
        ready=True, status=Runner.ONLINE, last_heartbeat_at=timezone.now(), **kw,
    )


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _beat(client, runner, **extra):
    body = {"active_turn_ids": [], "degraded": False, "note": "", **extra}
    return client.post(
        f"/api/harness/runners/{runner.id}/heartbeat", body, content_type="application/json",
    )


# ── the heartbeat report ─────────────────────────────────────────────────────


def test_a_reported_list_is_stored_lowercased_and_stamped(client, owner, workspace):
    runner = _runner(owner, workspace)
    assert _beat(client, runner, mailboxes_readable=["Eva@Dimagi-AI.com", ""]).status_code == 200
    runner.refresh_from_db()
    assert runner.mailboxes_readable == [ADDRESS]
    assert runner.mailboxes_checked_at is not None


def test_an_absent_report_leaves_the_stored_list_alone(client, owner, workspace):
    """None is "not reported this beat" — a lease renewal or an older runner —
    and must not wipe what the last probe found."""
    runner = _runner(owner, workspace, mailboxes_readable=[ADDRESS])
    assert _beat(client, runner).status_code == 200
    runner.refresh_from_db()
    assert runner.mailboxes_readable == [ADDRESS]
    assert runner.mailboxes_checked_at is None


def test_an_empty_report_is_a_real_answer(client, owner, workspace):
    runner = _runner(owner, workspace, mailboxes_readable=[ADDRESS])
    _beat(client, runner, mailboxes_readable=[])
    runner.refresh_from_db()
    assert runner.mailboxes_readable == []


def test_the_runner_list_serves_what_was_reported(client, owner, workspace):
    runner = _runner(owner, workspace)
    _beat(client, runner, mailboxes_readable=[ADDRESS])
    rows = client.get("/api/harness/runners/").json()
    row = next(r for r in rows if r["id"] == str(runner.id))
    assert row["mailboxes_readable"] == [ADDRESS]
    assert row["mailboxes_checked_at"]


# ── the doorbell ─────────────────────────────────────────────────────────────


def test_the_doorbell_skips_a_runner_that_cannot_read_the_mailbox(mailbox, agent, owner, workspace):
    """The #1087 case: assigned, online, holds no token — ringing it does nothing."""
    blind = _runner(owner, workspace, mailboxes_readable=[])
    RunnerAssignment.objects.create(agent=agent, runner=blind, rank=0)
    assert services.online_runners_for(mailbox) == []


def test_the_doorbell_rings_a_runner_that_reports_it_can_read(mailbox, agent, owner, workspace):
    reader = _runner(owner, workspace, mailboxes_readable=[ADDRESS])
    blind = _runner(owner, workspace, name="cloud-ec2-1", mailboxes_readable=["hal@dimagi-ai.com"])
    RunnerAssignment.objects.create(agent=agent, runner=blind, rank=0)
    RunnerAssignment.objects.create(agent=agent, runner=reader, rank=1)
    with mock.patch("apps.inbound.services.publish") as pub:
        rang = services.ring(mailbox)
    assert [r.name for r in rang] == ["jj-mbp-cdp"]
    assert pub.call_count == 1


def test_a_runner_that_never_reported_is_still_rung(mailbox, agent, owner, workspace):
    """Backwards compatible: an older runner sends nothing, which is UNKNOWN."""
    legacy = _runner(owner, workspace)
    assert legacy.mailboxes_readable is None
    RunnerAssignment.objects.create(agent=agent, runner=legacy, rank=0)
    assert services.online_runners_for(mailbox) == [legacy]


def test_the_comparison_ignores_case(mailbox, agent, owner, workspace):
    runner = _runner(owner, workspace, mailboxes_readable=["EVA@dimagi-ai.com"])
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    assert services.online_runners_for(mailbox) == [runner]


# ── visibility on the mailbox list ───────────────────────────────────────────


def test_the_mailbox_list_names_its_readers(client, mailbox, agent, owner, workspace):
    routed_blind = _runner(owner, workspace, mailboxes_readable=[])
    legacy = _runner(owner, workspace, name="old-laptop")
    reporter = _runner(owner, workspace, name="cloud-ec2-1", mailboxes_readable=[ADDRESS])
    RunnerAssignment.objects.create(agent=agent, runner=routed_blind, rank=0)
    RunnerAssignment.objects.create(agent=agent, runner=legacy, rank=1)
    readers = client.get("/api/inbound/mailboxes/dimagi").json()["items"][0]["readers"]
    by_name = {r["runner"]: r for r in readers}
    assert set(by_name) == {"jj-mbp-cdp", "old-laptop", "cloud-ec2-1"}
    assert by_name["jj-mbp-cdp"]["can_read"] is False
    assert by_name["old-laptop"]["can_read"] is None, "never reported is unknown, not no"
    assert by_name["cloud-ec2-1"]["can_read"] is True, "a reporter counts even unrouted"
    assert by_name["cloud-ec2-1"]["status"] == "online"
    assert reporter.pk  # (fixture used)


def test_another_tenants_runner_is_never_named(client, mailbox, owner, workspace):
    stranger = User.objects.create_user("o2", "o2@example.org", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=stranger)
    WorkspaceMembership.objects.create(user=stranger, workspace=other_ws,
                                       role=WorkspaceMembership.OWNER)
    _runner(stranger, other_ws, name="their-box", mailboxes_readable=[ADDRESS])
    readers = client.get("/api/inbound/mailboxes/dimagi").json()["items"][0]["readers"]
    assert readers == []


# ── what the runner's probe reads ────────────────────────────────────────────


def test_runner_mailboxes_carry_the_agent_and_include_topicless_rows(client, mailbox, workspace):
    """The probe needs every mailbox as a candidate — a workspace with no watch
    topic still has mail to read — and the agent each one belongs to."""
    items = client.get("/api/inbound/runner-mailboxes").json()["items"]
    assert items == [{"address": ADDRESS, "agent_slug": "eva", "watch_topic": ""}]
    InboundPushConfig.objects.create(workspace=workspace, watch_topic="projects/p/topics/t")
    items = client.get("/api/inbound/runner-mailboxes").json()["items"]
    assert items == [{"address": ADDRESS, "agent_slug": "eva",
                      "watch_topic": "projects/p/topics/t"}]
