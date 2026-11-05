"""An agent's review-grant reads (access leg 5) answer to the turn's asker.

Trellis's login may read the sessions of people who let it review them, but only inside
a running turn of Trellis, and only as far as that turn's asker goes
(`access.review_grantors`, the asker taken from the server's `Turn.initiator_*`):

* a person asked  -> that person's sessions alone ("coach me" is me only);
* nobody asked (schedule, trigger, canopy or another agent started it) -> the roster and no
  session, unless it was dispatched for one grantor (tests/test_weekly_pass_one_grantor.py);
* an administrator of the agent asked -> every grantor's (they run it for all it serves);
* a contact or an unverified user asked, a stored token, a session sign-in, a finished
  or confined turn -> nobody's.
Every door is the one ACL, so REST (by id, list, search, messages) and MCP agree.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session, SessionReviewGrant
from apps.contacts.models import Contact
from apps.harness import turn_agent_tokens
from apps.harness.models import Runner, Turn, TurnAgentToken
from apps.mcp import api_tools
from apps.tokens.middleware import BearerTokenAuthMiddleware
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
    for name in ("lili", "bo", "col"):
        people[name] = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
        wsvc.ensure_member(ws, people[name], WorkspaceMembership.EDITOR)
    login = User.objects.create_user("trellis-login", "trellis@dimagi-ai.com", "pw")
    wsvc.ensure_member(ws, login, WorkspaceMembership.EDITOR)
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=owner, user=login)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    for name in ("lili", "bo"):
        SessionReviewGrant.objects.create(user=people[name], agent=trellis)
    sessions = {name: Session.objects.create(workspace=ws, created_by=people[name], title=f"{name}'s chat")
                for name in ("lili", "bo", "col")}
    sealed = Session.objects.create(workspace=ws, created_by=people["lili"], title="",
                                    metadata={"runner_requirements": ["sensitive_data"]})
    return {"ws": ws, "owner": owner, **people, "trellis": trellis, "box": box,
            "chat": sessions, "sealed": sealed}


def _turn(w, *, kind="user", by=None, assurance="session", status=Turn.RUNNING, **kw):
    return Turn.objects.create(
        agent=w["trellis"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="coach",
        idempotency_key=uuid.uuid4().hex, status=status, claimed_by=w["box"],
        initiator_kind=kind, initiator_via="chat", initiator_assurance=assurance,
        initiator_user=by, **kw)


def _login(w, turn) -> str:
    return turn_agent_tokens.issue(turn, w["box"])["token"]


def _get(bearer, path, **headers):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {bearer}", **headers)


def _reads(bearer, session) -> bool:
    return _get(bearer, f"/api/canopy-sessions/{session.pk}").status_code == 200


def _asked_by(w, who):
    return _login(w, _turn(w, by=w[who]))


def _another_agent(w):
    other = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    Agent.objects.create(slug="ada", name="Ada", workspace=w["ws"], owner=w["owner"], user=other)
    return other


def test_a_person_asking_reads_only_their_own_sessions(w):
    bearer = _asked_by(w, "lili")
    assert _reads(bearer, w["chat"]["lili"])
    assert not _reads(bearer, w["chat"]["bo"])           # bo granted trellis, but did not ask
    assert not _reads(bearer, w["chat"]["col"])          # never granted anyone


def test_every_door_agrees_for_a_person_asking(w):
    bearer = _asked_by(w, "lili")
    mine, theirs = str(w["chat"]["lili"].pk), str(w["chat"]["bo"].pk)
    assert _get(bearer, f"/api/canopy-sessions/{mine}/messages?before=100").status_code == 200
    assert _get(bearer, f"/api/canopy-sessions/{theirs}/messages?before=100").status_code == 404
    listed = {s["id"] for s in _get(bearer, f"/api/canopy-sessions/?of_user={w['lili'].pk}").json()}
    assert listed == {mine}
    searched = {s["id"] for s in _get(bearer, "/api/canopy-sessions/search").json()["sessions"]}
    assert theirs not in searched and mine in searched


def test_a_forged_of_user_shows_nothing(w):
    """lili's turn names bo in the filter: the filter only narrows what the ACL allows."""
    bearer = _asked_by(w, "lili")
    assert _get(bearer, f"/api/canopy-sessions/?of_user={w['bo'].pk}").json() == []
    ids = {s["id"] for s in _get(bearer, "/api/canopy-sessions/").json()}
    assert str(w["chat"]["bo"].pk) not in ids


def test_a_forged_on_behalf_header_is_refused(w):
    bearer = _asked_by(w, "lili")
    r = _get(bearer, f"/api/canopy-sessions/{w['chat']['bo'].pk}", HTTP_X_CANOPY_ON_BEHALF_OF="bo@dimagi.com")
    assert r.status_code == 403


def test_a_schedule_its_owner_made_answers_to_them_as_if_they_asked(w):
    """Canopy started it on the agent's admin's account: every grantor's, as they would read."""
    bearer = _login(w, _turn(w, kind="system", by=w["owner"], assurance="internal"))
    assert _reads(bearer, w["chat"]["lili"]) and _reads(bearer, w["chat"]["bo"])
    assert not _reads(bearer, w["chat"]["col"])          # not a grantor
    assert not _reads(bearer, w["sealed"])               # sensitive: never


def test_an_agent_dispatch_from_a_turn_nobody_asked_reads_no_one_unless_it_names_one(w):
    """The server stamps `unattended_dispatch` only when the dispatching turn was itself
    nobody-asked (`turn_asker.dispatch_stamp`); tests/test_adversarial_agent_reads.py."""
    other = _another_agent(w)
    bearer = _login(w, _turn(w, kind="user", by=other, assurance="pat",
                             origin_ref={"unattended_dispatch": True}))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])
    bearer = _login(w, _turn(w, kind="user", by=other, assurance="pat",
                             origin_ref={"unattended_dispatch": True, "reads_for": {"user_id": w["lili"].pk}}))
    assert _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_an_agent_dispatch_the_server_did_not_stamp_reads_nothing(w):
    bearer = _login(w, _turn(w, kind="user", by=_another_agent(w), assurance="pat"))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_a_dispatch_for_a_person_keeps_that_person(w):
    """An agent started the turn on lili's behalf (stamped by the server): lili's only."""
    turn = _turn(w, kind="user", by=_another_agent(w), assurance="pat",
                 origin_ref={"on_behalf_of": {"user_id": w["lili"].pk}})
    bearer = _login(w, turn)
    assert _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_the_agents_administrator_asking_reads_every_grantor(w):
    bearer = _asked_by(w, "owner")
    assert _reads(bearer, w["chat"]["lili"]) and _reads(bearer, w["chat"]["bo"])


def test_a_member_who_granted_nothing_reads_nothing(w):
    bearer = _asked_by(w, "col")
    assert not any(_reads(bearer, s) for s in w["chat"].values())


def test_a_contact_asking_reads_nothing(w):
    contact = Contact.objects.create(workspace=w["ws"], email="x@example.com")
    bearer = _login(w, _turn(w, kind="contact", assurance="dmarc", initiator_contact=contact))
    assert not _reads(bearer, w["chat"]["lili"]) and not _reads(bearer, w["chat"]["bo"])


def test_an_unverified_user_asking_reads_nothing(w):
    bearer = _login(w, _turn(w, by=w["lili"], assurance="host_signed"))
    assert not _reads(bearer, w["chat"]["lili"])


def test_a_sensitive_session_is_never_read_even_by_its_person(w):
    bearer = _asked_by(w, "lili")
    assert not _reads(bearer, w["sealed"])


def test_outside_a_running_turn_the_agents_login_reads_nothing_by_grant(w):
    """A stored token, a browser session, a finished or a confined turn cannot show whom
    it is answering to."""
    stored, _ = PersonalToken.create_for_user(user=w["trellis"].user, label="canopy-pat")
    assert not _reads(stored, w["chat"]["lili"])
    c = Client()
    c.force_login(w["trellis"].user)
    assert c.get(f"/api/canopy-sessions/{w['chat']['lili'].pk}").status_code == 404

    done = _turn(w, by=w["lili"])
    bearer = _login(w, done)
    assert _reads(bearer, w["chat"]["lili"])
    Turn.objects.filter(pk=done.pk).update(status=Turn.DONE)
    assert not _reads(bearer, w["chat"]["lili"])

    confined = _turn(w, by=w["lili"])
    bearer = _login(w, confined)
    Turn.objects.filter(pk=confined.pk).update(capability="ask")
    assert not _reads(bearer, w["chat"]["lili"])


def test_the_grantor_and_everyone_else_are_unchanged(w):
    lili = Client()
    lili.force_login(w["lili"])
    assert lili.get(f"/api/canopy-sessions/{w['chat']['lili'].pk}").status_code == 200
    assert lili.get(f"/api/canopy-sessions/{w['chat']['bo'].pk}").status_code == 404
    assert access.can_read(w["bo"], w["chat"]["bo"])
    assert not access.can_read(w["owner"], w["chat"]["bo"])


def test_a_revoked_grant_ends_it_inside_the_turn(w):
    bearer = _asked_by(w, "lili")
    assert _reads(bearer, w["chat"]["lili"])
    SessionReviewGrant.objects.filter(user=w["lili"]).update(revoked_at=timezone.now())
    assert not _reads(bearer, w["chat"]["lili"])


def _mcp_user(w, token_id):
    request = RequestFactory().get("/api/me/")
    request.scope = {api_tools.MCP_PRINCIPAL_SCOPE_KEY: {
        "user_id": w["trellis"].user.pk, "auth_method": "pat",
        "credential": {"type": "pat", "id": token_id, "label": ""}}}
    request.user = AnonymousUser()
    BearerTokenAuthMiddleware._authenticate(request)
    return request.user


def test_mcp_is_the_same_rule(w):
    asked = _turn(w, by=w["lili"])
    turn_agent_tokens.issue(asked, w["box"])
    user = _mcp_user(w, TurnAgentToken.objects.get(turn=asked).token_id)
    assert access.can_read(user, w["chat"]["lili"]) and not access.can_read(user, w["chat"]["bo"])
    _, row = PersonalToken.create_for_user(user=w["trellis"].user, label="canopy-pat")
    assert not access.can_read(_mcp_user(w, row.pk), w["chat"]["lili"])
