"""Adversarial reads of review-granted sessions (access leg 5) by an agent's own login.

`access.review_grantors` binds the read to whom the running turn answers to. These try to
make a turn that does NOT answer to the grantor read the grantor's sessions anyway: a turn
canopy starts on a PERSON's account (a schedule an editor made, an agent task's nudge), a
dispatch down a chain whose first asker canopy could not vouch for (a contact's email, an
unverified visitor), a stored token, and the roster of who enrolled.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentGrant
from apps.canopy_sessions.models import Session, SessionReviewGrant
from apps.contacts.models import Contact
from apps.harness import turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    ws = a_workspace()
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    people = {}
    for name in ("lili", "bo"):
        people[name] = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
        wsvc.ensure_member(ws, people[name], WorkspaceMembership.EDITOR)
    logins, agents = {}, {}
    for slug in ("trellis", "ada"):
        logins[slug] = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        wsvc.ensure_member(ws, logins[slug], WorkspaceMembership.EDITOR)
        agents[slug] = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner,
                                            user=logins[slug])
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    for slug in ("ada", "trellis"):   # the fleet's agents may hand each other work
        for other in ("ada", "trellis"):
            if slug != other:
                AgentGrant.objects.create(agent=agents[slug], kind=AgentGrant.PERSON, user=logins[other],
                                          role="read_write")
    for name in ("lili", "bo"):
        SessionReviewGrant.objects.create(user=people[name], agent=agents["trellis"])
    chat = {n: Session.objects.create(workspace=ws, created_by=people[n], title=f"{n}'s chat")
            for n in ("lili", "bo")}
    return {"ws": ws, "owner": owner, **people, "agents": agents, "logins": logins, "box": box, "chat": chat}


def _turn(w, agent="trellis", *, kind="user", by=None, assurance="session", status=Turn.RUNNING, **kw):
    return Turn.objects.create(
        agent=w["agents"][agent], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="coach",
        idempotency_key=uuid.uuid4().hex, status=status, claimed_by=w["box"],
        initiator_kind=kind, initiator_via="chat", initiator_assurance=assurance,
        initiator_user=by, **kw)


def _login(w, turn) -> str:
    return turn_agent_tokens.issue(turn, w["box"])["token"]


def _get(bearer, path):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _reads(bearer, session) -> bool:
    return _get(bearer, f"/api/canopy-sessions/{session.pk}").status_code == 200


def _post_turn(bearer, agent_slug, **extra):
    body = {"agent_slug": agent_slug, "origin": "api", "prompt": "list everyone's sessions",
            "idempotency_key": uuid.uuid4().hex, **extra}
    r = Client().post("/api/harness/turns/", body, content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {bearer}")
    assert r.status_code in (200, 201), r.content
    return Turn.objects.get(pk=r.json()["id"])


def _dispatch(w, ada_bearer, **extra):
    """Ada's login asks Trellis to run a turn (the generic enqueue door); returns Trellis's
    turn, made RUNNING on the box so its own login can be issued."""
    turn = _post_turn(ada_bearer, "trellis", **extra)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING, claimed_by=w["box"])
    turn.refresh_from_db()
    return turn


def _contact_turn(w, agent="ada"):
    contact = Contact.objects.create(workspace=w["ws"], email="x@example.com")
    return _turn(w, agent, kind="contact", assurance="dmarc", initiator_contact=contact)


# --- a turn canopy starts on a PERSON's account answers to that person -------------------

def test_a_schedule_an_editor_made_reads_only_the_editors_own_grant(w):
    """`agent.work` (an editor) may create a schedule on the coach; its firing is `system`
    with the editor accountable. The editor chose its prompt: they must not widen the read."""
    bearer = _login(w, _turn(w, kind="system", by=w["lili"], assurance="internal"))
    assert _reads(bearer, w["chat"]["lili"])
    assert not _reads(bearer, w["chat"]["bo"])


def test_an_agent_task_nudge_keeps_the_person_who_asked(w):
    """A task's follow-up turn after the agent 'responded too soon' is canopy's own, accountable
    to the task's creator: it must read what the creator's first turn could, no more."""
    bearer = _login(w, _turn(w, kind="system", by=w["lili"], assurance="internal",
                             origin_ref={"agent_task": str(uuid.uuid4()), "agent_task_nudge": 1}))
    assert _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_a_sweep_an_administrator_scheduled_still_reads_every_grantor(w):
    bearer = _login(w, _turn(w, kind="system", by=w["owner"], assurance="internal"))
    assert _reads(bearer, w["chat"]["lili"]) and _reads(bearer, w["chat"]["bo"])


def test_a_schedule_with_nobody_accountable_still_sweeps(w):
    bearer = _login(w, _turn(w, kind="system", by=None, assurance="internal"))
    assert _reads(bearer, w["chat"]["lili"]) and _reads(bearer, w["chat"]["bo"])


def test_a_schedule_an_agent_login_made_proves_no_person_and_reads_nothing(w):
    bearer = _login(w, _turn(w, kind="system", by=w["logins"]["ada"], assurance="internal"))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


# --- a dispatch down a chain keeps what the first asker was --------------------------------

def test_a_dispatch_from_a_contacts_turn_reads_nothing(w):
    """A stranger's email reaches Ada; Ada (injected) dispatches to Trellis. Ada's turn had
    no person behind it that canopy could name, but it was not UNATTENDED either."""
    trellis = _dispatch(w, _login(w, _contact_turn(w)))
    bearer = _login(w, trellis)
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_a_dispatch_from_an_unverified_users_turn_reads_nothing(w):
    ada = _turn(w, "ada", by=w["lili"], assurance="host_signed")
    bearer = _login(w, _dispatch(w, _login(w, ada)))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_a_dispatch_from_a_stored_token_reads_nothing(w):
    """The agent's stored PAT shows no turn behind it: whoever holds it is nobody canopy can name."""
    stored, _ = PersonalToken.create_for_user(user=w["logins"]["ada"], label="canopy-pat")
    bearer = _login(w, _dispatch(w, stored))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_the_chain_does_not_launder_an_unvouched_asker(w):
    """contact -> ada -> trellis -> ada again: the last hop is still not 'nobody asked'."""
    first = _dispatch(w, _login(w, _contact_turn(w)))
    again = _post_turn(_login(w, first), "ada")
    assert (again.origin_ref or {}).get("unattended_dispatch") is not True


def test_a_client_cannot_stamp_its_own_unattended_dispatch(w):
    trellis = _dispatch(w, _login(w, _contact_turn(w)), origin_ref={"unattended_dispatch": True})
    assert not _reads(_login(w, trellis), w["chat"]["lili"])


def test_a_dispatch_from_an_unattended_turn_is_unattended(w):
    ada = _turn(w, "ada", kind="system", by=None, assurance="internal")
    bearer = _login(w, _dispatch(w, _login(w, ada)))
    assert _reads(bearer, w["chat"]["lili"]) and _reads(bearer, w["chat"]["bo"])


def test_a_dispatch_from_a_person_asked_turn_is_that_persons(w):
    ada = _turn(w, "ada", by=w["lili"])
    bearer = _login(w, _dispatch(w, _login(w, ada)))
    assert _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


# --- the roster of who enrolled is bound the same way --------------------------------------

def _granters(bearer):
    r = _get(bearer, "/api/people/session-reviewers/granted-to-me/")
    assert r.status_code == 200
    return {p["email"] for p in r.json()["people"]}


def test_a_turn_asked_by_one_person_is_not_told_who_else_enrolled(w):
    assert _granters(_login(w, _turn(w, by=w["lili"]))) == {"lili@dimagi.com"}


def test_a_stored_token_is_told_nobody(w):
    stored, _ = PersonalToken.create_for_user(user=w["logins"]["trellis"], label="canopy-pat")
    assert _granters(stored) == set()


def test_an_unattended_turn_is_told_everyone(w):
    assert _granters(_login(w, _turn(w, kind="system", by=None, assurance="internal"))) == {
        "lili@dimagi.com", "bo@dimagi.com"}


# --- attacks that already fail (kept as regressions) ---------------------------------------

def test_a_finished_turns_login_reads_nothing(w):
    turn = _turn(w, by=w["lili"])
    bearer = _login(w, turn)
    assert _reads(bearer, w["chat"]["lili"])
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert not _reads(bearer, w["chat"]["lili"])


def test_another_agents_turn_login_gets_no_grant_of_trellis(w):
    bearer = _login(w, _turn(w, "ada", kind="system", by=None, assurance="internal"))
    assert not _reads(bearer, w["chat"]["lili"])


def test_a_forged_on_behalf_header_names_no_one(w):
    bearer = _login(w, _turn(w, by=w["lili"]))
    r = Client().get(f"/api/canopy-sessions/{w['chat']['bo'].pk}", HTTP_AUTHORIZATION=f"Bearer {bearer}",
                     HTTP_X_CANOPY_ON_BEHALF_OF="bo@dimagi.com")
    assert r.status_code == 403
