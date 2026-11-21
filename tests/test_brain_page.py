"""The `/brain` page's routes: `GET /api/brain/state`, `/state/links` and `/state/runs`.

Person-only (never an agent's login, a turn's token, an act-as token or an MCP call), legs 1 to 6
only (a reader who has a session only through the brain's project leg sees no row of it), workspace-wide counts only
where the viewer holds `logs.read`, and not one write: every route's queries are captured and none writes."""
from __future__ import annotations


import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import consent
from apps.brain.models import BrainOutput, BrainRun, BrainRunRead, RederiveRequest
from apps.canopy_sessions.models import Session
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.mcp import api_tools
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.brain_project_leg import SECRET, client, project_leg_world
from tests.fixtures.brain_world import member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

STATE, LINKS, RUNS = "/api/brain/state", "/api/brain/state/links", "/api/brain/state/runs"
ROUTES = (STATE, LINKS, RUNS)


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = True          # the leg-7 reader really has the session through the project leg
    out = project_leg_world()
    learner = out["proj"]
    learner.registered_by = out["owner"]
    learner.save(update_fields=["registered_by"])
    run = BrainRun.objects.create(capability=learner, subject_type="project", subject=str(out["project"].pk),
                                  purpose="learn", state=BrainRun.DONE)
    BrainRunRead.objects.create(run=run, source=out["source"])
    out["run"] = run
    output = BrainOutput.objects.create(capability=learner, kind="note", workspace=out["ws"], subject_type="project",
                                        subject=str(out["project"].pk), title="t", state=BrainOutput.STALE)
    RederiveRequest.objects.create(output=output)
    consent.exclude(out["carol"], _carol_source(out))
    return out


def _carol_source(out):
    from apps.brain import sources

    s = Session.objects.create(workspace=out["ws"], created_by=out["carol"], origin=Session.ORIGIN_WEB, title="c")
    return sources.for_session(s)


def _writes(ctx) -> list[str]:
    return [q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


def test_a_member_sees_their_own_and_counts(w):
    body = client(w["alice"]).get(STATE).json()
    assert body["reads_enabled"] is True and body["workspaces"] == ["acme"]
    assert body["admin_workspaces"] == [] and body["canopy_admin"] is False
    assert body["consent"] == {"mine": ["project"], "workspaces": []}           # counts are the owners' and admins'

    # Sources: only the ones alice is in (she holds no logs.read), and nobody's name anywhere.
    assert body["sources"] == [{"workspace": "acme", "scope": "yours", "open": 1, "withdrawn": 0, "sealed": {}}]
    assert body["links"] == [{"project": "supply", "name": "Supply", "workspace": "acme", "tier": "core", "count": 1}]
    assert {x["slug"] for x in body["learners"]} >= {"proj", "project-readers"}
    assert next(x for x in body["learners"] if x["slug"] == "proj")["accountable"] == "jj@dimagi.com"
    assert body["runs"] == {"open": 0, "done": 0, "failed": 0}                 # a project run is not a member's
    # Carol's removal is not alice's: her own scope counts none of it.
    assert all(c["removed_recently"] == 0 for c in body["cascade"])
    assert "carol" not in str(body)


def test_an_admin_sees_the_workspace_whole(w):
    body = client(w["owner"]).get(STATE).json()
    assert body["admin_workspaces"] == ["acme"]
    acme = body["consent"]["workspaces"][0]
    assert acme["members"] == 5 and acme["agreed"]["project"] == 2 and acme["agreed"]["system"] == 0
    assert body["runs"] == {"open": 0, "done": 1, "failed": 0}
    assert body["sources"][0]["scope"] == "workspace" and body["sources"][0]["open"] == 2
    cascade = body["cascade"][0]
    assert cascade["scope"] == "workspace" and cascade["removed_recently"] == 1 and cascade["rederive_open"] == 1


def test_links_and_runs_name_only_sessions_the_viewer_reads(w):
    links = client(w["alice"]).get(LINKS).json()
    assert [(x["project"], x["tier"], x["session_id"]) for x in links["links"]] == [
        ("supply", "core", str(w["session"].pk))]
    assert SECRET not in str(links) and links["done"] is True
    assert client(w["alice"]).get(RUNS).json()["runs"] == []                    # a project run: the owners'
    runs = client(w["owner"]).get(RUNS).json()["runs"]
    assert runs[0]["reads"] == 1 and runs[0]["subject"] == "supply"
    assert len(runs[0]["sessions"]) + runs[0]["hidden"] == 1
    assert all(s["session_id"] == str(w["session"].pk) for s in runs[0]["sessions"])


def test_a_leg_7_only_reader_sees_no_row_of_the_session(w):
    reader = client(w["reader"])
    assert reader.get(f"/api/canopy-sessions/{w['session'].pk}").status_code == 200   # leg 7 is open to them
    assert reader.get(LINKS).json()["links"] == []
    assert reader.get(STATE).json()["links"] == []
    assert reader.get(RUNS).json()["runs"] == []


def test_a_non_member_sees_nothing_of_the_workspace(w):
    other = user("mallory")
    elsewhere = Workspace.objects.create(slug="else", display_name="Else", created_by=other)
    member(other, elsewhere, WorkspaceMembership.OWNER)
    body = client(other).get(STATE).json()
    assert body["workspaces"] == ["else"] and body["sources"] == [] and body["links"] == []
    assert body["runs"] == {"open": 0, "done": 0, "failed": 0} and body["cascade"] == []
    assert [c["workspace"] for c in body["consent"]["workspaces"]] == ["else"]
    assert client(other).get(LINKS).json()["links"] == [] and client(other).get(RUNS).json()["runs"] == []


def test_no_route_writes(w, django_capture_on_commit_callbacks):
    for c in (client(w["alice"]), client(w["owner"]), Client(**bearer_headers(pat_token(w["alice"])))):
        for url in ROUTES:
            with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
                r = c.get(url)
            assert r.status_code == 200, (url, r.content)
            assert all(q.split(" ", 2)[1] in ('"personal_tokens"', '"django_session"') for q in _writes(ctx)), (
                url, _writes(ctx))
            assert callbacks == []


def test_the_page_costs_the_same_for_more_sessions(w):
    def queries(n_extra):
        for _ in range(n_extra):
            Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, title="x")
        with CaptureQueriesContext(connection) as ctx:
            client(w["alice"]).get(STATE)
            client(w["alice"]).get(LINKS)
            client(w["alice"]).get(RUNS)
        return len(ctx.captured_queries)

    assert queries(0) == queries(5)


def test_only_the_person_as_themselves(w):
    owner = w["owner"]
    login = user("hal_login")
    member(login, w["ws"])
    hal = Agent.objects.create(slug="hal", name="Hal", owner=owner, workspace=w["ws"], user=login)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=hal, prompt="x", idempotency_key="k2", status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_user=w["alice"])
    turn_login = turn_agent_tokens.issue(turn, box)["token"]
    for url in ROUTES:
        assert Client().get(url).status_code in (401, 403)                                   # nobody
        assert Client().get(url, **bearer_headers(pat_token(login))).status_code == 403     # an agent's login
        assert Client().get(url, **bearer_headers(turn_login)).status_code == 403           # a turn's token
        assert client(w["alice"]).get(url, HTTP_X_CANOPY_TURN_ID="x").status_code == 403    # naming a turn
        # An act-as-you grant reaches none of it.
        assert act_as.route_refusal("GET", url, "read", is_tool=False) is not None
    for name in ("brain_state", "brain_state_links", "brain_state_runs", "run_brain_capability"):
        assert name in api_tools.EXCLUDED                                                     # not an MCP tool
