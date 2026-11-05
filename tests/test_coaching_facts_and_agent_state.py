"""A coach's analytics facts and an agent's state document, through the real routes.

Facts: `POST /api/coaching/facts/`: closed kinds and ids, only from inside a running,
unconfined turn of the agent whose login calls, through `activity.emit`.

State document: `/api/agents/{slug}/state-document`: one versioned JSON document. The
login writes from inside its running turn; only the agent's owner and admins, or a turn
answering to nobody or to one of them, may read it, since it holds everyone's records.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session, SessionReviewGrant
from apps.events import activity, analytics
from apps.events.models import Activity
from apps.harness import agent_state, caller_tokens, turn_agent_tokens
from apps.harness.models import AgentStateDocument, Runner, Turn
from apps.mcp import api_tools
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

FACTS = "/api/coaching/facts/"
DOC = "/api/agents/trellis/state-document"
LEDGER = {"version": 1, "people": {"lili@dimagi.com": {"notes": [], "paused_until": "2026-12-01"}}}


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
    return {"ws": ws, "owner": owner, **people, "trellis": trellis, "box": box}


def _turn(w, *, kind="user", by=None, assurance="session", status=Turn.RUNNING, **kw):
    return Turn.objects.create(
        agent=w["trellis"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="coach",
        idempotency_key=uuid.uuid4().hex, status=status, claimed_by=w["box"],
        initiator_kind=kind, initiator_via="chat", initiator_assurance=assurance,
        initiator_user=by, **kw)


def _login(w, turn) -> str:
    return turn_agent_tokens.issue(turn, w["box"])["token"]


def _sweep(w) -> str:
    """The login of a turn nobody asked for: the weekly coaching pass."""
    return _login(w, _turn(w, kind="system"))


def _asked_by(w, who) -> str:
    return _login(w, _turn(w, by=w[who]))


def _stored(w) -> str:
    return PersonalToken.create_for_user(user=w["trellis"].user, label="canopy-pat")[0]


def _pat(user) -> str:
    return PersonalToken.create_for_user(user=user, label="theirs")[0]


def _post(bearer, body):
    return Client().post(FACTS, body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _get_doc(bearer):
    return Client().get(DOC, HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _put_doc(bearer, document=LEDGER, expected=0):
    return Client().put(DOC, {"expected_version": expected, "document": document},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _note(w, who="lili", **over):
    return {"kind": "coaching.note_sent", "ref": "n-0a1b2c3d", "person_id": w[who].pk,
            "note_type": "retrospective", **over}


def _chat_turn(w, session):
    return Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="coach",
        idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=w["box"],
        initiator_kind="user", initiator_via="chat", initiator_assurance="session", initiator_user=w["lili"])


def _confine(turn) -> None:
    Turn.objects.filter(pk=turn.pk).update(capability="ask")


# ---- facts: what gets through, and what it becomes ------------------------------------

def test_each_kind_becomes_one_fact_through_emit(w):
    bearer = _sweep(w)
    reply = {"kind": "coaching.reply", "ref": "n-0a1b2c3d", "person_id": w["bo"].pk, "reply": "pause"}
    finding = {"kind": "coaching.finding_routed", "ref": "f-0a1b2c3d", "finding_type": "system-defect"}
    for body in (_note(w), reply, finding):
        r = _post(bearer, body)
        assert r.status_code == 200 and r.json() == {"recorded": True}, r.content
    note = Activity.objects.get(kind=activity.COACHING_NOTE_SENT)
    assert note.workspace_id == w["ws"].slug and note.agent == w["trellis"]
    assert note.actor == w["trellis"].user and note.actor_kind == Activity.AGENT
    assert note.ref == "n-0a1b2c3d" and note.meta["person_id"] == w["lili"].pk
    assert note.meta["note_type"] == "retrospective"
    assert Activity.objects.get(kind=activity.COACHING_REPLY).meta["reply"] == "pause"


def test_a_friction_finding_carries_no_person(w):
    bearer = _sweep(w)
    r = _post(bearer, {"kind": "coaching.finding_routed", "ref": "f-0a1b2c3d", "finding_type": "knowledge-gap",
                       "person_id": w["lili"].pk})
    assert r.status_code == 422
    assert not Activity.objects.filter(kind=activity.COACHING_FINDING_ROUTED).exists()
    _post(bearer, {"kind": "coaching.finding_routed", "ref": "f-0a1b2c3d", "finding_type": "knowledge-gap"})
    fact = Activity.objects.get(kind=activity.COACHING_FINDING_ROUTED)
    assert "person_id" not in fact.meta and fact.actor == w["trellis"].user


@pytest.mark.parametrize("over", [
    {"kind": "coaching.anything"},
    {"note_type": "a sentence about what they did"},
    {"ref": "they pasted a secret"},
    {"ref": "n-0a1b2c3d9"},
    {"text": "free text has no field"},
    {"quote": "free text has no field"},
    {"meta": {"x": "free text has no field"}},
    {"reply": "useful"},              # another kind's field
])
def test_only_closed_words_and_ids_get_through(w, over):
    assert _post(_sweep(w), _note(w, **over)).status_code == 422
    assert not Activity.objects.filter(surface="coaching").exists()


def test_recording_the_same_fact_twice_counts_once_but_a_new_reply_counts(w):
    bearer = _sweep(w)
    assert _post(bearer, _note(w)).json() == {"recorded": True}
    assert _post(bearer, _note(w)).json() == {"recorded": False}
    assert Activity.objects.filter(kind=activity.COACHING_NOTE_SENT).count() == 1
    for status in ("useful", "useful", "pause"):
        _post(bearer, {"kind": "coaching.reply", "ref": "n-0a1b2c3d", "person_id": w["lili"].pk, "reply": status})
    assert Activity.objects.filter(kind=activity.COACHING_REPLY).count() == 2


def test_a_coachs_facts_do_not_make_anyone_an_active_person(w):
    _post(_sweep(w), _note(w))
    assert analytics.build([w["ws"].slug])["series"][-1]["active_users"] == 0


# ---- facts: who may call ---------------------------------------------------------------

def test_another_persons_token_cannot_record_a_fact(w):
    for who in ("lili", "owner", "col"):
        assert _post(_pat(w[who]), _note(w)).status_code == 403, who
    assert not Activity.objects.filter(surface="coaching").exists()


def test_a_browser_session_cannot_record_a_fact(w):
    c = Client()
    c.force_login(w["owner"])
    assert c.post(FACTS, _note(w), content_type="application/json").status_code == 403


def test_a_stored_token_of_the_agents_own_login_cannot_either(w):
    """A stored PAT cannot show which turn it is in."""
    assert _post(_stored(w), _note(w)).status_code == 403


def test_a_confined_turn_cannot_record_a_fact(w):
    turn = _turn(w, by=w["lili"])
    bearer = _login(w, turn)
    _confine(turn)
    assert _post(bearer, _note(w)).status_code == 403
    assert not Activity.objects.filter(surface="coaching").exists()


def test_a_confined_turns_caller_token_is_refused_on_rest(w):
    session = Session.objects.create(workspace=w["ws"], created_by=w["lili"], title="", agent=w["trellis"])
    turn = _chat_turn(w, session)
    _confine(turn)
    raw = caller_tokens.mint(turn)
    assert _post(raw, _note(w)).status_code == 401
    assert _get_doc(raw).status_code == 401
    assert _put_doc(raw).status_code == 401


def test_a_colleagues_scoped_token_acts_as_them_and_reaches_neither(w):
    """A colleague's full turn reaches canopy as the colleague: not the agent's login."""
    _put_doc(_sweep(w))
    session = Session.objects.create(workspace=w["ws"], created_by=w["lili"], title="", agent=w["trellis"])
    raw = caller_tokens.mint(_chat_turn(w, session), scoped=True)
    assert _get_doc(raw).status_code == 404
    assert _post(raw, _note(w)).status_code == 403
    assert _put_doc(raw, expected=1).status_code == 403
    assert not Activity.objects.filter(surface="coaching").exists()


def test_a_finished_turns_login_cannot_record_a_fact(w):
    turn = _turn(w, by=w["lili"])
    bearer = _login(w, turn)
    assert _post(bearer, _note(w)).status_code == 200
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _post(bearer, _note(w, ref="n-11111111")).status_code == 403


def test_a_turn_asked_by_one_person_cannot_file_a_fact_about_another(w):
    bearer = _asked_by(w, "lili")
    assert _post(bearer, _note(w, "lili")).status_code == 200
    assert _post(bearer, _note(w, "bo", ref="n-11111111")).status_code == 404


def test_no_fact_about_someone_without_a_live_grant(w):
    bearer = _sweep(w)
    assert _post(bearer, _note(w, "col")).status_code == 404     # never enrolled
    SessionReviewGrant.objects.filter(user=w["lili"]).update(revoked_at=timezone.now())
    assert _post(bearer, _note(w, "lili")).status_code == 404    # stopped


def test_another_agents_grant_does_not_count(w):
    other_login = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    wsvc.ensure_member(w["ws"], other_login, WorkspaceMembership.EDITOR)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=w["ws"], owner=w["owner"], user=other_login)
    turn = Turn.objects.create(agent=ada, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=w["box"],
                               initiator_kind="system", initiator_via="chat", initiator_assurance="session")
    bearer = turn_agent_tokens.issue(turn, w["box"])["token"]
    assert _post(bearer, _note(w, "lili")).status_code == 404    # lili granted trellis, not ada
    assert _get_doc(bearer).status_code == 404
    assert _put_doc(bearer).status_code == 404


# ---- state document: reading ------------------------------------------------------------

def test_the_agents_turn_writes_and_reads_it_back(w):
    bearer = _sweep(w)
    assert _get_doc(bearer).json() == {"version": 0, "document": None, "updated_at": None}
    r = _put_doc(bearer)
    assert r.status_code == 200 and r.json()["version"] == 1
    body = _get_doc(bearer).json()
    assert body["version"] == 1 and body["document"] == LEDGER


def test_its_owner_and_admins_may_read_and_nobody_else(w):
    _put_doc(_sweep(w))
    assert _get_doc(_pat(w["owner"])).json()["document"] == LEDGER
    AgentAdmin.objects.create(agent=w["trellis"], user=w["bo"])
    assert _get_doc(_pat(w["bo"])).json()["document"] == LEDGER
    for who in ("lili", "col"):
        r = _get_doc(_pat(w[who]))
        assert r.status_code == 404, who         # the same answer as an agent that is not there
        assert b"lili@dimagi.com" not in r.content


def test_someone_outside_the_workspace_gets_the_same_404(w):
    outsider = User.objects.create_user("out", "out@example.com", "pw")
    assert _get_doc(_pat(outsider)).status_code == 404


def test_a_stored_token_of_the_agents_login_may_not_read_it(w):
    _put_doc(_sweep(w))
    r = _get_doc(_stored(w))
    assert r.status_code == 403 and b"lili@dimagi.com" not in r.content


def test_a_confined_turn_may_not_read_or_write_it(w):
    _put_doc(_sweep(w))
    turn = _turn(w, by=w["lili"])
    bearer = _login(w, turn)
    _confine(turn)
    r = _get_doc(bearer)
    assert r.status_code == 403 and b"lili@dimagi.com" not in r.content
    assert _put_doc(bearer, {"people": {}}, expected=1).status_code == 403
    assert AgentStateDocument.objects.get(agent=w["trellis"]).version == 1


def test_a_finished_turn_may_not_read_or_write_it(w):
    turn = _turn(w, kind="system")
    bearer = _login(w, turn)
    _put_doc(bearer)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _get_doc(bearer).status_code == 403
    assert _put_doc(bearer, expected=1).status_code == 403


def test_a_colleagues_turn_may_write_but_never_read_everyone(w):
    """Their request to be forgotten has to reach the backup; everyone's records must not
    reach their turn."""
    _put_doc(_sweep(w))
    bearer = _asked_by(w, "lili")
    r = _get_doc(bearer)
    assert r.status_code == 403 and b"paused_until" not in r.content
    w_ = _put_doc(bearer, {"people": {}}, expected=1)
    assert w_.status_code == 200 and w_.json()["version"] == 2


def test_an_administrator_asking_may_read_it(w):
    _put_doc(_sweep(w))
    assert _get_doc(_asked_by(w, "owner")).json()["document"] == LEDGER


def test_a_contact_or_unverified_asker_may_not_read_it(w):
    from apps.contacts.models import Contact

    _put_doc(_sweep(w))
    contact = Contact.objects.create(workspace=w["ws"], email="x@example.com")
    assert _get_doc(_login(w, _turn(w, kind="contact", assurance="dmarc", initiator_contact=contact))).status_code == 403
    assert _get_doc(_login(w, _turn(w, by=w["owner"], assurance="host_signed"))).status_code == 403


# ---- state document: writing ------------------------------------------------------------

def test_only_the_agents_own_login_writes_it(w):
    for who, status in (("owner", 403), ("lili", 404), ("col", 404)):
        assert _put_doc(_pat(w[who])).status_code == status, who
    assert _put_doc(_stored(w)).status_code == 403
    assert not AgentStateDocument.objects.exists()


def test_overlapping_writes_cannot_overwrite_each_other_silently(w):
    first, second = _sweep(w), _sweep(w)
    assert _put_doc(first).json()["version"] == 1
    assert _put_doc(first, {"people": {"a": 1}}, expected=1).json()["version"] == 2
    stale = _put_doc(second, {"people": {"lost": 1}}, expected=1)
    assert stale.status_code == 409 and stale.json()["current_version"] == 2
    row = AgentStateDocument.objects.get(agent=w["trellis"])
    assert row.document == {"people": {"a": 1}} and row.version == 2


def test_a_first_write_that_lost_the_race_is_a_conflict(w):
    bearer = _sweep(w)
    assert _put_doc(bearer).status_code == 200
    r = _put_doc(bearer, {"people": {}}, expected=0)
    assert r.status_code == 409 and r.json()["current_version"] == 1


def test_it_is_size_capped(w):
    bearer = _sweep(w)
    r = _put_doc(bearer, {"pad": "x" * (agent_state.MAX_BYTES + 1)})
    assert r.status_code == 413
    assert not AgentStateDocument.objects.exists()
    assert _put_doc(bearer, {"pad": "x" * 1000}).status_code == 200


def test_the_write_records_which_turn_wrote_it(w):
    turn = _turn(w, kind="system")
    _put_doc(_login(w, turn))
    assert AgentStateDocument.objects.get(agent=w["trellis"]).updated_by_turn == turn.pk


def test_a_bad_body_is_refused(w):
    bearer = _sweep(w)
    assert _put_doc(bearer, document="not an object").status_code == 422
    assert _put_doc(bearer, expected=-1).status_code == 422


def test_deleting_the_agent_deletes_it(w):
    _put_doc(_sweep(w))
    w["trellis"].delete()
    assert not AgentStateDocument.objects.exists()


# ---- it is not analytics; MCP is the same routes -------------------------------------------

def test_a_write_or_read_records_no_fact_and_analytics_has_no_way_to_it(w):
    bearer = _sweep(w)
    before = Activity.objects.exclude(kind__in=(activity.TURN_ENQUEUED,)).count()
    _put_doc(bearer)
    _get_doc(bearer)
    assert Activity.objects.exclude(kind__in=(activity.TURN_ENQUEUED,)).count() == before
    from pathlib import Path

    for name in ("apps/events/analytics.py", "apps/events/activity.py"):
        assert "AgentStateDocument" not in Path(name).read_text()


def test_mcp_serves_the_routes():
    """MCP is the REST API: both routes and the fact are tools, secured in the route."""
    assert api_tools.is_tool_route("GET", "/api/agents/trellis/state-document")
    assert api_tools.is_tool_route("PUT", "/api/agents/trellis/state-document")
    assert api_tools.is_tool_route("POST", FACTS)
