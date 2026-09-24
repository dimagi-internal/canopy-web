"""Each tenant registers a connected system itself — no shared row, no custodian.

A site used to be one shared row: one tenant ("the custodian") maintained its
name, keys and origins, and every other tenant held a grant to it (#944, #955).
That arbitration existed only because the row was shared. Once keys became a
URL (#929), what two tenants would have to agree on is where a host publishes
its JWKS — cheap to copy, and a copy that cannot drift from the keys it points
at. So each tenant now registers the system on its own (2026-09-24).

What this file pins is the consequence: **nothing one tenant does can reach
another tenant's integration** — not registering a name, not editing keys, not
disconnecting, not deleting the workspace. And the cost that buys, stated as
tests too: a site's name identifies it only within a tenant, so the tenant must
come from somewhere — the agent a host names.
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
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ORIGIN = "https://labs.example.com"


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
    Agent.objects.create(slug=f"{slug}-agent", name=slug, workspace=ws)
    c = Client()
    c.force_login(owner)
    return owner, c


def _register(owner, slug, pub, name="connect-labs", origins=(ORIGIN,)):
    app = embed_apps.register(
        user=owner, workspace_slug=slug, name=name, origins=list(origins),
        agents=[f"{slug}-agent"], public_keys=[pub],
    )
    return app


def _world(*, same_key=True):
    """Two tenants, each registering the SAME external system under the SAME
    name — which is exactly what the shared row used to forbid."""
    owner_a, c_a = _tenant("alpha", "a@dimagi.com")
    owner_b, c_b = _tenant("beta", "b@dimagi.com")
    priv, pub = _keypair()
    priv_b, pub_b = (priv, pub) if same_key else _keypair()
    app_a = _register(owner_a, "alpha", pub)
    app_b = _register(owner_b, "beta", pub_b)
    return priv, priv_b, (owner_a, c_a, app_a), (owner_b, c_b, app_b)


def _assertion(priv, sub="u-1", iss="connect-labs"):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return jwt.encode({"iss": iss, "sub": sub, "aud": assertions.audience(),
                       "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
                      priv, algorithm="EdDSA")


def _mint(priv, agent_slug="", **kw):
    body = {"assertion": _assertion(priv, **kw)}
    if agent_slug:
        body["agent_slug"] = agent_slug
    return Client().post("/api/auth/contact-token", data=body, content_type="application/json")


def _workspace_of(r):
    from apps.contacts.models import Contact

    return Contact.objects.get(pk=r.json()["contact_id"]).workspace_id


# --- registering ---------------------------------------------------------------


def test_two_tenants_register_the_same_system_under_the_same_name():
    """The point. A name a stranger holds is no reason to refuse one: refusing
    would make one tenant's registration constrain another's."""
    _p, _pb, (_oa, _ca, app_a), (_ob, _cb, app_b) = _world()

    assert app_a.pk != app_b.pk
    assert {(a.workspace_id, a.name) for a in AppCredential.objects.all()} \
        == {("alpha", "connect-labs"), ("beta", "connect-labs")}


def test_a_name_is_still_unique_within_one_tenant():
    owner, _c = _tenant("alpha", "a@dimagi.com")
    _priv, pub = _keypair()
    _register(owner, "alpha", pub)

    with pytest.raises(embed_apps.EmbedAppError) as exc:
        _register(owner, "alpha", pub)
    assert exc.value.code == "duplicate_name"


def test_a_disconnected_name_can_be_registered_again():
    """Disconnecting retires the row rather than deleting it (its contacts hang
    off it), so the uniqueness has to be over LIVE rows or the name is gone for
    good."""
    owner, c = _tenant("alpha", "a@dimagi.com")
    _priv, pub = _keypair()
    app = _register(owner, "alpha", pub)
    assert c.delete(f"/api/workspaces/alpha/connected-apps/{app.pk}").status_code == 204

    again = _register(owner, "alpha", pub)

    assert again.pk != app.pk


def test_there_is_no_endpoint_for_granting_another_tenants_site():
    """Granting a site someone else registered was the shared-row model's
    second door. A tenant that wants the system registers it."""
    _p, _pb, _a, (_ob, c_b, _app_b) = _world()

    r = c_b.post("/api/workspaces/beta/connected-apps/grants",
                 data={"name": "connect-labs"}, content_type="application/json")

    assert r.status_code in (404, 405)


def test_a_site_offers_only_its_own_tenants_agents():
    owner_a, _c = _tenant("alpha", "a@dimagi.com")
    _tenant("beta", "b@dimagi.com")
    _priv, pub = _keypair()

    with pytest.raises(embed_apps.EmbedAppError) as exc:
        embed_apps.register(user=owner_a, workspace_slug="alpha", name="connect-labs",
                            origins=[ORIGIN], agents=["beta-agent"], public_keys=[pub])
    assert exc.value.code == "unknown_agent"
    assert not AppCredential.objects.exists(), "a refused registration leaves nothing behind"


# --- which tenant an assertion is for --------------------------------------------


def test_the_agent_named_decides_which_tenants_registration_verifies_it():
    priv, _pb, _a, _b = _world()

    in_alpha = _mint(priv, agent_slug="alpha-agent")
    in_beta = _mint(priv, agent_slug="beta-agent")

    assert in_alpha.status_code == 200, in_alpha.content
    assert in_beta.status_code == 200, in_beta.content
    assert (_workspace_of(in_alpha), _workspace_of(in_beta)) == ("alpha", "beta")


def test_an_assertion_is_checked_against_the_named_tenants_keys_only():
    """Two tenants' `connect-labs` may be different systems. A's key must not
    verify for B, whatever the rows are called — the name selects a row, and
    only that row's keys count."""
    priv_a, _priv_b, _a, _b = _world(same_key=False)

    r = _mint(priv_a, agent_slug="beta-agent")

    assert r.status_code == 401
    assert r.json()["detail"].startswith("bad_signature")


def test_a_site_cannot_reach_a_tenant_that_has_not_registered_it():
    owner_a, _c = _tenant("alpha", "a@dimagi.com")
    _tenant("beta", "b@dimagi.com")
    priv, pub = _keypair()
    _register(owner_a, "alpha", pub)

    r = _mint(priv, agent_slug="beta-agent")

    assert r.status_code == 401
    assert r.json()["detail"].startswith("unknown_issuer")


def test_naming_no_agent_works_while_the_name_is_unambiguous():
    """Every integration written before per-tenant sites sends only an
    assertion, and connect-labs and ace-web still do."""
    owner_a, _c = _tenant("alpha", "a@dimagi.com")
    priv, pub = _keypair()
    _register(owner_a, "alpha", pub)

    r = _mint(priv)

    assert r.status_code == 200, r.content
    assert _workspace_of(r) == "alpha"


def test_naming_no_agent_is_refused_rather_than_guessed_once_the_name_is_shared():
    """Picking one would route a visitor into a tenant their host never meant.
    The refusal names the fix."""
    priv, _pb, _a, _b = _world()

    r = _mint(priv)

    assert r.status_code == 409
    assert r.json()["detail"].startswith("ambiguous_issuer")
    assert "agent" in r.json()["detail"]


# --- nothing one tenant does reaches another --------------------------------------


def test_editing_keys_and_origins_touches_only_this_tenants_row():
    _p, _pb, (_oa, _ca, app_a), (_ob, c_b, app_b) = _world()

    r = c_b.patch(f"/api/workspaces/beta/connected-apps/{app_b.pk}",
                  data={"origins": ["https://elsewhere.example.com"], "public_keys": []},
                  content_type="application/json")

    assert r.status_code == 200, r.content
    app_a.refresh_from_db()
    assert app_a.frame_origins() == [ORIGIN]
    assert app_a.public_keys


def test_a_tenant_cannot_address_another_tenants_row_at_all():
    _p, _pb, (_oa, _ca, app_a), (_ob, c_b, _app_b) = _world()

    assert c_b.patch(f"/api/workspaces/beta/connected-apps/{app_a.pk}",
                     data={"origins": []}, content_type="application/json").status_code == 404
    assert c_b.delete(f"/api/workspaces/beta/connected-apps/{app_a.pk}").status_code == 404


def test_each_tenant_lists_only_its_own_registration():
    _p, _pb, (_oa, c_a, app_a), (_ob, c_b, app_b) = _world()

    assert [r["id"] for r in c_a.get("/api/workspaces/alpha/connected-apps").json()] == [app_a.pk]
    assert [r["id"] for r in c_b.get("/api/workspaces/beta/connected-apps").json()] == [app_b.pk]


def test_disconnecting_ends_only_this_tenants_integration():
    priv, _pb, (_oa, c_a, app_a), _b = _world()

    assert c_a.delete(f"/api/workspaces/alpha/connected-apps/{app_a.pk}").status_code == 204

    assert _mint(priv, agent_slug="alpha-agent").status_code == 401
    assert _mint(priv, agent_slug="beta-agent").status_code == 200


def test_deleting_a_workspace_takes_its_registration_and_nothing_else():
    priv, _pb, (_oa, _ca, app_a), _b = _world()

    Agent.objects.filter(workspace_id="alpha").delete()  # PROTECTs its workspace
    Workspace.objects.filter(slug="alpha").delete()

    assert not AppCredential.objects.filter(pk=app_a.pk).exists()
    assert _mint(priv, agent_slug="beta-agent").status_code == 200


# --- the embed shell ---------------------------------------------------------------


def test_the_embed_shell_frames_for_the_named_agents_tenant():
    owner_a, _c = _tenant("alpha", "a@dimagi.com")
    owner_b, _c = _tenant("beta", "b@dimagi.com")
    _priv, pub = _keypair()
    _register(owner_a, "alpha", pub, origins=["https://a.example.com"])
    _register(owner_b, "beta", pub, origins=["https://b.example.com"])

    r = Client().get("/embed/chat", {"app": "connect-labs", "agent": "beta-agent"})

    assert r.status_code == 200
    assert r["Content-Security-Policy"] == "frame-ancestors https://b.example.com"


def test_the_embed_shell_refuses_an_ambiguous_name():
    """Never a union of both tenants' origins: that would let one tenant's
    registration widen who may frame another's."""
    _world()

    assert Client().get("/embed/chat", {"app": "connect-labs"}).status_code == 404
