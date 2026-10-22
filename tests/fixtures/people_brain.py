"""Builders for the people brain's world: members, agents with their own logins, human
turns and facts. Plain helpers, shared by the people-brain, proposals, ideas and
contributor-page suites.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import PersonFact
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership


def _member(ws, username, role=WorkspaceMembership.EDITOR, **kw):
    u = User.objects.create_user(username, f"{username}@dimagi.com", "pw", **kw)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    return u


def _agent(ws, slug, owner, *, login=True):
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
    if login:
        u = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
        agent.user = u
        agent.save(update_fields=["user"])
    return agent


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _human_turn(agent, user, key, **kw):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=kw.pop("prompt", "hi"),
        initiator=who.for_user(user, via="chat", assurance=who.SESSION), **kw)
    return turn


def _finish(turn, status=Turn.DONE):
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    return services.finish_turn(turn, status=status)


def _fact(person, ws, kind="role", statement="Program lead for KC.", **kw):
    if kw.get("by_agent") is not None and "source_turn" not in kw:
        # An agent's write names the turn it comes from (hcp.refuse_if_zdr).
        kw["source_turn"] = _human_turn(kw["by_agent"], person.user,
                                        f"src-{PersonFact.objects.count()}-{statement[:20]}")
    return people.record_fact(person=person, workspace=ws, kind=kind, statement=statement, **kw)


@pytest.fixture()
def proposals_world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    editor = _member(ws, "ed", role=WorkspaceMembership.EDITOR, first_name="Ed")
    viewer = _member(ws, "sophie", role=WorkspaceMembership.VIEWER, first_name="Sophie")
    ada, eva, echo = (_agent(ws, s, owner) for s in ("ada", "eva", "echo"))
    for u in (owner, editor, viewer):
        contacts.person_for(user=u)
    return {"owner": owner, "ws": ws, "editor": editor, "viewer": viewer,
            "ada": ada, "eva": eva, "echo": echo}
