"""A guest reaches exactly the app their scope names, at its role, and nothing else of the workspace.

A guest (`WorkspaceMembership.GUEST`, docs/architecture/access.md § Guests) is listed in the
workspace and invited and removed like any member, but every membership reader answers None
for them unless its caller opts in. This walks the real OpenAPI schema as a guest of the
personal-project guard's workspace (`tests/test_personal_project_never_leaks.py`, whose world
writes a sentinel into every text field it holds), with two apps installed there: the guest's
own (`household`, at `assistant`) and one they are not in (`secretapp`). Every GET must come
back with no sentinel, no member's address, no agent's name, and no record of the other app.

Beside the walk: the decisions that are not routes (access-list lines, mail routes, MCP caller
resolution, the permission table) treat the guest as outside, and the controls — the guest
reads and acts in their app at exactly their role, and a member reads what the walk kept out.
"""
from __future__ import annotations

import itertools
import re

import pytest
from django.contrib.auth.models import User

from apps.agents import grants
from apps.agents.models import AgentGrant
from apps.workspace_apps import access as app_access
from apps.workspace_apps.models import AppInstallation
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceInvite, WorkspaceMembership
from tests.fixtures.personal_project_world import (
    MARK,
    _body,
    _client,
    _get_routes,
    _make_world,
    _query_variants,
    path_values,
)

pytestmark = [pytest.mark.guard, pytest.mark.django_db]

J = "application/json"
HOUSE = "hsldok"          # the guest's own app's records: these they may read
SECRET = "scrtapp"        # the other app's records: these they may not


def _manifest(key: str, name: str, role: str, collection: str) -> dict:
    return {
        "key": key, "name": name,
        "roles": {role: {"description": f"{name} {role}"}, "parent": {"capability": "content.write"}},
        "collections": {collection: {"read": role, "fields": {"title": {"type": "text", "required": True}}}},
        "actions": {
            "add": {"role": role, "op": "create", "collection": collection, "input": ["title"]},
            "plan": {"role": "parent", "op": "create", "collection": collection, "input": ["title"]},
        },
        "pages": [{"slug": "list", "role": role, "code": "function View({data}){return <Table rows={data.v} columns={[]}/>}",
                   "sources": [{"alias": "v", "collection": collection}]}],
    }


@pytest.fixture()
def world() -> dict:
    w = _make_world()
    ws, admin = w["ws"], w["wsadmin"]
    owner = User.objects.create_user("gowner", "gowner@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    c = _client(owner)
    for key, name, role, coll, mark in (("household", "Household", "assistant", "chore", HOUSE),
                                        ("secretapp", "Secret", "keeper", "secret", SECRET)):
        r = c.post(f"/api/workspace-apps/{ws.slug}/", {"manifest": _manifest(key, name, role, coll)}, content_type=J)
        assert r.status_code == 201, r.content
        r = c.post(f"/api/workspace-apps/{ws.slug}/{key}/actions/plan", {"input": {"title": f"{mark}-record"}},
                   content_type=J)
        assert r.status_code == 200, r.content
    # The guest arrives the way a guest does: an invite with a scope, accepted.
    r = c.post(f"/api/workspaces/{ws.slug}/invites/",
               {"email": "addy@jacksonhome.org", "role": "guest", "scope": {"apps": {"household": "assistant"}}},
               content_type=J)
    assert r.status_code == 201, r.content
    guest = User.objects.create_user("addy", "addy@jacksonhome.org", "pw")
    token = WorkspaceInvite.objects.get(email="addy@jacksonhome.org").token
    assert _client(guest).post(f"/api/workspaces/invites/{token}/accept").status_code == 200
    return {**w, "owner": owner, "admin": admin, "guest": guest,
            "household": AppInstallation.objects.get(workspace=ws, key="household"),
            "secretapp": AppInstallation.objects.get(workspace=ws, key="secretapp")}


# ---- what the guest is ------------------------------------------------------------------


def test_accepting_a_guest_invite_makes_exactly_a_scoped_guest(world):
    row = WorkspaceMembership.objects.get(user=world["guest"], workspace=world["ws"])  # authz-exempt: the test's own check
    assert (row.role, row.scope) == ("guest", {"apps": {"household": "assistant"}})


def test_every_membership_reader_treats_a_guest_as_outside(world):
    guest, ws = world["guest"], world["ws"]
    assert wsvc.member_role(guest, ws) is None and not wsvc.is_member(guest, ws.slug)
    assert wsvc.membership(guest, ws) is None
    assert wsvc.member_role(guest, ws, include_guests=True) == "guest"
    assert ws.slug not in wsvc.member_roles(guest) and ws.slug not in wsvc.member_roles_of([guest])[guest.pk]
    assert ws.slug not in wsvc.user_workspace_slugs(guest)
    assert guest.pk not in wsvc.member_user_ids(ws) and guest.pk not in wsvc.workspace_member_ids(ws)
    assert wsvc.user_default_workspace(guest) is None
    assert not wsvc.has_role_at_least(guest, ws, WorkspaceMembership.VIEWER)
    assert all(not perms.can(guest, ws, p) for p in perms.MINIMUM_ROLE)
    assert perms.slugs_with(guest, perms.READ) == set()
    assert wsvc.workspace_slugs_for_user_id(guest.pk, ws.slug) == set()
    # The permission table never reaches a guest, and no access-list line can name one.
    assert WorkspaceMembership.GUEST not in perms.MINIMUM_ROLE.values()
    assert not perms.is_role("guest") and not perms.role_meets("guest", "")
    assert perms.ROLES.permissions_of("guest") == frozenset()
    from apps.workspaces import login_gate

    assert not login_gate.can_create_workspace(guest)


def test_access_list_lines_for_everyone_in_the_workspace_do_not_match_a_guest(world):
    ws, agent, guest = world["ws"], world["lead_agent"], world["guest"]
    for at_least in ("", WorkspaceMembership.VIEWER):
        AgentGrant.objects.filter(agent=agent).delete()
        line = AgentGrant.objects.create(agent=agent, kind=AgentGrant.WORKSPACE, subject_workspace=ws,
                                         at_least=at_least, role="read_only")
        asker = grants.Identity(user=guest, email=guest.email, verified=True)
        assert not grants.matches(line, asker)
        assert grants.resolve(agent, asker).role is None
        bulk = grants.Identity(user=guest, email=guest.email, verified=True,
                               workspace_roles=wsvc.member_roles_of([guest])[guest.pk])
        assert not grants.matches(line, bulk)
        # The control: a viewer of the same workspace matches the same line.
        assert grants.matches(line, grants.Identity(user=world["editor"], email="editor@dimagi.com", verified=True))


def test_mail_routes_treat_a_guest_as_a_non_member(world):
    from apps.mail import domains
    from apps.mail.models import MailDomain

    d = MailDomain.objects.create(workspace=world["ws"], name="guard.test", status="verified")
    with pytest.raises(domains.DomainError):
        domains.save_route(d, local_part="addy", kind="forward", forward_to_user_id=world["guest"].pk)
    assert domains.save_route(d, local_part="ed", kind="forward", forward_to_user_id=world["editor"].pk)


def test_mcp_caller_resolution_treats_a_guest_as_a_non_member(world):
    from apps.mcp.tools.caller import TurnNotFound, _envelope_sync

    turn = world["turn"]
    with pytest.raises(TurnNotFound):
        _envelope_sync(world["guest"].pk, str(turn.pk))


# ---- the walk ---------------------------------------------------------------------------


def _values(w: dict) -> dict:
    v = path_values(w)
    return {**v, "key": [*v["key"], "household", "secretapp"], "collection": ["chore", "secret"],
            "user_id": [str(w["editor"].pk), str(w["guest"].pk)]}


def _urls(path: str, op: dict, values: dict, queries: dict):
    names = re.findall(r"\{([^}]+)\}", path)
    missing = [n for n in names if n not in values]
    assert not missing, f"{path}: no fixture mapping for {missing} (see tests/test_personal_project_never_leaks.py)"
    params = op.get("parameters", [])
    base = {"email": "editor@dimagi.com", "agent": "guard-lead", "workspace": "guardws", "start": "2026-01-05",
            "agents": "guard-lead", "before": "2030-01-01T00:00:00Z", "platform": "darwin"}
    required_names = [q["name"] for q in params if q["in"] == "query" and q.get("required")]
    collections = ["chore", "secret"] if "collection" in required_names else [None]
    for combo in itertools.product(*(values[n] for n in names)):
        url = path
        for name, value in zip(names, combo):
            url = url.replace("{%s}" % name, value)
        for coll in collections:
            required = {n: (coll if n == "collection" else base.get(n, "guard")) for n in required_names}
            yield url, required, list(combo)
            for q in params:
                if q["in"] == "query" and q["name"] in queries and q["name"] not in required:
                    for value in queries[q["name"]]:
                        yield url, {**required, q["name"]: value}, [*combo, value]


#: What a guest must never read, beyond the guard world's sentinels: the other app's records,
#: the workspace's people and agents.
FORBIDDEN = [SECRET, "lead@dimagi.com", "wsadmin@dimagi.com", "gowner@dimagi.com", "Guard Lead", "Guard Outsider"]


def test_every_get_as_a_guest_shows_nothing_of_the_workspace_but_their_app(world):
    client = _client(world["guest"])
    values, queries = _values(world), _query_variants(world)
    leaks, broken, statuses, total, house_reads = [], [], {}, 0, 0
    for path, op in _get_routes():
        for url, query, echoed in _urls(path, op, values, queries):
            resp = client.get(url, query)
            total += 1
            statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1
            if resp.status_code >= 500 and resp.status_code != 503:
                broken.append(url)
            text = _body(resp)
            for value in sorted(echoed + list(query.values()), key=len, reverse=True):
                if value:
                    text = text.replace(value, "")
            found = re.findall(MARK + r"[\w.-]*", text, re.I) + [f for f in FORBIDDEN if f in text]
            if found:
                leaks.append(f"{url} {query} [{resp.status_code}]: {sorted(set(found))[:4]}")
            house_reads += f"{HOUSE}-record" in text
    assert not leaks, "a guest read workspace data through:\n" + "\n".join(leaks)
    assert not broken, f"a route that errors cannot be shown not to leak: {sorted(set(broken))}"
    assert total > 500, f"only {total} requests: the walk is not walking"
    assert house_reads > 0, "the walk never reached the guest's own app: it proves nothing"


def test_a_member_reads_what_the_guest_walk_kept_out(world):
    """The control: the same routes answer a member with what the guest did not see."""
    from apps.workspace_apps.models import AppRoleGrant

    AppRoleGrant.objects.create(installation=world["secretapp"], user=world["owner"], role="keeper",
                                granted_by=world["owner"])
    owner = _client(world["owner"])
    assert SECRET in _body(owner.get("/api/workspace-apps/guardws/secretapp/query", {"collection": "secret"}))
    assert "lead@dimagi.com" in _body(owner.get("/api/workspaces/guardws/members/"))
    assert "Guard Lead" in _body(owner.get("/api/agents/", {"workspace": "guardws"}))


# ---- exactly their app, at exactly their role ---------------------------------------------


def test_a_guest_uses_exactly_their_app_at_exactly_their_role(world):
    guest, ws = _client(world["guest"]), "guardws"
    assert app_access.roles_for(world["guest"], world["household"]) == {"public", "assistant"}
    assert app_access.roles_for(world["guest"], world["secretapp"]) == {"public"}
    app = guest.get(f"/api/workspace-apps/{ws}/household")
    assert app.status_code == 200, app.content
    assert app.json()["my_roles"] == ["assistant", "public"] and not app.json()["may_maintain"]
    assert [p["slug"] for p in app.json()["pages"]] == ["list"]
    rows = guest.get(f"/api/workspace-apps/{ws}/household/query", {"collection": "chore"})
    assert rows.status_code == 200 and HOUSE in _body(rows)
    assert guest.post(f"/api/workspace-apps/{ws}/household/actions/add", {"input": {"title": "trash"}},
                      content_type=J).status_code == 200
    # Not the app's other role, not its maintenance, not the other app, not the app list.
    assert guest.post(f"/api/workspace-apps/{ws}/household/actions/plan", {"input": {"title": "x"}},
                      content_type=J).status_code == 403
    assert guest.get(f"/api/workspace-apps/{ws}/household/draft").status_code == 404
    assert guest.get(f"/api/workspace-apps/{ws}/household/guide").status_code == 404
    for url in (f"/api/workspace-apps/{ws}/secretapp", f"/api/workspace-apps/{ws}/", f"/api/workspaces/{ws}/"):
        assert guest.get(url).status_code == 404, url
    assert guest.get(f"/api/workspace-apps/{ws}/secretapp/query", {"collection": "secret"}).status_code == 404
    assert guest.post(f"/api/workspace-apps/{ws}/secretapp/actions/add", {"input": {"title": "x"}},
                      content_type=J).status_code == 404
    # The pinned tenant prefix is a member's door, and stays shut.
    assert guest.get(f"/api/w/{ws}/agents/").status_code == 404
    # Switched off, the app is gone for the guest too.
    world["household"].enabled = False
    world["household"].save(update_fields=["enabled"])
    assert guest.get(f"/api/workspace-apps/{ws}/household").status_code == 404


def test_the_shell_and_the_members_list_name_the_guest_for_what_they_are(world):
    guest_of = _client(world["guest"]).get("/api/workspaces/guest-of").json()
    assert guest_of == [{"slug": "guardws", "display_name": "Guard", "scope": {"apps": {"household": "assistant"}}}]
    assert _client(world["guest"]).get("/api/workspaces/").json() == []
    members = _client(world["owner"]).get("/api/workspaces/guardws/members/").json()
    row = next(m for m in members if m["email"] == "addy@jacksonhome.org")
    assert (row["role"], row["label"]) == ("guest", "Guest: Household (assistant)")
    # A full member is never listed as a guest of the same workspace.
    assert _client(world["owner"]).get("/api/workspaces/guest-of").json() == []


# ---- inviting and removing ----------------------------------------------------------------


def test_a_guest_invite_needs_a_scope_the_app_declares_and_an_inviter_who_runs_the_app(world):
    owner, admin = _client(world["owner"]), _client(world["admin"])
    url = "/api/workspaces/guardws/invites/"

    def invite(c, scope, role="guest", email="g2@jacksonhome.org"):
        return c.post(url, {"email": email, "role": role, "scope": scope}, content_type=J)

    assert invite(owner, None).status_code == 422
    assert invite(owner, {"apps": {}}).status_code == 422
    assert invite(owner, {"apps": {"nope": "assistant"}}).status_code == 422
    assert invite(owner, {"apps": {"household": "keeper"}}).status_code == 422      # a role of another app
    for role in ("maintainer", "member", "public"):
        assert invite(owner, {"apps": {"household": role}}).status_code == 422
    assert invite(owner, {"projects": {"x": "y"}}).status_code == 422
    assert invite(owner, {"apps": {"household": "assistant"}}, role="viewer").status_code == 422
    # Managing members is not enough: the inviter must be able to run the app (an admin can).
    editor = _client(world["editor"])
    assert invite(editor, {"apps": {"household": "assistant"}}).status_code == 403
    assert invite(admin, {"apps": {"household": "assistant"}}).status_code == 201


def test_a_guest_is_removed_or_promoted_like_any_member(world):
    owner, guest = _client(world["owner"]), world["guest"]
    pk = guest.pk
    # A role change never makes a guest; promoting one drops the scope.
    assert owner.patch(f"/api/workspaces/guardws/members/{world['editor'].pk}/", {"role": "guest"},
                       content_type=J).status_code == 422
    r = owner.patch(f"/api/workspaces/guardws/members/{pk}/", {"role": "viewer"}, content_type=J)
    assert r.status_code == 200, r.content
    row = WorkspaceMembership.objects.get(user=guest, workspace=world["ws"])  # authz-exempt: the test's own check
    assert (row.role, row.scope) == ("viewer", {})
    assert wsvc.is_member(guest, "guardws")
    assert owner.delete(f"/api/workspaces/guardws/members/{pk}/").status_code == 204
    assert not WorkspaceMembership.objects.filter(user=guest).exists()


def test_removing_a_guest_shuts_their_app(world):
    owner, guest = _client(world["owner"]), _client(world["guest"])
    assert guest.get("/api/workspace-apps/guardws/household").status_code == 200
    assert owner.delete(f"/api/workspaces/guardws/members/{world['guest'].pk}/").status_code == 204
    assert guest.get("/api/workspace-apps/guardws/household").status_code == 404
