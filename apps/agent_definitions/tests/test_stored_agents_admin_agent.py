"""An agent that administers ANOTHER agent publishes and rolls back that agent's definition.

The rule (`harness.acting_person.person_act_as_or_admin_agent`): publish and rollback are a
person admin's act, an act-as agent's for them, or another agent's own login (its PAT or
OAuth, or the login canopy-web issued for one of its turns) when that agent administers the
target and is not the target. The revision names it (`published_by_agent`, `published_by`
"<slug> (agent)"). Its drafts of that agent ask no approval; a pending approval a draft
filed is closed as superseded when anyone publishes the draft directly.

Still refused: an agent publishing ITSELF (even as its own admin through a workspace role),
an agent that is not an admin, caller and contact tokens, runners, and
`X-Canopy-On-Behalf-Of`. Convert, export and detach stay a person's act.

An agent whose login creates an agent administers it, beside the person who owns it, and
stays its admin when ownership is transferred.
"""
from __future__ import annotations

import importlib
import json
import uuid
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.agents import definition_git as git
from apps.agents import definition_store as store
from apps.agents.models import Agent, AgentAdmin, AgentDefinitionRevision
from apps.approvals import services as approvals
from apps.approvals.models import ApprovalRequest
from apps.harness import acting_person, caller_tokens, turn_agent_tokens
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.tokens import kinds
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member
from tests.fixtures.stored_agents import SLUG, _browser, _create, _draft, _files, _post, _runner_client
from tests.token_helpers import bearer_client, pat_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def ace(w, agent):
    """Another agent, whose login administers `scout`."""
    login = a_member(w["ws"], email="ace@agents.test", role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=w["ws"], owner=w["owner"], user=login)
    AgentAdmin.objects.create(agent=agent, user=login, granted_by=w["owner"])
    return ace


@pytest.fixture
def nova(w):
    """An agent whose login is an editor of the workspace and administers nothing."""
    login = a_member(w["ws"], email="nova@agents.test", role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug="nova", name="Nova", workspace=w["ws"], owner=w["owner"], user=login)


def _publish(client, number=2, **extra):
    return _post(client, f"/api/agents/{SLUG}/definition/revisions/{number}/publish", **extra)


def _live(agent) -> int:
    agent.refresh_from_db()
    return agent.live_revision.number


def _turn_login(w, ace) -> str:
    box = Runner.objects.create(name=f"box-{uuid.uuid4().hex[:6]}", kind=Runner.CLOUD, owner=w["owner"],
                                workspace=w["ws"], last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=ace, runner=box, rank=0)
    turn = Turn.objects.create(agent=ace, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="publish scout",
                               idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_via="chat", initiator_assurance="session",
                               initiator_user=w["owner"])
    return turn_agent_tokens.issue(turn, box)["token"]


# --- an admin agent publishes and rolls back -------------------------------------------


def test_an_admin_agent_publishes_another_agents_draft_and_is_named(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _publish(pat_client(ace.user))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["published_by"] == "ace (agent)" and body["published_by_agent"] == "ace"
    assert body["acted_by"] is None
    agent.refresh_from_db()
    live = agent.live_revision
    assert live.number == 2 and live.published_by_agent == ace and live.published_by == ace.user
    assert live.acting_agent is None and live.act_as_grant is None
    assert json.loads(store.read_file(live, ".claude-plugin/plugin.json"))["version"] == "0.2.0"
    # The Definition tab reads it from the definition and the revision list.
    d = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition").json()
    assert d["live"]["published_by"] == "ace (agent)" and d["live"]["published_by_agent"] == "ace"
    revs = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition/revisions").json()
    assert revs[0]["published_by_agent"] == "ace" and revs[1]["published_by_agent"] is None


def test_an_admin_agent_rolls_back(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _publish(_browser(w["owner"])).status_code == 200
    r = _post(pat_client(ace.user), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1})
    assert r.status_code == 201, r.content
    assert r.json()["published_by"] == "ace (agent)"
    rev = AgentDefinitionRevision.objects.get(agent=agent, number=3)
    assert rev.status == "published" and rev.published_by_agent == ace and rev.by_agent
    commit = store.object_store(agent)[rev.commit.encode("ascii")]
    assert commit.author == git.agent_author("ace")
    assert _live(agent) == 3


def test_an_admin_agents_turn_login_publishes(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _publish(bearer_client(_turn_login(w, ace)))
    assert r.status_code == 200, r.content
    assert r.json()["published_by_agent"] == "ace"


def test_can_publish_says_so_to_an_admin_agent_and_not_to_the_agent_itself(w, agent, ace):
    assert pat_client(ace.user).get(f"/api/agents/{SLUG}/definition").json()["can_publish"] is True
    assert pat_client(w["bot"]).get(f"/api/agents/{SLUG}/definition").json()["can_publish"] is False


# --- still refused ----------------------------------------------------------------------


def test_an_agent_never_publishes_or_rolls_back_itself(w, agent):
    # scout's own login holds an explicit admin grant AND owns the workspace: still its own definition.
    AgentAdmin.objects.create(agent=agent, user=w["bot"], granted_by=w["owner"])
    WorkspaceMembership.objects.filter(workspace=w["ws"], user=w["bot"]).update(role=WorkspaceMembership.OWNER)
    assert agent.is_admin(w["bot"])
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _publish(pat_client(w["bot"]))
    assert r.status_code == 403 and r.json()["reason"] == acting_person.OWN_DEFINITION
    r = _post(pat_client(w["bot"]), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1})
    assert r.status_code == 403
    assert _live(agent) == 1


def test_an_agent_that_is_not_an_admin_publishes_nothing(w, agent, nova):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _publish(pat_client(nova.user)).status_code == 403
    assert _post(pat_client(nova.user), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1}).status_code == 403
    assert _live(agent) == 1


def test_an_admin_agent_naming_a_person_it_acts_for_publishes_nothing(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _publish(pat_client(ace.user), HTTP_X_CANOPY_ON_BEHALF_OF="olive@dimagi.com")
    assert r.status_code == 403
    assert _live(agent) == 1


def test_an_admin_agent_naming_another_agents_turn_publishes_nothing(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    theirs = Turn.objects.create(agent=agent, origin="api", prompt="x", idempotency_key="t-scout",
                                 status=Turn.RUNNING)
    r = _publish(pat_client(ace.user), HTTP_X_CANOPY_TURN_ID=str(theirs.pk))
    assert r.status_code == 403 and r.json()["reason"] == "turn"
    assert _live(agent) == 1


def test_a_caller_token_of_an_admin_agents_turn_publishes_nothing(w, agent, ace):
    # The turn's asker (olive) administers scout; her turn's caller token still may not.
    assert _draft(_browser(w["editor"])).status_code == 201
    turn = Turn.objects.create(agent=ace, origin="api", prompt="x", idempotency_key="t-ace", status=Turn.RUNNING,
                               initiator_kind="user", initiator_user=w["owner"], initiator_assurance="session")
    for scoped in (False, True):
        assert _publish(bearer_client(caller_tokens.mint(turn, scoped=scoped))).status_code in (401, 403)
    assert _live(agent) == 1


def test_a_runner_publishes_nothing_even_for_an_admin_agent(w, agent, ace):
    assert _draft(_browser(w["editor"])).status_code == 201
    box, _ = _runner_client(w["owner"], w["ws"], ace)
    assert _publish(box).status_code in (401, 403)
    assert _live(agent) == 1


def test_convert_export_and_detach_stay_a_persons_act(w, agent, ace, repo_agent):
    AgentAdmin.objects.create(agent=repo_agent, user=ace.user, granted_by=w["owner"])
    c = pat_client(ace.user)
    r = _post(c, "/api/agents/ranger/definition/convert", {"files": _files(**{"CLAUDE.md": "# R\n"}), "message": "m"})
    assert r.status_code == 403 and r.json()["error"] == "not_a_person"
    r = _post(c, f"/api/agents/{SLUG}/definition/detach", {"repo_url": "https://github.com/olive/scout"})
    assert r.status_code == 403
    r = _post(c, f"/api/agents/{SLUG}/definition/export", {"repo": "olive/scout", "github_token": "x" * 40})
    assert r.status_code == 403
    agent.refresh_from_db()
    repo_agent.refresh_from_db()
    assert agent.source == "stored" and repo_agent.source == "repo"


# --- the check itself -------------------------------------------------------------------


def _req(user, cred_type=kinds.PAT, **extra):
    meta = extra.pop("META", {})
    return SimpleNamespace(user=user, auth_credential={"type": cred_type, "id": 1}, META=meta, **extra)


def test_admin_agent_answers_every_principal(w, agent, ace, nova):
    assert acting_person.admin_agent(_req(ace.user), agent) == ace
    assert acting_person.admin_agent(_req(ace.user, kinds.OAUTH), agent) == ace
    assert acting_person.admin_agent(_req(w["bot"]), agent) == acting_person.OWN_DEFINITION
    assert acting_person.admin_agent(_req(nova.user), agent) == acting_person.AGENT_NOT_ADMIN
    assert acting_person.admin_agent(_req(ace.user, kinds.CALLER_TOKEN), agent) == "turn"
    assert acting_person.admin_agent(_req(ace.user, kinds.SESSION), agent) == "not_an_agent"
    assert acting_person.admin_agent(_req(ace.user, runner_token=object()), agent) == "runner"
    assert acting_person.admin_agent(_req(ace.user, contact=object()), agent) == "contact"
    assert acting_person.admin_agent(
        _req(ace.user, META={"HTTP_X_CANOPY_ON_BEHALF_OF": "olive@dimagi.com"}), agent) == "on_behalf"
    # A person is no agent; the combined gate lets them through as themselves.
    assert acting_person.admin_agent(_req(w["admin"]), agent) == "not_an_agent"
    assert acting_person.person_act_as_or_admin_agent(_req(w["admin"]), agent) == (w["admin"], None, None)
    assert acting_person.person_act_as_or_admin_agent(_req(ace.user), agent) == (ace.user, None, ace)
    assert acting_person.person_act_as_or_admin_agent(_req(w["bot"]), agent) == acting_person.OWN_DEFINITION


# --- drafts and their approvals ---------------------------------------------------------


def test_an_admin_agents_draft_asks_no_approval(w, agent, ace):
    r = _draft(pat_client(ace.user))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["approval_id"] is None and body["author"]["by_agent"] is True
    assert not ApprovalRequest.objects.filter(kind="agent.definition.publish").exists()
    rev = AgentDefinitionRevision.objects.get(agent=agent, number=2)
    assert store.object_store(agent)[rev.commit.encode("ascii")].author == git.agent_author("ace")
    assert _publish(pat_client(ace.user)).status_code == 200


def test_an_agents_own_draft_and_a_non_admin_agents_draft_still_ask(w, agent, nova):
    own = _draft(pat_client(w["bot"])).json()
    assert own["approval_id"] is not None
    other = _draft(pat_client(nova.user)).json()
    req = ApprovalRequest.objects.get(pk=other["approval_id"])
    assert req.requested_by_agent == nova and "nova's change to Scout" in req.title


def test_publishing_directly_closes_the_pending_approval_as_superseded(w, agent, ace):
    rid = _draft(pat_client(w["bot"])).json()["approval_id"]
    assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.PENDING
    assert _publish(pat_client(ace.user)).status_code == 200
    req = ApprovalRequest.objects.get(pk=rid)
    assert req.status == ApprovalRequest.CANCELLED and req.result == {"superseded": True}
    assert "ace (agent)" in req.comment


def test_a_person_publishing_directly_also_supersedes_it(w, agent):
    rid = _draft(pat_client(w["bot"])).json()["approval_id"]
    assert _publish(_browser(w["owner"])).status_code == 200
    assert ApprovalRequest.objects.get(pk=rid).result == {"superseded": True}


def test_approving_still_publishes_and_closes_as_done(w, agent):
    rid = _draft(pat_client(w["bot"])).json()["approval_id"]
    req = approvals.decide(rid, w["owner"], "approve", stepped_up=True)
    assert req.status == ApprovalRequest.DONE, req.error
    assert _live(agent) == 2


def test_an_admin_agent_rolling_past_a_pending_draft_closes_its_approval(w, agent, ace):
    rid = _draft(pat_client(w["bot"])).json()["approval_id"]
    assert _post(pat_client(ace.user), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1}).status_code == 201
    assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.CANCELLED


# --- the creator stays an admin ---------------------------------------------------------


def test_an_agent_that_creates_a_stored_agent_administers_it(w, agent, ace):
    r = _create(pat_client(ace.user), slug="guide")
    assert r.status_code == 201, r.content
    guide = Agent.objects.get(slug="guide")
    assert guide.owner == w["owner"]
    assert AgentAdmin.objects.filter(agent=guide, user=ace.user).exists() and guide.is_admin(ace.user)
    # Ownership moves to another person; the creator stays an admin and may still publish.
    assert _browser(w["owner"]).put("/api/agents/guide/owner", data={"user_id": w["editor"].pk},
                                    content_type="application/json").status_code == 200
    guide.refresh_from_db()
    assert guide.owner == w["editor"] and guide.is_admin(ace.user)
    r = _post(pat_client(ace.user), "/api/agents/guide/definition/drafts", {
        "base": 1, "message": "m",
        "changes": [{"path": "persona.md", "content": "Guides walkers.\n"}]})
    assert r.status_code == 201 and r.json()["approval_id"] is None
    assert _post(pat_client(ace.user), "/api/agents/guide/definition/revisions/2/publish").status_code == 200


def test_a_person_who_creates_one_adds_no_agent_admin(w, agent):
    assert _create(_browser(w["editor"]), slug="plain").status_code == 201
    assert not AgentAdmin.objects.filter(agent__slug="plain").exists()


def test_an_agent_that_registers_a_repo_agent_administers_it(w, agent, ace):
    r = pat_client(ace.user).post("/api/agents/", content_type="application/json", data={
        "slug": "rover", "name": "Rover", "workspace": "scouts", "repo_url": "https://github.com/olive/rover"})
    assert r.status_code == 201, r.content
    rover = Agent.objects.get(slug="rover")
    assert rover.owner == w["owner"] and AgentAdmin.objects.filter(agent=rover, user=ace.user).exists()


def test_the_migration_makes_each_creating_agent_an_admin_once(w, agent, ace, nova):
    # `guide` was created by ace's login, `plain` by a person; scout's revision 1 is olive's.
    assert _create(pat_client(ace.user), slug="guide").status_code == 201
    assert _create(_browser(w["editor"]), slug="plain").status_code == 201
    AgentAdmin.objects.filter(agent__slug="guide").delete()
    # An agent's own login as its revision 1's author makes it no admin of itself.
    AgentDefinitionRevision.objects.filter(agent=agent, number=1).update(author_user=w["bot"])
    migration = importlib.import_module("apps.agents.migrations.0143_creator_agent_admins")
    migration.creators_administer(django_apps, None)
    migration.creators_administer(django_apps, None)
    assert list(AgentAdmin.objects.filter(agent__slug="guide").values_list("user_id", flat=True)) == [ace.user.pk]
    assert not AgentAdmin.objects.filter(agent__slug="plain").exists()
    assert not AgentAdmin.objects.filter(agent=agent, user=w["bot"]).exists()
