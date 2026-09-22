# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""One Slack, several canopy tenants.

Dimagi staff sit in one Slack and support several canopy workspaces, so a Slack
can be linked to more than one. The rule these tests pin: the tenant of any
message is decided by what it is ABOUT (its thread's session, or the agent it
names), never by the Slack it came through. Driven through the same signed
webhooks and Slack fake as test_slack.py.
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.contacts.models import Contact
from apps.events.models import Event
from apps.harness.models import Turn
from apps.slack import commands, share
from apps.slack.models import SlackUserLink, SlackWorkspaceLink
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user, a_workspace
from tests.test_slack import (  # noqa: F401
    ALICE, BOB, TEAM, _commands, alice, configured, event, hal, installation, linked, mention, slack, ws,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant_b(installation):
    """A second canopy workspace, linked to the SAME Slack, with its own agent."""
    b = a_workspace("otherco")
    SlackWorkspaceLink.objects.create(installation=installation, workspace=b)
    return b


@pytest.fixture
def bea(tenant_b):
    return Agent.objects.create(slug="bea", name="Bea", workspace=tenant_b, slack_enabled=True)


def test_a_named_agent_answers_in_its_own_tenant(slack, linked, alice, hal, bea, tenant_b, ws):
    mention("bea what's new?")
    turn = Turn.objects.get()
    assert turn.chat_session.workspace_id == tenant_b.slug
    assert turn.chat_session.agent == bea


def test_a_member_of_one_tenant_is_a_contact_of_the_other(slack, linked, alice, hal, bea, tenant_b, ws):
    """Alice belongs to `dimagi` only. To tenant B's agent she is a contact —
    recorded IN B — exactly like anyone else outside B."""
    assert not wsvc.is_member(alice, tenant_b.slug)
    mention("bea help me")
    session = Turn.objects.get().chat_session
    assert session.created_by is None and session.contact is not None
    assert session.contact.workspace_id == tenant_b.slug
    assert not Contact.objects.filter(workspace=ws).exists()


def test_a_member_of_both_acts_as_themselves_in_each(slack, linked, alice, hal, bea, tenant_b):
    wsvc.ensure_member(tenant_b, alice, WorkspaceMembership.EDITOR)
    mention("bea help me")
    session = Turn.objects.get().chat_session
    assert session.created_by == alice and session.contact is None


def test_a_thread_continues_in_the_tenant_it_started_in(slack, linked, alice, hal, bea, tenant_b, ws):
    mention("hal first", ts="1700000000.000100")
    # A plain reply with several agents across tenants: the thread decides.
    mention("and a follow-up", ts="1700000050.000100", thread_ts="1700000000.000100")
    sessions = {t.chat_session_id for t in Turn.objects.all()}
    assert len(sessions) == 1
    assert Session.objects.get().workspace_id == ws.slug


def test_naming_nobody_lists_every_tenants_agents(slack, linked, alice, hal, bea):
    mention("anyone there?")
    assert not Turn.objects.exists()
    note = slack.said("chat.postEphemeral")[-1]["text"]
    assert "`bea`" in note and "`hal`" in note


def test_the_only_agent_across_tenants_answers_unnamed(slack, linked, alice, tenant_b, bea):
    mention("anyone there?")
    assert Turn.objects.get().chat_session.agent == bea


def test_an_unlinked_tenants_thread_is_unreachable(slack, linked, alice, hal, bea, tenant_b):
    mention("bea first", ts="1700000000.000100")
    SlackWorkspaceLink.objects.filter(workspace=tenant_b).delete()
    Agent.objects.filter(pk=bea.pk).update(slack_enabled=False)
    event({"type": "message", "channel_type": "channel", "user": ALICE, "text": "more",
           "ts": "1700000050.000100", "thread_ts": "1700000000.000100", "channel": "C1"})
    assert Turn.objects.count() == 1


def test_a_refusal_with_no_tenant_is_logged_to_the_home_tenant(slack, linked, alice, hal, bea, ws):
    mention("anyone there?")
    assert Event.objects.filter(kind="slack.no_agent", workspace=ws).exists()


def test_a_sync_from_one_tenant_keeps_the_others_commands(slack, installation, hal, bea, tenant_b, ws):
    installation.app_id = "A_CANOPY"
    installation.save()
    commands.set_config_token(installation, "xoxe-pasted", user=None)
    commands.reconcile(installation)
    assert {"/hal", "/bea"} <= set(_commands(slack))
    # Tenant B turns its agent off: only /bea goes, and A's /hal survives.
    Agent.objects.filter(pk=bea.pk).update(slack_enabled=False)
    result = commands.reconcile(installation)
    assert result["removed"] == ["/bea"]
    assert "/hal" in _commands(slack)


def test_sharing_from_a_newly_linked_tenants_session(slack, installation, tenant_b):
    carol = a_user("carol@otherco.com")
    wsvc.ensure_member(tenant_b, carol, WorkspaceMembership.OWNER)
    SlackUserLink.objects.create(installation=installation, slack_user_id=BOB, user=carol)
    session = Session.objects.create(workspace=tenant_b, project="widget", origin=Session.ORIGIN_RUNNER, title="w")
    RunnerBinding.objects.create(session=session, session_key="w", emdash_project="widget")
    result = share.share_session(carol, channel="C1", summary="hello", mode=share.BIND, session=session)
    assert result.ok
    session.refresh_from_db()
    assert session.metadata["slack_team"] == TEAM
    # And a reply in that thread reaches it — the thread's tenant is B.
    wsvc.ensure_member(tenant_b, carol, WorkspaceMembership.OWNER)
    mention("keep going", user=BOB, ts="1700001000.000100", thread_ts=result.ts)
    assert Turn.objects.get().chat_session_id == session.id


def test_a_workspace_with_no_slack_link_still_cannot_share(slack, installation, alice):
    lonely = a_workspace("lonely")
    wsvc.ensure_member(lonely, alice, WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=lonely, project="x", created_by=alice, title="x")
    assert share.share_session(alice, channel="C1", summary="hi", session=session).status == share.NOT_INSTALLED


@pytest.mark.django_db(transaction=True)
def test_the_migration_links_each_existing_installation_to_its_workspace():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = [("slack", "0010_agent_declared")], [("slack", "0011_workspace_links")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    owner = old.get_model("auth", "User").objects.create(username="legacy-owner")
    ws = old.get_model("workspaces", "Workspace").objects.create(slug="legacy", display_name="Legacy",
                                                                 created_by=owner)
    old.get_model("slack", "SlackInstallation").objects.create(
        team_id="TOLD", bot_user_id="U", bot_token_enc="x", workspace=ws)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    link = new.get_model("slack", "SlackWorkspaceLink").objects.get()
    assert (link.workspace_id, link.installation.team_id) == ("legacy", "TOLD")

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
