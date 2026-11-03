"""The one consent read a turn's caller token reaches: `GET /api/people/me/session-reviewers/`.

A confined or read-only agent turn speaks for its verified human asker with a caller
token. It must be able to ask "has my asker let me review their sessions?" — and see
only that: whether THIS agent is among the asker's reviewers. Changing consent
(POST/DELETE) stays the person's own.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionReviewGrant
from apps.contacts import services as contacts
from apps.contacts import session_reviewers
from apps.harness import caller_tokens
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.token_helpers import bearer_headers

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
URL = "/api/people/me/session-reviewers/"


@pytest.fixture()
def w():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    M.objects.create(user=jj, workspace=ws, role=M.OWNER)
    M.objects.create(user=lili, workspace=ws, role=M.EDITOR)
    agents = {}
    for slug in ("echo", "hal"):
        a = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=jj)
        a.user = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        a.save(update_fields=["user"])
        M.objects.create(user=a.user, workspace=ws, role=M.EDITOR)
        agents[slug] = a
    return {"jj": jj, "lili": lili, "ws": ws, **agents}


def _turn(w, user, agent=None, *, assurance=who.SESSION, session=None):
    session = session or Session.objects.create(workspace=w["ws"], agent=agent, created_by=user,
                                                title="chat", project="" if agent else "canopy-web")
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hi",
                               idempotency_key=uuid.uuid4().hex, initiator_kind=who.USER,
                               initiator_user=user, initiator_assurance=assurance)


def _enrol(user, *agents):
    person = contacts.person_for(user=user)
    for a in agents:
        session_reviewers.grant(person, a, user=user)


def _token(turn):
    return bearer_headers(caller_tokens.mint(turn, scoped=True))


def test_the_asker_sees_only_whether_this_agent_is_among_their_reviewers(client, w):
    headers = _token(_turn(w, w["lili"], w["echo"]))
    body = client.get(URL, **headers).json()
    assert body["reviewers"] == [] and body["agents"] == [] and body["coach"] is None
    _enrol(w["lili"], w["echo"], w["hal"])
    body = client.get(URL, **headers).json()
    assert [r["agent"] for r in body["reviewers"]] == ["echo"]    # hal is not echo's business
    assert body["agents"] == [] and body["coach"] is None


def test_it_is_the_askers_own_list_never_someone_elses(client, w):
    _enrol(w["jj"], w["echo"])                                    # jj enrolled; lili did not
    assert client.get(URL, **_token(_turn(w, w["lili"], w["echo"]))).json()["reviewers"] == []
    assert [r["agent"] for r in client.get(URL, **_token(_turn(w, w["jj"], w["echo"]))).json()["reviewers"]] == ["echo"]


def test_a_caller_token_cannot_change_consent(client, w):
    headers = _token(_turn(w, w["lili"], w["echo"]))
    assert client.post(URL, data={"agent": "echo"}, content_type="application/json", **headers).status_code == 403
    assert client.delete(URL + "echo/", **headers).status_code == 403
    repo = _token(_turn(w, w["jj"]))                              # a repo chat: not read-only
    assert client.post(URL, data={"agent": "echo"}, content_type="application/json", **repo).status_code == 403
    assert client.get(URL, **repo).status_code == 403             # no agent to answer for
    assert not SessionReviewGrant.objects.exists()


def test_an_unverified_asker_is_refused(client, w):
    turn = _turn(w, w["lili"], w["echo"])
    headers = _token(turn)
    _turn(w, w["lili"], assurance=who.UNKNOWN, session=turn.chat_session)
    assert client.get(URL, **headers).status_code in (401, 403)


def test_the_person_keeps_the_full_view(client, w):
    _enrol(w["lili"], w["echo"])
    client.force_login(w["lili"])
    body = client.get(URL).json()
    assert [r["agent"] for r in body["reviewers"]] == ["echo"]
    assert [a["slug"] for a in body["agents"]] == ["hal"]
