"""Connecting a site to canopy, as a person actually does it.

Everything the widget needs was reachable only through the Django admin, which
is the wrong door twice: it is staff-only, and "connect my site" is a product
act rather than a row an administrator edits. These tests pin the surface that
replaces it, and in particular the refusals — every one of the widget's inputs
fails CLOSED and silently, so a wrong value shows up as a launcher that never
appears rather than as an error.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.tokens import embed_apps
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


def _member(ws, email, role):
    user = User.objects.create_user(email.split("@")[0], email, "pw")
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)
    return user


def _ctx(role=WorkspaceMembership.OWNER):
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=boss)
    WorkspaceMembership.objects.create(user=boss, workspace=ws, role=WorkspaceMembership.OWNER)
    user = boss if role == WorkspaceMembership.OWNER else _member(ws, f"{role}@dimagi.com", role)
    c = Client()
    c.force_login(user)
    return user, ws, c


def _agent(ws, slug="labs-helper"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def _connect(c, body=None, slug="w1"):
    return c.post(
        f"/api/workspaces/{slug}/connected-apps",
        data=body or {"name": "connect-labs", "origins": [LABS]},
        content_type="application/json",
    )


# --- connecting a site --------------------------------------------------------


def test_an_owner_can_connect_a_site_and_is_shown_the_secret_once():
    _user, _ws, c = _ctx()

    r = _connect(c)

    assert r.status_code == 201, r.content
    body = r.json()
    assert body["app"]["origins"] == [LABS]
    # It must be the REAL secret — a response that prints something other than
    # a working credential is worse than no response.
    assert AppCredential.lookup(body["secret"]).name == "connect-labs"

    # And it is nowhere in the list afterwards, because only a hash was kept.
    listed = c.get("/api/workspaces/w1/connected-apps").json()
    assert "secret" not in listed[0]


def test_an_editor_cannot():
    """Same rule as members and invites: administering a tenant is the owner's."""
    _user, _ws, c = _ctx(role=WorkspaceMembership.EDITOR)
    assert _connect(c).status_code == 403


def test_a_non_member_gets_404_not_403():
    """403 would confirm the workspace exists."""
    _user, _ws, _c = _ctx()
    outsider = User.objects.create_user("out", "out@dimagi.com", "pw")
    c = Client()
    c.force_login(outsider)
    assert c.get("/api/workspaces/w1/connected-apps").status_code == 404


def test_a_second_workspaces_owner_cannot_see_my_apps():
    _user, _ws, c = _ctx()
    _connect(c)

    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="w2", display_name="W2", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=ws2, role=WorkspaceMembership.OWNER)
    c2 = Client()
    c2.force_login(other)

    assert c2.get("/api/workspaces/w2/connected-apps").json() == []


def test_an_app_registered_before_this_page_existed_is_not_adoptable_by_anyone():
    """It has no owning workspace, and `workspace_id__in` cannot match NULL.

    This is the nullable-tenant-FK hazard ARCHITECTURE.md records against
    `Agent.workspace` — a predicate that reads "no tenant ⇒ allowed". Here the
    filter shape makes it unreachable rather than the code remembering to
    exclude it.
    """
    _user, _ws, c = _ctx()
    AppCredential.create_credential(name="legacy", domains=[], created_by=None)

    assert c.get("/api/workspaces/w1/connected-apps").json() == []


# --- the URLs, which are the point --------------------------------------------


def test_a_wildcard_url_is_refused_with_a_reason():
    """It would be filtered on read anyway, but silently — and this list is the
    only thing stopping any other site framing the agent."""
    _user, _ws, c = _ctx()

    r = _connect(c, {"name": "x", "origins": ["*"]})

    assert r.status_code == 422
    assert "wildcard" in r.content.decode()
    assert not AppCredential.objects.filter(name="x").exists()


def test_a_url_with_a_path_is_refused():
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [f"{LABS}/supply"]})
    assert r.status_code == 422


def test_a_trailing_slash_is_accepted_and_normalised():
    """`https://host/` is what a person copies out of the address bar, and it is
    not a valid `frame-ancestors` entry. Refusing it would be pedantry."""
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [f"{LABS}/"]})
    assert r.status_code == 201
    assert r.json()["app"]["origins"] == [LABS]


def test_several_environments_can_be_connected_at_once():
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [LABS, "http://localhost:8000"]})
    assert r.json()["app"]["origins"] == [LABS, "http://localhost:8000"]


# --- delegation domains, the strong grant -------------------------------------


def test_you_can_vouch_for_your_own_domain():
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [LABS], "delegation_domains": ["dimagi.com"]})
    assert r.status_code == 201
    assert r.json()["app"]["delegation_domains"] == ["dimagi.com"]


def test_you_cannot_vouch_for_a_domain_that_is_not_yours():
    """The grant lets the holder act as ANY canopy user in the domain, so it is
    bounded by the reach the owner already has."""
    _user, _ws, c = _ctx()

    r = _connect(c, {"name": "x", "origins": [LABS], "delegation_domains": ["example.com"]})

    assert r.status_code == 422
    assert "your own email domain" in r.content.decode()
    assert not AppCredential.objects.filter(name="x").exists()


def test_an_address_is_not_a_domain():
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [LABS], "delegation_domains": ["jj@dimagi.com"]})
    assert r.status_code == 422


def test_no_domains_is_the_default_and_is_a_real_configuration():
    """An app that never calls token-exchange should vouch for nobody."""
    _user, _ws, c = _ctx()
    r = _connect(c)
    assert r.json()["app"]["delegation_domains"] == []


def test_the_surface_cannot_grant_provisioning():
    """`provision_workspace` lets a credential add users to a tenant — a larger
    and different power than embedding, and not something a form should hand
    out. Nothing here can set it."""
    _user, _ws, c = _ctx()
    _connect(c, {"name": "x", "origins": [LABS], "provision_workspace": "w1",
                 "provision_role": "editor"})
    assert AppCredential.objects.get(name="x").provision_workspace_id is None


# --- agents -------------------------------------------------------------------


def test_agents_can_be_offered_and_replaced():
    _user, ws, c = _ctx()
    _agent(ws, "alpha")
    _agent(ws, "beta")
    app_id = _connect(c, {"name": "x", "origins": [LABS], "agents": ["alpha"]}).json()["app"]["id"]

    r = c.patch(f"/api/workspaces/w1/connected-apps/{app_id}",
                data={"agents": ["beta"]}, content_type="application/json")

    assert [a["slug"] for a in r.json()["agents"]] == ["beta"]


def test_an_agent_from_another_workspace_is_refused_rather_than_silently_ignored():
    """The picker intersects the allowlist with the viewer's memberships, so a
    foreign agent would be invisible to everyone — a grant that does nothing
    reads as a bug in the widget."""
    _user, _ws, c = _ctx()
    other = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="w2", display_name="W2", created_by=other)
    _agent(ws2, "theirs")

    r = _connect(c, {"name": "x", "origins": [LABS], "agents": ["theirs"]})

    assert r.status_code == 422
    assert "theirs" in r.content.decode()


# --- lifecycle ----------------------------------------------------------------


def test_rotating_replaces_the_secret_and_kills_the_old_one():
    _user, _ws, c = _ctx()
    created = _connect(c).json()
    old = created["secret"]

    new = c.post(f"/api/workspaces/w1/connected-apps/{created['app']['id']}/rotate").json()["secret"]

    assert new != old
    assert AppCredential.lookup(old) is None
    assert AppCredential.lookup(new) is not None


def test_disconnecting_makes_the_embed_shell_404_immediately():
    _user, _ws, c = _ctx()
    app_id = _connect(c).json()["app"]["id"]
    assert Client().get("/embed/chat?app=connect-labs").status_code == 200

    c.delete(f"/api/workspaces/w1/connected-apps/{app_id}")

    assert Client().get("/embed/chat?app=connect-labs").status_code == 404


def test_a_duplicate_name_is_a_conflict_not_a_second_row():
    """The name is an identifier — it goes in the embed URL and in the host's
    own `canopy.init` call."""
    _user, _ws, c = _ctx()
    _connect(c)
    r = _connect(c)
    assert r.status_code == 409
    assert AppCredential.objects.filter(name="connect-labs").count() == 1


def test_a_name_that_would_not_survive_a_url_is_refused():
    _user, _ws, c = _ctx()
    assert _connect(c, {"name": "my app!", "origins": [LABS]}).status_code == 422


# --- canopy's own widget, in one act ------------------------------------------


def test_enabling_canopys_own_widget_needs_no_decisions(settings):
    """Name, empty delegation list and the frame origin are all facts about
    canopy, not choices. Asking produced the two ways this goes wrong: a name
    that does not match `EMBED_SELF_APP` (nothing mounts, nothing says why) and
    a delegation domain granted by reflex."""
    settings.EMBED_SELF_APP = "canopy-web"
    _user, ws, c = _ctx()
    _agent(ws, "echo")

    r = c.post("/api/workspaces/w1/connected-apps/enable-self",
               data={"agents": ["echo"]}, content_type="application/json")

    assert r.status_code == 200, r.content
    app = AppCredential.objects.get(name="canopy-web")
    assert app.allowed_delegation_domains == []
    # The origin comes from the request, the one value certainly right.
    assert app.frame_origins() == ["http://testserver"]
    assert r.json()["is_self"] is True
    # And the shell now serves, which is the whole point.
    assert Client().get("/embed/chat?app=canopy-web").status_code == 200


def test_enabling_twice_adds_the_second_environment_rather_than_failing(settings):
    settings.EMBED_SELF_APP = "canopy-web"
    # The second call arrives on the real host; without this Django rejects it
    # at ALLOWED_HOSTS and the test would pass or fail for the wrong reason.
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "labs.connect.dimagi.com"]
    _user, _ws, c = _ctx()
    c.post("/api/workspaces/w1/connected-apps/enable-self",
           data={}, content_type="application/json")

    c.post("/api/workspaces/w1/connected-apps/enable-self",
           data={}, content_type="application/json", HTTP_HOST="labs.connect.dimagi.com")

    origins = AppCredential.objects.get(name="canopy-web").frame_origins()
    assert origins == ["http://testserver", "http://labs.connect.dimagi.com"]


def test_enabling_adopts_a_row_left_over_from_the_admin(settings):
    """Otherwise the one credential that already exists is the one nobody can
    edit."""
    settings.EMBED_SELF_APP = "canopy-web"
    _user, _ws, c = _ctx()
    AppCredential.create_credential(name="canopy-web", domains=[], created_by=None)

    c.post("/api/workspaces/w1/connected-apps/enable-self",
           data={}, content_type="application/json")

    assert AppCredential.objects.get(name="canopy-web").workspace_id == "w1"
    assert len(c.get("/api/workspaces/w1/connected-apps").json()) == 1


def test_a_deployment_that_does_not_offer_the_self_widget_says_so(settings):
    settings.EMBED_SELF_APP = ""
    _user, _ws, c = _ctx()
    r = c.post("/api/workspaces/w1/connected-apps/enable-self",
               data={}, content_type="application/json")
    assert r.status_code == 409
    assert "EMBED_SELF_APP" in r.content.decode()


def test_enable_self_is_not_shadowed_by_the_by_id_route(settings):
    """`enable-self` sits exactly where an app id goes.

    It WAS shadowed: Ninja does not narrow `app_id: int` to a numeric path
    converter, so the by-id route matched the literal string first and the POST
    came back 405 (`Allow: PATCH, DELETE`) — which reads as a missing feature,
    not a routing bug. Asserting `resolve()` succeeds is not enough; it
    succeeded, on the wrong view.
    """
    settings.EMBED_SELF_APP = "canopy-web"
    _user, _ws, c = _ctx()

    r = c.post("/api/workspaces/w1/connected-apps/enable-self",
               data={}, content_type="application/json")

    assert r.status_code == 200, f"shadowed by the by-id route: {r.status_code}"


def test_only_an_owner_can_turn_it_on(settings):
    settings.EMBED_SELF_APP = "canopy-web"
    _user, _ws, c = _ctx(role=WorkspaceMembership.EDITOR)
    r = c.post("/api/workspaces/w1/connected-apps/enable-self",
               data={}, content_type="application/json")
    assert r.status_code == 403
    assert not AppCredential.objects.exists()


# --- the service layer's own guard --------------------------------------------


def test_owned_workspace_slugs_excludes_editor_memberships():
    _user, ws, _c = _ctx()
    editor = _member(ws, "ed@dimagi.com", WorkspaceMembership.EDITOR)
    assert embed_apps.owned_workspace_slugs(editor) == set()
