"""`POST /api/connections/operations`: the lease a write needs. canopy-web can't see a click, so
what it holds to is the limit: this person, this screen, this revision, 120 seconds after the last
write, 10 minutes from the click, 500 writes.
Salesforce is stubbed at the `requests` boundary, as in `test_connection_calls_route`."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.connections import call_service, declarations, keeper, server_key, server_tokens
from apps.connections.models import ConnectionOperation
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from apps.tokens.models import PersonalToken
from apps.workspace_apps.models import AppInstallation, AppTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import FakeSF, sealed_to_owner, sf_app

pytestmark = pytest.mark.django_db

J = "application/json"
CALL = "/api/connections/salesforce/request"
OPEN = "/api/connections/operations"
CODE = "function View({ canopy }) { return <div/>; }"
LEAD = "00Q5g00000AbCdEFGH"
READ_QUERY = {"method": "GET", "path": "query"}
WRITE_LEAD = {"method": "PATCH", "path": "sobjects/Lead/*"}
WRITE_TASK = {"method": "POST", "path": "sobjects/Task"}
DECL = {"provider": "salesforce", "as": "viewer", "reads": [READ_QUERY], "writes": [WRITE_LEAD]}


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


def _view(world, title="Leads", connections=(DECL,)):
    return view_services.create(workspace_id="w", project=None, user=world.person,
                                data={"title": title, "code": CODE, "connections": list(connections)})


@pytest.fixture
def w(key):
    """jj's own Salesforce, canopy-web allowed to use it, and a view of theirs that declares a write."""
    world = TokensWorld()
    world.conn = world.shared(sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True),
                              canopy_web=server_key.thumbprint())
    world.view = _view(world)
    world.client = Client()
    world.client.force_login(world.person)
    return world


def surface_of(view, version=None) -> dict:
    return {"kind": "view", "id": view.pk, "version": version or view.version}


def open_op(world, surface=None, client=None):
    body = {"surface": surface or surface_of(world.view)}
    return (client or world.client).post(OPEN, json.dumps(body), content_type=J)


def op_id(world, surface=None, client=None) -> str:
    r = open_op(world, surface, client)
    assert r.status_code == 200, r.content
    return r.json()["operation_id"]


def write(world, operation_id=None, surface=None, client=None):
    body = {"method": "PATCH", "path": f"sobjects/Lead/{LEAD}", "body": {"Status": "Open"},
            "surface": surface or surface_of(world.view)}
    if operation_id:
        body["operation_id"] = operation_id
    return (client or world.client).post(CALL, json.dumps(body), content_type=J)


def error_of(resp) -> str:
    return resp.json().get("error")


def done() -> Answer:
    return Answer(status=204, raw=b"")


# ---- the lease ------------------------------------------------------------------------------

def test_opening_an_operation_says_what_it_covers_for_120_seconds(w):
    before = timezone.now()
    r = open_op(w)
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["writes"] == [{"provider": "salesforce", "method": "PATCH", "path": "sobjects/Lead/*"}]
    row = ConnectionOperation.objects.get(pk=out["operation_id"])
    assert row.user == w.person and row.surface_kind == "view" and row.surface_ref == str(w.view.pk)
    assert row.revision == 1 and row.writes_used == 0
    assert timedelta(seconds=119) < row.expires_at - before <= timedelta(seconds=121)


def test_a_write_with_its_operation_goes_through_and_is_counted(w):
    operation = op_id(w)
    with FakeSF(), Salesforce(done()) as sf:
        r = write(w, operation)
    assert r.status_code == 200, r.content
    assert len(sf.calls) == 1
    assert ConnectionOperation.objects.get(pk=operation).writes_used == 1
    assert Event.objects.get(kind="connections.connection_call").payload["operation_id"] == operation


def test_write_needs_operation(w):
    with FakeSF(), Salesforce() as sf:
        missing = write(w)
        junk = write(w, "not-a-uuid")
        unknown = write(w, str(uuid.uuid4()))
    for r in (missing, junk, unknown):
        assert r.status_code == 409 and error_of(r) == "needs_operation"
        assert "click" in r.json()["detail"]
    assert sf.calls == []


def test_a_read_needs_none_and_spends_none(w):
    operation = op_id(w)
    body = {"method": "GET", "path": "query", "query": {"q": "SELECT Id FROM Lead"}, "surface": surface_of(w.view)}
    with FakeSF(), Salesforce():
        assert w.client.post(CALL, json.dumps(body), content_type=J).status_code == 200
    assert ConnectionOperation.objects.get(pk=operation).writes_used == 0


def test_operation_for_another_surface_refused(w):
    other = _view(w, "Other")
    operation = op_id(w)
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation, surface=surface_of(other))
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=operation).writes_used == 0


def _install(ws, key="crm", connections=(DECL,)) -> AppInstallation:
    template, _ = AppTemplate.objects.get_or_create(key="crm", defaults={"name": "CRM"})
    return AppInstallation.objects.create(workspace=ws, key=key, name="CRM", template=template,
                                          manifest={"pages": [], "collections": {}, "connections": list(connections)})


def test_an_app_of_the_same_key_in_another_workspace_is_another_surface(w):
    _install(w.ws)
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=w.person)
    WorkspaceMembership.objects.create(workspace=other, user=w.person, role=WorkspaceMembership.EDITOR)
    _install(other)
    # jj's own there too, so what stops the write is the operation.
    w.shared(sealed_to_owner({"user": w.person}, w.person, app=sf_app(other, secret="app-secret"), server_use=True),
             canopy_web=server_key.thumbprint())
    operation = op_id(w, surface={"kind": "app", "workspace": "w", "key": "crm"})
    assert ConnectionOperation.objects.get(pk=operation).surface_ref == "w/crm"
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation, surface={"kind": "app", "workspace": "w2", "key": "crm"})
        assert error_of(r) == "needs_operation"
        assert sf.calls == []
        r = write(w, operation, surface={"kind": "app", "workspace": "w", "key": "crm"})
    assert r.status_code == 200, r.content


def test_operation_for_another_person_refused(w):
    operation = op_id(w)
    ada = Client()
    ada.force_login(w.ada)
    with FakeSF(), Salesforce() as sf, mock.patch.object(call_service, "_viewer_connection", return_value=w.conn):
        r = write(w, operation, client=ada)
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=operation).writes_used == 0


def test_operation_for_another_revision_refused(w):
    operation = op_id(w)
    view_services.update(w.view, data={"title": "Renamed"}, user=w.person)
    w.view.refresh_from_db()
    assert w.view.version == 2
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation, surface=surface_of(w.view))
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == []


def test_opening_says_when_it_ends_at_the_latest(w):
    before = timezone.now()
    out = open_op(w).json()
    row = ConnectionOperation.objects.get(pk=out["operation_id"])
    assert timedelta(seconds=599) < row.hard_limit_at - before <= timedelta(seconds=601)
    assert out["hard_limit_at"].replace("Z", "+00:00") == row.hard_limit_at.isoformat()


def test_each_write_gives_the_lease_another_120_seconds(w):
    operation = op_id(w)
    ConnectionOperation.objects.filter(pk=operation).update(expires_at=timezone.now() + timedelta(seconds=5))
    before = timezone.now()
    with FakeSF(), Salesforce(done()):
        assert write(w, operation).status_code == 200
    row = ConnectionOperation.objects.get(pk=operation)
    assert timedelta(seconds=119) < row.expires_at - before <= timedelta(seconds=121)


def test_a_read_does_not_extend_the_lease(w):
    operation = op_id(w)
    soon = timezone.now() + timedelta(seconds=5)
    ConnectionOperation.objects.filter(pk=operation).update(expires_at=soon)
    with FakeSF(), Salesforce(Answer(body={"records": []})):
        r = w.client.post(CALL, json.dumps({"method": "GET", "path": "query", "surface": surface_of(w.view),
                                            "query": {"q": "SELECT Id FROM Lead"}}), content_type=J)
    assert r.status_code == 200, r.content
    assert ConnectionOperation.objects.get(pk=operation).expires_at == soon


def test_writes_never_extend_past_the_hard_limit(w):
    operation = op_id(w)
    limit = timezone.now() + timedelta(seconds=30)
    ConnectionOperation.objects.filter(pk=operation).update(hard_limit_at=limit, expires_at=limit)
    with FakeSF(), Salesforce(done()):
        assert write(w, operation).status_code == 200
    assert ConnectionOperation.objects.get(pk=operation).expires_at == limit


def test_past_the_hard_limit_a_write_needs_a_new_click(w):
    operation = op_id(w)
    past = timezone.now() - timedelta(seconds=1)
    ConnectionOperation.objects.filter(pk=operation).update(hard_limit_at=past, expires_at=past)
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation)
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == []


def test_many_writes_outlast_the_first_120_seconds(w):
    """Bulk logging: every write resets the 120 seconds, so a long run isn't cut off midway."""
    operation = op_id(w)
    opened = timezone.now()
    with FakeSF(), Salesforce(*[done() for _ in range(3)]) as sf:
        for minutes in (1.9, 3.8, 5.7):
            with mock.patch("apps.connections.call_service.timezone.now",
                            return_value=opened + timedelta(minutes=minutes)):
                assert write(w, operation).status_code == 200
    assert len(sf.calls) == 3


def test_expired_operation_refused(w):
    operation = op_id(w)
    ConnectionOperation.objects.filter(pk=operation).update(expires_at=timezone.now() - timedelta(seconds=1))
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation)
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=operation).writes_used == 0


def test_cap_500(w):
    assert call_service.OPERATION_MAX_WRITES == 500
    operation = op_id(w)
    ConnectionOperation.objects.filter(pk=operation).update(writes_used=499)
    with FakeSF(), Salesforce(done()) as sf:
        assert write(w, operation).status_code == 200
        r = write(w, operation)
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert "500" in r.json()["detail"]
    assert len(sf.calls) == 1
    assert ConnectionOperation.objects.get(pk=operation).writes_used == 500


def test_the_count_is_one_conditional_update(w):
    """No read-modify-write: two callers can't both take the last place."""
    operation = op_id(w)
    with mock.patch.object(ConnectionOperation, "save", side_effect=AssertionError("no save")):
        call_service._spend_write(w.person, operation, _surface_of_view(w))
    assert ConnectionOperation.objects.get(pk=operation).writes_used == 1


def _surface_of_view(world):
    return declarations.Surface(kind="view", ref=str(world.view.pk), workspace="w", revision=1, declared=[DECL])


# ---- who, and what ---------------------------------------------------------------------------

def test_a_token_cannot_open_one(w):
    raw, _ = PersonalToken.create_for_user(user=w.person, label="cli")
    r = Client().post(OPEN, json.dumps({"surface": surface_of(w.view)}), content_type=J,
                      HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 403 and error_of(r) == "session_required"
    assert not ConnectionOperation.objects.exists()


def test_someone_who_cannot_open_the_screen_gets_a_404(w):
    stranger = w.person.__class__.objects.create_user(username="stranger", email="s@example.com")
    client = Client()
    client.force_login(stranger)
    assert open_op(w, client=client).status_code == 404
    assert not ConnectionOperation.objects.exists()


def test_a_screen_that_declares_no_writes_gets_no_operation(w):
    view = _view(w, "Reads", [{**DECL, "writes": []}])
    r = open_op(w, surface_of(view))
    assert r.status_code == 403 and error_of(r) == "not_declared"
    assert not ConnectionOperation.objects.exists()


def test_a_page_on_an_old_revision_gets_only_what_both_declare(w):
    view_services.update(w.view, data={"connections": [{**DECL, "writes": [WRITE_TASK]}]}, user=w.person)
    w.view.refresh_from_db()
    r = open_op(w, surface_of(w.view, version=1))
    assert r.status_code == 403 and error_of(r) == "not_declared"


def test_a_write_on_an_old_revision_still_needs_both_declarations(w):
    operation = op_id(w)  # the page drew revision 1, which declares the write
    view_services.update(w.view, data={"connections": [{**DECL, "writes": []}]}, user=w.person)
    w.view.refresh_from_db()
    with FakeSF(), Salesforce() as sf:
        r = write(w, operation, surface=surface_of(w.view, version=1))
    assert error_of(r) == "not_declared"
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=operation).writes_used == 0


def test_opening_too_many_in_a_minute_is_refused(w):
    with mock.patch.object(call_service, "OPERATIONS_PER_MINUTE", 1):
        assert open_op(w).status_code == 200
        r = open_op(w)
    assert r.status_code == 429 and error_of(r) == "rate_limited"


def test_a_person_deleted_takes_their_operations(w):
    leaver = w.person.__class__.objects.create_user(username="leaver", email="leaver@dimagi.com")
    WorkspaceMembership.objects.create(workspace=w.ws, user=leaver, role=WorkspaceMembership.VIEWER)
    client = Client()
    client.force_login(leaver)
    op_id(w, client=client)
    leaver.delete()
    assert not ConnectionOperation.objects.exists()


# ---- the sweep -------------------------------------------------------------------------------

def _row(world, *, expired: bool) -> ConnectionOperation:
    delta = timedelta(seconds=-1 if expired else 60)
    return ConnectionOperation.objects.create(user=world.person, surface_kind="view", surface_ref="1", revision=1,
                                              expires_at=timezone.now() + delta,
                                              hard_limit_at=timezone.now() + delta)


def test_sweep_deletes_expired_operations_only(w):
    gone, kept = _row(w, expired=True), _row(w, expired=False)
    out = keeper.sweep()
    assert out["operations_expired"] == 1
    assert not ConnectionOperation.objects.filter(pk=gone.pk).exists()
    assert ConnectionOperation.objects.filter(pk=kept.pk).exists()


def test_sweep_is_bounded(w):
    for _ in range(3):
        _row(w, expired=True)
    with mock.patch.object(keeper, "OPERATIONS_PER_TICK", 2):
        assert keeper.sweep()["operations_expired"] == 2
    assert ConnectionOperation.objects.count() == 1
