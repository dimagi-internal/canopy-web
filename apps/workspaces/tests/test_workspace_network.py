"""The network view: people, contacts and agents linked by the threads they wrote in.

GET /api/workspaces/{slug}/network reads only the analytics fact log — each
`turn.enqueued` fact names its thread, who asked and the agent that answered
(`activity.who_and_where`) — and shows a chat's title only to someone the session
ACL lets open it.
"""
from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.contacts.models import Contact
from apps.events.models import Activity
from apps.harness import initiator as _initiator
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email, name=""):
    first, _, last = name.partition(" ")
    return User.objects.create(username=email, email=email, first_name=first, last_name=last)


def _get(user, path):
    c = Client()
    c.force_login(user)
    return c.get(path)


@pytest.fixture
def tree():
    ceo, staff, builder = (_user("ceo@dimagi.com", "Cee Oh"), _user("staff@dimagi.com"),
                           _user("b@dimagi.com", "Bea Builder"))
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=ceo)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=ceo, parent=dimagi)
    WorkspaceMembership.objects.create(workspace=dimagi, user=ceo, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=staff, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=connect, user=builder, role=WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=builder)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=ceo)
    return {"ceo": ceo, "staff": staff, "builder": builder, "hal": hal, "eva": eva,
            "dimagi": dimagi, "connect": connect}


def _ask(agent, key, who, *, session=None, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, origin_ref=None, user=None):
    turn, _ = services.enqueue_turn(initiator=who, agent=None if session else agent, session=session,
                                    origin=origin,
                                    origin_ref=origin_ref, idempotency_key=key, prompt="go",
                                    enqueued_by=user)
    return turn


def _person(user):
    return _initiator.for_user(user, via="test", assurance="verified")


def _network(user, slug="dimagi", query=""):
    return _get(user, f"/api/workspaces/{slug}/network?days=7{query}")


@pytest.fixture
def traffic(tree):
    """A shared chat with Hal (Bea and staff both write), Cee's own chat with Eva, an
    email thread a contact started with Hal, and Eva asking Hal in Hal's chat."""
    shared = Session.objects.create(workspace=tree["connect"], agent=tree["hal"],
                                    created_by=tree["builder"], title="Launch plan")
    own = Session.objects.create(workspace=tree["dimagi"], agent=tree["eva"],
                                 created_by=tree["ceo"], title="My week")
    _ask(tree["hal"], "s1", _person(tree["builder"]), session=shared, user=tree["builder"])
    _ask(tree["hal"], "s2", _person(tree["staff"]), session=shared, user=tree["staff"])
    _ask(tree["hal"], "s3", _person(tree["builder"]), session=shared, user=tree["builder"])
    _ask(tree["eva"], "o1", _person(tree["ceo"]), session=own, user=tree["ceo"])
    _ask(tree["hal"], "a1", _initiator.for_agent("eva", via="test"), session=shared)
    contact = Contact.objects.create(workspace=tree["connect"], source="email",
                                     email="pat@partner.org", display_name="Pat Partner")
    _ask(tree["hal"], "m1", _initiator.for_contact(contact, via="email"), origin=Turn.ORIGIN_EMAIL,
         origin_ref={"thread_id": "7a3c0b52-0000-4000-8000-000000000001"})
    _ask(tree["hal"], "sched", _initiator.system(via="test"), origin=Turn.ORIGIN_CANOPY_SCHEDULER)
    return {"shared": shared, "own": own, "contact": contact}


def test_a_turn_fact_names_its_thread_and_who_asked(tree, traffic):
    meta = {t.idempotency_key: Activity.objects.get(kind="turn.enqueued", ref=str(t.pk)).meta
            for t in Turn.objects.all()}
    assert meta["s1"]["thread"] == f"session:{traffic['shared'].pk}"
    # An email lands in its thread's session, like a chat.
    mail = Session.objects.get(pk=Turn.objects.get(idempotency_key="m1").chat_session_id)
    assert meta["m1"]["thread"] == f"session:{mail.pk}"
    assert meta["m1"]["contact"] == traffic["contact"].pk
    assert meta["a1"]["by_agent"] == "eva"
    assert "thread" not in meta["sched"]


def test_the_network_links_writers_to_threads_and_threads_to_their_agent(tree, traffic):
    body = _network(tree["ceo"]).json()
    nodes = {n["id"]: n for n in body["nodes"]}
    links = {(link["source"], link["target"]): link for link in body["links"]}
    shared, own = f"session:{traffic['shared'].pk}", f"session:{traffic['own'].pk}"
    mail = f"session:{Turn.objects.get(idempotency_key='m1').chat_session_id}"
    bea, staff = f"person:{tree['builder'].pk}", f"person:{tree['staff'].pk}"
    pat = f"contact:{traffic['contact'].pk}"

    assert nodes[bea]["label"] == "Bea Builder" and nodes[staff]["label"] == "staff@dimagi.com"
    assert nodes[pat] == {**nodes[pat], "kind": "contact", "label": "Pat Partner", "workspace": "connect"}
    assert links[(bea, shared)]["turns"] == 2 and links[(staff, shared)]["turns"] == 1
    assert links[(pat, mail)]["kind"] == "wrote"
    # Eva wrote in Hal's chat; Hal answered it.
    assert links[("agent:eva", shared)]["kind"] == "wrote"
    assert links[(shared, "agent:hal")]["kind"] == "answered"
    assert nodes["agent:hal"] == {**nodes["agent:hal"], "kind": "agent", "label": "Hal",
                                  "workspace": "connect", "ref": "hal"}
    assert nodes[shared]["channel"] == "chat" and nodes[mail]["channel"] == "email"
    assert links[(f"person:{tree['ceo'].pk}", own)]["turns"] == 1
    assert nodes[mail]["agent"] == "hal"
    # A schedule's turn is in no thread.
    assert sum(1 for n in body["nodes"] if n["kind"] == "thread") == 3
    assert "sched" not in {n["label"] for n in body["nodes"]}
    assert body["totals"] == {"people": 3, "contacts": 1, "agents": 2, "threads": 3,
                              "shared_pairs": 1, "shared_threads": 1, "threads_seen": 3}
    assert body["truncated"] is False


def test_a_thread_title_shows_only_to_someone_who_may_open_it(tree, traffic):
    nodes = {n["id"]: n for n in _network(tree["ceo"]).json()["nodes"]}
    own, shared = nodes[f"session:{traffic['own'].pk}"], nodes[f"session:{traffic['shared'].pk}"]
    assert own["label"] == "My week" and own["openable"] is True
    assert shared["label"] == "Chat with Hal" and shared["openable"] is False


def test_the_network_spans_the_tree_unless_asked_not_to(tree, traffic):
    only_root = _network(tree["ceo"], query="&tree=false").json()
    assert {n["workspace"] for n in only_root["nodes"] if n["kind"] == "thread"} == {"dimagi"}
    assert only_root["workspaces"] == ["dimagi"]


def test_the_network_is_the_admin_tier(tree, traffic):
    assert _network(tree["staff"]).status_code in (403, 404)


def test_facts_recorded_before_threads_were_named_get_them_from_the_turn(tree, traffic):
    for fact in Activity.objects.filter(kind="turn.enqueued"):
        fact.meta = {k: v for k, v in fact.meta.items() if k not in ("thread", "contact", "by_agent")}
        fact.save(update_fields=["meta"])
    import_module("apps.events.migrations.0007_turn_facts_name_their_thread").name_threads(django_apps, None)
    by_key = {t.idempotency_key: str(t.pk) for t in Turn.objects.all()}
    meta = lambda key: Activity.objects.get(kind="turn.enqueued", ref=by_key[key]).meta  # noqa: E731
    assert meta("s2")["thread"] == f"session:{traffic['shared'].pk}"
    assert meta("m1")["contact"] == traffic["contact"].pk
    assert meta("a1")["by_agent"] == "eva"
