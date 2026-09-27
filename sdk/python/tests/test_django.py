"""The Django integration: settings → views, stores, the gate, and the panel."""
from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings

from canopy_sdk import contract
from canopy_sdk.django import conf, views
from canopy_sdk.django.asgi import dpop_gate
from canopy_sdk.django.models import DelegatedToken, SeenJti
from canopy_sdk.django.stores import DjangoJtiStore
from canopy_sdk.keys import private_pem
from canopy_sdk.stores import Replayed

from .helpers import CANOPY, CLIENT_ID, ISSUER, RESOURCE, World, call_asgi, echo_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return World()


@pytest.fixture
def user():
    return get_user_model().objects.create_user("gillian", "g@example.org", "pw")


@pytest.fixture
def host(world, user):
    settings = {
        "SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY, "APP_NAME": "connect-labs",
        "AGENT_SLUG": "ace", "CLIENT_ID": CLIENT_ID, "ISSUER": ISSUER, "RESOURCE": RESOURCE,
        "TOKEN_ENDPOINT": "http://testserver/o/token/",
        "SCOPE_TOOLS": {"marketplace:read": ["marketplace_orgs_get"]},
        "PAGE_SCOPES": {"network": ["marketplace:read"]},
    }
    cache.clear()
    with override_settings(CANOPY_HOST=settings), \
            mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        yield world
    cache.clear()


def _grant(world, user, **extra):
    data = world.form(assertion=world.id_jag(sub=str(user.pk)), **extra)
    proof = world.proof(htu="http://testserver/o/token/")
    return Client().post("/o/token/", data, headers={"DPoP": proof})


def test_the_migrations_are_in_sync():
    out = StringIO()
    call_command("makemigrations", "canopy_host", "--check", "--dry-run", stdout=out)


def test_the_token_view_redeems_and_stores_only_a_hash(host, user):
    response = _grant(host, user)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["token_type"] == "DPoP" and response["Cache-Control"] == "no-store"
    row = DelegatedToken.objects.get()
    assert row.subject == str(user.pk) and row.token_checksum == contract.token_checksum(body["access_token"])
    assert SeenJti.objects.count() == 3


def test_other_grant_types_still_reach_the_existing_token_view(host):
    response = Client().post("/o/token/", {"grant_type": "client_credentials"})
    assert response.json() == {"error": "unsupported_grant_type", "from": "toolkit"}


def test_an_inactive_user_gets_no_token(host, user):
    user.is_active = False
    user.save()
    response = _grant(host, user)
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


def test_a_replayed_grant_is_refused_through_the_database(host, user):
    token = host.id_jag(sub=str(user.pk))
    for expected in (200, 400):
        response = Client().post("/o/token/", host.form(assertion=token),
                                 headers={"DPoP": host.proof(htu="http://testserver/o/token/")})
        assert response.status_code == expected


def test_an_unconfigured_host_refuses_the_grant(world):
    with override_settings(CANOPY_HOST={}):
        response = Client().post("/canopy/token/", world.form(), headers={"DPoP": world.proof()})
    assert response.status_code == 400 and response.json()["error"] == "unsupported_grant_type"


def test_the_jwks_view_publishes_public_keys_only(host):
    keys = Client().get("/canopy/jwks.json").json()["keys"]
    assert [k["kid"] for k in keys] == [host.config.kid] and "d" not in keys[0]
    with override_settings(CANOPY_HOST={}):
        assert Client().get("/canopy/jwks.json").status_code == 503


def test_the_django_jti_store_is_all_or_nothing():
    store = DjangoJtiStore()
    store.consume([("a", "1", 2_000_000_000)])
    with pytest.raises(Replayed):
        store.consume([("b", "2", 2_000_000_000), ("a", "1", 2_000_000_000)])
    assert not SeenJti.objects.filter(key__startswith="b:").exists()


# --- the gate ---------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_gate_resolves_a_token_issued_through_the_view(host, user):
    access = _grant(host, user).json()["access_token"]
    seen = []
    status, _, _ = call_asgi(dpop_gate(echo_app(seen), require_principal=True), "POST", "/mcp/",
                             headers={"Authorization": f"DPoP {access}",
                                      "DPoP": host.proof(htu=RESOURCE, access_token=access)})
    assert status == 200 and seen[0]["principal"].subject == str(user.pk)
    assert seen[0]["principal"].allowed_tools == {"marketplace_orgs_get"}


# --- the panel ---------------------------------------------------------------------------------


def _rendered_token(body: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    start = body.index('tokenUrl: "') + len('tokenUrl: "')
    url = body[start:body.index('"', start)].encode().decode("unicode_escape")
    query = parse_qs(urlsplit(url).query)
    assert "page" in query, "the registered page carries its token to the widget"
    return query["page"][0]


def test_a_registered_page_renders_a_token_that_yields_its_scopes(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    assert "/embed/widget.js" in body and "canopy-page-state" in body
    assert conf.page_tokens().scopes(_rendered_token(body), user.pk) == ("marketplace:read",)


def test_an_unregistered_page_renders_the_panel_without_a_token(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/elsewhere/").content.decode()
    assert "/embed/widget.js" in body
    assert "page" not in body[body.index("tokenUrl"):].split(",")[0]


def test_nothing_renders_when_unconfigured(user):
    client = Client()
    client.force_login(user)
    assert "widget.js" not in client.get("/marketplace/network/").content.decode()


def test_the_panel_token_endpoint_turns_the_page_token_into_scopes(host, user):
    client = Client()
    client.force_login(user)
    token = _rendered_token(client.get("/marketplace/network/").content.decode())
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t", "expires_at": ""}) as mint:
        response = client.post(f"/canopy/panel-token/?page={token}")
    assert response.json() == {"token": "t", "expires_at": ""}
    payload = mint.call_args.args[1]
    assert payload["agent_slug"] == "ace" and payload["id_jag"]
    import jwt

    assert jwt.decode(payload["assertion"], options={"verify_signature": False})["sub"] == str(user.pk)


def test_scopes_in_the_body_or_query_are_ignored(host, user):
    client = Client()
    client.force_login(user)
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t", "expires_at": ""}) as mint:
        client.post("/canopy/panel-token/?scope=admin&page=network",
                    data=json.dumps({"scope": "marketplace:read", "sub": "someone"}),
                    content_type="application/json")
    assert "id_jag" not in mint.call_args.args[1]


def test_a_failed_mint_is_a_502_without_canopys_words(host, user):
    from canopy_sdk.host import MintFailed

    client = Client()
    client.force_login(user)
    with mock.patch.object(views, "mint_contact_token", side_effect=MintFailed("replayed: used")):
        response = client.post("/canopy/panel-token/")
    assert response.status_code == 502 and "replayed" not in response.content.decode()


def test_the_panel_token_endpoint_needs_a_session(host):
    assert Client().post("/canopy/panel-token/").status_code in (302, 401, 403)
