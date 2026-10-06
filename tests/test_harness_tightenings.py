"""Five doors that skipped a rule the design already states — each closed, and the
legitimate path through it pinned beside the bypass.

1. `origin=email` names its own (possibly VERIFIED) asker from `origin_ref`, so
   only the agent's admins — the owner of the runner that read the mailbox — may
   post one (runner_may_hold_agent).
2. A PINNED project turn must name a runner that declares that repo; the claim's
   pin arm skips target matching, so the enqueue is the only place to ask.
3. `record-session` with a session-id thread_key binds only a session of THIS
   target, in a tenant the runner's owner belongs to (404 otherwise).
4. A new chat's `runner_id` is a pin, decided at create by `may_pin_runner`.
5. Changing an existing agent's repo/engine/secrets/sources is its admins';
   re-sending the stored values is not a change.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

PASS_ALL = ("mx.google.com; dkim=pass header.i=@dimagi.com; spf=pass "
            "smtp.mailfrom=dimagi.com; dmarc=pass (p=NONE) header.from=dimagi.com")


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner,
                                 repo_url="https://github.com/dimagi/echo", repo_ref="main",
                                 runtime_secrets=["gog-token"])
    return owner, editor, ws, agent


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _pat(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="runner")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _post(c, url, body):
    return c.post(url, body, content_type="application/json")


def _email_turn(key="e-1"):
    return {"agent_slug": "echo", "origin": "email", "idempotency_key": key,
            "origin_ref": {"from": "jj@dimagi.com", "thread_id": "t-1", "subject": "hi",
                           "headers": [{"name": "Authentication-Results", "value": PASS_ALL}]}}


# --- 1. origin=email ---------------------------------------------------------------


def test_an_editor_cannot_post_an_email_turn_and_be_promoted_to_the_owner(world):
    _o, editor, _ws, _a = world
    r = _post(_pat(editor), "/api/harness/turns/", _email_turn())
    assert r.status_code == 403, r.content
    assert "mailbox" in r.json()["detail"]
    assert not Turn.objects.exists()


def test_the_agents_admin_runner_owner_still_posts_email_turns(world):
    owner, editor, _ws, agent = world
    r = _post(_pat(owner), "/api/harness/turns/", _email_turn())
    assert r.status_code == 201, r.content
    # An explicit admin is a holder too (runner_may_hold_agent) — same door.
    AgentAdmin.objects.create(agent=agent, user=editor, granted_by=owner)
    r2 = _post(_pat(editor), "/api/harness/turns/", _email_turn("e-2"))
    assert r2.status_code == 201, r2.content


def test_an_email_project_turn_is_refused(world):
    owner, _e, _ws, _a = world
    r = _post(_as(owner), "/api/w/canopy/harness/turns/",
              {"project": "canopy-web", "origin": "email", "idempotency_key": "p-e"})
    assert r.status_code == 403, r.content


# --- 2. pinned project turns -------------------------------------------------------


def _runner(owner, ws, projects=(), name="box"):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, host=name, owner=owner,
                                 workspace=ws, capabilities={"projects": list(projects)})


def test_a_pinned_project_turn_needs_a_runner_that_declares_the_repo(world):
    _o, editor, ws, _a = world
    box = _runner(editor, ws, projects=["ace"])
    r = _post(_as(editor), "/api/w/canopy/harness/turns/",
              {"project": "canopy-web", "origin": "manual", "idempotency_key": "p1",
               "runner_id": str(box.id)})
    assert r.status_code == 403, r.content
    assert "does not declare" in r.json()["detail"]
    assert not Turn.objects.exists()


def test_a_pinned_project_turn_on_a_declaring_runner_and_an_unpinned_one_still_work(world):
    _o, editor, ws, _a = world
    box = _runner(editor, ws, projects=["canopy-web"])
    pinned = _post(_as(editor), "/api/w/canopy/harness/turns/",
                   {"project": "canopy-web", "origin": "manual", "idempotency_key": "p2",
                    "runner_id": str(box.id)})
    assert pinned.status_code == 201, pinned.content
    assert Turn.objects.get(idempotency_key="p2").pinned_runner_id == box.id
    unpinned = _post(_as(editor), "/api/w/canopy/harness/turns/",
                     {"project": "never-declared", "origin": "manual", "idempotency_key": "p3"})
    assert unpinned.status_code == 201, unpinned.content


# --- 3. record-session -------------------------------------------------------------


def test_record_session_will_not_rebind_another_agents_chat(world):
    owner, _e, ws, _a = world
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    box = _runner(owner, ws)
    hals_chat = Session.objects.create(workspace=ws, agent=hal, created_by=owner,
                                       origin=Session.ORIGIN_WEB, title="hal chat")
    r = _post(_as(owner), f"/api/harness/runners/{box.id}/record-session",
              {"agent_slug": "echo", "thread_key": str(hals_chat.id), "session_key": "x"})
    assert r.status_code == 404, r.content
    assert not RunnerBinding.objects.filter(session=hals_chat).exists()


def test_record_session_binds_its_own_agents_chat(world):
    owner, _e, ws, agent = world
    box = _runner(owner, ws)
    chat = Session.objects.create(workspace=ws, agent=agent, created_by=owner,
                                  origin=Session.ORIGIN_WEB, title="echo chat")
    r = _post(_as(owner), f"/api/harness/runners/{box.id}/record-session",
              {"agent_slug": "echo", "thread_key": str(chat.id), "session_key": "echo-1"})
    assert r.status_code == 200, r.content
    assert RunnerBinding.objects.get(session=chat).runner_id == box.id


# --- 4. a new chat's runner_id -----------------------------------------------------


def test_a_new_chat_cannot_pin_an_agent_to_a_box_the_caller_does_not_administer(world):
    owner, editor, ws, _a = world
    owners_box = _runner(owner, ws, name="jj-box")
    r = _post(_as(editor), "/api/w/canopy/canopy-sessions/",
              {"agent_slug": "echo", "runner_id": str(owners_box.id)})
    assert r.status_code == 403, r.content
    assert "jj-box" not in r.json()["detail"]
    assert not Session.objects.exists()


def test_a_new_chat_may_pin_a_box_the_caller_administers_or_any_box_as_admin(world):
    owner, editor, ws, _a = world
    eds_box = _runner(editor, ws, name="ed-box")
    r = _post(_as(editor), "/api/w/canopy/canopy-sessions/",
              {"agent_slug": "echo", "runner_id": str(eds_box.id)})
    assert r.status_code == 200, r.content
    r2 = _post(_as(owner), "/api/w/canopy/canopy-sessions/",
               {"agent_slug": "echo", "runner_id": str(eds_box.id)})
    assert r2.status_code == 200, r2.content


# --- 5. agent definition fields ----------------------------------------------------


def _upsert(c, **fields):
    return _post(c, "/api/w/canopy/agents/", {"slug": "echo", "name": "Echo", **fields})


@pytest.mark.parametrize("field,value", [
    ("repo_url", "https://github.com/evil/echo"),
    ("repo_ref", "evil-branch"),
    ("runtime_engine", "cloud_p"),
    ("runtime_secrets", ["gog-token", "aws-root"]),
    ("runtime_sources", {"gog-token": "op://Elsewhere/x"}),
])
def test_an_editor_cannot_change_what_an_existing_agent_runs(world, field, value):
    _o, editor, _ws, agent = world
    before = getattr(agent, field)
    r = _upsert(_as(editor), **{field: value})
    assert r.status_code == 403, r.content
    assert field in r.json()["detail"]
    agent.refresh_from_db()
    assert getattr(agent, field) == before


def test_an_editor_may_resend_the_stored_definition_and_edit_the_rest(world):
    _o, editor, _ws, agent = world
    r = _upsert(_as(editor), description="new words", repo_url=agent.repo_url,
                repo_ref=agent.repo_ref, runtime_secrets=agent.runtime_secrets)
    assert r.status_code == 201, r.content
    agent.refresh_from_db()
    assert agent.description == "new words"


def test_an_editor_may_create_an_agent_with_a_repo_and_an_admin_may_change_it(world):
    owner, editor, _ws, agent = world
    r = _post(_as(editor), "/api/w/canopy/agents/",
              {"slug": "nova", "name": "Nova", "repo_url": "https://github.com/dimagi/nova"})
    assert r.status_code == 201, r.content
    r2 = _upsert(_as(owner), repo_url="https://github.com/dimagi/echo2")
    assert r2.status_code == 201, r2.content
    agent.refresh_from_db()
    assert agent.repo_url == "https://github.com/dimagi/echo2"
