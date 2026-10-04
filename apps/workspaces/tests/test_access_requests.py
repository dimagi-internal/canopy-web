"""Request an invitation — the way in that replaced self-join (owner decision,
2026-10-04).

A person whose login email is at one of a workspace's `access_request_domains`
may ASK; every admin and owner (inherited owners included) is emailed a deep
link to the request; an admin or owner approves at a role they may grant, or
denies. With the workspace's `auto_approve_role` set (dimagi: editor, for now)
the request is approved on the spot at that role, still as a normal request
record, and the admins are still told.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.workspaces import services
from apps.workspaces.models import Workspace, WorkspaceAccessRequest, WorkspaceMembership as M

pytestmark = pytest.mark.django_db
User = get_user_model()
BASE = "https://labs.connect.dimagi.com/canopy"


@pytest.fixture(autouse=True)
def _public_base(settings):
    settings.CANOPY_PUBLIC_BASE_URL = BASE


def _user(email, name=""):
    first, _, last = name.partition(" ")
    return User.objects.create(username=email, email=email, first_name=first, last_name=last)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _post(c, url, data=None):
    return c.post(url, data=json.dumps(data or {}), content_type="application/json")


@pytest.fixture
def org():
    """A dimagi-like org workspace: owner + admin + editor, takes requests from
    dimagi.com, auto-approve OFF (each test turns it on where it wants it)."""
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(slug="org", display_name="Org", created_by=owner,
                                  access_request_domains=["dimagi.com"])
    M.objects.create(workspace=ws, user=owner, role=M.OWNER)
    admin = _user("admin@dimagi.com")
    M.objects.create(workspace=ws, user=admin, role=M.ADMIN)
    editor = _user("editor@dimagi.com")
    M.objects.create(workspace=ws, user=editor, role=M.EDITOR)
    return {"ws": ws, "owner": owner, "admin": admin, "editor": editor}


def _request(user, slug="org", note=""):
    return _post(_client(user), f"/api/workspaces/{slug}/access-requests", {"note": note})


def _memberships(user):
    return set(M.objects.filter(user=user).values_list("workspace_id", flat=True))


# ---- asking -----------------------------------------------------------------


def test_requestable_lists_domain_matches_only_and_grants_nothing(org):
    other = Workspace.objects.create(slug="acme", display_name="Acme", created_by=org["owner"],
                                     access_request_domains=["acme.com"])
    asker = _user("asker@dimagi.com")
    body = _client(asker).get("/api/workspaces/requestable").json()
    assert [r["slug"] for r in body] == ["org"]
    assert body[0]["domain"] == "dimagi.com" and body[0]["pending_request_id"] is None
    assert other.slug not in {r["slug"] for r in body}
    assert _memberships(asker) == set()
    # A member sees nothing to request.
    assert _client(org["editor"]).get("/api/workspaces/requestable").json() == []


def test_domain_mismatch_cannot_request_and_gets_the_same_404_as_a_missing_slug(org):
    outsider = _user("someone@partner.example")
    r_mismatch = _request(outsider)
    r_missing = _request(outsider, slug="no-such-ws")
    assert r_mismatch.status_code == r_missing.status_code == 404
    assert r_mismatch.json()["type"] == r_missing.json()["type"]
    assert not WorkspaceAccessRequest.objects.exists()
    assert _memberships(outsider) == set()


def test_manual_mode_request_is_pending_and_creates_no_membership(org):
    asker = _user("asker@dimagi.com", "Ada Lovelace")
    r = _request(asker, note="I run the ACE demos")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "pending" and body["auto"] is False and body["role"] == ""
    assert body["note"] == "I run the ACE demos"
    assert _memberships(asker) == set()
    listed = _client(asker).get("/api/workspaces/requestable").json()
    assert listed[0]["pending_request_id"] == body["id"]


def test_duplicate_open_request_is_idempotent_and_notifies_once(org):
    asker = _user("asker@dimagi.com")
    first = _request(asker)
    sent = len(mail.outbox)
    again = _request(asker, note="please?")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert WorkspaceAccessRequest.objects.filter(user=asker).count() == 1
    assert len(mail.outbox) == sent  # no second round of emails


def test_a_member_cannot_request(org):
    r = _request(org["editor"])
    assert r.status_code == 409


# ---- telling the admins ----------------------------------------------------


def test_every_admin_and_owner_is_emailed_a_deep_link_to_the_request(org):
    parent_owner = _user("boss@dimagi.com")
    parent = Workspace.objects.create(slug="parent", display_name="Parent", created_by=parent_owner)
    M.objects.create(workspace=parent, user=parent_owner, role=M.OWNER)
    org["ws"].parent = parent
    org["ws"].save()

    asker = _user("asker@dimagi.com", "Ada Lovelace")
    r = _request(asker, note="hello")
    req_id = r.json()["id"]
    link = f"{BASE}/w/org/settings/access-requests/{req_id}"
    to = {m.to[0]: m for m in mail.outbox}
    # owner, admin, and the INHERITED owner of the parent; never the editor or the asker.
    assert set(to) == {"owner@dimagi.com", "admin@dimagi.com", "boss@dimagi.com"}
    for message in to.values():
        assert message.subject == "Ada Lovelace <asker@dimagi.com> requested access to Org"
        assert link in message.body
        assert "approve / deny" in message.body
        assert "hello" in message.body
        assert link in message.alternatives[0][0]
    stored = WorkspaceAccessRequest.objects.get(pk=req_id).notify_result
    assert sorted(stored["emailed"]) == ["admin@dimagi.com", "boss@dimagi.com", "owner@dimagi.com"]


def test_a_failing_email_does_not_fail_the_request_and_is_visible(org, monkeypatch):
    from apps.events.models import Event

    def boom(self, *a, **k):
        raise RuntimeError("SES down")

    monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", boom)
    asker = _user("asker@dimagi.com")
    r = _request(asker)
    assert r.status_code == 201
    req = WorkspaceAccessRequest.objects.get(pk=r.json()["id"])
    assert len(req.notify_result["failed"]) == 2
    ev = Event.objects.get(workspace=org["ws"], kind="access_request.created")
    assert ev.level == "warn" and "FAILED" in ev.summary
    # And the admins' list shows it.
    listed = _client(org["admin"]).get("/api/workspaces/org/access-requests").json()
    assert listed[0]["notify_result"]["failed"]


# ---- auto-approve ------------------------------------------------------------


def test_auto_approve_editor_lets_them_in_as_editor_and_still_emails_the_admins(org):
    org["ws"].auto_approve_role = "editor"
    org["ws"].save()
    asker = _user("asker@dimagi.com", "Ada Lovelace")
    r = _request(asker)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "approved" and body["auto"] is True and body["role"] == "editor"
    req = WorkspaceAccessRequest.objects.get(pk=body["id"])
    assert req.decided_by is None and req.decided_at is not None
    assert M.objects.get(workspace=org["ws"], user=asker).role == M.EDITOR
    link = f"{BASE}/w/org/settings/access-requests/{body['id']}"
    admin_mail = [m for m in mail.outbox if m.to[0] in {"owner@dimagi.com", "admin@dimagi.com"}]
    assert len(admin_mail) == 2
    for m in admin_mail:
        assert "auto-approved as editor" in m.body and link in m.body
    # The requester hears too, with a link into the workspace.
    mine = [m for m in mail.outbox if m.to == ["asker@dimagi.com"]]
    assert len(mine) == 1 and f"{BASE}/w/org" in mine[0].body


def test_auto_approve_off_means_pending(org):
    org["ws"].auto_approve_role = "editor"
    org["ws"].save()
    owner = _client(org["owner"])
    r = owner.put("/api/workspaces/org/access-settings", data=json.dumps({"auto_approve_role": ""}),
                  content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["auto_approve_role"] == ""
    asker = _user("asker@dimagi.com")
    assert _request(asker).json()["status"] == "pending"
    assert _memberships(asker) == set()


def test_only_an_owner_may_change_the_auto_approve_setting(org):
    for who in ("admin", "editor"):
        r = _client(org[who]).put("/api/workspaces/org/access-settings",
                                  data=json.dumps({"auto_approve_role": "editor"}),
                                  content_type="application/json")
        assert r.status_code == 403
    r = _client(org["owner"]).put("/api/workspaces/org/access-settings",
                                  data=json.dumps({"auto_approve_role": "admin"}),
                                  content_type="application/json")
    assert r.status_code == 422  # auto-approval is capped at editor


def test_an_unknown_auto_approve_value_reads_as_off(org):
    Workspace.objects.filter(slug="org").update(auto_approve_role="owner")
    asker = _user("asker@dimagi.com")
    assert _request(asker).json()["status"] == "pending"


def test_other_workspaces_default_to_off(org):
    acme_owner = _user("acme-owner@dimagi.com")
    _post(_client(acme_owner), "/api/workspaces/", {"slug": "acme", "display_name": "Acme"})
    assert Workspace.objects.get(slug="acme").auto_approve_role == ""
    assert Workspace.objects.get(slug="org").auto_approve_role == ""


# ---- deciding ----------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_owner_approves_at_each_role(org, role):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    mail.outbox.clear()
    r = _post(_client(org["owner"]), f"/api/workspaces/org/access-requests/{req_id}/approve",
              {"role": role})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "approved" and body["role"] == role and body["auto"] is False
    assert body["decided_by_email"] == "owner@dimagi.com" and body["current_role"] == role
    assert M.objects.get(workspace=org["ws"], user=asker).role == role
    assert [m.to for m in mail.outbox] == [["asker@dimagi.com"]]
    assert "approved" in mail.outbox[0].body and f"{BASE}/w/org" in mail.outbox[0].body


def test_approve_defaults_to_viewer(org):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    r = _post(_client(org["admin"]), f"/api/workspaces/org/access-requests/{req_id}/approve")
    assert r.json()["role"] == "viewer"


def test_an_approver_cannot_grant_above_their_own_role(org):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    admin = _client(org["admin"])
    r = _post(admin, f"/api/workspaces/org/access-requests/{req_id}/approve", {"role": "admin"})
    assert r.status_code == 403
    assert _memberships(asker) == set()
    assert WorkspaceAccessRequest.objects.get(pk=req_id).status == "pending"
    # owner is never an approvable role, even for an owner.
    r = _post(_client(org["owner"]), f"/api/workspaces/org/access-requests/{req_id}/approve",
              {"role": "owner"})
    assert r.status_code == 422
    # An editor cannot decide at all, nor see the list.
    ed = _client(org["editor"])
    assert _post(ed, f"/api/workspaces/org/access-requests/{req_id}/approve").status_code == 403
    assert ed.get("/api/workspaces/org/access-requests").status_code == 403


def test_deny_with_a_reason_tells_the_requester_and_grants_nothing(org):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    mail.outbox.clear()
    r = _post(_client(org["admin"]), f"/api/workspaces/org/access-requests/{req_id}/deny",
              {"reason": "contractors go through Jane"})
    assert r.status_code == 200
    assert r.json()["status"] == "denied" and r.json()["decision_reason"] == "contractors go through Jane"
    assert _memberships(asker) == set()
    assert [m.to for m in mail.outbox] == [["asker@dimagi.com"]]
    assert "contractors go through Jane" in mail.outbox[0].body
    # They may ask again later.
    assert _request(asker).status_code == 201


def test_a_decided_request_is_read_only(org):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    owner = _client(org["owner"])
    _post(owner, f"/api/workspaces/org/access-requests/{req_id}/deny")
    assert _post(owner, f"/api/workspaces/org/access-requests/{req_id}/approve").status_code == 409
    assert _post(owner, f"/api/workspaces/org/access-requests/{req_id}/deny").status_code == 409
    assert _memberships(asker) == set()
    got = owner.get(f"/api/workspaces/org/access-requests/{req_id}").json()
    assert got["status"] == "denied"


def test_a_request_is_only_reachable_through_its_own_workspace(org):
    asker = _user("asker@dimagi.com")
    req_id = _request(asker).json()["id"]
    other_owner = _user("o@dimagi.com")
    _post(_client(other_owner), "/api/workspaces/", {"slug": "other", "display_name": "Other"})
    c = _client(other_owner)
    assert c.get(f"/api/workspaces/other/access-requests/{req_id}").status_code == 404
    assert _post(c, f"/api/workspaces/other/access-requests/{req_id}/approve").status_code == 404


def test_existing_members_are_unchanged_by_approval_semantics(org):
    """Approval is upgrade-only, like an invite: it never demotes."""
    req = WorkspaceAccessRequest.objects.create(workspace=org["ws"], user=org["admin"])
    services.approve_access_request(request=req, by=org["owner"], role="viewer")
    assert M.objects.get(workspace=org["ws"], user=org["admin"]).role == M.ADMIN


# ---- invites carry a role ----------------------------------------------------


def test_invite_defaults_to_viewer_and_accept_grants_the_stored_role(org):
    owner = _client(org["owner"])
    r = _post(owner, "/api/workspaces/org/invites/", {"email": "new@dimagi.com"})
    assert r.status_code == 201 and r.json()["role"] == "viewer"
    r2 = _post(owner, "/api/workspaces/org/invites/", {"email": "ed@dimagi.com", "role": "editor"})
    newbie, ed = _user("new@dimagi.com"), _user("ed@dimagi.com")
    assert _post(_client(newbie), f"/api/workspaces/invites/{r.json()['token']}/accept").json()["role"] == "viewer"
    assert _post(_client(ed), f"/api/workspaces/invites/{r2.json()['token']}/accept").json()["role"] == "editor"


# ---- structural: nothing else creates a membership --------------------------


def test_only_invite_acceptance_approval_and_creation_create_memberships():
    """Every call that can CREATE a WorkspaceMembership in app code lives in a
    named, deliberate door: `_grant` (invite acceptance + request approval),
    `ensure_member` (the default-workspace bootstrap), and workspace creation
    (the creator becomes its owner). A new one fails here."""
    root = pathlib.Path(__file__).resolve().parents[3]
    allowed = {
        ("apps/workspaces/services.py", "_grant"),
        ("apps/workspaces/services.py", "ensure_member"),
        ("apps/workspaces/api.py", "create_workspace"),
    }
    creating = {"create", "get_or_create", "update_or_create", "bulk_create"}
    offenders = []
    for path in root.glob("apps/**/*.py"):
        rel = str(path.relative_to(root))
        if "/migrations/" in rel or "/tests/" in rel or rel.endswith("testing.py"):
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in creating):
                    continue
                target = ast.unparse(node.func.value)
                if "WorkspaceMembership" in target or target.endswith("memberships"):
                    if (rel, fn.name) not in allowed:
                        offenders.append(f"{rel}:{node.lineno} in {fn.name}")
    assert not offenders, offenders


def test_no_route_joins_without_an_invite_or_an_approved_request(org):
    """Behavioural twin of the structural test: a domain-matching stranger who
    calls every non-admin workspace route they can reach is still in nothing
    while auto-approve is off."""
    asker = _user("asker@dimagi.com")
    c = _client(asker)
    c.get("/api/workspaces/requestable")
    _request(asker)
    _post(c, "/api/workspaces/org/join")
    c.get("/api/workspaces/org/")
    assert _memberships(asker) == set()


# ---- migration ---------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_migration_keeps_dimagis_domains_and_auto_approves_dimagi_only():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("workspaces", "0011_admin_role")]
    after = [("workspaces", "0012_access_requests")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    OldUser = old.get_model("auth", "User")
    OldWs = old.get_model("workspaces", "Workspace")
    u = OldUser.objects.create(username="mig")
    OldWs.objects.create(slug="dimagi", display_name="Dimagi", created_by=u,
                         self_join_domains=["dimagi.com", "dimagi-associate.com"])
    OldWs.objects.create(slug="connect", display_name="Connect", created_by=u, self_join_domains=[])

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps.get_model("workspaces", "Workspace")
    dimagi, connect = new.objects.get(slug="dimagi"), new.objects.get(slug="connect")
    assert dimagi.access_request_domains == ["dimagi.com", "dimagi-associate.com"]
    assert dimagi.auto_approve_role == "editor"
    assert connect.access_request_domains == [] and connect.auto_approve_role == ""

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
