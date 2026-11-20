"""`/api/me/learning/`: the person's own agreement. Every non-person credential is refused, no route
is an MCP tool, and coaching's opt-in and this consent stay the same thing."""
from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.brain import consent
from apps.brain.models import BrainSource, LearningConsent, SourceExclusion
from apps.canopy_sessions.models import CoachingOptIn
from apps.contacts.models import PersonAuditEvent
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey, TurnAgentToken
from apps.mcp.api_tools import excluded_reason
from apps.tokens.models import PersonalToken
from tests.fixtures.brain_world import brain_world, user

pytestmark = pytest.mark.django_db

URL = "/api/me/learning/"


@pytest.fixture()
def w():
    return brain_world()


def client(u):
    c = Client()
    c.force_login(u)
    return c


def put(c, body, url=URL, **extra):
    return c.put(url, data=json.dumps(body), content_type="application/json", **extra)


# ---- the person ----------------------------------------------------------------------------------

def test_a_person_agrees_sees_it_and_ends_it(w):
    c = client(w["alice"])
    got = c.get(URL).json()
    assert got["on"] is False and [k["name"] for k in got["classes"]] == ["individual", "memory", "project", "system"]
    assert all(k["summary"] and k["seen_by"] for k in got["classes"])
    r = put(c, {"classes": ["individual", "project"], "directives": ["never share my family details"]})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["on"] and {k["name"] for k in body["classes"] if k["on"]} == {"individual", "project"}
    assert body["directives"] == ["never share my family details"] and body["include_new"] is True
    assert c.delete(URL).json()["on"] is False
    assert c.delete(URL).status_code == 404                                    # nothing left to end
    assert LearningConsent.objects.filter(user=w["alice"]).count() == 1        # one row, revoked, never deleted


def test_a_change_is_on_the_persons_log_and_ids_only_in_the_workspace_log(w):
    c = client(w["alice"])
    put(c, {"classes": ["individual"]})
    put(c, {"classes": ["individual", "system"]})
    put(c, {"classes": ["system"]})
    c.delete(URL)
    kinds = list(PersonAuditEvent.objects.filter(event_type__startswith="learning.")
                 .order_by("pk").values_list("event_type", flat=True))
    assert kinds == ["learning.consented", "learning.class_added", "learning.class_removed", "learning.revoked"]
    rows = Event.objects.filter(workspace_id="acme", source="brain.consent").order_by("pk")
    assert {e.kind for e in rows} >= {"learning.consented", "learning.revoked"}
    for e in rows:                                                              # ids and class names, no content
        assert set(e.payload) <= {"user_id", "classes", "class", "source", "ref"}


def test_it_is_audited_as_a_setting(w):
    from apps.events.models import SettingChange

    put(client(w["alice"]), {"classes": ["memory"]})
    assert SettingChange.objects.filter(operation="set_my_learning", actor=w["alice"]).exists()


@pytest.mark.parametrize("body", [
    {"classes": []}, {"classes": ["nonsense"]}, {"classes": ["individual"], "workspaces": ["not-mine"]},
    {"classes": ["individual"], "standing_rules": [{}]}, {"classes": ["individual"], "standing_rules": [{"x": "y"}]},
    {"classes": ["individual"], "standing_rules": "deny"}, {"classes": ["individual"], "directives": [""]},
    {"classes": ["individual"], "surprise": 1},
])
def test_a_bad_consent_is_422_and_writes_nothing(w, body):
    assert put(client(w["alice"]), body).status_code == 422
    assert not LearningConsent.objects.exists()


def test_one_person_s_consent_is_not_another_s(w):
    put(client(w["alice"]), {"classes": ["individual"]})
    assert client(w["bob"]).get(URL).json()["on"] is False


# ---- one session ---------------------------------------------------------------------------------

def test_a_participant_takes_a_session_out_and_puts_it_back(w):
    c = client(w["bob"])
    url = f"{URL}sessions/{w['session'].pk}/"
    assert c.get(url).json()["excluded"] is False
    assert c.put(url).json()["excluded"] is True
    assert SourceExclusion.objects.filter(user=w["bob"], source=w["source"]).exists()
    assert c.get(url).json()["excluded"] is True
    assert c.put(url).status_code == 200                                        # again: nothing new
    assert PersonAuditEvent.objects.filter(event_type="learning.source_withdrawn").count() == 1
    assert c.delete(url).json()["excluded"] is False
    assert not SourceExclusion.objects.exists()


def test_a_stranger_cannot_touch_a_session_and_learns_nothing(w):
    stranger = user("stranger")
    from tests.fixtures.brain_world import member

    member(stranger, w["ws"])
    c = client(stranger)
    for verb in (c.get, c.put, c.delete):
        assert verb(f"{URL}sessions/{w['session'].pk}/").status_code == 404
        assert verb(f"{URL}sessions/{uuid.uuid4()}/").status_code == 404
    assert not SourceExclusion.objects.filter(user=stranger).exists()


# ---- who may not ---------------------------------------------------------------------------------

def _agent_login(w):
    bot = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"])
    bot.user = user("hal_login")
    bot.save(update_fields=["user"])
    from tests.fixtures.brain_world import member

    member(bot.user, w["ws"])
    return bot


def _calls(c, w, **extra):
    sid = w["session"].pk
    return [c.get(URL, **extra), put(c, {"classes": ["individual"]}, **extra), c.delete(URL, **extra),
            c.get(f"{URL}sessions/{sid}/", **extra), c.put(f"{URL}sessions/{sid}/", **extra),
            c.delete(f"{URL}sessions/{sid}/", **extra)]


def test_an_agent_s_login_is_refused_everywhere(w):
    bot = _agent_login(w)
    assert {r.status_code for r in _calls(client(bot.user), w)} == {403}
    assert not LearningConsent.objects.exists()


def test_a_request_naming_a_turn_is_refused_even_as_a_person(w):
    assert {r.status_code for r in _calls(client(w["alice"]), w, HTTP_X_CANOPY_TURN_ID="t-1")} == {403}
    assert not LearningConsent.objects.exists()


def test_a_turns_login_is_refused(w):
    bot = _agent_login(w)
    from apps.harness.models import Turn

    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"])
    turn = Turn.objects.create(agent=bot, origin=Turn.ORIGIN_EMAIL, prompt="x", idempotency_key=uuid.uuid4().hex)
    raw, tok = PersonalToken.create_for_user(user=bot.user, label="turn", ttl_days=1)
    TurnAgentToken.objects.create(turn=turn, runner=runner, token=tok)
    assert {r.status_code for r in _calls(Client(), w, HTTP_AUTHORIZATION=f"Bearer {raw}")} == {403}
    assert not LearningConsent.objects.exists()


def test_a_runner_s_own_credential_is_refused(w):
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"])
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE, jkt=uuid.uuid4().hex,
                                         status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    codes = {r.status_code for r in _calls(Client(), w, HTTP_AUTHORIZATION=f"Bearer {raw}")}
    assert codes <= {401, 403} and not LearningConsent.objects.exists()


def test_nobody_signed_out_reaches_it(w):
    assert {r.status_code for r in _calls(Client(), w)} <= {401, 403}


# ---- off MCP -------------------------------------------------------------------------------------

def test_no_learning_route_is_an_mcp_tool():
    from apps.api.api import api

    schema = api.get_openapi_schema()
    seen = 0
    for path, item in schema["paths"].items():
        if "/me/learning" not in path:
            continue
        for method, op in item.items():
            if isinstance(op, dict):
                seen += 1
                assert excluded_reason(path, method, op), f"{method} {path} would be an MCP tool"
    assert seen == 8


# ---- coaching and this consent are one fact ------------------------------------------------------

def test_agreeing_to_individual_opts_into_coaching_and_the_reverse(w):
    c = client(w["alice"])
    put(c, {"classes": ["individual"]})
    assert CoachingOptIn.objects.filter(user=w["alice"], revoked_at__isnull=True).exists()
    put(c, {"classes": ["memory"]})                                             # individual dropped
    assert not CoachingOptIn.objects.filter(user=w["alice"], revoked_at__isnull=True).exists()
    put(c, {"classes": ["individual", "memory"]})
    c.delete(URL)
    assert not CoachingOptIn.objects.filter(user=w["alice"], revoked_at__isnull=True).exists()


def test_coaching_s_own_opt_in_still_works_and_shows_up_as_individual(w):
    c = client(w["alice"])
    assert c.post("/api/people/me/session-reviewers/", data="{}", content_type="application/json").status_code == 200
    got = c.get(URL).json()
    assert got["on"] and [k["name"] for k in got["classes"] if k["on"]] == ["individual"]
    put(c, {"classes": ["individual", "project"]})
    assert c.delete("/api/people/me/session-reviewers/").status_code == 200   # opt out of coaching only
    got = c.get(URL).json()
    assert [k["name"] for k in got["classes"] if k["on"]] == ["project"]
    # the last class gone through coaching ends the consent
    put(c, {"classes": ["individual"]})
    c.delete("/api/people/me/session-reviewers/")
    assert consent.live(w["alice"]) is None
    assert BrainSource.objects.count() == 1                                     # nothing here made sources
