"""A person's own definition acts, done by an agent acting AS them (act-as-you).

Publish, roll back, convert, export, detach and create are a person's acts. An agent may do
them for its person only with an act-as-you token (`apps/harness/act_as.py`) whose
permission is live, approved, `write`, names no other turn, and whose person is the
agent's owner or an admin (`acting_person.person_or_act_as`). The revision, the response
(`acted_by`) and the audit row name the agent and the permission.

Still refused: an agent's own login, a turn's token or login, `X-Canopy-On-Behalf-Of` with
no permission, a runner, and every act-as token that fails one of the conditions.
"""
from __future__ import annotations

import datetime as dt
import secrets
import uuid
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.agent_definitions import services
from apps.agents.models import Agent, AgentDefinitionRevision
from apps.common.token_hash import hash_token
from apps.events.models import SettingChange
from apps.harness import act_as, acting_person, turn_agent_tokens
from apps.harness.models import ActAsGrant, Runner, RunnerAssignment, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member
from tests.fixtures.stored_agents import SLUG, _browser, _draft, _files, _post, _runner_client
from tests.token_helpers import bearer_client, pat_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def ace(w):
    """Another agent, which the people of the workspace may let act as them."""
    login = a_member(w["ws"], email="ace@agents.test", role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug="ace", name="ACE", workspace=w["ws"], owner=w["owner"], user=login)


def _turn(w, ace, person, **kw) -> Turn:
    kw.setdefault("status", Turn.RUNNING)
    return Turn.objects.create(agent=ace, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="publish it",
                               idempotency_key=uuid.uuid4().hex, initiator_kind="user", initiator_via="chat",
                               initiator_assurance="session", initiator_user=person, **kw)


def _token(w, ace, person=None, *, scope=ActAsGrant.WRITE, status=ActAsGrant.APPROVED, turn=None,
           expires_in=dt.timedelta(minutes=30)) -> tuple[str, ActAsGrant]:
    """An act-as-you token for `ace` acting as `person` (the agent's owner by default)."""
    person = person or w["owner"]
    turn = turn or _turn(w, ace, person)
    raw = act_as.PREFIX + secrets.token_urlsafe(32)
    now = timezone.now()
    grant = ActAsGrant.objects.create(
        person=person, agent=ace, turn=turn, login_token_id=1, reason="Publish scout's draft",
        requested_scope=ActAsGrant.EITHER, scope=scope, status=status, request_expires_at=now + expires_in,
        approved_at=now, expires_at=now + expires_in, token_hash=hash_token(raw), token_issued_at=now)
    return raw, grant


# --- the act is done, and attributed --------------------------------------------------


def test_an_agent_acting_as_the_owner_publishes_and_is_named(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    raw, grant = _token(w, ace)
    r = _post(bearer_client(raw), f"/api/agents/{SLUG}/definition/revisions/2/publish")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["acted_by"] == {"agent": "ace", "person": "olive@dimagi.com", "act_as_grant": str(grant.pk)}
    assert body["published_by"] == "olive@dimagi.com"
    agent.refresh_from_db()
    live = agent.live_revision
    assert live.number == 2 and live.acting_agent == ace and live.act_as_grant == grant
    # The Definition tab reads it from the revision list and the definition.
    d = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition").json()
    assert d["live"]["acted_by"]["agent"] == "ace"
    revs = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition/revisions").json()
    assert revs[0]["acted_by"]["act_as_grant"] == str(grant.pk) and revs[1]["acted_by"] is None
    row = SettingChange.objects.filter(path__endswith="/revisions/2/publish").latest("pk")
    assert row.acting == {"agent": "ace", "act_as_grant": str(grant.pk), "for_id": w["owner"].pk,
                          "for": "olive@dimagi.com"}
    assert row.credential["label"] == "ace as olive@dimagi.com"


def test_a_person_who_publishes_themselves_is_named_alone(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish")
    assert r.status_code == 200 and r.json()["acted_by"] is None


def test_an_agent_acting_as_an_admin_rolls_back(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 200
    raw, grant = _token(w, ace, w["admin"])
    r = _post(bearer_client(raw), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1})
    assert r.status_code == 201, r.content
    assert r.json()["acted_by"]["person"] == "ada@dimagi.com"
    assert AgentDefinitionRevision.objects.get(agent=agent, number=3).act_as_grant == grant


def test_an_agent_acting_as_the_owner_converts(w, repo_agent, ace):
    raw, grant = _token(w, ace)
    r = _post(bearer_client(raw), "/api/agents/ranger/definition/convert",
              {"files": _files(**{"CLAUDE.md": "# Ranger\n"}), "message": "move"})
    assert r.status_code == 201, r.content
    assert r.json()["acted_by"]["act_as_grant"] == str(grant.pk)
    repo_agent.refresh_from_db()
    assert repo_agent.source == "stored" and repo_agent.live_revision.acting_agent == ace


def test_an_agent_acting_as_the_owner_detaches(w, agent, ace):
    raw, grant = _token(w, ace)
    r = _post(bearer_client(raw), f"/api/agents/{SLUG}/definition/detach",
              {"repo_url": "https://github.com/olive/scout", "ref": "main"})
    assert r.status_code == 200, r.content
    assert r.json()["acted_by"]["agent"] == "ace"
    agent.refresh_from_db()
    assert agent.source == "repo"


def test_an_agent_acting_as_the_owner_exports(w, agent, ace, monkeypatch):
    seen = {}

    def fake_export(agent, *, repo, github_token, by, ref="main"):
        seen["by"] = by
        return {"repo": repo, "repo_url": f"https://github.com/{repo}", "repo_ref": ref, "commit": "a" * 40,
                "revision": 1, "tags": ["refs/tags/r1"]}

    monkeypatch.setattr(services, "export", fake_export)
    raw, grant = _token(w, ace)
    r = _post(bearer_client(raw), f"/api/agents/{SLUG}/definition/export",
              {"repo": "olive/scout", "github_token": "x" * 40})
    assert r.status_code == 200, r.content
    assert r.json()["acted_by"]["act_as_grant"] == str(grant.pk) and seen["by"] == w["owner"]


def test_an_agent_acting_as_an_editor_creates_a_stored_agent_they_own(w, ace):
    raw, grant = _token(w, ace, w["editor"])
    r = _post(bearer_client(raw), "/api/agents/stored",
              {"name": "Guide", "slug": "guide", "mandate": "Guides walkers.", "workspace": "scouts"})
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["owner"] == "eddie@dimagi.com"
    assert body["acted_by"] == {"agent": "ace", "person": "eddie@dimagi.com", "act_as_grant": str(grant.pk)}
    made = Agent.objects.get(slug="guide")
    assert made.owner == w["editor"] and made.live_revision.acting_agent == ace


# --- refused ---------------------------------------------------------------------------


def _publish(client, **extra):
    return _post(client, f"/api/agents/{SLUG}/definition/revisions/2/publish", **extra)


def _still_one(agent):
    agent.refresh_from_db()
    assert agent.live_revision.number == 1


def test_a_read_only_permission_publishes_nothing(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    raw, _ = _token(w, ace, scope=ActAsGrant.READ)
    assert _publish(bearer_client(raw)).status_code == 403
    _still_one(agent)


@pytest.mark.parametrize("dead", ["expired", "turn_ended", "stopped", "pending"])
def test_a_permission_that_is_not_live_publishes_nothing(w, agent, ace, dead):
    assert _draft(_browser(w["editor"])).status_code == 201
    kw = {}
    if dead == "expired":
        kw["expires_in"] = dt.timedelta(minutes=-1)
    elif dead == "turn_ended":
        kw["turn"] = _turn(w, ace, w["owner"], status=Turn.DONE)
    elif dead == "stopped":
        kw["status"] = ActAsGrant.STOPPED
    else:
        kw["status"] = ActAsGrant.PENDING
    raw, _ = _token(w, ace, **kw)
    assert _publish(bearer_client(raw)).status_code in (401, 403)
    _still_one(agent)


def test_a_permission_of_someone_who_does_not_administer_the_agent_publishes_nothing(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    raw, _ = _token(w, ace, w["editor"])
    assert _publish(bearer_client(raw)).status_code == 403
    _still_one(agent)


def test_a_permission_used_from_another_turn_publishes_nothing(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    raw, grant = _token(w, ace)
    other = _turn(w, ace, w["owner"])
    r = _publish(bearer_client(raw), HTTP_X_CANOPY_TURN_ID=str(other.pk))
    assert r.status_code == 403 and r.json()["reason"] == "turn"
    # Naming its own turn is fine.
    assert _publish(bearer_client(raw), HTTP_X_CANOPY_TURN_ID=str(grant.turn_id)).status_code == 200


def test_an_agents_login_its_turn_login_and_the_on_behalf_header_stay_refused(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    # The acting agent's own login, even naming the owner it works for.
    assert _publish(pat_client(ace.user), HTTP_X_CANOPY_ON_BEHALF_OF="olive@dimagi.com").status_code == 403
    assert _publish(pat_client(ace.user)).status_code == 403
    # The login canopy-web issued for its turn, which works for the owner.
    box = Runner.objects.create(name="box-act", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=ace, runner=box, rank=0)
    turn = _turn(w, ace, w["owner"], claimed_by=box)
    login = turn_agent_tokens.issue(turn, box)["token"]
    assert _publish(bearer_client(login)).status_code == 403
    # The owner's own token, naming a turn.
    assert _publish(pat_client(w["owner"]), HTTP_X_CANOPY_TURN_ID=str(turn.pk)).status_code == 403
    _still_one(agent)


def test_a_runner_publishes_nothing(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    box, _ = _runner_client(w["owner"], w["ws"], agent)
    assert _publish(box).status_code in (401, 403)
    _still_one(agent)


# --- the check itself, as an MCP call reaches it ----------------------------------------


def _request(grant, user, **meta):
    return SimpleNamespace(user=user, act_as_grant=grant, META=meta, via_mcp=True,
                           auth_credential=act_as.credential(grant))


def test_person_or_act_as_over_mcp(w, ace):
    raw, grant = _token(w, ace)
    assert acting_person.person_or_act_as(_request(grant, w["owner"])) == (w["owner"], grant)
    # The token signs in as its own person only.
    assert acting_person.person_or_act_as(_request(grant, w["editor"])) == acting_person.ACT_AS_MISMATCH
    ActAsGrant.objects.filter(pk=grant.pk).update(scope=ActAsGrant.READ)
    assert acting_person.person_or_act_as(_request(grant, w["owner"])) == acting_person.ACT_AS_READ_ONLY
    ActAsGrant.objects.filter(pk=grant.pk).update(scope=ActAsGrant.WRITE, status=ActAsGrant.STOPPED)
    assert acting_person.person_or_act_as(_request(grant, w["owner"])) == acting_person.ACT_AS_DEAD
