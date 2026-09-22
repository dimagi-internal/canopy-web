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
    AppCredential.create_credential(name="legacy", created_by=None)

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


# --- delegation domains: the capability this page used to have ---------------
# The tests that pinned its bounds ("you may vouch only for your own domain")
# are gone with the capability, replaced by the refusal below and by
# tests/test_contact_assertions.py, which covers what took its place.


def test_the_surface_hands_out_no_email_vouching_at_all():
    """A connected site cannot speak for canopy's users. Vouching by email
    domain, and the provisioning that rode with it, went with token-exchange —
    so there is no field for a form to set and none for the API to echo. A site
    proves who its visitor is with a signature instead, and an arrival never
    creates an account."""
    _user, _ws, c = _ctx()

    app = _connect(c, {"name": "x", "origins": [LABS],
                       # Ignored, not honoured: these keys no longer exist.
                       "delegation_domains": ["dimagi.com"],
                       "provision_workspace": "w1", "provision_role": "editor"}).json()["app"]

    assert "delegation_domains" not in app
    row = AppCredential.objects.get(name="x")
    assert not hasattr(row, "allowed_delegation_domains")
    assert not hasattr(row, "provision_workspace_id")


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


# --- canopy's own pages are just another connected site ----------------------


def test_ticking_the_box_makes_canopy_show_that_apps_panel():
    """No special name, no setting. `EMBED_SELF_APP` made canopy a special case
    twice: the page needed its own section, and a name that did not match the
    setting produced no widget and no error on either side."""
    _user, ws, c = _ctx()
    _agent(ws, "echo")

    body = _connect(c, {"name": "canopy-itself", "origins": [LABS],
                        "agents": ["echo"], "show_on_canopy_pages": True}).json()

    assert body["app"]["shows_on_canopy_pages"] is True
    assert AppCredential.objects.get(name="canopy-itself").show_on_canopy_pages


def test_ticking_it_also_adds_canopys_own_origin():
    """The two are not independent: `frame-ancestors` is built from the URL
    list, so a ticked app without canopy's origin is on by every visible
    measure and dead in the browser."""
    _user, _ws, c = _ctx()

    _connect(c, {"name": "x", "origins": [], "show_on_canopy_pages": True})

    assert "http://testserver" in AppCredential.objects.get(name="x").frame_origins()
    # And the shell serves, which is the only proof that matters.
    assert Client().get("/embed/chat?app=x").status_code == 200


def test_only_one_app_may_show_there():
    _user, _ws, c = _ctx()
    _connect(c, {"name": "first", "origins": [LABS], "show_on_canopy_pages": True})

    r = _connect(c, {"name": "second", "origins": [LABS], "show_on_canopy_pages": True})

    assert r.status_code == 409
    assert b"first" in r.content, "the refusal should name the app already using it"


def test_it_can_be_turned_off_again():
    _user, _ws, c = _ctx()
    app_id = _connect(c, {"name": "x", "origins": [LABS],
                          "show_on_canopy_pages": True}).json()["app"]["id"]

    r = c.patch(f"/api/workspaces/w1/connected-apps/{app_id}",
                data={"show_on_canopy_pages": False}, content_type="application/json")

    assert r.json()["shows_on_canopy_pages"] is False


def test_the_widget_is_off_when_no_app_shows_there():
    """Off is the default, and it stays the default: this mounts a chat panel
    on every authenticated page, which no deployment should grow by surprise."""
    user, _ws, c = _ctx()
    _connect(c)

    assert c.get("/api/embed/self").json()["enabled"] is False


def test_the_widget_reports_the_app_that_shows_there():
    _user, _ws, c = _ctx()
    _connect(c, {"name": "x", "origins": [LABS], "show_on_canopy_pages": True})

    body = c.get("/api/embed/self").json()

    assert body == {"enabled": True, "app": "x", "agent": ""}


def test_a_revoked_app_stops_showing_there():
    _user, _ws, c = _ctx()
    app_id = _connect(c, {"name": "x", "origins": [LABS],
                          "show_on_canopy_pages": True}).json()["app"]["id"]

    c.delete(f"/api/workspaces/w1/connected-apps/{app_id}")

    assert c.get("/api/embed/self").json()["enabled"] is False


# --- vouching for a domain is no longer something this page can do -----------


# --- the service layer's own guard --------------------------------------------


def test_owned_workspace_slugs_excludes_editor_memberships():
    _user, ws, _c = _ctx()
    editor = _member(ws, "ed@dimagi.com", WorkspaceMembership.EDITOR)
    assert embed_apps.owned_workspace_slugs(editor) == set()


# --- signing keys -------------------------------------------------------------


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


def test_a_site_can_register_a_signing_key():
    _user, _ws, c = _ctx()
    _priv, pub = _keypair()

    body = _connect(c, {"name": "x", "origins": [LABS], "public_keys": [pub]}).json()

    assert body["app"]["signs_assertions"] is True
    # Stored normalised (PEM keeps a trailing newline; the stored form strips
    # it), so compare like for like rather than pinning the incidental.
    assert AppCredential.objects.get(name="x").public_keys == [pub.strip()]


def test_pasting_a_PRIVATE_key_is_refused_loudly():
    """The dangerous mistake, because it would WORK — and the site's signing
    key would then be sitting in canopy's database, undoing the whole reason
    for using signatures."""
    _user, _ws, c = _ctx()
    priv, _pub = _keypair()

    r = _connect(c, {"name": "x", "origins": [LABS], "public_keys": [priv]})

    assert r.status_code == 422
    assert b"PRIVATE key" in r.content
    assert not AppCredential.objects.filter(name="x").exists()


def test_an_unreadable_key_is_refused_at_the_paste():
    """Otherwise it fails far from here — assertions stop verifying and nothing
    points back to this field."""
    _user, _ws, c = _ctx()
    r = _connect(c, {"name": "x", "origins": [LABS], "public_keys": ["hunter2"]})
    assert r.status_code == 422
    assert b"PEM public key" in r.content


def test_keys_can_be_rotated_by_editing():
    _user, _ws, c = _ctx()
    _p1, pub1 = _keypair()
    _p2, pub2 = _keypair()
    app_id = _connect(c, {"name": "x", "origins": [LABS], "public_keys": [pub1]}).json()["app"]["id"]

    r = c.patch(f"/api/workspaces/w1/connected-apps/{app_id}",
                data={"public_keys": [pub1, pub2]}, content_type="application/json")

    assert len(r.json()["public_keys"]) == 2


def test_a_site_with_no_key_says_so():
    _user, _ws, c = _ctx()
    assert _connect(c).json()["app"]["signs_assertions"] is False
