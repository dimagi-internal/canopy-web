"""One site, several tenants, and the walls between them.

A connected site is one identity in the world — one name, one key, one `iss` —
and it may act for more than one canopy tenant. That makes "who decided this"
the question the whole file is about: each tenant grants the site for itself,
and nothing one tenant's owner does may reach into another's.

The failure this replaces: `AppCredential.workspace` alone decided who
administers a site, whose agents it may offer, and where its visitors are
recorded. A site serving two tenants therefore had to be registered twice under
two names and choose which name to sign with — and the second tenant's owner
had no way to grant, or revoke, anything.
"""

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.tokens import assertions, embed_apps
from apps.tokens.models import AppCredential, AppCredentialAgent, AppCredentialTenant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(encoding=serialization.Encoding.PEM,
                           format=serialization.PrivateFormat.PKCS8,
                           encryption_algorithm=serialization.NoEncryption()).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def _tenant(slug, email):
    owner = User.objects.create_user(slug, email, "pw")
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws,
                                       role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(owner)
    return owner, ws, c


def _world():
    """One site, registered by tenant A, and a second tenant B that has not
    granted it yet."""
    owner_a, ws_a, c_a = _tenant("alpha", "a@dimagi.com")
    owner_b, ws_b, c_b = _tenant("beta", "b@dimagi.com")
    Agent.objects.create(slug="a-agent", name="A", workspace=ws_a)
    Agent.objects.create(slug="b-agent", name="B", workspace=ws_b)
    priv, pub = _keypair()
    _raw, app = embed_apps.register(
        user=owner_a, workspace_slug="alpha", name="connect-labs",
        origins=["https://labs.example.com"], agents=["a-agent"], public_keys=[pub],
    )
    return app, priv, (owner_a, ws_a, c_a), (owner_b, ws_b, c_b)


def _assert(priv, sub="u-1", aud=None, email="", verified=False):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"iss": "connect-labs", "sub": sub, "aud": aud or assertions.audience(),
              "iat": int(now.timestamp()), "exp": int(now.timestamp()) + 60,
              "jti": str(uuid.uuid4())}
    if email:
        claims.update({"email": email, "email_verified": verified})
    return jwt.encode(claims, priv, algorithm="EdDSA")


def _mint(priv, agent_slug="", **kw):
    body = {"assertion": _assert(priv, **kw)}
    if agent_slug:
        body["agent_slug"] = agent_slug
    return Client().post("/api/auth/contact-token", data=body, content_type="application/json")


# --- one identity, many tenants -------------------------------------------------


def test_a_second_tenant_grants_the_same_site_under_the_same_name():
    """The point. Before this, tenant B had to register `connect-labs-b` and the
    site had to know which `iss` meant which tenant."""
    app, priv, (_oa, _wa, _ca), (_ob, _wb, c_b) = _world()

    r = c_b.post("/api/workspaces/beta/connected-apps/grants",
                 data={"name": "connect-labs", "agents": ["b-agent"]},
                 content_type="application/json")

    assert r.status_code == 201, r.content
    assert AppCredential.objects.filter(name="connect-labs").count() == 1
    assert {g.workspace_id for g in app.tenant_grants.all()} == {"alpha", "beta"}


def test_which_tenant_a_visitor_lands_in_is_decided_by_the_agent_they_name():
    """An agent belongs to exactly one workspace, so naming one names the tenant
    — and it is the thing a host already knows when it mounts a widget."""
    app, priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs", "agents": ["b-agent"]},
             content_type="application/json")

    in_alpha = _mint(priv, agent_slug="a-agent", sub="u-1")
    in_beta = _mint(priv, agent_slug="b-agent", sub="u-1")

    assert in_alpha.status_code == 200 and in_beta.status_code == 200
    from apps.contacts.models import Contact

    # The SAME person at the site is two contacts, one per tenant — deliberate:
    # merging them would leak one tenant's dealings into the other.
    assert Contact.objects.filter(app=app, external_id="u-1").count() == 2
    assert {c.workspace_id for c in Contact.objects.filter(app=app, external_id="u-1")} \
        == {"alpha", "beta"}
    assert in_alpha.json()["contact_id"] != in_beta.json()["contact_id"]


def test_naming_no_agent_still_means_the_tenant_that_registered_it():
    """Every integration written before this sent only an assertion."""
    _app, priv, _a, _b = _world()
    r = _mint(priv, sub="u-9")
    assert r.status_code == 200
    from apps.contacts.models import Contact

    assert Contact.objects.get(external_id="u-9").workspace_id == "alpha"


# --- the walls ------------------------------------------------------------------


def test_a_site_cannot_reach_a_tenant_that_has_not_granted_it():
    """B's agent exists and the site is real; B has said nothing. Fail closed."""
    _app, priv, _a, _b = _world()
    r = _mint(priv, agent_slug="b-agent")
    assert r.status_code == 403
    assert b"not_granted" in r.content


def test_a_tenant_can_only_offer_its_own_agents():
    app, _priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs"}, content_type="application/json")

    r = c_b.patch(f"/api/workspaces/beta/connected-apps/{app.pk}",
                  data={"agents": ["a-agent"]}, content_type="application/json")

    assert r.status_code == 422
    assert AppCredentialAgent.objects.filter(app=app, agent__slug="a-agent").exists(), \
        "alpha's own grant is untouched"


def test_one_tenant_editing_its_agents_never_withdraws_anothers():
    """The delete half of `set_agents` is the dangerous one: scoped wrongly, B
    saving its list would silently unoffer A's agents."""
    app, _priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs", "agents": ["b-agent"]},
             content_type="application/json")

    c_b.patch(f"/api/workspaces/beta/connected-apps/{app.pk}",
              data={"agents": []}, content_type="application/json")

    assert [a.agent.slug for a in AppCredentialAgent.objects.filter(app=app)] == ["a-agent"]


def test_a_granting_tenant_cannot_change_the_sites_identity():
    """Origins and keys belong to the tenant that registered the site. B editing
    them would reach into every other tenant's integration."""
    app, _priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs"}, content_type="application/json")

    r = c_b.patch(f"/api/workspaces/beta/connected-apps/{app.pk}",
                  data={"origins": ["https://evil.example.com"]},
                  content_type="application/json")

    assert r.status_code == 403
    app.refresh_from_db()
    assert app.frame_origins() == ["https://labs.example.com"]


def test_a_tenant_sees_only_the_agents_it_granted():
    """Listing another tenant's agents would leak that they exist to an owner
    with no business knowing."""
    app, _priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs", "agents": ["b-agent"]},
             content_type="application/json")

    rows = c_b.get("/api/workspaces/beta/connected-apps").json()
    assert [a["slug"] for a in rows[0]["agents"]] == ["b-agent"]
    assert rows[0]["administered_here"] is False
    assert rows[0]["agent_workspaces"] == ["alpha", "beta"], "that it is shared is visible"


def test_a_domain_one_tenant_allows_is_not_allowed_everywhere():
    """`resolvable_domains` lets a site speak for existing canopy users. Held on
    the site, one tenant's owner would be widening what it may do in another."""
    app, priv, (_oa, _wa, c_a), (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs", "agents": ["b-agent"]},
             content_type="application/json")
    c_a.patch(f"/api/workspaces/alpha/connected-apps/{app.pk}",
              data={"resolvable_domains": ["dimagi.com"]}, content_type="application/json")

    grants = {g.workspace_id: list(g.resolvable_domains or []) for g in app.tenant_grants.all()}
    assert grants == {"alpha": ["dimagi.com"], "beta": []}


def test_withdrawing_one_grant_leaves_every_other_tenant_working():
    app, priv, _a, (_ob, _wb, c_b) = _world()
    c_b.post("/api/workspaces/beta/connected-apps/grants",
             data={"name": "connect-labs", "agents": ["b-agent"]},
             content_type="application/json")

    r = c_b.delete(f"/api/workspaces/beta/connected-apps/{app.pk}/grant")

    assert r.status_code == 204
    assert _mint(priv, agent_slug="b-agent").status_code == 403
    assert _mint(priv, agent_slug="a-agent").status_code == 200, "alpha is unaffected"
    assert AppCredential.objects.filter(name="connect-labs").exists(), "the site remains"


def test_granting_needs_a_site_that_already_exists():
    """Otherwise typing a free name would quietly create an identity nobody
    controls, with this tenant trusting it."""
    _app, _priv, _a, (_ob, _wb, c_b) = _world()
    r = c_b.post("/api/workspaces/beta/connected-apps/grants",
                 data={"name": "not-a-site"}, content_type="application/json")
    assert r.status_code == 404
    assert not AppCredentialTenant.objects.filter(workspace_id="beta").exists()


def test_only_an_owner_of_this_tenant_may_grant():
    app, _priv, _a, (_ob, ws_b, _cb) = _world()
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=ws_b,
                                       role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(editor)

    r = c.post("/api/workspaces/beta/connected-apps/grants",
               data={"name": "connect-labs"}, content_type="application/json")

    assert r.status_code == 403
    assert not AppCredentialTenant.objects.filter(workspace_id="beta").exists()


def test_a_stranger_cannot_even_learn_the_tenant_exists():
    app, _priv, _a, _b = _world()
    outsider = User.objects.create_user("out", "out@dimagi.com", "pw")
    c = Client()
    c.force_login(outsider)

    r = c.post("/api/workspaces/beta/connected-apps/grants",
               data={"name": "connect-labs"}, content_type="application/json")

    assert r.status_code == 404, "not 403 — that would confirm beta exists"
