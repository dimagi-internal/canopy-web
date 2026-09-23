"""Keys move by publication, not by copy-paste.

Two halves, and the risks are different in each. INBOUND, canopy fetches a URL
an operator supplied — so most of this file is about that request refusing to
become a way to read canopy's own network. OUTBOUND, canopy publishes its own
keys — so the question is whether a host can still verify across a rotation.
"""

import json
from unittest import mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.core.cache import cache
from django.test import Client, override_settings

from apps.tokens import jwks, onbehalf

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(encoding=serialization.Encoding.PEM,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, pub_pem


def _jwk(pem_private: str, kid: str | None = None) -> dict:
    from jwt.algorithms import OKPAlgorithm

    key = serialization.load_pem_private_key(pem_private.encode(), password=None)
    d = OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
    d.update({"use": "sig", "alg": "EdDSA"})
    if kid:
        d["kid"] = kid
    return d


class _Resp:
    def __init__(self, body: bytes, status=200):
        self.status_code = status
        self.raw = mock.Mock()
        self.raw.read.return_value = body


def _serving(doc: dict, status=200):
    return mock.patch("apps.tokens.jwks.requests.get",
                      return_value=_Resp(json.dumps(doc).encode(), status))


def _public_dns():
    """Every hostname resolves somewhere public, unless a test says otherwise."""
    return mock.patch("apps.tokens.jwks.socket.getaddrinfo",
                      return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])


# --- the outbound request must not become a way into canopy's network --------


@pytest.mark.parametrize("addr,what", [
    ("169.254.169.254", "the cloud metadata service"),
    ("127.0.0.1", "canopy itself"),
    ("10.0.1.7", "something else in the VPC"),
    ("192.168.1.10", "a private network"),
])
def test_a_url_resolving_into_private_space_is_refused(addr, what):
    """Canopy makes this request from inside its own VPC. Without this check a
    'JWKS URL' is a way to read whatever lives there — starting with the
    instance's credentials."""
    with mock.patch("apps.tokens.jwks.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", (addr, 443))]):
        with pytest.raises(jwks.JwksError) as exc:
            jwks.validate_url("https://keys.partner.test/jwks.json")
    assert addr in str(exc.value), what


def test_plain_http_is_refused():
    """A key fetched in the clear is a key an intermediary can replace, which
    makes every signature it verifies meaningless."""
    with pytest.raises(jwks.JwksError) as exc:
        jwks.validate_url("http://keys.partner.test/jwks.json")
    assert "https" in str(exc.value)


def test_the_address_is_checked_again_at_fetch_not_only_at_save():
    """A name that was public when it was typed can point anywhere later."""
    priv, _pub = _keypair()
    with _public_dns():
        url = jwks.validate_url("https://keys.partner.test/jwks.json")
    with mock.patch("apps.tokens.jwks.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("169.254.169.254", 443))]):
        with pytest.raises(jwks.JwksError):
            jwks.keys_for(url)


def test_a_redirect_is_not_followed():
    """The cheapest way around a host check is to be sent somewhere else."""
    with _public_dns(), mock.patch("apps.tokens.jwks.requests.get") as get:
        get.return_value = _Resp(b"", status=302)
        with pytest.raises(jwks.JwksError):
            jwks.keys_for("https://keys.partner.test/jwks.json")
    assert get.call_args.kwargs["allow_redirects"] is False


def test_an_oversized_body_is_refused():
    big = {"keys": [{"kty": "OKP", "padding": "x" * (jwks.MAX_BYTES + 10)}]}
    with _public_dns(), _serving(big):
        with pytest.raises(jwks.JwksError) as exc:
            jwks.keys_for("https://keys.partner.test/jwks.json")
    assert "bytes" in str(exc.value)


# --- following a rotation ------------------------------------------------------


def test_a_key_published_by_the_site_verifies_its_assertion():
    priv, _pub = _keypair()
    with _public_dns(), _serving({"keys": [_jwk(priv, kid="k1")]}):
        keys = jwks.keys_for("https://keys.partner.test/jwks.json", kid="k1")
    token = jwt.encode({"hello": "world"}, priv, algorithm="EdDSA", headers={"kid": "k1"})
    assert jwt.decode(token, keys[0], algorithms=["EdDSA"]) == {"hello": "world"}


def test_an_unknown_kid_forces_one_refetch_so_a_rotation_is_picked_up_at_once():
    """The site published a new key a minute ago. Waiting out the cache would
    mean refusing valid assertions until it expired."""
    old, _ = _keypair()
    new, _ = _keypair()
    url = "https://keys.partner.test/jwks.json"
    with _public_dns(), _serving({"keys": [_jwk(old, kid="old")]}):
        jwks.keys_for(url)  # warm the cache with only the old key

    with _public_dns(), _serving({"keys": [_jwk(old, kid="old"), _jwk(new, kid="new")]}) as get:
        keys = jwks.keys_for(url, kid="new")
    assert get.call_count == 1 and len(keys) == 1


def test_a_made_up_kid_does_not_buy_a_fetch_per_call():
    """Otherwise an unauthenticated caller can make canopy hammer a third party
    by naming a different kid each time."""
    old, _ = _keypair()
    url = "https://keys.partner.test/jwks.json"
    with _public_dns(), _serving({"keys": [_jwk(old, kid="old")]}):
        jwks.keys_for(url)
    with _public_dns(), _serving({"keys": [_jwk(old, kid="old")]}) as get:
        for _ in range(5):
            assert jwks.keys_for(url, kid="nope") == []
    assert get.call_count == 1, "one refetch, then the miss is remembered"


def test_a_site_whose_server_is_down_keeps_working_on_the_last_good_copy():
    """A key does not stop being valid because a web server had a bad minute.

    The fresh entry is expired deliberately, because otherwise this passes
    without any fallback at all — the cache answers and nothing is fetched,
    which is how a dead fallback path hides.
    """
    import requests as _requests

    priv, _ = _keypair()
    url = "https://keys.partner.test/jwks.json"
    with _public_dns(), _serving({"keys": [_jwk(priv, kid="k1")]}):
        jwks.keys_for(url)
    cache.delete(jwks._cache_key(url))  # the 10-minute copy has aged out

    with _public_dns(), mock.patch(
        "apps.tokens.jwks.requests.get",
        side_effect=_requests.ConnectionError("connection refused"),
    ) as get:
        assert len(jwks.keys_for(url)) == 1
    assert get.called, "it really did try, and really did fall back"


def test_with_no_copy_at_all_it_refuses_rather_than_verifying_against_nothing():
    import requests as _requests

    with _public_dns(), mock.patch(
        "apps.tokens.jwks.requests.get",
        side_effect=_requests.ConnectionError("connection refused"),
    ):
        with pytest.raises(jwks.JwksError):
            jwks.keys_for("https://keys.partner.test/jwks.json")


# --- canopy's own keys ----------------------------------------------------------


def _key_settings(active: str, retired: list[str] | None = None):
    return dict(ONBEHALF_SIGNING_KEY=active,
                ONBEHALF_RETIRED_PUBLIC_KEYS="|".join(retired or []),
                EMBED_ASSERTION_AUDIENCE="https://canopy.test")


def test_the_kid_is_derived_from_the_key_so_two_keys_are_distinguishable():
    """A fixed `kid` was the bug: both keys claimed the same name, so a host had
    nothing to select on and rotation could not work."""
    a, _ = _keypair()
    b, _ = _keypair()
    with override_settings(**_key_settings(a)):
        kid_a = onbehalf.active_kid()
    with override_settings(**_key_settings(b)):
        kid_b = onbehalf.active_kid()
    assert kid_a and kid_b and kid_a != kid_b


def test_the_signature_carries_that_kid_so_a_host_can_pick_the_right_key():
    from apps.agents.models import Agent
    from apps.contacts.models import Contact
    from apps.harness.models import Turn
    from apps.tokens.models import AppCredential
    from apps.workspaces.models import Workspace, WorkspaceMembership
    from django.contrib.auth.models import User

    a, _ = _keypair()
    with override_settings(**_key_settings(a)):
        owner = User.objects.create_user("o", "o@dimagi.com", "pw")
        ws = Workspace.objects.create(slug="w1", display_name="W", created_by=owner)
        WorkspaceMembership.objects.create(user=owner, workspace=ws,
                                           role=WorkspaceMembership.OWNER)
        agent = Agent.objects.create(slug="echo", name="E", workspace=ws)
        _raw, app = AppCredential.create_credential(name="partner", created_by=owner)
        contact = Contact.objects.create(workspace=ws, app=app, external_id="u-1")
        turn = Turn.objects.create(agent=agent, prompt="hi", initiator_contact=contact,
                                   initiator_kind="contact", capability="ask")
        out = onbehalf.mint(turn, agent_slug="echo")
        assert jwt.get_unverified_header(out["assertion"])["kid"] == onbehalf.active_kid()


def test_a_rotation_publishes_both_keys_so_nothing_in_flight_breaks():
    """Switching signer without this is an outage you schedule: every assertion
    already issued, and every host with a warm cache, verifies against the key
    canopy just stopped publishing."""
    old_priv, old_pub = _keypair()
    new_priv, _ = _keypair()

    with override_settings(**_key_settings(old_priv)):
        old_kid = onbehalf.active_kid()
    with override_settings(**_key_settings(new_priv, retired=[old_pub])):
        served = Client().get("/api/tokens/on-behalf-of/jwks").json()["keys"]
        assert onbehalf.active_kid() == served[0]["kid"]
    assert {k["kid"] for k in served} == {served[0]["kid"], old_kid}
    assert all("d" not in k for k in served), "public halves only"


def test_a_malformed_retired_key_never_takes_the_live_one_down():
    new_priv, _ = _keypair()
    with override_settings(**_key_settings(new_priv, retired=["not a key at all"])):
        served = Client().get("/api/tokens/on-behalf-of/jwks").json()["keys"]
        assert len(served) == 1 and served[0]["kid"] == onbehalf.active_kid()
