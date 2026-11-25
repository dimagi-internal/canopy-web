"""Asking for a finding's fix: the plan says exactly what happens, and asking does exactly that (`brain.fixes`)."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.brain.models import BrainOutput
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from tests.fixtures.brain_world import brain_world, member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

BODY = {"severity": "high", "what_happened": "Cards keep coming back.", "suggested_fix": "Close the card's asks.",
        "success_measure": "No closed card reappears.", "fix_belongs": {"repo": "canopy-web", "skill": "tasks"},
        "evidence": [{"source": 1, "index": 3, "quote": "it kept surfacing"}]}


@pytest.fixture()
def w():
    out = brain_world()
    login = user("ada-login")
    member(login, out["ws"])
    out["ada"] = Agent.objects.create(slug="ada", name="Ada", workspace=out["ws"], owner=out["owner"], user=login)
    out["sys"].operator = out["ada"]
    out["sys"].save(update_fields=["operator"])
    out["finding"] = BrainOutput.objects.create(capability=out["sys"], kind="finding", workspace=out["ws"],
                                                subject_type="system", subject="acme", title="Settled cards resurface",
                                                body=BODY)
    return out


def _as(u):
    c = Client()
    c.force_login(u)
    return c


def _url(w, tail=""):
    return f"/api/w/acme/brain/outputs/{w['finding'].pk}/fix{tail}"


def test_the_plan_says_who_is_asked_what_happens_and_the_brief_word_for_word(w):
    r = _as(w["owner"]).get(_url(w))
    assert r.status_code == 200
    plan = r.json()
    assert plan["agent"] == {"slug": "ada", "name": "Ada"} and plan["label"] == "Ask Ada to fix this"
    assert "Starts a conversation between you and Ada" in plan["what_happens"]
    assert "in canopy-web · tasks" in plan["what_happens"] and "marks this finding done" in plan["what_happens"]
    assert f"finding {w['finding'].pk}" in plan["brief"] and "set_output_status" in plan["brief"]
    assert "Close the card's asks." in plan["brief"] and "it kept surfacing" not in plan["brief"]
    assert not Session.objects.filter(agent=w["ada"]).exists()                 # the plan changes nothing


def test_asking_starts_the_persons_own_conversation_with_the_brief_and_marks_it_fixing(w):
    r = _as(w["owner"]).post(_url(w), content_type="application/json")
    assert r.status_code == 200, r.content
    got = r.json()
    session = Session.objects.get(pk=got["session_id"])
    assert session.created_by == w["owner"] and session.agent == w["ada"]
    assert session.metadata["brain_fix"] == str(w["finding"].pk)
    turn = Turn.objects.get(chat_session=session)
    assert turn.initiator_user == w["owner"] and "set_output_status" in turn.prompt
    assert got["output"]["status"] == "accepted" and got["output"]["fix_session"] == f"/w/acme/sessions/{session.pk}"
    listed = {o["id"]: o for o in _as(w["owner"]).get("/api/w/acme/brain/outputs/").json()}
    assert listed[w["finding"].pk]["fix_session"] == f"/w/acme/sessions/{session.pk}"
    assert listed[w["finding"].pk]["fixer"] == {"slug": "ada", "name": "Ada"}
    assert listed[w["finding"].pk]["may_request_fix"] is True
    again = _as(w["owner"]).post(_url(w), content_type="application/json").json()
    assert again["session_id"] == str(session.pk)                               # asking again continues it
    assert "Continues your conversation with Ada" in _as(w["owner"]).get(_url(w)).json()["what_happens"]


def test_only_the_people_who_act_for_it_ask_and_never_through_a_token(w):
    assert _as(w["alice"]).get(_url(w)).status_code == 404                     # a system finding is not hers to see
    assert _as(w["alice"]).post(_url(w), content_type="application/json").status_code == 404
    agent_pat = pat_token(w["ada"].user)
    r = Client().post(_url(w), content_type="application/json", **bearer_headers(agent_pat))
    assert r.status_code == 403
    assert not Session.objects.filter(agent=w["ada"]).exists()


def test_a_learner_with_no_operator_has_nobody_to_ask(w):
    w["sys"].operator = None
    w["sys"].save(update_fields=["operator"])
    assert _as(w["owner"]).get("/api/w/acme/brain/outputs/").json()[0]["may_request_fix"] is False
    plan = _as(w["owner"]).get(_url(w)).json()
    assert plan["agent"] is None and plan["label"] == ""
    r = _as(w["owner"]).post(_url(w), content_type="application/json")
    assert r.status_code == 422
