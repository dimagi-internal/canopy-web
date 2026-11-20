"""`GET /api/brain/preview`: what the brain WOULD do over the sessions a person reads, storing nothing.

Person-only (browser, hand-minted PAT; the act-as read leg is pinned in `tests/test_act_as_you_on_canopy.py`),
legs 1 to 6 only, answers with the rollout gate off, and the whole call writes nothing: no row, no source, no
on-commit callback."""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.brain import associate, consent, preview, registry, sources
from apps.brain.models import BRAIN_RUN_KEY, BrainSource, LearningConsent, SourceLink
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.harness import turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.mcp import api_tools
from tests.fixtures.brain_world import member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

URL = "/api/brain/preview"
SECRET = "ghp_" + "0123456789abcdefABCDEF" + "0123456789abcd"


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = False                 # the preview needs no gate: it reads what you already read
    from apps.workspaces.models import Workspace, WorkspaceMembership

    registry.ensure_builtin()
    owner = user("jj")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    member(owner, ws, WorkspaceMembership.OWNER)
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    for u in (alice, bob, carol):
        member(u, ws)
    shared = Session.objects.create(workspace=ws, created_by=alice, origin=Session.ORIGIN_WEB, title="shared")
    SessionParticipant.objects.create(session=shared, user=bob)
    rows = [(Message.USER, f"deploy with {SECRET}", {}, {"user_id": alice.pk, "name": "Alice"}),
            (Message.TOOL_USE, "", {"id": "t1", "name": "Bash", "input": {"command": "make test"}}, None),
            (Message.TOOL_RESULT, "3 passed", {"tool_use_id": "t1", "is_error": False}, None),
            (Message.ASSISTANT, "all green", {}, None),
            (Message.USER, "bob's words", {}, {"user_id": bob.pk, "name": "Bob"})]
    for i, (role, text, content, author) in enumerate(rows):
        Message.objects.create(session=shared, role=role, turn_index=i, plaintext=text, content=content, author=author)
    carols = Session.objects.create(workspace=ws, created_by=carol, origin=Session.ORIGIN_WEB, title="carol's own")
    run = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="a run",
                                 metadata={BRAIN_RUN_KEY: 1}, created_by=alice)
    return {"ws": ws, "owner": owner, "alice": alice, "bob": bob, "carol": carol, "shared": shared,
            "carols": carols, "run": run}


def _browser(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


def _get(client, **params):
    return client.get(URL, params)


def test_a_person_previews_the_sessions_they_read_and_nothing_else(w):
    r = _get(_browser(w["alice"]))
    assert r.status_code == 200, r.content
    body = r.json()
    assert [s["title"] for s in body["sessions"]] == ["shared"]          # not carol's, not a learning run's
    assert body["done"] is True and body["view"] == "full"
    s = body["sessions"][0]
    assert {(p["user_id"], p["name"]) for p in s["participants"]} == {(w["alice"].pk, "alice@dimagi.com"),
                                                                      (w["bob"].pk, "bob@dimagi.com")}
    roles = [m["role"] for m in s["transcript"]["messages"]]
    assert roles == ["user", "tool_use", "tool_result", "assistant", "user"]       # whole: multiplayer is not cut
    texts = " ".join(m["text"] for m in s["transcript"]["messages"])
    assert SECRET not in texts and "redacted" in texts and "bob's words" in texts
    assert s["transcript"]["messages"][2]["tool"] == {"name": "Bash"}
    assert BrainSource.objects.count() == 0                               # nothing made for it


def test_a_narrower_view_and_the_window(w):
    body = _get(_browser(w["alice"]), view="userinput").json()
    assert [m["text"] for m in body["sessions"][0]["transcript"]["messages"]][1:] == ["bob's words"]
    old = timezone.now() - dt.timedelta(days=3)
    Session.objects.filter(pk=w["shared"].pk).update(created_at=old)
    Message.objects.filter(session=w["shared"]).update(created_at=old)
    assert _get(_browser(w["alice"])).json()["sessions"] == []
    since = (timezone.now() - dt.timedelta(days=4)).isoformat()
    assert len(_get(_browser(w["alice"]), since=since).json()["sessions"]) == 1
    too_long = (timezone.now() - dt.timedelta(days=30)).isoformat()
    assert _get(_browser(w["alice"]), since=too_long).status_code == 422


def test_would_read_now_and_if_everyone_agreed_with_reasons(w):
    LearningConsent.objects.create(user=w["alice"], classes=["project", "individual"])
    s = _get(_browser(w["alice"])).json()["sessions"][0]
    proj = s["would_read"]["project"]
    assert proj["now"] == {"ok": False, "reason": f"no_consent:{w['bob'].pk}"}
    assert proj["if_everyone_agreed"] == {"ok": True, "reason": ""}
    ind = s["would_read"]["individual"]
    assert ind[str(w["alice"].pk)]["now"]["ok"] is True and ind[str(w["bob"].pk)]["now"]["ok"] is False
    # A removal is a hard fence: assuming consent does not lift it.
    consent.exclude(w["bob"], sources.for_session(w["shared"]))
    s = _get(_browser(w["alice"])).json()["sessions"][0]
    assert s["would_read"]["project"]["if_everyone_agreed"] == {"ok": False, "reason": f"excluded:{w['bob'].pk}"}
    # Ending a consent is a person's act, not a gap.
    LearningConsent.objects.create(user=w["carol"], classes=["system"], revoked_at=timezone.now())
    SessionParticipant.objects.create(session=w["carols"], user=w["alice"])
    rows = {x["title"]: x for x in _get(_browser(w["alice"])).json()["sessions"]}
    assert rows["carol's own"]["would_read"]["system"]["if_everyone_agreed"]["reason"] == f"no_consent:{w['carol'].pk}"


def test_links_are_what_the_layers_would_make_and_a_person_s_rejection_keeps_its_place(w):
    lead = Agent.objects.create(slug="eva", name="Eva", owner=w["owner"], workspace=w["ws"])
    project = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply", workspace=w["ws"])
    task = AgentTask.objects.create(agent=lead, project=project, ext_id="T1", title="tender")
    Turn.objects.create(chat_session=w["shared"], raised_from_task=task, prompt="go", idempotency_key="k1")
    s = _get(_browser(w["alice"])).json()["sessions"][0]
    assert s["links"] == [{"project": "supply", "tier": "core", "how": "dispatched", "evidence": [f"task:{task.pk}"],
                           "outranked_by": None}]
    assert SourceLink.objects.count() == 0 and BrainSource.objects.count() == 0
    associate.declare(sources.for_session(w["shared"]), project, tier="rejected", by=w["alice"])
    s = _get(_browser(w["alice"])).json()["sessions"][0]
    assert s["links"][0]["outranked_by"] == {"how": "declared", "tier": "rejected"}


def test_the_whole_call_writes_nothing(w, django_capture_on_commit_callbacks):
    LearningConsent.objects.create(user=w["alice"], classes=["project"])
    sources.for_session(w["shared"])                                 # a stored source, and one with none
    SessionParticipant.objects.create(session=w["carols"], user=w["alice"])
    with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
        out = preview.preview(w["alice"], {"acme"}, since=timezone.now() - dt.timedelta(days=1),
                              until=timezone.now() + dt.timedelta(minutes=1))
    assert len(out["sessions"]) == 2 and out["sessions"][0]["transcript"]["messages"] is not None
    assert _writes(ctx) == [] and callbacks == []
    assert BrainSource.objects.count() == 1
    # Over the wire, the only writes are the sign-in's own (a PAT's last use, a browser's session row), never the
    # preview's.
    for client in (Client(**bearer_headers(pat_token(w["alice"]))), _browser(w["alice"])):
        with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
            r = _get(client)
        assert r.status_code == 200 and len(r.json()["sessions"]) == 2
        assert all(q.split(" ", 2)[1] in ('"personal_tokens"', '"django_session"') for q in _writes(ctx)), _writes(ctx)
        assert callbacks == []


def _writes(ctx) -> list[str]:
    return [q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


def test_only_the_person_as_themselves(w):
    assert _get(Client()).status_code in (401, 403)
    assert Client().get(URL, **bearer_headers(pat_token(w["alice"]))).status_code == 200       # a PAT they minted
    login = user("hal_login")
    member(login, w["ws"])
    hal = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"], user=login)
    assert Client().get(URL, **bearer_headers(pat_token(login))).status_code == 403           # an agent's login
    browser = _browser(w["alice"])
    assert browser.get(URL, HTTP_X_CANOPY_TURN_ID="x").status_code == 403                   # a request naming a turn
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=hal, prompt="x", idempotency_key="k2", status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_user=w["alice"])
    turn_login = turn_agent_tokens.issue(turn, box)["token"]
    assert Client().get(URL, **bearer_headers(turn_login)).status_code == 403                 # a turn's login
    assert "brain_preview" in api_tools.EXCLUDED                                              # not an MCP tool
