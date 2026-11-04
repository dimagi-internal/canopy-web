"""A connection lent with limits: it ends by itself, and only its owner's turns get it.

The route-level half (what a box is handed on a turn) is in `test_connections_tokens.py`,
the browser legs in `test_connections_views.py`."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.connections import keeper, services
from apps.connections.models import Connection
from apps.harness import initiator as who
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import _stored, in_force, sf_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def gina():
    return get_user_model().objects.create_user(username="gina", email="gina@example.com")


@pytest.fixture
def other():
    return get_user_model().objects.create_user(username="ellis", email="ellis@example.com")


@pytest.fixture
def bailey(gina, other):
    ws = Workspace.objects.create(slug="family", display_name="Family", created_by=gina)
    for person in (gina, other):
        WorkspaceMembership.objects.create(workspace=ws, user=person, role=WorkspaceMembership.EDITOR)
    sf_app(ws)
    agent = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=gina)
    AgentAdmin.objects.create(agent=agent, user=other)
    return agent


def _lent(person, agent, **fields) -> Connection:
    return in_force(_stored({"user": person}, person, app=agent.workspace.connection_apps.get(),
                            account_id=f"acct-{person.pk}", **fields), agent, by=person)


def _turn(user, kind=who.USER, via="chat", assurance=who.SESSION):
    return SimpleNamespace(initiator_user_id=user.pk if user else None, initiator_kind=kind, initiator_via=via,
                           initiator_assurance=assurance)


def test_an_ordinary_lend_is_any_turns_with_no_end(gina, other, bailey):
    conn = _lent(gina, bailey)
    assert services.connection_for_agent(bailey, "salesforce", turn=_turn(other)) == conn
    assert services.connection_for_agent(bailey, "salesforce") == conn


def test_a_lend_stops_the_moment_its_time_runs_out(gina, bailey):
    conn = _lent(gina, bailey, expires_at=timezone.now() + timedelta(minutes=5))
    assert services.connection_for_agent(bailey, "salesforce") == conn
    Connection.objects.filter(pk=conn.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert services.providers_for_agent(bailey) == []
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(bailey, "salesforce")


@pytest.mark.parametrize(("make", "allowed"), [
    (lambda g, o: _turn(g), True),
    (lambda g, o: _turn(g, assurance=who.PAT), True),
    (lambda g, o: _turn(g, kind=who.SYSTEM, via="agent-task", assurance=who.INTERNAL), True),  # a task's follow-up
    (lambda g, o: _turn(o), False),                                                            # someone else
    (lambda g, o: _turn(None, kind=who.CONTACT, assurance="dkim"), False),                     # a stranger's email
    (lambda g, o: _turn(g, assurance="dkim"), False),                                          # mail claiming to be her
    (lambda g, o: _turn(g, kind=who.SYSTEM, via="schedule", assurance=who.INTERNAL), False),   # what she set up
    (lambda g, o: None, False),                                                                # no turn at all
])
def test_kept_for_its_owner_only_their_own_asking_gets_it(gina, other, bailey, make, allowed):
    conn = _lent(gina, bailey, owner_turns_only=True)
    turn = make(gina, other)
    assert services.providers_for_agent(bailey, turn=turn) == (["salesforce"] if allowed else [])
    if allowed:
        assert services.connection_for_agent(bailey, "salesforce", turn=turn) == conn
    else:
        with pytest.raises(services.NotConnected):
            services.connection_for_agent(bailey, "salesforce", turn=turn)


def test_two_people_lending_for_their_own_turns_are_not_ambiguous(gina, other, bailey):
    hers = _lent(gina, bailey, owner_turns_only=True)
    theirs = _lent(other, bailey, owner_turns_only=True)
    assert services.connection_for_agent(bailey, "salesforce", turn=_turn(gina)) == hers
    assert services.connection_for_agent(bailey, "salesforce", turn=_turn(other)) == theirs


def test_the_sweep_disconnects_what_has_run_out_and_nothing_else(gina, other, bailey):
    gone = _lent(gina, bailey, expires_at=timezone.now() - timedelta(minutes=1))
    kept = _lent(other, bailey, expires_at=timezone.now() + timedelta(hours=1))
    assert keeper.sweep()["lapsed"] == 1
    assert not Connection.objects.filter(pk=gone.pk).exists()  # no box holds it here, so deleted at once
    assert Connection.objects.filter(pk=kept.pk).exists()
    assert keeper.sweep()["lapsed"] == 0


def test_a_person_s_own_computer_stops_too(gina, bailey):
    _lent(gina, bailey, expires_at=timezone.now() - timedelta(minutes=1))
    assert services.person_connections(gina, "salesforce") == []
