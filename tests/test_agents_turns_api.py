"""API-level (ninja route) tests for the agent turns endpoints — reproduces the
live 500 the service-level tests missed."""
from __future__ import annotations

import pytest

from apps.agents import services
from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def authed_user(django_user_model):
    return django_user_model.objects.create_user(username="dev", email="dev@dimagi.com", password="pw")


@pytest.fixture()
def workspace(authed_user):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=authed_user)
    WorkspaceMembership.objects.create(user=authed_user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def authed_client(client, authed_user):
    client.force_login(authed_user)
    return client


def _echo(workspace: Workspace) -> Agent:
    # `workspace` is a required keyword on the service now: Agent.workspace is
    # NOT NULL (agents/0013), so the tenant is chosen before the row is written
    # rather than patched on afterwards.
    from types import SimpleNamespace
    return services.upsert_agent(
        SimpleNamespace(slug="echo", name="Echo", description="", persona="", email="", avatar_url=""),
        workspace=workspace,
    )


def test_list_turns_empty(authed_client, workspace):
    _echo(workspace)
    resp = authed_client.get("/api/agents/echo/turns/?limit=1")
    assert resp.status_code == 200, resp.content
    assert resp.json()["items"] == []


def test_post_then_list_turn(authed_client, workspace):
    _echo(workspace)
    resp = authed_client.post(
        "/api/agents/echo/turns/",
        data={"cli_session_id": "s1", "title": "Did a thing", "task_ext_ids": ["t1"],
              "work_product_urls": [], "source": "turn"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    resp2 = authed_client.get("/api/agents/echo/turns/?limit=10")
    assert resp2.status_code == 200, resp2.content
    assert resp2.json()["items"][0]["task_ext_ids"] == ["t1"]


def test_turns_are_invisible_for_another_tenants_agent(authed_client):
    """Security review 2026-07-26, hole A: this endpoint's AgentTurnOut
    serializes `share_token`, a public `/share/<token>` transcript link, so
    `_get_agent_or_404` must fail CLOSED for an agent the caller cannot see.

    This used to construct an UNHOMED agent, because that was the strongest
    version of "cannot see" the model allowed. It no longer is — an unhomed
    agent cannot exist (agents/0013; see tests/test_agent_workspace_not_null.py)
    — so the case with something left to prove is the cross-tenant one."""
    from types import SimpleNamespace

    from apps.workspaces.testing import a_workspace

    services.upsert_agent(
        SimpleNamespace(slug="secret", name="Secret", description="", persona="", email="", avatar_url=""),
        # self_join_domains=[] keeps this workspace reachable only by an
        # explicit membership row — there is no more auto-join to silently
        # admit a domain-matching caller.
        workspace=a_workspace("other-tenant", self_join_domains=[]),
    )
    resp = authed_client.get("/api/agents/secret/turns/?limit=10")
    assert resp.status_code == 404


def test_unreported_dispatch_turn_carries_its_prompt_and_result(authed_client, workspace):
    """Most turns are dispatched (schedule, api, email) and run, but the agent
    never files a close-out report — report_title/summary stay empty. The list
    must still hand back what the turn WAS (its prompt, outcome, trigger) or the
    Turns page renders a column of bare dates."""
    from apps.harness.models import Turn

    agent = _echo(workspace)
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="d1", status="done",
        prompt="Fix the brief from Ada's fleet conduct", result_note="opened PR #12",
        origin_ref={"slot": "daily"},
    )
    item = authed_client.get("/api/agents/echo/turns/?limit=10").json()["items"][0]
    assert item["title"] == ""
    assert item["reported_at"] is None
    assert item["prompt"] == "Fix the brief from Ada's fleet conduct"
    assert item["result_note"] == "opened PR #12"
    assert item["origin_ref"] == {"slot": "daily"}
    assert (item["status"], item["origin"]) == ("done", "api")


# ---- where a turn's work lives: chat_session_id / has_transcript --------------

def _turn(agent, key, **kw):
    from apps.harness.models import Turn

    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, **kw)


def _runner_session(agent, session_key, created_at=None):
    from apps.canopy_sessions.models import RunnerBinding, Session

    session = Session.objects.create(
        agent=agent, workspace=agent.workspace, origin=Session.ORIGIN_RUNNER, title=session_key,
    )
    if created_at is not None:
        Session.objects.filter(pk=session.pk).update(created_at=created_at)
    RunnerBinding.objects.create(session=session, session_key=session_key)
    return session


def _items(client):
    return {i["id"]: i for i in client.get("/api/agents/echo/turns/?limit=50").json()["items"]}


def test_a_laptop_turn_links_to_the_emdash_session_it_drove(authed_client, workspace):
    """emdash_task_id on the turn is the RunnerBinding.session_key the runner's
    session report records — the join that takes a turn to its chat."""
    from django.utils import timezone

    agent = _echo(workspace)
    session = _runner_session(agent, "c-daily-turn-ad53")
    turn = _turn(agent, "t1", status="done", emdash_task_id="c-daily-turn-ad53",
                 finished_at=timezone.now())
    item = _items(authed_client)[str(turn.id)]
    assert item["chat_session_id"] == str(session.id)
    assert item["has_transcript"] is False


def test_a_reused_session_name_never_links_to_a_later_session(authed_client, workspace):
    """emdash task names get reused. A session that only came into being after
    the turn finished is a different conversation that shares the name."""
    import datetime as dt

    from django.utils import timezone

    agent = _echo(workspace)
    finished = timezone.now() - dt.timedelta(days=3)
    old = _runner_session(agent, "run", created_at=finished - dt.timedelta(seconds=5))
    _runner_session(agent, "run", created_at=timezone.now())
    turn = _turn(agent, "t1", status="done", emdash_task_id="run", finished_at=finished)
    assert _items(authed_client)[str(turn.id)]["chat_session_id"] == str(old.id)


def test_a_chat_turn_links_to_its_own_session(authed_client, workspace):
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    agent = _echo(workspace)
    session = Session.objects.create(agent=agent, workspace=workspace, title="chat")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key="c1", status="done")
    # A chat turn targets the session, not the agent, so it is not on the agent's
    # own turn list — the link is exercised directly.
    from apps.agents import services as agent_services

    agent_services._link_turn_sessions(agent, [turn])
    assert turn.linked_session_id == session.id


def test_a_cloud_turn_has_no_session_but_has_its_transcript(authed_client, workspace):
    from apps.harness import services as harness_services

    agent = _echo(workspace)
    turn = _turn(agent, "t1", status="done", session_id="cloud-609d0a02")
    harness_services.append_transcript(turn, ['{"type": "result"}'])
    item = _items(authed_client)[str(turn.id)]
    assert item["chat_session_id"] is None
    assert item["has_transcript"] is True
