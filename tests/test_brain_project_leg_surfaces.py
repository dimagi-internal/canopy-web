"""The brain's project leg reaches a session's words ONLY scrubbed and recorded, and nothing else.

Leg 7 is opt-in per surface (`access.visible_session_q(brain=True)`): every other surface reads legs 1 to
6, so a reader who has a session only through a project is refused by turn content, attachments, the chat
socket, exports and Slack, and the surfaces that do carry the leg (the list, `GET /{id}`, its scroll-back
and human inputs) serve no secret. Plus who the leg is for (people, never an agent's login or a turn's
token), its cost per page, and the coaching leg honouring a person's removal."""
from __future__ import annotations

import json
import uuid
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.agents.models import Agent
from apps.brain import consent
from apps.brain.models import LearningConsent
from apps.canopy_sessions import access, audience, coaching
from apps.canopy_sessions.models import Attachment, CoachingOptIn, CoachingRole, Session
from apps.harness import ledger
from apps.harness.models import Turn
from tests.fixtures.brain_project_leg import SECRET, client, project_leg_world
from tests.fixtures.brain_project_leg import socket_connects as _socket_role
from tests.fixtures.brain_world import member, user

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(settings):
    return project_leg_world()


@pytest.fixture()
def content(w):
    """A chat turn in the session carrying the secret in its prompt, ledger and raw transcript, and a file."""
    s = w["session"]
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt=f"deploy {SECRET}",
                               idempotency_key=uuid.uuid4().hex, initiator_user=w["alice"])
    ledger.append_events(turn, [{"kind": "tool_result", "payload": {"output": SECRET}}])
    ledger.append_transcript(turn, [json.dumps({"type": "user", "message": {"content": f"x {SECRET}"}})])
    att = Attachment.objects.create(session=s, uploaded_by=w["alice"], filename="k.txt", content_type="text/plain",
                                    size_bytes=len(SECRET), storage_key=f"sessions/{s.id}/{uuid.uuid4()}/k.txt")
    return {"turn": turn, "attachment": att}


#: Every route that serves a session's or its turns' words, by path template. `refused` ones must 403/404
#: a leg-7-only reader; `scrubbed` ones answer 200 with no secret in the body.
REFUSED = [
    "/api/harness/turns/{turn}/events",
    "/api/harness/turns/{turn}/messages",
    "/api/harness/turns/{turn}/transcript",
    "/api/harness/turns/{turn}/caller-context",
    "/api/canopy-sessions/attachments/{attachment}/content",
    "/api/canopy-sessions/{session}/export",
    "/api/canopy-sessions/{session}/info",
    "/api/canopy-sessions/{session}/context",
    "/api/canopy-sessions/{session}/turns/{turn}/context",
]
SCRUBBED = [
    "/api/canopy-sessions/{session}",
    "/api/canopy-sessions/{session}/messages?before=5",
    "/api/canopy-sessions/{session}/human-inputs",
    "/api/canopy-sessions/?state=all&reply=true",
    "/api/canopy-sessions/search",
]
#: Served, but with the turn's content blanked for this reader (`turn_access.redact`).
HIDDEN = [
    "/api/harness/turns/{turn}",
    "/api/harness/turns/",
]


def _paths(w, content, templates):
    ids = {"session": w["session"].pk, "turn": content["turn"].pk, "attachment": content["attachment"].pk}
    return [t.format(**ids) for t in templates]


def _get(c, path):
    with mock.patch("apps.canopy_sessions.attachment_storage.get",
                    return_value=mock.Mock(body=SECRET.encode(), content_type="text/plain")), \
            mock.patch("apps.canopy_sessions.attachment_storage.is_configured", return_value=True):
        return c.get(path)


def _text(resp) -> str:
    return b"".join(resp.streaming_content).decode() if resp.streaming else resp.content.decode()


def test_a_leg_7_only_reader_gets_no_content_route_and_only_scrubbed_words(w, content):
    c = client(w["reader"])
    for path in _paths(w, content, REFUSED):
        assert _get(c, path).status_code in (403, 404), path
    for path in _paths(w, content, SCRUBBED):
        resp = _get(c, path)
        assert resp.status_code == 200, path
        assert SECRET not in _text(resp), path
    for path in _paths(w, content, HIDDEN):
        resp = _get(c, path)
        assert resp.status_code in (200, 403, 404), path
        assert SECRET not in _text(resp), path
    assert _socket_role(w["reader"], w["session"]) is False           # no live channel: it reads the REST copy


def test_the_people_in_it_still_read_every_route_in_clear(w, content):
    """The refusals above are the leg's, not a broken route: the session's own person reads them all."""
    c = client(w["alice"])
    for path in _paths(w, content, ["/api/harness/turns/{turn}", "/api/harness/turns/{turn}/transcript",
                                    "/api/canopy-sessions/attachments/{attachment}/content"]):
        resp = _get(c, path)
        assert resp.status_code == 200 and SECRET in _text(resp), path
    assert _socket_role(w["alice"], w["session"]) is True


def test_the_leg_only_scrubs_and_never_lets_its_reader_act(w):
    s = w["session"]
    assert access.can_read(w["reader"], s, brain=True) and not access.can_read(w["reader"], s)
    assert access.role_for(w["reader"], s) is None and not access.can_write(w["reader"], s)
    assert access.readable_ids(w["reader"], [s.pk]) == set()
    assert access.readable_ids(w["reader"], [s.pk], brain=True) == {s.pk}


def test_a_list_row_read_only_through_the_leg_has_its_title_scrubbed_and_none_of_its_words(w):
    s = w["session"]
    rows = client(w["reader"]).get("/api/canopy-sessions/?state=all&reply=true").json()
    row = next(r for r in rows if r["id"] == str(s.pk))
    assert "redacted:github-token" in row["title"] and row["opening"] == "" and row["last_reply"] == ""


def test_searching_never_matches_the_unscrubbed_title_of_a_leg_7_row(w):
    s = w["session"]
    needle = SECRET[4:20]
    mine = {r["id"] for r in client(w["alice"]).get(f"/api/canopy-sessions/?state=all&q={needle}").json()}
    assert str(s.pk) in mine                                         # its own people find it by its words
    for path in (f"/api/canopy-sessions/?state=all&q={needle}", f"/api/canopy-sessions/search?q={needle}"):
        body = client(w["reader"]).get(path).json()
        found = {r["id"] for r in (body["sessions"] if isinstance(body, dict) else body)}
        assert str(s.pk) not in found, path
    assert str(s.pk) in {r["id"] for r in client(w["reader"]).get("/api/canopy-sessions/?state=all").json()}


def test_a_viewer_through_the_leg_cannot_bind_it_to_slack(w):
    from apps.slack import share

    result = share.share_session(w["reader"], channel="#dev", summary="hi", mode=share.BIND, session=w["session"])
    assert not result.ok


def test_a_viewer_participant_cannot_bind_a_session_to_slack(w):
    """Binding hands a thread the session; a viewer reads, and may not."""
    from apps.canopy_sessions.models import SessionParticipant
    from apps.slack import share

    SessionParticipant.objects.create(session=w["session"], user=w["carol"], role=SessionParticipant.VIEWER)
    result = share.share_session(w["carol"], channel="#dev", summary="hi", mode=share.BIND, session=w["session"])
    assert result.status == share.CANNOT_WRITE
    resp = client(w["carol"]).post("/api/canopy-sessions/slack/share", data=json.dumps(
        {"channel": "#dev", "summary": "hi", "mode": "bind", "session_id": str(w["session"].pk)}),
        content_type="application/json")
    assert resp.status_code == 403


# ---- who the leg is for ---------------------------------------------------------------------------

def test_an_agents_login_is_never_a_projects_reader(w):
    bot = user("eva-bot")
    member(bot, w["ws"])
    Agent.objects.filter(pk=w["lead"].pk).update(user=bot)
    assert not access.can_read(bot, w["session"], brain=True)
    assert bot.pk not in audience.readers(w["session"])
    assert w["reader"].pk in audience.readers(w["session"])


def test_a_request_held_to_a_turns_reach_gets_no_project_leg(w):
    access.limit_reach(w["reader"], access.Reach(access.Q(pk=w["session"].pk)))
    assert not access.can_read(w["reader"], w["session"], brain=True)


def test_a_page_of_rows_asks_the_leg_a_fixed_number_of_times(w):
    """Two queries for the page (`access.brain_only_ids`), not two per row."""
    from apps.brain import associate, sources

    def linked(n):
        for i in range(n):
            s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB,
                                       title=f"l{i}")
            associate.put(sources.for_session(s), w["project"], how="dispatched", tier="core")

    def queries():
        c = client(w["reader"])
        with CaptureQueriesContext(connection) as q:
            assert c.get("/api/canopy-sessions/?state=all").status_code == 200
        return len(q.captured_queries)

    linked(2)
    few = queries()
    linked(12)
    many = queries()
    assert many - few < 12, (few, many)


# ---- the coaching leg honours a removal ------------------------------------------------------------

@pytest.fixture()
def coached(w):
    """Alice is coached by a person who coaches the workspace."""
    coach = user("coach")
    member(coach, w["ws"])
    CoachingRole.objects.create(workspace=w["ws"], user=coach)
    CoachingOptIn.objects.create(user=w["alice"])
    LearningConsent.objects.filter(user=w["alice"]).update(classes=["project", "individual"])
    return coach


def test_a_coach_loses_a_session_its_person_took_out_of_learning(w, coached):
    s = w["session"]
    assert access.can_read(coached, s) and coached.pk in audience.readers(s)
    consent.exclude(w["alice"], w["source"])
    assert not access.can_read(coached, s)
    assert coached.pk not in audience.readers(s)
    consent.include(w["alice"], w["source"])
    assert access.can_read(coached, s)


def test_a_coach_loses_a_session_another_person_in_it_took_out(w, coached):
    """A removal makes the session unavailable to every learning read, whoever in it removed it."""
    consent.exclude(w["bob"], w["source"])
    assert not access.can_read(coached, w["session"])


def test_a_coach_reads_only_the_workspaces_the_persons_consent_covers(w, coached):
    s = w["session"]
    LearningConsent.objects.filter(user=w["alice"]).update(workspaces=["elsewhere"], include_new=False,
                                                           known_workspaces=["acme", "elsewhere"])
    assert not access.can_read(coached, s)
    assert coached.pk not in audience.readers(s)
    LearningConsent.objects.filter(user=w["alice"]).update(workspaces=["acme"])
    assert access.can_read(coached, s)


def test_a_coach_still_reads_what_nobody_removed(w, coached):
    other = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB)
    consent.exclude(w["alice"], w["source"])
    assert access.can_read(coached, other)


def test_the_runners_coaching_upload_leaves_out_a_removed_session(w, coached):
    from apps.brain import sources
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    box = Runner.objects.create(name="laptop", owner=w["alice"], workspace=w["ws"])
    s = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=box, session_key="k1")
    assert s.pk in coaching.full_upload_session_ids(box)
    consent.exclude(w["alice"], sources.for_session(s))
    assert s.pk not in coaching.full_upload_session_ids(box)


@pytest.mark.parametrize("rule", [{"workspace": "acme"}, {"class": "individual"}, {"capability": "coaching"},
                                  {"capability": "coaching", "purpose": "review"}])
def test_a_coach_honours_the_persons_standing_rules(w, coached, rule):
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    s = w["session"]
    box = Runner.objects.create(name="laptop", owner=w["alice"], workspace=w["ws"])
    found = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=found, runner=box, session_key="k1")
    assert access.can_read(coached, s) and found.pk in coaching.full_upload_session_ids(box)
    assert audience._grantor(s) == w["alice"].pk
    LearningConsent.objects.filter(user=w["alice"]).update(standing_rules=[rule])
    assert not access.can_read(coached, s)
    assert audience._grantor(s) is None                  # the coaching part of the audience (the project leg is its own)
    assert found.pk not in coaching.full_upload_session_ids(box)


def test_a_rule_about_something_else_leaves_coaching_alone(w, coached):
    LearningConsent.objects.filter(user=w["alice"]).update(standing_rules=[{"capability": "project-learning"},
                                                                          {"workspace": "elsewhere"}])
    assert access.can_read(coached, w["session"]) and audience._grantor(w["session"]) == w["alice"].pk
