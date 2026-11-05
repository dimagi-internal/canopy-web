"""An agent acting ON BEHALF OF the person in its turn (apps/harness/turn_asker.py).

hal, in Jonathan's session, dispatches eva's turn pinned to Jonathan's cloud box. hal
administers neither; on the login canopy-web issued for Jonathan's turn it is working for
him, so canopy-web judges the pin as Jonathan, and the pin goes through, recorded. Nobody
declares it: canopy-web works the person out from the login. A stored token, a finished
turn, an unverified asker or a turn canopy itself started work for no one — the agent acts
as itself. `X-Canopy-On-Behalf-Of` still asserts it, and naming anyone else refuses.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.harness import initiator as who
from apps.harness import turn_agent_tokens
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace
from tests.access_helpers import editors_run

pytestmark = pytest.mark.django_db
JJ = "jj@dimagi.com"


@pytest.fixture
def world():
    ws = a_workspace()
    jj = User.objects.create_user("jj", JJ, "pw")
    wsvc.ensure_member(ws, jj, WorkspaceMembership.OWNER)
    colleague = User.objects.create_user("col", "col@dimagi.com", "pw")
    wsvc.ensure_member(ws, colleague, WorkspaceMembership.EDITOR)
    hal_login = User.objects.create_user("hal-agent", "hal@dimagi-ai.com", "pw")
    wsvc.ensure_member(ws, hal_login, WorkspaceMembership.EDITOR)
    eva_login = User.objects.create_user("eva-agent", "eva@dimagi-ai.com", "pw")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, user=hal_login)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj, user=eva_login)
    editors_run(hal)
    editors_run(eva)
    laptop = _runner("haldimagi-mbp-cdp", jj, ws, Runner.EMDASH)
    RunnerAssignment.objects.create(agent=hal, runner=laptop, rank=0)
    cloud = _runner("cloud-ec2-2", jj, ws, Runner.CLOUD)
    RunnerAssignment.objects.create(agent=eva, runner=cloud, rank=0)
    return {"ws": ws, "jj": jj, "colleague": colleague, "hal": hal, "eva": eva,
            "laptop": laptop, "cloud": cloud}


def _runner(name, owner, ws, kind):
    return Runner.objects.create(name=name, kind=kind, owner=owner, workspace=ws,
                                 last_heartbeat_at=timezone.now(), status=Runner.ONLINE)


def _turn(world, *, by=None, status=Turn.RUNNING, assurance="session", **kw):
    by = world["jj"] if by is None else by
    kw.setdefault("agent", world["hal"])
    return Turn.objects.create(
        origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="republish it on cloud-ec2-2",
        idempotency_key=uuid.uuid4().hex, status=status, claimed_by=world["laptop"],
        initiator_kind="user", initiator_via="chat", initiator_assurance=assurance,
        initiator_user=by, **kw)


def _login(world, turn, session=None) -> str:
    if session is not None:
        return turn_agent_tokens.issue_for_session(turn, world["laptop"], session)["token"]
    return turn_agent_tokens.issue(turn, world["laptop"])["token"]


def _dispatch(world, bearer, *, on_behalf_of=None, **body):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {bearer}"}
    if on_behalf_of:
        headers["HTTP_X_CANOPY_ON_BEHALF_OF"] = on_behalf_of
    payload = {"agent_slug": "eva", "origin": "api", "idempotency_key": uuid.uuid4().hex,
               "prompt": "republish the KB", "runner_id": str(world["cloud"].pk), **body}
    return Client().post("/api/harness/turns/", payload, content_type="application/json", **headers)


def test_in_jonathans_turn_hal_works_for_him_without_saying_so(world):
    turn = _turn(world)
    r = _dispatch(world, _login(world, turn))
    assert r.status_code == 201, r.content
    made = Turn.objects.get(pk=r.json()["id"])
    assert made.pinned_runner == world["cloud"]
    ob = made.origin_ref["on_behalf_of"]
    assert (ob["email"], ob["user_id"], ob["turn"]) == (JJ, world["jj"].pk, str(turn.pk))


def test_on_a_stored_token_hal_acts_for_itself_and_may_not_pin(world):
    bearer, _ = PersonalToken.create_for_user(user=world["hal"].user, label="canopy-pat")
    r = _dispatch(world, bearer)
    assert r.status_code == 403 and "pinning eva's turn" in r.json()["detail"]


def test_a_turn_canopy_started_works_for_no_one(world):
    """A schedule's turn: hal acts as itself, and the next agent is told so."""
    turn = _turn(world)
    Turn.objects.filter(pk=turn.pk).update(initiator_kind="system", initiator_assurance="internal")
    r = _dispatch(world, _login(world, turn), runner_id=None)
    assert r.status_code == 201, r.content
    made = Turn.objects.get(pk=r.json()["id"])
    assert "on_behalf_of" not in made.origin_ref
    assert who.describe(made)["on_behalf_of"] is None  # said outright, never left out


def test_a_chain_of_agents_keeps_the_person(world):
    """Jonathan asks hal; hal dispatches eva; eva's login dispatches on — still for him."""
    first = _dispatch(world, _login(world, _turn(world)), runner_id=None)
    eva_turn = Turn.objects.get(pk=first.json()["id"])
    Turn.objects.filter(pk=eva_turn.pk).update(status=Turn.RUNNING, claimed_by=world["laptop"])
    eva_turn.refresh_from_db()
    wsvc.ensure_member(world["ws"], world["eva"].user, WorkspaceMembership.EDITOR)
    bearer = turn_agent_tokens.issue(eva_turn, world["laptop"])["token"]
    payload = {"agent_slug": "hal", "origin": "api", "idempotency_key": uuid.uuid4().hex,
               "prompt": "and tell hal"}
    r = Client().post("/api/harness/turns/", payload, content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {bearer}")
    assert r.status_code == 201, r.content
    made = Turn.objects.get(pk=r.json()["id"])
    assert made.origin_ref["on_behalf_of"]["email"] == JJ
    assert made.origin_ref["on_behalf_of"]["agent"] == "eva"


def test_a_send_into_another_session_carries_the_person(world):
    """The door ada used on 2026-10-26: a follow-up into an existing session."""
    target = Session.objects.create(workspace=world["ws"], agent=world["eva"], status=Session.ACTIVE)
    SessionParticipant.objects.create(session=target, user=world["hal"].user,
                                      role=SessionParticipant.EDITOR)
    bearer = _login(world, _turn(world))
    r = Client().post(f"/api/canopy-sessions/{target.pk}/send", {"text": "please re-run it"},
                      content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {bearer}")
    assert r.status_code == 200, r.content
    made = Turn.objects.get(pk=r.json()["turn_id"])
    assert made.origin_ref["on_behalf_of"]["email"] == JJ
    seen = who.describe(made)["on_behalf_of"]
    assert seen["user"]["email"] == JJ and seen["relationship"] == "owner"
    assert seen["via_agent"] == "hal"


def test_on_behalf_of_jonathan_the_pin_is_his_to_decide_and_recorded(world):
    turn = _turn(world)
    r = _dispatch(world, _login(world, turn), on_behalf_of=JJ)
    assert r.status_code == 201, r.content
    made = Turn.objects.get(pk=r.json()["id"])
    assert made.pinned_runner == world["cloud"]
    ob = made.origin_ref["on_behalf_of"]
    assert (ob["email"], ob["turn"], ob["agent"]) == (JJ, str(turn.pk), "hal")
    assert ob["decided"] == ["pin eva's turn to cloud-ec2-2"]
    assert made.initiator_user == world["hal"].user  # hal made the call; the record says for whom


def test_auto_in_jonathans_turn_is_requested_by_him(world):
    bearer = _login(world, _turn(world))
    r = _dispatch(world, bearer, turn_mode="auto")
    assert r.status_code == 201, r.content
    made = Turn.objects.get(pk=r.json()["id"])
    assert made.requested_turn_mode_by == world["jj"]  # the claim re-checks him, an admin of eva
    assert "run eva's turn auto" in made.origin_ref["on_behalf_of"]["decided"]


def test_it_is_judged_on_the_askers_access_not_lifted_past_it(world):
    """A colleague who administers neither eva nor the box: on their behalf, still no."""
    turn = _turn(world, by=world["colleague"])
    r = _dispatch(world, _login(world, turn), on_behalf_of="col@dimagi.com")
    assert r.status_code == 403 and "pinning eva's turn" in r.json()["detail"]


def test_an_agent_admin_asker_also_suffices(world):
    AgentAdmin.objects.create(agent=world["eva"], user=world["colleague"])
    turn = _turn(world, by=world["colleague"])
    assert _dispatch(world, _login(world, turn), on_behalf_of="col@dimagi.com").status_code == 201


@pytest.mark.parametrize("case,expect", [
    ("someone_else", "started by jj@dimagi.com, not"),
    ("stored_token", "stored token"),
    ("finished", "no longer running"),
    ("unverified", "not verified"),
    ("confined", "confined"),
])
def test_the_claim_is_checked_not_taken(world, case, expect):
    turn = _turn(world, assurance="widget" if case == "unverified" else "session")
    bearer = _login(world, turn)
    if case == "stored_token":
        bearer, _ = PersonalToken.create_for_user(user=world["hal"].user, label="canopy-pat")
    elif case == "finished":
        Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)  # login issued while it ran
    elif case == "confined":
        Turn.objects.filter(pk=turn.pk).update(capability="ask")
    who = "other@dimagi.com" if case == "someone_else" else JJ
    r = _dispatch(world, bearer, on_behalf_of=who)
    assert r.status_code == 403
    assert expect in r.json()["detail"] and "On-Behalf-Of refused" in r.json()["detail"]
    assert not Turn.objects.filter(agent=world["eva"]).exists()


def test_a_browser_session_cannot_claim_to_act_for_anyone(world):
    c = Client()
    c.force_login(world["colleague"])
    r = c.get("/api/me/", HTTP_X_CANOPY_ON_BEHALF_OF=JJ)
    assert r.status_code == 403 and "On-Behalf-Of refused" in r.json()["detail"]


def test_a_session_login_acts_for_whoever_sent_the_newest_message(world):
    session = Session.objects.create(workspace=world["ws"], agent=world["hal"], status=Session.ACTIVE)

    def message(by):
        Turn.objects.filter(chat_session=session).update(status=Turn.DONE)
        return _turn(world, by=by, agent=None, chat_session=session)

    bearer = _login(world, message(world["jj"]), session=session)
    message(world["jj"])  # Jonathan's newer message in the same chat: still him
    assert _dispatch(world, bearer, on_behalf_of=JJ).status_code == 201
    message(world["colleague"])  # then a colleague writes: it acts for them now, not him
    r = _dispatch(world, bearer, on_behalf_of=JJ)
    assert r.status_code == 403 and "col@dimagi.com" in r.json()["detail"]


def test_a_client_cannot_write_its_own_on_behalf_record(world):
    bearer, _ = PersonalToken.create_for_user(user=world["hal"].user, label="canopy-pat")
    r = _dispatch(world, bearer, runner_id=None,
                  origin_ref={"on_behalf_of": {"email": JJ, "user_id": world["jj"].pk}})
    assert r.status_code == 201, r.content
    assert "on_behalf_of" not in Turn.objects.get(pk=r.json()["id"]).origin_ref
