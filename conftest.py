"""Shared test fixtures — currently just the tenancy baseline.

`Agent.workspace` is NOT NULL as of `agents/0013`, so creating an Agent now
requires a Workspace to home it in, and a Workspace requires a `created_by`
User. Every real deployment satisfies both by construction (you cannot reach
`POST /api/agents/` without being logged in, and the login is what makes the
default workspace exist), but an in-memory test DB starts with neither — which
is why `wsvc.ensure_default_workspace()` returns `None` in a bare test and why
a fixture that passed its result straight to `Agent.objects.create` used to
quietly produce the very unhomed agent the constraint exists to forbid.

Depend on `default_workspace` from any fixture that needs a tenant. Fixtures
that already call `wsvc.ensure_default_workspace()` only need to take it as a
parameter — with a user present, that call starts returning a real workspace.
Plain (non-fixture) helper functions call `apps.workspaces.testing.a_workspace()`
directly instead; both go through the same code.

Deliberately NOT autouse: tests that assert on the empty-database case (the
workspace-backfill migration tests, `ensure_default_workspace()` returning
None) must keep seeing an empty database.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def default_workspace(db):
    """The default (`dimagi`) workspace, created the way the app creates it.

    Returned as the Workspace instance; `wsvc.ensure_default_workspace()` finds
    the same row, so a fixture can keep calling that and simply depend on this.
    """
    from apps.workspaces.testing import a_workspace

    return a_workspace()


@pytest.fixture(autouse=True)
def _no_live_dns(monkeypatch):
    """No test reaches a real DNS resolver. The interface's mail-domain warning
    (apps/contacts/domain_proof.py) asks `_dmarc.<domain>` on every interface
    read; here every lookup answers "unknown", which raises no warning. Tests of
    that warning patch `_lookup_dmarc` themselves."""
    try:
        from apps.contacts import domain_proof
    except Exception:  # a suite with no Django (runner/*) has nothing to patch
        return
    monkeypatch.setattr(domain_proof, "_lookup_dmarc", lambda domain: "unknown")


@pytest.fixture()
def agent_memory_on():
    """Every Person created while this is active starts with BOTH agent-memory
    features available and ON by default (`Person.hcp_*_available` / `_default`;
    the real default is off).
    For suites that pin what the people brain / HCP do for a person who has
    turned them on."""
    from django.db.models.signals import pre_save

    from apps.contacts.models import Person

    def _on(sender, instance, **kwargs):
        if instance._state.adding:
            instance.hcp_record_available = instance.hcp_record_default = True
            instance.hcp_use_available = instance.hcp_use_default = True

    pre_save.connect(_on, sender=Person, dispatch_uid="test-agent-memory-on")
    yield
    pre_save.disconnect(sender=Person, dispatch_uid="test-agent-memory-on")


@pytest.fixture()
def grants_on_turn():
    """As if every person had already granted ("always") each agent they start a
    turn with, for whatever they have made available at that moment — through the
    real act (`hcp.issue_agent_grant`), so the grant rows and `grant.issued` events
    are the real ones. For suites that pin what a GRANTED agent does; how a grant
    comes to exist is pinned in tests/test_hcp_agent_grants.py."""
    from django.db.models.signals import post_save

    from apps.contacts import hcp, people
    from apps.harness.models import Turn

    def _grant(sender, instance, created, **kwargs):
        if not created:
            return
        agent = (instance.agent if instance.agent_id else
                 instance.chat_session.agent if instance.chat_session_id else None)
        person = people.initiator_person(instance) if agent is not None else None
        if person is None:
            return
        features = [f for f in hcp.FEATURES if getattr(person, f"hcp_{f}_available")]
        have = hcp.granted(person, agent)
        if any(not have[f] for f in features):
            actor = (hcp.user_actor(person.user) if person.user_id
                     else hcp.Actor(f"person:{person.pk}", "user"))
            hcp._record_grant(person, agent=agent, features=features,
                              gtype="persistent", actor=actor, surface="chat")

    post_save.connect(_grant, sender=Turn, dispatch_uid="test-grants-on-turn")
    yield
    post_save.disconnect(sender=Turn, dispatch_uid="test-grants-on-turn")


@pytest.fixture()
def agents_granted(agent_memory_on, grants_on_turn):
    """Agent memory on for every person, and every agent they talk to granted."""
    yield
