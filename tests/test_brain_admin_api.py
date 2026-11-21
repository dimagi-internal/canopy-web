"""Capabilities (registered by a canopy admin only, audience never wider than the class), reporting lines
(set by a workspace owner) and who reads the outputs (by class, never wider)."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.agents.models import Agent, AgentProject
from apps.brain import registry, reporting
from apps.brain.models import BrainCapability, BrainOutput, ReportingLine
from apps.canopy_sessions.models import CoachingRole
from apps.events.models import Event
from apps.mcp.api_tools import excluded_reason
from tests.fixtures.brain_world import brain_world, member, user

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = brain_world()
    out["root"] = user("root")
    out["root"].is_superuser = True
    out["root"].save()
    member(out["root"], out["ws"])
    return out


def client(u):
    c = Client()
    c.force_login(u)
    return c


def send(c, verb, url, body=None):
    return getattr(c, verb)(url, data=json.dumps(body or {}), content_type="application/json")


GOOD = {"slug": "pace", "name": "Pace", "purpose_class": "individual", "purposes": ["coach"]}


# ---- capabilities --------------------------------------------------------------------------------

def test_only_a_canopy_admin_registers_and_it_starts_dark_with_the_whole_class_audience(w):
    assert send(client(w["alice"]), "post", "/api/brain/capabilities/", GOOD).status_code == 403
    assert send(client(w["owner"]), "post", "/api/brain/capabilities/", GOOD).status_code == 403   # a workspace owner is not
    r = send(client(w["root"]), "post", "/api/brain/capabilities/", GOOD)
    assert r.status_code == 200, r.content
    assert r.json()["enabled"] is False and r.json()["audience"] == ["person", "coaches", "manager"]
    assert r.json()["subject_type"] == "person" and r.json()["source_kinds"] == ["session"]
    assert send(client(w["root"]), "post", "/api/brain/capabilities/", GOOD).status_code == 422     # taken
    slugs = [c["slug"] for c in client(w["alice"]).get("/api/brain/capabilities/").json()]
    assert "pace" in slugs and "project-readers" in slugs                                         # anyone may list


@pytest.mark.parametrize("bad", [
    {"audience": ["person", "everyone"]}, {"audience": ["project_readers"]}, {"audience": []},
    {"purpose_class": "nonsense"}, {"purposes": "coach"}, {"source_kinds": ["meeting"]}, {"operator": "nobody"},
])
def test_a_wider_audience_or_a_bad_registration_is_refused(w, bad):
    assert send(client(w["root"]), "post", "/api/brain/capabilities/", {**GOOD, **bad}).status_code == 422
    assert not BrainCapability.objects.filter(slug="pace").exists()


def test_an_audience_may_only_narrow_and_the_class_never_changes(w):
    send(client(w["root"]), "post", "/api/brain/capabilities/", GOOD)
    c = client(w["root"])
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"audience": ["person"], "enabled": True}).json()["audience"] == ["person"]
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"audience": ["person", "manager"]}).status_code == 200
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"audience": ["serving_agents"]}).status_code == 422
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"purpose_class": "system"}).status_code == 422
    assert send(c, "patch", "/api/brain/capabilities/nope/", {"enabled": True}).status_code == 404
    assert send(client(w["alice"]), "patch", "/api/brain/capabilities/pace/", {"enabled": False}).status_code == 403
    with pytest.raises(registry.RefusedError):
        registry.update(BrainCapability.objects.get(slug="pace"), purpose_class="system")


def test_a_scope_starts_whole_may_only_narrow_and_each_change_is_an_event(w):
    from apps.agents.models import Agent
    from apps.events.models import Event

    Agent.objects.create(slug="trellis", name="Trellis", owner=w["owner"], workspace=w["ws"])
    c = client(w["root"])
    assert send(c, "post", "/api/brain/capabilities/", {**GOOD, "operator": "trellis"}).json()["scope"] == "full"
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"scope": "conversation"}).json()["scope"] == "conversation"
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"scope": "full"}).status_code == 422        # wider
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"scope": "everything"}).status_code == 422
    assert send(c, "patch", "/api/brain/capabilities/pace/", {"scope": "conversation"}).status_code == 200  # same
    rows = list(Event.objects.filter(source="brain.capability", kind="capability.scope_changed"))
    assert [(e.payload["from"], e.payload["to"]) for e in rows] == [("full", "conversation")]
    assert rows[0].payload["by_id"] == w["root"].pk and rows[0].workspace_id == "acme"


def test_enabling_a_seeded_learner_makes_the_admin_accountable_for_its_runs(w):
    seeded = registry.ensure_learners()["coaching"]
    assert seeded.registered_by_id is None and not seeded.enabled
    with pytest.raises(registry.RefusedError, match="accountable"):
        registry.update(seeded, enabled=True)                                # nobody to answer for its runs
    r = send(client(w["root"]), "patch", "/api/brain/capabilities/coaching/", {"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    seeded.refresh_from_db()
    assert seeded.registered_by_id == w["root"].pk and registry.accountable(seeded) == w["root"]


def test_a_feature_whose_admin_is_gone_is_not_run_until_another_enables_it(w, settings):
    from apps.brain import runs

    other = user("root2")
    other.is_superuser = True
    other.save()
    op = Agent.objects.create(slug="trellis", name="Trellis", owner=w["owner"], workspace=w["ws"],
                              user=user("trellis_login"))
    cap = registry.register(slug="pace", name="Pace", purpose_class="individual", purposes=["coach"],
                            operator=op, enabled=True, registered_by=w["root"])
    w["root"].is_active = False
    w["root"].save()
    with pytest.raises(runs.StartRefusedError, match="accountable"):
        runs.start(cap, subject=w["alice"])
    assert send(client(other), "patch", "/api/brain/capabilities/pace/", {"schedule": ""}).status_code == 200
    cap.refresh_from_db()
    assert registry.accountable(cap) == other
    with pytest.raises(registry.RefusedError):
        registry.register(slug="dark", name="D", purpose_class="system", purposes=["x"], enabled=True)


# ---- reporting lines -----------------------------------------------------------------------------

def test_an_owner_sets_who_manages_whom_and_each_change_is_an_event(w):
    url = "/api/brain/workspaces/acme/reporting/"
    assert send(client(w["alice"]), "put", url, {"person": "bob@dimagi.com", "manager": "alice@dimagi.com"}).status_code == 403
    r = send(client(w["owner"]), "put", url, {"person": "bob@dimagi.com", "manager": "alice@dimagi.com"})
    assert r.status_code == 200 and r.json()["manager"] == "alice@dimagi.com"
    r = send(client(w["owner"]), "put", url, {"person": "bob@dimagi.com", "manager": "carol@dimagi.com"})   # replaces
    assert ReportingLine.objects.filter(ended_at__isnull=True).count() == 1
    assert ReportingLine.objects.get(ended_at__isnull=True).manager_id == w["carol"].pk
    assert {e.kind for e in Event.objects.filter(source="brain.reporting")} == {"reporting.set", "reporting.ended"}
    assert [x["person_id"] for x in client(w["owner"]).get(url).json()] == [w["bob"].pk]
    assert [x["person_id"] for x in client(w["carol"]).get(url).json()] == [w["bob"].pk]      # a party sees its own line
    assert client(w["alice"]).get(url).json() == []                                           # a bystander sees none
    assert client(w["owner"]).delete(f"{url}{w['bob'].pk}/").status_code == 204
    assert client(w["owner"]).delete(f"{url}{w['bob'].pk}/").status_code == 404


@pytest.mark.parametrize("body", [
    {"person": "alice@dimagi.com", "manager": "alice@dimagi.com"},          # their own manager
    {"person": "alice@dimagi.com", "manager": "stranger@example.org"},       # not a person canopy knows
])
def test_a_bad_line_is_refused(w, body):
    r = send(client(w["owner"]), "put", "/api/brain/workspaces/acme/reporting/", body)
    assert r.status_code in (404, 422) and not ReportingLine.objects.exists()


def test_no_account_and_not_a_member_answer_the_same(w):
    user("outsider")                                                         # has an account, not in acme
    url = "/api/brain/workspaces/acme/reporting/"
    a = send(client(w["owner"]), "put", url, {"person": "alice@dimagi.com", "manager": "outsider@dimagi.com"})
    b = send(client(w["owner"]), "put", url, {"person": "alice@dimagi.com", "manager": "nobody@dimagi.com"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) and a.status_code == 404


def test_reporting_is_a_person_s_own_act_never_an_agent_s(w):
    agent = Agent.objects.create(slug="helper", name="Helper", owner=w["owner"], workspace=w["ws"],
                                 user=user("helper_login"))
    member(agent.user, w["ws"], role="owner")
    url = "/api/brain/workspaces/acme/reporting/"
    body = {"person": "bob@dimagi.com", "manager": "alice@dimagi.com"}
    assert send(client(agent.user), "put", url, body).status_code == 403
    assert client(agent.user).get(url).status_code == 403
    assert not ReportingLine.objects.exists()
    assert send(client(w["owner"]), "put", url, body).status_code == 200
    r = client(w["owner"]).delete(f"{url}{w['bob'].pk}/", HTTP_X_CANOPY_TURN_ID="some-turn")
    assert r.status_code == 403 and ReportingLine.objects.filter(ended_at__isnull=True).exists()


def test_a_manager_must_be_a_member(w):
    outsider = user("outsider")
    with pytest.raises(reporting.RefusedError):
        reporting.set_manager(w["ws"], person=w["alice"], manager=outsider, by=w["owner"])


# ---- who reads an output -------------------------------------------------------------------------

def _out(w, cap, subject_type, subject, **kw):
    return BrainOutput.objects.create(capability=cap, kind="note", workspace=w["ws"], subject_type=subject_type,
                                      subject=str(subject), title="t", body={"x": 1}, **kw)


def _ids(u, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return {o["id"] for o in client(u).get(f"/api/brain/outputs/?{qs}").json()}


def test_an_individual_output_goes_to_the_person_their_coach_and_their_manager_only(w):
    coach = user("coach_person")
    member(coach, w["ws"])
    CoachingRole.objects.create(workspace=w["ws"], user=coach)
    from tests.fixtures.brain_world import agree

    agree(w["alice"], "individual")                                  # opts into coaching too (the same fact)
    o = _out(w, w["coach"], "person", w["alice"].pk)
    reporting.set_manager(w["ws"], person=w["alice"], manager=w["bob"], by=w["owner"])
    assert o.pk in _ids(w["alice"]) and o.pk in _ids(coach) and o.pk in _ids(w["bob"])
    assert o.pk not in _ids(w["carol"]) and o.pk not in _ids(w["owner"])     # not a colleague, not even a workspace owner
    ReportingLine.objects.update(ended_at=__import__("django").utils.timezone.now())
    assert o.pk not in _ids(w["bob"])                                        # the line ended
    BrainOutput.objects.filter(pk=o.pk).update(state="withdrawn")
    assert o.pk not in _ids(w["alice"])
    BrainOutput.objects.filter(pk=o.pk).update(state="stale")
    assert o.pk not in _ids(w["alice"])                                      # a source was removed: hidden too


def test_a_manager_reads_only_the_outputs_of_the_workspace_their_line_is_in(w):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(slug="beta", display_name="Beta", created_by=w["owner"])
    for u in (w["alice"], w["bob"]):
        member(u, other)
    here = _out(w, w["coach"], "person", w["alice"].pk)
    there = BrainOutput.objects.create(capability=w["coach"], kind="note", workspace=other, subject_type="person",
                                       subject=str(w["alice"].pk), title="t", body={"x": 1})
    reporting.set_manager(w["ws"], person=w["alice"], manager=w["bob"], by=w["owner"])
    assert here.pk in _ids(w["bob"]) and there.pk not in _ids(w["bob"])     # the line is in acme only


def test_the_manager_leg_is_the_person_s_own_never_a_token_acting_for_them(w):
    from apps.brain import outputs

    o = _out(w, w["coach"], "person", w["alice"].pk)
    reporting.set_manager(w["ws"], person=w["alice"], manager=w["bob"], by=w["owner"])
    assert o in outputs.visible_to(w["bob"], as_person=True)
    assert o not in outputs.visible_to(w["bob"])                              # not asserted to be the person: closed
    assert o.pk in _ids(w["bob"])
    r = client(w["bob"]).get("/api/brain/outputs/", HTTP_X_CANOPY_TURN_ID="some-turn")   # a turn acting for bob
    assert r.status_code == 200 and o.pk not in {x["id"] for x in r.json()}


def test_a_coach_sees_only_those_who_opted_in(w):
    coach = user("coach_person")
    member(coach, w["ws"])
    CoachingRole.objects.create(workspace=w["ws"], user=coach)
    o = _out(w, w["coach"], "person", w["alice"].pk)
    assert o.pk not in _ids(coach)                                           # alice never opted in


def test_memory_outputs_are_the_persons_alone_and_project_outputs_the_project_s_readers(w):
    lead = Agent.objects.create(slug="eva", name="Eva", owner=w["owner"], workspace=w["ws"])
    p = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    mine = AgentProject.objects.create(agent=lead, ext_id="P2", name="Mine", handle="mine", visibility="personal",
                                       owner_user=w["carol"])
    mem = _out(w, w["mem"], "person", w["alice"].pk)
    pub, priv = _out(w, w["proj"], "project", p.pk), _out(w, w["proj"], "project", mine.pk)
    assert mem.pk in _ids(w["alice"]) and mem.pk not in _ids(w["bob"])
    assert pub.pk in _ids(w["alice"]) and pub.pk in _ids(w["bob"])
    assert priv.pk in _ids(w["carol"]) and priv.pk not in _ids(w["alice"]) and priv.pk not in _ids(w["owner"])
    assert _ids(w["alice"], subject_type="project", subject=p.pk) == {pub.pk}


def test_system_outputs_go_to_canopy_admins_and_workspace_owners(w):
    s = _out(w, w["sys"], "system", "canopy")
    assert s.pk in _ids(w["root"]) and s.pk in _ids(w["owner"])
    assert s.pk not in _ids(w["alice"])


def test_nothing_signed_out_reads_outputs(w):
    assert Client().get("/api/brain/outputs/").status_code in (401, 403)


def test_the_new_routes_are_mcp_tools_but_each_is_secured_in_the_route(w):
    from apps.api.api import api

    schema = api.get_openapi_schema()
    tools = {op["operationId"] for path, item in schema["paths"].items() if path.startswith("/api/brain/")
             for m, op in item.items() if isinstance(op, dict) and not excluded_reason(path, m, op)}
    assert {"list_capabilities", "register_capability", "patch_capability", "list_outputs",
            "declare_session_project"} <= tools
    assert not {"list_reporting", "set_reporting", "end_reporting"} & tools        # who manages whom: person-only


@pytest.mark.needs_migrations
def test_the_migrated_built_in_is_the_one_the_code_ensures():
    """Migration 0005 and `registry.PROJECT_READERS` say the same thing."""
    from apps.brain import registry

    row = BrainCapability.objects.get(slug="project-readers")
    for field, value in registry.PROJECT_READERS.items():
        assert getattr(row, field) == value, field


@pytest.mark.needs_migrations
def test_what_learns_is_registered_dark_by_migration_and_equals_the_code(w):
    from apps.brain import registry
    from apps.brain.registry import CLASS_AUDIENCE

    rows = {c.slug: c for c in BrainCapability.objects.filter(slug__in=[s["slug"] for s in registry.LEARNERS])}
    assert set(rows) == {s["slug"] for s in registry.LEARNERS}
    for spec in registry.LEARNERS:
        c = rows[spec["slug"]]
        assert tuple(c.audience) == CLASS_AUDIENCE[c.purpose_class] and c.purposes == spec["purposes"], spec["slug"]
        assert c.output_kinds == spec["output_kinds"] and c.enabled is False and c.operator_id is None, spec["slug"]


def test_ensure_learners_registers_the_missing_ones_dark_and_leaves_the_rest_alone(w):
    from apps.brain import registry

    BrainCapability.objects.filter(slug="coaching").delete()
    registry.update(registry.ensure_learners()["system-learning"], schedule="daily")
    again = registry.ensure_learners()
    assert set(again) == {s["slug"] for s in registry.LEARNERS} and again["coaching"].enabled is False
    assert BrainCapability.objects.get(slug="system-learning").schedule == "daily"


@pytest.mark.parametrize("cred, via_mcp, person", [
    (None, False, True),                                        # the manager's browser session
    ({"type": "pat", "id": 1}, False, True),                     # a PAT they minted by hand
    (None, True, False),                                        # any MCP call
    ({"type": "pat", "id": 1}, True, False),
    ({"type": "oauth", "id": 2}, False, False),                  # an OAuth client signed in as them
    ({"type": "delegated", "id": 3}, False, False),              # a host site's or MCP host grant's token
    ({"type": "act_as", "id": 4}, False, False),
    ({"type": "caller_token", "id": 5}, False, False),
])
def test_the_manager_leg_opens_only_for_the_manager_s_own_session_or_hand_minted_pat(w, cred, via_mcp, person):
    from types import SimpleNamespace

    from apps.brain import api_admin

    req = SimpleNamespace(user=w["bob"], META={}, auth_credential=cred, via_mcp=via_mcp)
    assert api_admin.manager_leg_open(req) is person


def test_a_coach_sees_outputs_only_of_the_workspaces_they_coach(w):
    from apps.workspaces.models import Workspace

    coach = user("coach_person")
    member(coach, w["ws"])
    CoachingRole.objects.create(workspace=w["ws"], user=coach)
    other = Workspace.objects.create(slug="beta", display_name="Beta", created_by=w["owner"])
    for u in (w["alice"], coach):
        member(u, other)                                         # both in beta too, but coaching is only in acme
    from tests.fixtures.brain_world import agree

    agree(w["alice"], "individual")
    here = _out(w, w["coach"], "person", w["alice"].pk)
    there = BrainOutput.objects.create(capability=w["coach"], kind="note", workspace=other, subject_type="person",
                                       subject=str(w["alice"].pk), title="t", body={"x": 1})
    assert here.pk in _ids(coach) and there.pk not in _ids(coach)
    assert there.pk in _ids(w["alice"])
