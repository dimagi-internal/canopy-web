"""Canopy knows two tenants' contacts are the same human — and shows neither.

Two rules that sound contradictory and are not:

* a `Contact` is per workspace, because merging what two tenants know about
  somebody would leak one's dealings into the other;
* canopy still records that the two records are the same PERSON, because that
  is knowable exactly once — at the moment the record is made — and a
  connection nobody wrote down cannot be offered later.

So the tests come in pairs: the link exists, and nothing reads across it.
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
from apps.contacts import services as contact_services
from apps.contacts.models import Contact, Person
from apps.tokens import assertions, embed_apps
from apps.tokens.models import AppCredentialTenant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def _tenant(slug, email):
    owner = User.objects.create_user(slug, email, "pw")
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws,
                                       role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(owner)
    return owner, ws, c


def _world():
    owner_a, ws_a, c_a = _tenant("alpha", "a@dimagi.com")
    owner_b, ws_b, c_b = _tenant("beta", "b@dimagi.com")
    Agent.objects.create(slug="a-agent", name="A", workspace=ws_a)
    Agent.objects.create(slug="b-agent", name="B", workspace=ws_b)
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(encoding=serialization.Encoding.PEM,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    _raw, app = embed_apps.register(
        user=owner_a, workspace_slug="alpha", name="connect-labs",
        origins=["https://labs.example.com"], agents=["a-agent"], public_keys=[pub])
    embed_apps.authorize_tenant(user=owner_b, app=app, workspace_slug="beta")
    embed_apps.set_agents(app, "beta", ["b-agent"])
    return app, pem, (ws_a, c_a), (ws_b, c_b)


def _mint(priv, agent_slug, sub="visitor-1"):
    now = dt.datetime.now(dt.timezone.utc)
    token = jwt.encode(
        {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
         "iat": int(now.timestamp()), "exp": int(now.timestamp()) + 60,
         "jti": str(uuid.uuid4())}, priv, algorithm="EdDSA")
    return Client().post("/api/auth/contact-token",
                         data={"assertion": token, "agent_slug": agent_slug},
                         content_type="application/json")


def test_the_same_visitor_in_two_tenants_is_two_contacts_and_one_person():
    """The whole point. Two records, because that is what a tenant may see;
    one person, because that is what is true."""
    app, priv, _a, _b = _world()

    _mint(priv, "a-agent")
    _mint(priv, "b-agent")

    rows = Contact.objects.filter(app=app, external_id="visitor-1")
    assert rows.count() == 2
    assert {r.workspace_id for r in rows} == {"alpha", "beta"}
    assert len({r.person_id for r in rows}) == 1
    assert rows[0].person_id is not None


def test_neither_tenant_can_see_the_others_record_of_them():
    """Knowing must not become showing: the contact list is still one tenant's."""
    _app, priv, (_wa, c_a), (_wb, c_b) = _world()
    _mint(priv, "a-agent")
    _mint(priv, "b-agent")

    in_alpha = c_a.get("/api/contacts/").json()
    in_beta = c_b.get("/api/contacts/").json()

    assert [c["workspace_id"] for c in in_alpha["items"]] == ["alpha"]
    assert [c["workspace_id"] for c in in_beta["items"]] == ["beta"]
    # And the id one tenant holds is not fetchable from the other.
    other = in_beta["items"][0]["id"]
    assert c_a.get(f"/api/contacts/{other}/").status_code == 404


def test_the_person_is_not_exposed_on_the_contact_api():
    """Nothing reads across the link today, and the API must not hand out a key
    that invites somebody to try."""
    _app, priv, (_wa, c_a), _b = _world()
    _mint(priv, "a-agent")

    body = c_a.get("/api/contacts/").json()["items"][0]

    assert "person" not in body and "person_id" not in body


def test_a_correspondent_writing_to_two_tenants_is_one_person():
    """For a correspondent the ADDRESS is the identity, so the same key works
    without a site involved."""
    owner_a, ws_a, _ca = _tenant("alpha", "a@dimagi.com")
    _ob, ws_b, _cb = _tenant("beta", "b@dimagi.com")

    first = contact_services.record_inbound_sender(
        workspace=ws_a, address="partner@example.org", display_name="P")
    second = contact_services.record_inbound_sender(
        workspace=ws_b, address="partner@example.org", display_name="P")

    assert first.pk != second.pk
    assert first.person_id == second.person_id is not None


def test_a_site_asserting_an_email_does_not_join_it_to_a_correspondent():
    """The site's id is the stronger key. Matching on an address it merely
    claims would be believing the assertion — what the grade exists to avoid."""
    app, priv, (ws_a, _ca), _b = _world()
    mailed = contact_services.record_inbound_sender(
        workspace=ws_a, address="visitor@example.org", display_name="V")
    visitor = contact_services.record_embed_visitor(
        workspace=ws_a, app=app, external_id="visitor-1", email="visitor@example.org")

    assert mailed.person_id != visitor.person_id


def test_a_slack_contact_has_no_person_rather_than_a_guessed_one():
    """A null reads as "cannot tell", which is true. A guess would be a wrong
    answer baked into data, and this is the table other features will trust."""
    _oa, ws_a, _ca = _tenant("alpha", "a@dimagi.com")

    contact = contact_services.record_slack_user(
        workspace=ws_a, team_id="T1", slack_user_id="U1", email="s@example.org")

    assert contact.person_id is None


def test_meeting_the_same_person_twice_does_not_make_a_second_one():
    app, priv, _a, _b = _world()
    for _ in range(3):
        _mint(priv, "a-agent")
    assert Person.objects.filter(app=app, external_id="visitor-1").count() == 1


def test_two_sites_using_the_same_id_are_two_people():
    """`external_id` lives in the SITE's namespace — `u-1` at one site and `u-1`
    at another are unrelated, and joining them would invent a person."""
    owner, _ws, _c = _tenant("alpha", "a@dimagi.com")
    _raw1, site1 = embed_apps.register(user=owner, workspace_slug="alpha", name="site-one",
                                       origins=["https://one.example.com"])
    _raw2, site2 = embed_apps.register(user=owner, workspace_slug="alpha", name="site-two",
                                       origins=["https://two.example.com"])

    one = contact_services.person_for(app=site1, external_id="u-1")
    two = contact_services.person_for(app=site2, external_id="u-1")

    assert one.pk != two.pk


def test_nothing_to_key_on_is_no_person_at_all():
    assert contact_services.person_for() is None
    assert contact_services.person_for(external_id="u-1") is None, "an id needs its site"


def test_the_grant_rows_are_what_made_this_reachable():
    """Guard for the setup above rather than a claim of its own: both tenants
    had to have granted the site for either contact to exist."""
    app, _priv, _a, _b = _world()
    assert {g.workspace_id for g in AppCredentialTenant.objects.filter(app=app)} \
        == {"alpha", "beta"}
