"""An agent's connection serving a project's or an app's screens, as its admin approved.

Salesforce is stubbed at the `requests` boundary, as in `test_connection_calls_route`. Everything
between it and the route is real: the surface's gate, the declaration, the share, `server_tokens`."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentProject
from apps.connections import server_key, server_tokens, services
from apps.connections.models import Connection, ConnectionOperation, ConnectionSurfaceShare
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspace_apps.models import AppInstallation, AppTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import FakeSF, sealed_to_owner, sf_app

pytestmark = pytest.mark.django_db

J = "application/json"
CALL = "/api/connections/salesforce/request"
CODE = "function View({ canopy }) { return <div/>; }"
LEAD = "00Q5g00000AbCdEFGH"
READ_QUERY = {"method": "GET", "path": "query"}
READ_LIMITS = {"method": "GET", "path": "limits"}
WRITE_LEAD = {"method": "PATCH", "path": "sobjects/Lead/*"}


def entry(reads=(READ_QUERY,), writes=(), agent="eva") -> dict:
    return {"provider": "salesforce", "as": "shared", "agent": agent, "reads": list(reads), "writes": list(writes)}


@pytest.fixture(autouse=True)
def _clean():
    server_tokens._forget_all()
    cache.clear()
    yield
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


def eva_connection(world, account=None) -> Connection:
    named = {"account_id": account, "account_label": account} if account else {}
    conn = sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True, **named)
    return world.shared(conn, agents=[world.eva], canopy_web=server_key.thumbprint())


@pytest.fixture
def w(key):
    """Eva's Salesforce with canopy-web allowed, jj her admin; a project with a view that asks for it;
    and a member (`viewer`) who administers nothing, with the viewer role."""
    world = TokensWorld()
    world.conn = eva_connection(world)
    world.project = AgentProject.objects.create(agent=world.eva, ext_id="P1", name="Sales", handle="sales")
    world.view = view_services.create(workspace_id="w", project=world.project, user=world.person,
                                      data={"title": "Leads", "code": CODE, "connections": [entry()]})
    world.admin = Client()
    world.admin.force_login(world.person)
    grant_step_up(world.admin)
    world.viewer_user = world.person.__class__.objects.create_user(username="vi", email="vi@dimagi.com")
    WorkspaceMembership.objects.create(workspace=world.ws, user=world.viewer_user, role=WorkspaceMembership.VIEWER)
    world.viewer = Client()
    world.viewer.force_login(world.viewer_user)
    return world


def shares_url(conn) -> str:
    return f"/api/connections/{conn.pk}/surface-shares"


def approve(world, kind="project", ref=None, conn=None, client=None):
    body = {"kind": kind, "ref": str(world.project.pk) if ref is None else ref, "approve": "current"}
    return (client or world.admin).post(shares_url(conn or world.conn), json.dumps(body), content_type=J)


def approved(world, **kw) -> dict:
    r = approve(world, **kw)
    assert r.status_code == 200, r.content
    return r.json()


def surface_of(view, version=None) -> dict:
    return {"kind": "view", "id": view.pk, "version": version or view.version}


def call(world, view=None, *, client=None, surface=None, **over):
    body = {"method": "GET", "path": "query", "query": {"q": "SELECT Id FROM Lead"}, "as": "shared", "agent": "eva",
            "surface": surface or surface_of(view or world.view), **over}
    return (client or world.viewer).post(CALL, json.dumps(body), content_type=J)


def error_of(resp) -> str:
    return resp.json().get("error")


def change(view, user, connections) -> None:
    view_services.update(view, data={"connections": connections}, user=user)
    view.refresh_from_db()


# ---- sharing ---------------------------------------------------------------------------------

def test_approving_stores_what_the_project_really_declares(w):
    out = approved(w)
    assert out["kind"] == "project" and out["ref"] == str(w.project.pk) and out["title"] == "Sales"
    assert out["approved_reads"] == [READ_QUERY] and out["approved_writes"] == []
    assert out["pending_reads"] == [] and out["pending_writes"] == [] and out["needs_approval"] is False
    row = ConnectionSurfaceShare.objects.get()
    assert row.connection == w.conn and row.approved_by == w.person and row.approved_reads == [READ_QUERY]


def test_a_client_cannot_name_what_is_approved(w):
    body = {"kind": "project", "ref": str(w.project.pk), "approve": "current", "approved_reads": [READ_LIMITS]}
    r = w.admin.post(shares_url(w.conn), json.dumps(body), content_type=J)
    assert r.status_code == 422
    assert not ConnectionSurfaceShare.objects.exists()


def test_person_connection_cannot_be_shared(w):
    mine = w.shared(sealed_to_owner({"user": w.person}, w.person, app=w.app, server_use=True),
                    canopy_web=server_key.thumbprint())
    r = approve(w, conn=mine)
    assert r.status_code == 422
    assert "person" in r.json()["detail"] or "agent" in r.json()["detail"]
    with pytest.raises(ValueError):
        services.share_with_surface(mine, "project", str(w.project.pk), by=w.person)
    assert not ConnectionSurfaceShare.objects.exists()
    # Someone else's is not even there to refuse.
    assert approve(w, conn=mine, client=w.viewer).status_code == 404


def test_non_admin_cannot_share(w):
    assert approve(w, client=w.viewer).status_code == 404
    assert w.viewer.get(shares_url(w.conn)).status_code == 404
    share = approved(w)
    assert w.viewer.delete(f"{shares_url(w.conn)}/{share['id']}").status_code == 404
    assert ConnectionSurfaceShare.objects.count() == 1


def test_sharing_needs_a_passkey_at_a_browser(w):
    fresh = Client()
    fresh.force_login(w.person)
    r = approve(w, client=fresh)
    assert r.status_code == 403 and error_of(r) == "step_up_required"
    assert not ConnectionSurfaceShare.objects.exists()
    share = approved(w)
    r = fresh.delete(f"{shares_url(w.conn)}/{share['id']}")
    assert r.status_code == 403 and error_of(r) == "step_up_required"


@pytest.mark.parametrize("limit", [{"owner_turns_only": True},
                                   {"expires_at": timezone.now() + timedelta(days=1)}])
def test_a_limited_connection_cannot_be_shared(w, limit):
    Connection.objects.filter(pk=w.conn.pk).update(**limit)
    r = approve(w)
    assert r.status_code == 422
    assert not ConnectionSurfaceShare.objects.exists()


def test_a_being_disconnected_connection_cannot_be_shared(w):
    Connection.objects.filter(pk=w.conn.pk).update(state=Connection.REVOKING)
    r = approve(w)
    assert r.status_code == 409 and error_of(r) == "revoking"


def test_a_surface_outside_the_agents_workspace_is_refused_as_if_absent(w):
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=w.person)
    far_agent = Agent.objects.create(slug="far", name="Far", workspace=other, owner=w.person)
    far = AgentProject.objects.create(agent=far_agent, ext_id="P1", name="Far", handle="far")
    outside = approve(w, ref=str(far.pk))
    assert outside.status_code == 422
    for absent in ("999999", "9" * 190, "not-a-number", "../1", "0x1"):
        r = approve(w, ref=absent)
        assert r.status_code == 422 and r.json()["detail"] == outside.json()["detail"]
    r = approve(w, kind="app", ref="no-slash")
    assert r.status_code == 422 and r.json()["detail"] == outside.json()["detail"]
    assert not ConnectionSurfaceShare.objects.exists()


def test_a_connection_made_in_another_workspace_is_refused(w):
    """A moved agent: in the project's workspace now, its connection still of the old one's app."""
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=w.person)
    Connection.objects.filter(pk=w.conn.pk).update(app=sf_app(other))
    r = approve(w)
    assert r.status_code == 422
    assert not ConnectionSurfaceShare.objects.exists()


def test_a_ref_spelled_another_way_is_the_same_share(w):
    assert approved(w, ref=f"00{w.project.pk}")["ref"] == str(w.project.pk)
    with FakeSF(), Salesforce():
        assert call(w).status_code == 200


def test_approving_again_is_one_row_with_what_is_there_now(w):
    approved(w)
    change(w.view, w.person, [entry(reads=[READ_QUERY, READ_LIMITS])])
    out = approved(w)
    assert out["approved_reads"] == [READ_QUERY, READ_LIMITS] and out["needs_approval"] is False
    assert ConnectionSurfaceShare.objects.count() == 1


def test_sharing_and_stopping_are_audited(w):
    share = approved(w)
    event = Event.objects.get(kind="connections.surface_shared")
    assert event.workspace_id == "w"
    assert event.payload["by"] == w.person.email and event.payload["connection"] == str(w.conn.pk)
    assert event.payload["surface"] == f"project:{w.project.pk}"
    assert event.payload["approved_reads"] == [READ_QUERY]
    r = w.admin.delete(f"{shares_url(w.conn)}/{share['id']}")
    assert r.status_code == 204
    assert not ConnectionSurfaceShare.objects.exists()
    assert Event.objects.get(kind="connections.surface_unshared").payload["surface"] == f"project:{w.project.pk}"
    assert services.AUDITED >= {"surface_shared", "surface_unshared"}


def test_a_share_of_another_connection_is_not_found(w):
    share = approved(w)
    other = eva_connection(w, account="00D/other")
    assert w.admin.delete(f"{shares_url(other)}/{share['id']}").status_code == 404
    assert ConnectionSurfaceShare.objects.count() == 1


# ---- the list --------------------------------------------------------------------------------

def test_the_list_says_what_is_waiting_for_approval(w):
    approved(w)
    change(w.view, w.person, [entry(reads=[READ_LIMITS], writes=[WRITE_LEAD])])
    rows = w.admin.get(shares_url(w.conn)).json()["shares"]
    assert len(rows) == 1
    row = rows[0]
    assert row["approved_reads"] == [READ_QUERY]
    assert row["pending_reads"] == [READ_LIMITS] and row["pending_writes"] == [WRITE_LEAD]
    assert row["needs_approval"] is True and row["title"] == "Sales"


def test_the_list_reads_every_view_of_a_project(w):
    approved(w)
    view_services.create(workspace_id="w", project=w.project, user=w.person,
                         data={"title": "Second", "code": CODE, "connections": [entry(reads=[READ_LIMITS])]})
    row = w.admin.get(shares_url(w.conn)).json()["shares"][0]
    assert row["pending_reads"] == [READ_LIMITS]


def test_a_surface_that_is_gone_has_no_title(w):
    approved(w)
    w.view.delete()
    w.project.delete()
    row = w.admin.get(shares_url(w.conn)).json()["shares"][0]
    assert row["title"] is None and row["needs_approval"] is False and row["pending_reads"] == []


# ---- a call ----------------------------------------------------------------------------------

def test_shared_read_by_member(w):
    approved(w)
    with FakeSF(), Salesforce(Answer(body={"totalSize": 2})) as sf:
        r = call(w)
    assert r.status_code == 200, r.content
    assert r.json() == {"status": 200, "body": {"totalSize": 2}}
    assert len(sf.calls) == 1


def test_a_stranger_cannot_use_the_share(w):
    approved(w)
    stranger = w.person.__class__.objects.create_user(username="stranger", email="s@example.com")
    client = Client()
    client.force_login(stranger)
    with Salesforce() as sf:
        assert call(w, client=client).status_code == 404
    assert sf.calls == []


def test_shared_refused_without_share(w):
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_a_share_with_another_project_covers_nothing(w):
    other = AgentProject.objects.create(agent=w.eva, ext_id="P2", name="Other", handle="other")
    approved(w, ref=str(other.pk))
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_a_view_with_no_project_is_told_to_join_one(w):
    loose = view_services.create(workspace_id="w", project=None, user=w.person,
                                 data={"title": "Loose", "code": CODE, "connections": [entry()]})
    with Salesforce() as sf:
        r = call(w, loose)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert "project" in r.json()["detail"]
    assert sf.calls == []


def test_widened_revision_needs_approval(w):
    approved(w)
    change(w.view, w.person, [entry(reads=[READ_QUERY, READ_LIMITS])])
    with Salesforce() as sf:
        # Even the line that was approved: an uncovered line stops the whole entry.
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []
    approved(w)
    with FakeSF(), Salesforce():
        assert call(w).status_code == 200


def test_a_page_on_an_old_revision_stops_at_a_widened_one_too(w):
    approved(w)
    change(w.view, w.person, [entry(reads=[READ_QUERY, READ_LIMITS])])
    with Salesforce() as sf:
        r = call(w, surface=surface_of(w.view, version=1))
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_narrowed_revision_still_works(w):
    change(w.view, w.person, [entry(reads=[READ_QUERY, READ_LIMITS])])
    approved(w)
    change(w.view, w.person, [entry(reads=[READ_QUERY])])
    with FakeSF(), Salesforce():
        assert call(w).status_code == 200
    with Salesforce() as sf:
        r = call(w, path="limits", query=None)
    assert r.status_code == 403 and error_of(r) == "not_declared"
    assert sf.calls == []


def test_project_view_covered_by_project_share(w):
    approved(w)
    second = view_services.create(workspace_id="w", project=w.project, user=w.person,
                                  data={"title": "Second", "code": CODE, "connections": [entry()]})
    wider = view_services.create(workspace_id="w", project=w.project, user=w.person,
                                 data={"title": "Wider", "code": CODE,
                                       "connections": [entry(reads=[READ_QUERY, READ_LIMITS])]})
    with FakeSF(), Salesforce():
        assert call(w, second).status_code == 200
        # One view asking for more stops that view, not the project's others.
        assert error_of(call(w, wider)) == "needs_approval"
        assert call(w).status_code == 200


def _install(world, connections) -> AppInstallation:
    template = AppTemplate.objects.create(key="crm", name="CRM")
    return AppInstallation.objects.create(workspace=world.ws, key="crm", name="The CRM", template=template,
                                          manifest={"pages": [], "collections": {}, "connections": connections})


APP = {"kind": "app", "workspace": "w", "key": "crm"}


def test_an_app_page_is_covered_by_its_own_share(w):
    _install(w, [entry()])
    with Salesforce() as sf:
        assert error_of(call(w, surface=APP)) == "needs_approval"
    assert sf.calls == []
    out = approved(w, kind="app", ref="w/crm")
    assert out["title"] == "The CRM" and out["approved_reads"] == [READ_QUERY]
    with FakeSF(), Salesforce():
        assert call(w, surface=APP).status_code == 200


def test_a_shared_write_needs_approval_and_an_operation(w):
    write = {"method": "PATCH", "path": f"sobjects/Lead/{LEAD}", "query": None, "body": {"Status": "Open"}}
    change(w.view, w.person, [entry(writes=[WRITE_LEAD])])
    opened = w.viewer.post("/api/connections/operations", json.dumps({"surface": surface_of(w.view)}),
                           content_type=J).json()
    with Salesforce() as sf:
        assert error_of(call(w, **write, operation_id=opened["operation_id"])) == "needs_approval"
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=opened["operation_id"]).writes_used == 0
    approved(w)
    with FakeSF(), Salesforce(Answer(status=204, raw=b"")) as sf:
        assert error_of(call(w, **write)) == "needs_operation"
        assert sf.calls == []
        r = call(w, **write, operation_id=opened["operation_id"])
    assert r.status_code == 200, r.content


def test_the_audit_line_has_viewer_connection_and_as_shared(w):
    write = {"method": "PATCH", "path": f"sobjects/Lead/{LEAD}", "query": None, "body": {"Status": "Open"}}
    change(w.view, w.person, [entry(writes=[WRITE_LEAD])])
    approved(w)
    opened = w.viewer.post("/api/connections/operations", json.dumps({"surface": surface_of(w.view)}),
                           content_type=J).json()
    with FakeSF(), Salesforce(Answer(status=204, raw=b"")):
        assert call(w, **write, operation_id=opened["operation_id"]).status_code == 200
    p = Event.objects.get(kind="connections.connection_call").payload
    assert p["by"] == w.viewer_user.email and p["connection"] == str(w.conn.pk) and p["as"] == "shared"
    assert p["owner"] == "agent:eva" and p["surface"] == f"view:{w.view.pk}"


# ---- what stops a share working ----------------------------------------------------------------

def test_the_switch_off_is_a_sentence_for_the_agents_admin(w):
    approved(w)
    Connection.objects.filter(pk=w.conn.pk).update(server_use=False)
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "server_use_off"
    assert "eva" in r.json()["detail"] and "admin" in r.json()["detail"]
    assert sf.calls == []


def test_limits_added_after_sharing_stop_it(w):
    approved(w)
    Connection.objects.filter(pk=w.conn.pk).update(owner_turns_only=True)
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "server_use_off"
    assert sf.calls == []


def test_two_usable_shares_are_ambiguous(w):
    approved(w)
    second = eva_connection(w, account="00D/second")
    approved(w, conn=second)
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "ambiguous"
    assert "eva" in r.json()["detail"]
    assert sf.calls == []


def test_a_second_account_not_shared_with_this_screen_is_no_ambiguity(w):
    approved(w)
    eva_connection(w, account="00D/second")
    with FakeSF(), Salesforce():
        assert call(w).status_code == 200


def test_a_share_with_its_switch_off_is_no_ambiguity(w):
    approved(w)
    second = eva_connection(w, account="00D/second")
    approved(w, conn=second)
    Connection.objects.filter(pk=second.pk).update(server_use=False)
    with FakeSF(), Salesforce():
        assert call(w).status_code == 200


def test_an_agent_outside_the_surfaces_workspace_is_not_used(w):
    approved(w)
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=w.person)
    Agent.objects.filter(pk=w.eva.pk).update(workspace=other)
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_a_connection_of_another_workspaces_app_is_not_used(w):
    approved(w)
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=w.person)
    Connection.objects.filter(pk=w.conn.pk).update(app=sf_app(other))
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_being_disconnected_stops_it(w):
    approved(w)
    Connection.objects.filter(pk=w.conn.pk).update(state=Connection.REVOKING)
    with Salesforce() as sf:
        r = call(w)
    assert r.status_code == 409 and error_of(r) == "not_connected"
    assert "connect_url" not in r.json(), "connecting their own account would not bring back the agent's"
    assert sf.calls == []


def test_disconnect_removes_shares(w):
    approved(w)
    assert ConnectionSurfaceShare.objects.count() == 1
    Connection.objects.filter(pk=w.conn.pk).delete()  # what every disconnect ends in
    assert not ConnectionSurfaceShare.objects.exists()


def test_disconnecting_through_the_route_ends_the_share(w):
    approved(w)
    with FakeSF():
        r = w.admin.delete(f"/api/agents/eva/connections/{w.conn.pk}")
    assert r.status_code == 200, r.content
    assert not Connection.objects.filter(pk=w.conn.pk).exists()
    assert not ConnectionSurfaceShare.objects.exists()


def test_one_share_per_connection_and_surface(w):
    approved(w)
    with pytest.raises(IntegrityError), transaction.atomic():
        ConnectionSurfaceShare.objects.create(connection=w.conn, kind="project", ref=str(w.project.pk))


def test_a_token_can_read_the_list_as_an_admin_but_not_for_a_viewer(w):
    raw, _ = PersonalToken.create_for_user(user=w.person, label="cli")
    assert Client().get(shares_url(w.conn), HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 200
    raw, _ = PersonalToken.create_for_user(user=w.viewer_user, label="cli")
    assert Client().get(shares_url(w.conn), HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 404
