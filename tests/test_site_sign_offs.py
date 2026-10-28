"""Site sign-offs: a connected site asks a person to approve one action with their
passkey (`apps/host_grants/sign_offs.py`, `sign_offs_api.py`).

Driven over HTTP with requests signed the way the SDK signs them, and the approval is
checked with the SDK's own verifier against canopy's published client keys, so a pass
means a real site would believe it.
"""
from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.models import EmailAddress
from canopy_sdk import sign_offs as wire
from canopy_sdk.host import ClientKeyResolver
from canopy_sdk.keys import generate_private_key, public_pem
from canopy_sdk.stores import MemoryCache
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.attention.sources import gather
from apps.host_grants import client_identity
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import AppCredential, SiteSignOff
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SITE = "connect-labs"
RETURN = "https://labs.example.org/labs/mcp/tokens/approved/"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _person(username, email, ws=None, role=WorkspaceMembership.EDITOR):
    user = get_user_model().objects.create_user(username, email, "pw", first_name=username.title())
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    if ws is not None:
        WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)
    return user


@pytest.fixture()
def w():
    owner = _person("jo", "jo@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    member = _person("mo", "mo@dimagi.com", ws)
    outsider = _person("ox", "ox@elsewhere.org")
    ace_login = get_user_model().objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, user=ace_login)
    key = generate_private_key("EdDSA")
    app = AppCredential.create_credential(name=SITE, created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.allowed_frame_origins = ["https://labs.example.org"]
    app.save()
    return {"owner": owner, "member": member, "outsider": outsider, "ws": ws, "ace": ace,
            "ace_login": ace_login, "app": app, "key": key}


def _ask(w, *, subject, key=None, return_url=RETURN, site_ref="pending-1"):
    url = wire.create_url(client_identity.public_base())
    ask = wire.ask_payload(subject=subject, action_key="labs.mcp_token.full",
                           title="Create a full-access labs MCP token", site_ref=site_ref,
                           return_url=return_url)
    token = wire.request_assertion(SITE, key or w["key"], url, ask=ask)
    return Client().post(wire.PATH, HTTP_AUTHORIZATION=f"Bearer {token}")


def _asked(w, subject) -> dict:
    response = _ask(w, subject=subject)
    assert response.status_code == 201, response.content
    return response.json()


def _poll(w, sign_off_id, key=None):
    url = wire.status_url(client_identity.public_base(), sign_off_id)
    token = wire.request_assertion(SITE, key or w["key"], url)
    return Client().get(f"/api/sites/sign-offs/{sign_off_id}", HTTP_AUTHORIZATION=f"Bearer {token}")


def _browser(user, *, stepped_up=True) -> Client:
    client = Client()
    client.force_login(user)
    if stepped_up:
        grant_step_up(client)
    return client


def _canopy_keys():
    docs = {client_identity.client_id(): client_identity.client_metadata(),
            client_identity.jwks_uri(): client_identity.published_jwks()}
    return ClientKeyResolver(fetch_json=docs.__getitem__, cache=MemoryCache())


def _verify(jws, site_ref="pending-1"):
    return wire.verify_approval(jws, client_id=client_identity.client_id(), site_name=SITE,
                                client_keys=_canopy_keys(), expect_action="labs.mcp_token.full",
                                expect_site_ref=site_ref)


# --- the site asks ----------------------------------------------------------------


def test_a_members_own_request_is_theirs_and_lasts_fifteen_minutes(w):
    body = _asked(w, "Mo@Dimagi.com")
    assert body["approvers"] == "subject"
    assert body["approve_url"].endswith(f"/w/connect/sign-offs/{body['id']}")
    row = SiteSignOff.objects.get(pk=body["id"])
    assert row.subject == "mo@dimagi.com"
    assert timedelta(minutes=14) < row.expires_at - timezone.now() <= timedelta(minutes=15)


@override_settings(REQUIRE_AUTH=True)
def test_a_site_reaches_its_doors_with_no_login_and_a_person_does_not(w):
    # The site's request is its own credential, so the login gate must let it through
    # (test settings turn that gate off, which hid this until labs called it).
    body = _asked(w, "mo@dimagi.com")
    assert _poll(w, body["id"]).status_code == 200
    assert Client().get(f"/api/sign-offs/{body['id']}").status_code == 401


def test_an_agents_request_goes_to_its_admins_and_lasts_a_day(w):
    body = _asked(w, "ace@dimagi-ai.com")
    assert body["approvers"] == "agent_admins"
    row = SiteSignOff.objects.get(pk=body["id"])
    assert row.agent == w["ace"]
    assert row.expires_at - timezone.now() > timedelta(hours=23)


def test_someone_canopy_would_treat_as_a_contact_has_no_approver(w):
    for subject in ("ox@elsewhere.org", "nobody@example.org"):
        response = _ask(w, subject=subject)
        assert response.status_code == 422
        assert response.json()["detail"].startswith("no_approver")


def test_an_unverified_address_has_no_approver(w):
    user = get_user_model().objects.create_user("uv", "uv@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    assert _ask(w, subject="uv@dimagi.com").status_code == 422


def test_a_return_url_off_the_sites_origins_is_refused(w):
    response = _ask(w, subject="mo@dimagi.com", return_url="https://evil.example/steal")
    assert response.status_code == 422
    assert response.json()["detail"].startswith("bad_return_url")


def test_a_request_signed_by_another_key_is_refused(w):
    response = _ask(w, subject="mo@dimagi.com", key=generate_private_key("EdDSA"))
    assert response.status_code == 401
    assert "bad_signature" in response.json()["detail"]


def test_a_replayed_request_is_refused(w):
    url = wire.create_url(client_identity.public_base())
    token = wire.request_assertion(SITE, w["key"], url, ask=wire.ask_payload(
        subject="mo@dimagi.com", action_key="k", title="t", site_ref="r", return_url=RETURN))
    assert Client().post(wire.PATH, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 201
    again = Client().post(wire.PATH, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert again.status_code == 401
    assert "replayed" in again.json()["detail"]


# --- who approves -----------------------------------------------------------------


def test_the_person_approves_their_own_and_the_site_believes_it(w):
    body = _asked(w, "mo@dimagi.com")
    response = _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve")
    assert response.status_code == 200, response.content
    redirect = urlsplit(response.json()["redirect_url"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == RETURN
    query = parse_qs(redirect.query)
    assert query["id"] == [body["id"]]
    approval = _verify(query["sign_off"][0])
    assert approval.subject == "mo@dimagi.com"
    assert approval.approver_email == "mo@dimagi.com"


def test_nobody_else_sees_a_persons_request(w):
    body = _asked(w, "mo@dimagi.com")
    for user in (w["owner"], w["outsider"]):
        client = _browser(user)
        assert client.get(f"/api/sign-offs/{body['id']}").status_code == 404
        assert client.post(f"/api/sign-offs/{body['id']}/approve").status_code == 404


def test_approving_needs_a_passkey_step_up(w):
    body = _asked(w, "mo@dimagi.com")
    response = _browser(w["member"], stepped_up=False).post(f"/api/sign-offs/{body['id']}/approve")
    assert response.status_code == 403
    assert response.json()["error"] == "step_up_required"
    assert SiteSignOff.objects.get(pk=body["id"]).state == SiteSignOff.OPEN


def test_a_token_cannot_approve(w):
    from apps.tokens.models import PersonalToken

    body = _asked(w, "mo@dimagi.com")
    raw, _row = PersonalToken.create_for_user(user=w["member"], label="t")
    response = Client().post(f"/api/sign-offs/{body['id']}/approve", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 403
    assert SiteSignOff.objects.get(pk=body["id"]).state == SiteSignOff.OPEN


def test_an_agents_owner_and_admins_approve_but_not_the_agent_or_a_plain_member(w):
    body = _asked(w, "ace@dimagi-ai.com")
    assert _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve").status_code == 404
    assert _browser(w["ace_login"]).post(f"/api/sign-offs/{body['id']}/approve").status_code in (403, 404)
    AgentAdmin.objects.create(agent=w["ace"], user=w["member"], granted_by=w["owner"])
    assert _browser(w["member"]).get(f"/api/sign-offs/{body['id']}").json()["agent_slug"] == "ace"
    response = _browser(w["owner"]).post(f"/api/sign-offs/{body['id']}/approve")
    assert response.status_code == 200
    assert _verify(parse_qs(urlsplit(response.json()["redirect_url"]).query)["sign_off"][0]).subject \
        == "ace@dimagi-ai.com"


def test_a_person_who_left_the_workspace_can_no_longer_approve(w):
    body = _asked(w, "mo@dimagi.com")
    WorkspaceMembership.objects.filter(user=w["member"]).delete()
    assert _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve").status_code == 404


# --- closing, and the site reading the outcome ---------------------------------------


def test_the_site_polls_for_the_approval(w):
    body = _asked(w, "ace@dimagi-ai.com")
    assert _poll(w, body["id"]).json()["state"] == "open"
    _browser(w["owner"]).post(f"/api/sign-offs/{body['id']}/approve")
    polled = _poll(w, body["id"]).json()
    assert polled["state"] == "approved"
    first = _verify(polled["approval"])
    # Every copy is the same approval: spending its jti spends them all.
    assert _verify(_poll(w, body["id"]).json()["approval"]).jti == first.jti


def test_the_approval_stops_being_handed_out_after_ten_minutes(w):
    body = _asked(w, "ace@dimagi-ai.com")
    _browser(w["owner"]).post(f"/api/sign-offs/{body['id']}/approve")
    SiteSignOff.objects.filter(pk=body["id"]).update(decided_at=timezone.now() - timedelta(minutes=11))
    polled = _poll(w, body["id"]).json()
    assert polled["state"] == "approved"
    assert polled["approval"] is None


def test_another_site_cannot_read_a_request(w):
    body = _asked(w, "mo@dimagi.com")
    other_key = generate_private_key("EdDSA")
    other = AppCredential.create_credential(name="other-site", created_by=w["owner"], workspace=w["ws"])
    other.public_keys = [public_pem(other_key)]
    other.save()
    url = wire.status_url(client_identity.public_base(), body["id"])
    token = wire.request_assertion("other-site", other_key, url)
    response = Client().get(f"/api/sites/sign-offs/{body['id']}", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 404


def test_declining_closes_it(w):
    body = _asked(w, "mo@dimagi.com")
    client = _browser(w["member"], stepped_up=False)
    response = client.post(f"/api/sign-offs/{body['id']}/decline")
    assert response.status_code == 200
    assert parse_qs(urlsplit(response.json()["redirect_url"]).query) == {"declined": [body["id"]]}
    assert _poll(w, body["id"]).json() == {**_poll(w, body["id"]).json(), "state": "declined", "approval": None}
    assert _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve").status_code == 409


def test_an_expired_request_cannot_be_approved(w):
    body = _asked(w, "mo@dimagi.com")
    SiteSignOff.objects.filter(pk=body["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    response = _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve")
    assert response.status_code == 409
    assert _poll(w, body["id"]).json()["state"] == "expired"


def test_approving_is_recorded_as_a_setting_change(w):
    from apps.events.models import SettingChange

    body = _asked(w, "mo@dimagi.com")
    _browser(w["member"]).post(f"/api/sign-offs/{body['id']}/approve")
    change = SettingChange.objects.order_by("-pk").first()
    assert change is not None
    assert change.actor_id == w["member"].pk
    assert json.dumps(change.changes).find(body["id"]) != -1


# --- Home --------------------------------------------------------------------------


def test_an_open_request_waits_on_its_approvers_home(w):
    body = _asked(w, "ace@dimagi-ai.com")

    class Req:
        user = w["owner"]

    rows = [r for r in gather(Req(), slugs={"connect"}) if r.kind == "sign_off"]
    assert [r.target for r in rows] == [{"sign_off": body["id"]}]
    Req.user = w["member"]
    assert [r for r in gather(Req(), slugs={"connect"}) if r.kind == "sign_off"] == []


# --- deciding on the site's own page, in canopy's frame (canopy.signOff) ---------------


def _frame_token(w, user, app=None) -> str:
    from apps.tokens.models import DelegatedToken

    raw, _ = DelegatedToken.issue(app=app or w["app"], user=user, ttl_seconds=600)
    return raw


def _framed(raw, path, *, post=False, body=None):
    client = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    url = f"/api/embed/sign-offs/{path}"
    if not post:
        return client.get(url)
    return client.post(url, data=json.dumps(body or {}), content_type="application/json")


def test_the_person_approves_in_the_sites_frame_with_their_passkey(w):
    from tests.fixtures.passkey_confirm import give_passkey

    body = _asked(w, "mo@dimagi.com")
    auth = give_passkey(w["member"])
    raw = _frame_token(w, w["member"])

    shown = _framed(raw, body["id"])
    assert shown.status_code == 200 and shown.json()["action_title"] == "Create a full-access labs MCP token"

    begun = _framed(raw, f"{body['id']}/approve/begin", post=True)
    assert begun.status_code == 200, begun.content
    done = _framed(raw, f"{body['id']}/approve/finish", post=True,
                   body={"credential": auth.get(begun.json()["options"])})
    assert done.status_code == 200, done.content
    assert done.json()["state"] == "approved"
    # The frame is told the state; the site reads the approval itself, and believes it.
    assert "approval" not in done.json() and "redirect_url" not in done.json()
    approval = _poll(w, body["id"]).json()["approval"]
    assert _verify(approval).approver_email == "mo@dimagi.com"


def test_the_frame_cannot_approve_without_the_passkey(w):
    from tests.fixtures.passkey_confirm import give_passkey

    body = _asked(w, "mo@dimagi.com")
    give_passkey(w["member"])
    raw = _frame_token(w, w["member"])
    _framed(raw, f"{body['id']}/approve/begin", post=True)
    forged = _framed(raw, f"{body['id']}/approve/finish", post=True,
                     body={"credential": {"id": "nope", "rawId": "nope", "type": "public-key", "response": {}}})
    assert forged.status_code == 400
    assert SiteSignOff.objects.get(pk=body["id"]).state == SiteSignOff.OPEN


def test_the_frame_declines(w):
    body = _asked(w, "mo@dimagi.com")
    raw = _frame_token(w, w["member"])
    assert _framed(raw, f"{body['id']}/decline", post=True).json()["state"] == "declined"
    assert _poll(w, body["id"]).json()["state"] == "declined"


def test_only_the_person_and_only_the_asking_sites_frame_see_it(w):
    body = _asked(w, "mo@dimagi.com")
    # Someone else's token on the same site's page.
    assert _framed(_frame_token(w, w["owner"]), body["id"]).status_code == 404
    # The right person, through another site's frame.
    other = AppCredential.create_credential(name="other-site", created_by=w["owner"], workspace=w["ws"])
    assert _framed(_frame_token(w, w["member"], app=other), body["id"]).status_code == 404
    assert _framed(_frame_token(w, w["member"], app=other), f"{body['id']}/decline", post=True).status_code == 404


def test_a_personal_token_is_not_a_frame(w):
    from apps.tokens.models import PersonalToken

    body = _asked(w, "mo@dimagi.com")
    raw, _row = PersonalToken.create_for_user(user=w["member"], label="script")
    assert _framed(raw, body["id"]).status_code == 403
    assert _framed(raw, f"{body['id']}/decline", post=True).status_code == 403
    assert SiteSignOff.objects.get(pk=body["id"]).state == SiteSignOff.OPEN


def test_a_closed_request_cannot_be_begun_in_the_frame(w):
    body = _asked(w, "mo@dimagi.com")
    raw = _frame_token(w, w["member"])
    _framed(raw, f"{body['id']}/decline", post=True)
    assert _framed(raw, f"{body['id']}/approve/begin", post=True).status_code == 409


def test_the_sign_off_shell_is_framed_only_by_the_site(w, monkeypatch):
    from apps.embedding import views_embed

    # CI's backend job builds no frontend; the shell's body is what is under test.
    monkeypatch.setattr(views_embed, "_embed_assets", lambda: ("assets/embed-TEST.js", []))
    body = _asked(w, "mo@dimagi.com")
    response = Client().get(f"/embed/sign-off?app={SITE}&sign_off={body['id']}")
    assert response.status_code == 200
    assert response["Content-Security-Policy"] == "frame-ancestors https://labs.example.org"
    assert f'"sign_off": "{body["id"]}"' in response.content.decode()
    assert Client().get(f"/embed/sign-off?app={SITE}&sign_off=../etc").status_code == 404
