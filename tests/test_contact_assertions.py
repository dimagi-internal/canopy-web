"""A connected site vouching for a visitor who has no canopy account.

Most of this file is forgeries. That is the point: a signature check is only
worth having if it refuses, and every JWT vulnerability worth the name comes
from a verifier being lenient about something — the algorithm, the audience,
the lifetime, or replay.

The other half is the principal. A contact token must be unable to reach
anything a user token can, and that has to be true by construction rather than
because each view remembered.
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
from apps.contacts.models import Contact
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    """`jti` replay protection lives in the cache, which is process-wide and
    does not roll back with the test transaction."""
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pem_priv = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pem_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return pem_priv, pem_pub


def _setup(name="connect-labs", with_key=True):
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    _raw, app = AppCredential.create_credential(name=name, created_by=owner)
    app.workspace = ws
    priv, pub = _keypair()
    app.public_keys = [pub] if with_key else []
    app.save(update_fields=["workspace", "public_keys"])
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return app, priv, ws


def _assert(priv, *, iss="connect-labs", sub="u-42", aud=None, lifetime=60,
            alg="EdDSA", key=None, jti=None, **extra):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "iss": iss,
        "sub": sub,
        "aud": aud if aud is not None else assertions.audience(),
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + lifetime,
        "jti": jti or str(uuid.uuid4()),
        **extra,
    }
    return jwt.encode(claims, key if key is not None else priv, algorithm=alg)


def _exchange(token):
    return Client().post(
        "/api/auth/contact-token",
        data={"assertion": token},
        content_type="application/json",
    )


# --- the happy path -----------------------------------------------------------


def test_a_signed_assertion_gets_the_visitor_a_token_and_a_contact():
    app, priv, ws = _setup()

    r = _exchange(_assert(priv, sub="u-42", name="Amina", email="a@llo.org"))

    assert r.status_code == 200, r.content
    body = r.json()
    contact = Contact.objects.get(pk=body["contact_id"])
    assert contact.external_id == "u-42"
    assert contact.workspace_id == "w1"     # from the APP's row, never the assertion
    assert contact.source == Contact.SOURCE_EMBED
    # Tier 2: a signature over this specific visitor, the same standing as DKIM.
    assert contact.auth_at_least(Contact.TIER_SIGNED)
    assert not contact.auth_at_least(Contact.TIER_SIGNED_ALIGNED)
    assert ContactToken.lookup(body["token"]) is not None


def test_the_visitor_is_not_a_user_and_gains_no_membership():
    """The property the whole model exists for."""
    app, priv, ws = _setup()

    _exchange(_assert(priv))

    contact = Contact.objects.get()
    assert contact.user is None
    assert WorkspaceMembership.objects.filter(workspace=ws).count() == 1  # just the owner
    assert User.objects.count() == 1


def test_returning_visitors_are_the_same_contact():
    app, priv, _ws = _setup()
    a = _exchange(_assert(priv, sub="u-42")).json()["contact_id"]
    b = _exchange(_assert(priv, sub="u-42")).json()["contact_id"]
    assert a == b
    assert Contact.objects.get(pk=a).message_count == 2


# --- forgeries ----------------------------------------------------------------


def test_alg_none_is_refused():
    """The classic. It works only against a verifier that lets the token pick
    its own algorithm."""
    app, _priv, _ws = _setup()
    token = jwt.encode(
        {"iss": "connect-labs", "sub": "u-42", "aud": assertions.audience(),
         "iat": 0, "exp": 9999999999, "jti": "x"},
        key="", algorithm="none",
    )
    assert _exchange(token).status_code == 401


def test_an_hmac_signature_using_the_public_key_as_the_secret_is_refused():
    """The other classic: the "public" key is public, so if HMAC were allowed
    anyone could sign with it. Asymmetric-only is what makes publishing safe.

    Hand-rolled rather than built with `jwt.encode`, which refuses to HS256-sign
    with a PEM public key (`InvalidKeyError`). That guard is on the SIGNING
    side and protects nobody here — an attacker is not using our library — so
    building the token by hand is the only way this assertion reaches the
    verifier at all. The first version did not, and passed for the wrong reason.
    """
    import base64
    import hashlib
    import hmac
    import json as _json

    app, _priv, _ws = _setup()
    public_pem = app.public_keys[0].encode()

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    header = b64(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(_json.dumps({
        "iss": "connect-labs", "sub": "u-42", "aud": assertions.audience(),
        "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()),
    }).encode())
    signing_input = header + b"." + payload
    signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    token = (signing_input + b"." + signature).decode()

    r = _exchange(token)

    assert r.status_code == 401
    # Refused by OUR algorithm allowlist, having actually been checked — not
    # rejected earlier as malformed, which would leave the real question open.
    assert b"bad_signature" in r.content


def test_no_symmetric_algorithm_is_accepted():
    """The invariant behind the test above, stated where it cannot rot.

    PyJWT happens to refuse an HMAC verification against a PEM key, so the
    end-to-end test would keep passing even if `HS256` were added to the
    allowlist — right up until someone registered a non-PEM key. This is the
    assertion that actually holds the line.
    """
    symmetric = {"HS256", "HS384", "HS512", "none"}
    assert not symmetric & set(assertions.ALLOWED_ALGORITHMS)


def test_a_signature_from_the_wrong_key_is_refused():
    app, _priv, _ws = _setup()
    other_priv, _other_pub = _keypair()
    r = _exchange(_assert(other_priv))
    assert r.status_code == 401
    assert b"bad_signature" in r.content


def test_an_assertion_for_another_audience_is_refused():
    """Without this, an assertion the host minted for its own purposes — or for
    a different canopy — replays here."""
    app, priv, _ws = _setup()
    r = _exchange(_assert(priv, aud="https://someone-elses-canopy.test"))
    assert r.status_code == 401
    assert b"wrong_audience" in r.content


def test_an_expired_assertion_is_refused():
    app, priv, _ws = _setup()
    r = _exchange(_assert(priv, lifetime=-300))
    assert r.status_code == 401


def test_a_long_lived_assertion_is_refused_even_though_it_is_valid():
    """`exp` alone only proves the host chose an end. A host issuing year-long
    assertions has rebuilt the standing secret this exists to remove."""
    app, priv, _ws = _setup()
    r = _exchange(_assert(priv, lifetime=60 * 60 * 24 * 365))
    assert r.status_code == 400
    assert b"too_long" in r.content


def test_replaying_an_assertion_is_refused():
    """It travels through a page the visitor's own extensions can read."""
    app, priv, _ws = _setup()
    token = _assert(priv)

    assert _exchange(token).status_code == 200
    second = _exchange(token)

    assert second.status_code == 401
    assert b"replayed" in second.content


def test_an_assertion_with_no_jti_is_refused_rather_than_allowed_once():
    app, priv, _ws = _setup()
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = jwt.encode(
        {"iss": "connect-labs", "sub": "u", "aud": assertions.audience(),
         "iat": now, "exp": now + 60},
        priv, algorithm="EdDSA",
    )
    assert _exchange(token).status_code == 400


def test_an_app_with_no_registered_key_cannot_assert_anything():
    """Not a degraded mode — the absence of the capability. It must not fall
    back to anything weaker."""
    app, priv, _ws = _setup(with_key=False)
    r = _exchange(_assert(priv))
    assert r.status_code == 401
    assert b"no_key" in r.content


def test_an_unknown_or_revoked_issuer_is_refused_identically():
    """Distinguishing them tells a prober which of their guesses used to be
    real."""
    app, priv, _ws = _setup()
    unknown = _exchange(_assert(priv, iss="no-such-site"))

    app.revoked_at = dt.datetime.now(dt.timezone.utc)
    app.save(update_fields=["revoked_at"])
    revoked = _exchange(_assert(priv))

    assert unknown.status_code == revoked.status_code == 401
    assert b"unknown_issuer" in unknown.content and b"unknown_issuer" in revoked.content


def test_key_rotation_works_without_a_flag_day():
    """Both keys valid at once is the whole reason it is a list."""
    app, old_priv, _ws = _setup()
    new_priv, new_pub = _keypair()
    app.public_keys = [*app.public_keys, new_pub]
    app.save(update_fields=["public_keys"])

    assert _exchange(_assert(old_priv)).status_code == 200
    assert _exchange(_assert(new_priv)).status_code == 200


# --- the principal is not a user ----------------------------------------------


def _contact_client():
    app, priv, ws = _setup()
    token = _exchange(_assert(priv)).json()["token"]
    return Client(), {"HTTP_AUTHORIZATION": f"Bearer {token}"}, app, ws


def test_a_contact_sees_itself_and_the_agents_this_site_offers():
    c, hdr, _app, _ws = _contact_client()

    body = c.get("/api/contact/me", **hdr).json()

    assert body["identity"] == "connect-labs:u-42"
    assert [a["slug"] for a in body["agents"]] == ["echo"]


@pytest.mark.parametrize("path", [
    "/api/canopy-sessions/",
    "/api/insights/",
    "/api/agents/",
    "/api/workspaces/",
    "/api/embed/agents",
    "/api/contacts/",
])
def test_a_contact_token_reaches_nothing_else_in_canopy(path):
    """By construction, not by each view remembering: a contact token produces
    no `request.user`, and `/api/contact/` is the only prefix the login
    middleware opens to a request without one."""
    c, hdr, _app, _ws = _contact_client()

    r = c.get(path, **hdr)

    assert r.status_code in (401, 403), f"{path} answered {r.status_code} to a contact"


def test_the_contacts_admin_list_is_not_the_contact_prefix():
    """`/api/contacts/` is a tenant-admin surface and `/api/contact/` is the
    visitor one. The allowlist entry carries a trailing slash precisely so the
    first does not match the second — the same near-miss the middleware already
    documents for "/about"."""
    c, hdr, _app, ws = _contact_client()
    assert c.get("/api/contacts/", **hdr).status_code in (401, 403)


def test_a_blocked_contact_stops_at_the_door():
    """Refusing one person, without disconnecting the site. Enforced in the
    token lookup so it takes effect on the next request rather than whenever a
    view remembers."""
    from apps.contacts import services

    c, hdr, _app, _ws = _contact_client()
    assert c.get("/api/contact/me", **hdr).status_code == 200

    services.block(Contact.objects.get(), reason="abuse")

    assert c.get("/api/contact/me", **hdr).status_code == 401


def test_disconnecting_the_site_stops_its_contacts_immediately():
    from django.utils import timezone

    c, hdr, app, _ws = _contact_client()
    app.revoked_at = timezone.now()
    app.save(update_fields=["revoked_at"])

    assert c.get("/api/contact/me", **hdr).status_code == 401


def test_a_contact_token_is_not_accepted_where_a_user_token_would_be():
    """Two models rather than a nullable user on one, so nothing that asks for
    a user can be handed a contact by accident."""
    from apps.tokens.models import DelegatedToken

    c, hdr, _app, _ws = _contact_client()
    raw = hdr["HTTP_AUTHORIZATION"].removeprefix("Bearer ")

    assert DelegatedToken.lookup(raw) is None


def test_the_exchange_is_audited_including_its_refusals():
    from apps.tokens.models import EmbedAuditLog

    app, priv, _ws = _setup()
    _exchange(_assert(priv))
    _exchange(_assert(priv, aud="https://elsewhere.test"))

    rows = list(EmbedAuditLog.objects.order_by("created_at"))
    assert rows[0].ok and "app_signed" in rows[0].detail
    assert not rows[1].ok and rows[1].reason == "wrong_audience"


def test_a_key_registered_THROUGH_THE_PAGE_verifies_a_real_assertion():
    """End to end across the seam the other tests skip.

    Every test above sets `public_keys` on the model directly. The page stores
    a NORMALISED key (`_clean_keys` strips it), so "the verifier works" and
    "what an operator pastes works" are two different claims — and only this
    one is the claim that matters.
    """
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    priv, pub = _keypair()

    admin = Client()
    admin.force_login(owner)
    created = admin.post(
        "/api/workspaces/w1/connected-apps",
        data={"name": "connect-labs", "origins": ["https://labs.connect.dimagi.com"],
              "agents": ["echo"], "public_keys": [pub]},
        content_type="application/json",
    )
    assert created.status_code == 201, created.content

    r = _exchange(_assert(priv, sub="u-7"))

    assert r.status_code == 200, r.content
    assert Contact.objects.get().external_id == "u-7"


# --- budgets on the one unauthenticated endpoint ------------------------------


def test_the_minting_endpoint_is_rate_limited_per_client(settings):
    """It is the only UNAUTHENTICATED endpoint of the three that hand out
    identity — so the one that most needed a budget, and the one I shipped
    without one. Every call can cost a signature verification and a valid one
    writes two rows."""
    settings.CONTACT_TOKEN_CLIENT_LIMIT = 3
    app, priv, _ws = _setup()

    codes = [_exchange(_assert(priv, sub=f"u-{i}")).status_code for i in range(5)]

    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_the_client_budget_is_spent_before_anything_is_parsed(settings):
    """A budget checked after the expensive half has not saved the CPU the
    expensive half cost. Garbage counts against it too — that is the point."""
    settings.CONTACT_TOKEN_CLIENT_LIMIT = 2
    _app, priv, _ws = _setup()

    junk = [_exchange("not-even-a-token").status_code for _ in range(2)]
    then = _exchange(_assert(priv))

    assert junk == [400, 400]
    assert then.status_code == 429, "garbage did not count against the budget"


def test_a_single_site_is_bounded_separately(settings):
    """The real bound: how many visitors one site may vouch for. Caps the rows
    a leaked signing key can create."""
    settings.CONTACT_TOKEN_CLIENT_LIMIT = 100
    settings.CONTACT_TOKEN_ISSUER_LIMIT = 2
    app, priv, _ws = _setup()

    codes = [_exchange(_assert(priv, sub=f"u-{i}")).status_code for i in range(4)]

    assert codes[:2] == [200, 200]
    assert codes[2] == 429
    assert Contact.objects.count() == 2, "a throttled call still created a contact"


def test_a_throttled_attempt_is_audited(settings):
    from apps.tokens.models import EmbedAuditLog

    settings.CONTACT_TOKEN_ISSUER_LIMIT = 1
    _app, priv, _ws = _setup()
    _exchange(_assert(priv, sub="a"))
    _exchange(_assert(priv, sub="b"))

    throttled = [r for r in EmbedAuditLog.objects.all() if r.reason == "rate_limited"]
    assert throttled, "a refused mint left no trace"


def test_the_client_budget_is_generous_by_default():
    """A host's BACKEND calls this, so one site's legitimate traffic arrives
    from a handful of addresses. A tight per-client cap would throttle a busy
    partner rather than an attacker — the per-issuer limit is the real bound."""
    from apps.tokens import rate_limit

    assert rate_limit._over.__doc__  # the shared counter, not a third copy
    from django.conf import settings as s

    assert int(getattr(s, "CONTACT_TOKEN_CLIENT_LIMIT", 300)) >= 120


def test_the_audience_is_this_canopys_public_url_as_the_handoff_doc_says():
    """The doc tells a host `"aud": settings.CANOPY_BASE_URL`. The verifier used
    to fall back to an undefined `SITE_BASE_URL` and so to the literal "canopy",
    which refused every host that did what the doc said."""
    from django.test import override_settings

    with override_settings(EMBED_ASSERTION_AUDIENCE="",
                           CANOPY_PUBLIC_BASE_URL="https://labs.connect.dimagi.com/canopy/"):
        assert assertions.audience() == "https://labs.connect.dimagi.com/canopy"
    with override_settings(EMBED_ASSERTION_AUDIENCE="urn:canopy:x"):
        assert assertions.audience() == "urn:canopy:x"
