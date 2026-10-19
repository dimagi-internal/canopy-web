"""Minting a gog token from a browser.

The shape assertions here are not designed — they are transcribed from the tokens
that actually work. On 2026-09-06 the live items op://Agent-Echo/gog-token and
op://Agent-Eva/gog-token both read:

    services: [docs, drive, forms, gmail, sheets]
    scopes:   [email, .../documents, .../drive, .../forms.body,
               .../forms.responses.readonly, .../gmail.modify,
               .../gmail.settings.basic, .../gmail.settings.sharing,
               .../spreadsheets, .../userinfo.email, openid]

Notably NO calendar and NO slides — a first draft of the mint requested both on
the assumption they were wanted, and a token unlike every other token in the
fleet is a new thing to debug rather than a replacement for the old one.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apps.agents import google_oauth as g

# Exactly what echo's and eva's working tokens carry.
LIVE_FLEET_SCOPES = [
    "email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


# ace/config/agent.json, verbatim — the DECLARATION of what its mailbox must be
# able to do, alongside the note that two of them are missing from the fleet
# default. This is the authority on scope, not the incumbent tokens.
ACE_DECLARED_SERVICES = ["gmail", "calendar", "drive", "docs", "slides", "sheets", "forms"]


def test_the_request_covers_every_service_an_agent_declares():
    """The correction that matters. A first version requested exactly what the
    live tokens carry — but those record what was MINTED, not what is needed, and
    ace's config says the fleet default is insufficient for it. Minting from that
    set hands ACE a token that authorizes cleanly and fails its first slides
    call: the silent breakage this feature exists to end, reintroduced by the
    feature itself."""
    granted = g.services_for(g.FLEET_SCOPES)
    assert set(ACE_DECLARED_SERVICES) <= set(granted), (
        f"declared but not requested: {sorted(set(ACE_DECLARED_SERVICES) - set(granted))}"
    )


def test_the_request_is_a_superset_of_what_the_live_fleet_already_has():
    """A re-mint must not take capability AWAY from a working mailbox."""
    assert set(LIVE_FLEET_SCOPES) <= set(g.FLEET_SCOPES)


def test_calendar_and_slides_are_asked_for_because_an_agent_declares_them():
    assert "https://www.googleapis.com/auth/calendar" in g.FLEET_SCOPES
    assert "https://www.googleapis.com/auth/presentations" in g.FLEET_SCOPES


def test_services_are_derived_from_the_granted_scopes_not_declared():
    assert g.services_for(LIVE_FLEET_SCOPES) == ["docs", "drive", "forms", "gmail", "sheets"]


def test_a_scope_the_user_unchecked_drops_its_service():
    """A consent screen lets scopes be declined. Claiming a service that was not
    granted fails at call time, months later, instead of at mint time."""
    without_forms = [s for s in LIVE_FLEET_SCOPES if "/forms" not in s]
    assert "forms" not in g.services_for(without_forms)
    assert "gmail" in g.services_for(without_forms)


def test_sign_in_only_scopes_contribute_no_service():
    assert g.services_for(["openid", "email", "https://www.googleapis.com/auth/userinfo.email"]) == []


def test_the_token_has_exactly_the_five_keys_gog_imports():
    tok = g.build_gog_token(
        email="ace@dimagi-ai.com", refresh_token="1//rt", granted_scopes=LIVE_FLEET_SCOPES
    )
    assert set(tok) == {"email", "client", "services", "scopes", "created_at", "refresh_token"}


def test_the_token_matches_a_live_one_field_for_field():
    tok = g.build_gog_token(
        email="eva@dimagi-ai.com",
        refresh_token="1//rt",
        granted_scopes=LIVE_FLEET_SCOPES,
        now=datetime(2026, 7, 24, 4, 9, 54, tzinfo=timezone.utc),
    )
    assert tok["email"] == "eva@dimagi-ai.com"
    assert tok["services"] == ["docs", "drive", "forms", "gmail", "sheets"]
    assert tok["scopes"] == sorted(LIVE_FLEET_SCOPES)
    assert tok["created_at"] == "2026-07-24T04:09:54Z"


def test_the_token_declares_the_client_that_minted_it():
    """The load-bearing invariant of the whole feature, and the one a wrong
    inference broke on 2026-09-05: a refresh token works ONLY with the client it
    was minted for. The browser flow can only run on the Web client, so the token
    must say so — writing `canopy` here would name a Desktop client that never saw
    this token and cannot refresh it."""
    tok = g.build_gog_token(email="a@b.c", refresh_token="x", granted_scopes=["openid"])
    assert tok["client"] == "canopy-web"
    assert tok["client"] != "canopy"


def test_created_at_is_utc_with_the_trailing_z():
    """gog's importer reads this literally; a naive local timestamp would silently
    date a token hours off."""
    tok = g.build_gog_token(
        email="a@b.c", refresh_token="x", granted_scopes=["openid"],
        now=datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert tok["created_at"].endswith("Z") and "+" not in tok["created_at"]


# --- the authorize URL ----------------------------------------------------------

def test_the_authorize_url_forces_a_refresh_token_to_be_reissued():
    """Without prompt=consent a RE-mint for an already-consented account returns
    no refresh_token at all, and the result imports cleanly and then cannot
    refresh — a provisioned-looking dead mailbox, which is the exact failure this
    whole feature was built to surface."""
    url = g.authorize_url(client_id="cid", state="st")
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_one_redirect_uri_serves_every_agent():
    """A per-agent URI would need a Google console edit per new agent."""
    import re

    assert re.search(r"/api/oauth/google/callback$", g.callback_url())
    a = g.authorize_url(client_id="cid", state=g.sign_state(agent_slug="ace", user_pk=1))
    b = g.authorize_url(client_id="cid", state=g.sign_state(agent_slug="echo", user_pk=1))
    from urllib.parse import parse_qs, urlparse

    assert parse_qs(urlparse(a).query)["redirect_uri"] == parse_qs(urlparse(b).query)["redirect_uri"]


def test_state_round_trips_and_refuses_tampering():
    state = g.sign_state(agent_slug="ace", user_pk=7)
    assert g.unsign_state(state) == {"agent": "ace", "user": "7"}

    from django.core import signing

    with pytest.raises(signing.BadSignature):
        g.unsign_state(state[:-3] + "aaa")


def test_the_agent_travels_in_signed_state_not_a_query_param():
    """If the agent were a plain parameter, anyone who could get a user to follow
    a link could attach a mailbox to an agent of their choosing."""
    url = g.authorize_url(client_id="cid", state=g.sign_state(agent_slug="ace", user_pk=1))
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    assert "agent" not in q
    assert q["state"][0] != "ace"


def test_the_id_token_payload_yields_the_mailbox():
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"email": "ace@dimagi-ai.com"}).encode())
    jwt = b"h." + payload.rstrip(b"=") + b".sig"
    assert g.email_from_id_token(jwt.decode()) == "ace@dimagi-ai.com"


# --- the flow through the routes -------------------------------------------------

from django.contrib.auth import get_user_model  # noqa: E402

from apps.agents.models import Agent, AgentCredential  # noqa: E402
from apps.workspaces.models import Workspace, WorkspaceMembership  # noqa: E402

CALLBACK = "/api/oauth/google/callback"


@pytest.fixture
def fleet(client, settings):
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {"APP": {"client_id": "cid.apps.googleusercontent.com", "secret": "GOCSPX-x"}}
    }
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, runtime_secrets=["gog-token"])
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj, "ws": ws}


def _google_response(*, refresh="1//refresh", scope=None):
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"email": "ace@dimagi-ai.com"}).encode())
    out = {
        "id_token": "h." + payload.rstrip(b"=").decode() + ".sig",
        "scope": " ".join(scope if scope is not None else LIVE_FLEET_SCOPES),
    }
    if refresh:
        out["refresh_token"] = refresh
    return out


@pytest.mark.django_db
def test_authorize_hands_back_a_url_rather_than_redirecting(fleet):
    """A 302 out of an XHR is invisible — the fetch resolves on Google's HTML and
    nothing appears on screen. Only a top-level navigation can show consent."""
    res = fleet["client"].get("/api/agents/ace/google/authorize")
    assert res.status_code == 200
    url = res.json()["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "login_hint=ace%40dimagi-ai.com" in url


@pytest.mark.django_db
def test_the_callback_stores_a_gog_importable_token(fleet, monkeypatch):
    import json

    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response())
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    res = fleet["client"].get(CALLBACK, {"code": "c", "state": state})
    assert res.status_code == 302 and res["Location"].endswith("?google=ok#credentials")

    stored = json.loads(_stored_token(fleet["agent"]))
    assert stored["email"] == "ace@dimagi-ai.com"
    assert stored["client"] == "canopy-web"
    assert stored["refresh_token"] == "1//refresh"
    assert stored["services"] == ["docs", "drive", "forms", "gmail", "sheets"]


def _stored_token(agent) -> str:
    from apps.agents import services as svc

    return svc.resolve_agent_credentials(agent)["gog-token"]


@pytest.mark.django_db
def test_it_lands_in_the_same_slot_a_hand_minted_token_does(fleet, monkeypatch):
    """`gog-token` is the ref every runtime.yaml and every vault already names.
    A second slot would give one mailbox two sources of truth."""
    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response())
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    fleet["client"].get(CALLBACK, {"code": "c", "state": state})
    assert AgentCredential.objects.filter(agent=fleet["agent"], name="gog-token").exists()


@pytest.mark.django_db
def test_a_response_without_a_refresh_token_is_refused_not_stored(fleet, monkeypatch):
    """Google withholds it for an already-consented account. Storing what came
    back yields a token that imports cleanly and can never refresh — a mailbox
    that LOOKS provisioned, which is the exact failure mode this feature exists
    to end."""
    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response(refresh=""))
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    res = fleet["client"].get(CALLBACK, {"code": "c", "state": state})
    assert res["Location"].endswith("?google=no-refresh-token#credentials")
    assert not AgentCredential.objects.filter(agent=fleet["agent"], name="gog-token").exists()


@pytest.mark.django_db
def test_the_callback_refuses_a_state_signed_for_someone_else(fleet):
    """Otherwise a link followed by the wrong person attaches THEIR mailbox to
    this agent."""
    other = get_user_model().objects.create_user(username="m", email="m@dimagi.com")
    state = g.sign_state(agent_slug="ace", user_pk=other.pk)
    assert fleet["client"].get(CALLBACK, {"code": "c", "state": state}).status_code == 403


@pytest.mark.django_db
def test_the_callback_refuses_unsigned_state(fleet):
    assert fleet["client"].get(CALLBACK, {"code": "c", "state": "ace"}).status_code == 400


@pytest.mark.django_db
def test_a_denied_consent_stores_nothing_and_says_so(fleet):
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    res = fleet["client"].get(CALLBACK, {"error": "access_denied", "state": state})
    assert res["Location"].endswith("?google=denied#credentials")
    assert not AgentCredential.objects.filter(agent=fleet["agent"], name="gog-token").exists()


@pytest.mark.django_db
def test_the_minted_value_never_comes_back_through_the_browser(fleet, monkeypatch):
    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response())
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    fleet["client"].get(CALLBACK, {"code": "c", "state": state})
    body = fleet["client"].get("/api/agents/ace/credentials/status").content.decode()
    assert "1//refresh" not in body
    assert '"name": "gog-token"' in body or "gog-token" in body


@pytest.mark.django_db
def test_a_non_member_cannot_start_a_mint(client):
    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    ws = Workspace.objects.create(slug="private", display_name="P", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="S", workspace=ws, runtime_secrets=["gog-token"])
    client.force_login(get_user_model().objects.create_user(username="x", email="x@dimagi.com"))
    assert client.get("/api/agents/secret/google/authorize").status_code == 404


@pytest.mark.django_db
def test_the_return_trip_keeps_the_deployment_path_prefix(fleet, monkeypatch, settings):
    """The mint worked and the operator saw Resolver404.

    canopy-web is served under /canopy in production, so a root-relative redirect
    lands outside the app. The token was stored, `?google=ok` was set, and the
    page still read as a failure — the worst shape of bug on a screen whose whole
    job is telling you whether a credential is healthy."""
    settings.CANOPY_PUBLIC_BASE_URL = "https://labs.connect.dimagi.com/canopy"
    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response())

    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    res = fleet["client"].get(CALLBACK, {"code": "c", "state": state})

    assert res["Location"] == (
        "https://labs.connect.dimagi.com/canopy"
        "/w/connect/agents/ace/settings?google=ok#credentials"
    )


@pytest.mark.django_db
def test_a_failed_mint_returns_to_the_same_prefixed_page(fleet, monkeypatch, settings):
    """Every exit uses the same builder — a failure that 404s is how an operator
    concludes the whole feature is broken."""
    settings.CANOPY_PUBLIC_BASE_URL = "https://labs.connect.dimagi.com/canopy"
    monkeypatch.setattr(g, "exchange_code", lambda **kw: _google_response(refresh=""))

    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    res = fleet["client"].get(CALLBACK, {"code": "c", "state": state})
    assert res["Location"].startswith("https://labs.connect.dimagi.com/canopy/w/connect/")
    assert res["Location"].endswith("?google=no-refresh-token#credentials")


# --- only what the agent needs, and from the right account ------------------------

GMAIL = "https://www.googleapis.com/auth/gmail"


def _id_token_for(email: str) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    return "h." + payload.rstrip(b"=").decode() + ".sig"


def _hosted(agent, address="family@marshellis.com", status="verified"):
    """Give `agent` an address on a canopy-hosted domain of its own workspace (apps/mail),
    routed to it."""
    from apps.mail.models import MailDomain, MailRoute

    local, _, host = address.partition("@")
    d = MailDomain.objects.create(workspace=agent.workspace, name=host, status=status)
    MailRoute.objects.create(domain=d, local_part=local, kind=MailRoute.AGENT, agent=agent)
    agent.email = address
    agent.save(update_fields=["email"])
    return agent


@pytest.mark.django_db
def test_a_domain_registered_but_not_yet_verified_is_still_gmail(fleet, monkeypatch):
    """A workspace can register its domain before the MX cut-over; until the domain verifies,
    the address is still a Google mailbox — it keeps its Gmail scopes, its
    hint, and the account check."""
    agent = _hosted(fleet["agent"], "ace@dimagi-ai.com", status="pending")
    assert g.scopes_for(agent) == g.FLEET_SCOPES
    assert g.expected_account(agent) == "ace@dimagi-ai.com"
    url = fleet["client"].get("/api/agents/ace/google/authorize").json()["url"]
    assert "gmail.modify" in url and "login_hint=ace%40dimagi-ai.com" in url
    res = _mint(fleet, monkeypatch, {**_google_response(), "id_token": _id_token_for("jj@gmail.com")})
    assert "google=wrong-account" in res["Location"]


def _gmail(agent, address="ace@dimagi-ai.com"):
    agent.email = address
    agent.save(update_fields=["email"])
    return agent


def _mint(fleet, monkeypatch, resp):
    monkeypatch.setattr(g, "exchange_code", lambda **kw: resp)
    state = g.sign_state(agent_slug="ace", user_pk=fleet["user"].pk)
    return fleet["client"].get(CALLBACK, {"code": "c", "state": state})


@pytest.mark.django_db
def test_a_gmail_agent_is_asked_for_everything(fleet):
    assert g.scopes_for(_gmail(fleet["agent"])) == g.FLEET_SCOPES


@pytest.mark.django_db
def test_a_canopy_hosted_agent_is_never_asked_for_gmail(fleet):
    """Its mail runs through canopy (SES), so a Gmail grant would be a line on the
    consent screen for nothing — and access to whatever inbox the account has."""
    agent = _hosted(fleet["agent"])
    scopes = g.scopes_for(agent)
    assert not [s for s in scopes if s.startswith(GMAIL)]
    assert set(g.services_for(scopes)) == {"calendar", "docs", "drive", "forms", "sheets", "slides"}
    assert {"openid", "email"} <= set(scopes)

    from urllib.parse import parse_qs, urlparse

    url = fleet["client"].get("/api/agents/ace/google/authorize").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert GMAIL not in q["scope"][0]
    # The hosted address is not a Google account, so it is never the hint.
    assert q["login_hint"] == ["ace@dimagi-ai.com"]


@pytest.mark.django_db
def test_a_gmail_agent_is_hinted_at_its_own_address(fleet):
    _gmail(fleet["agent"], "Scout@Example.org")
    url = fleet["client"].get("/api/agents/ace/google/authorize").json()["url"]
    assert "login_hint=scout%40example.org" in url
    assert "gmail.modify" in url


@pytest.mark.django_db
def test_a_consent_from_another_google_account_is_refused_and_nothing_stored(fleet, monkeypatch):
    """The runner reads a Gmail agent's mailbox by its address (`gog --account
    <agent.email>`), so a token for any other account can never serve it — storing one
    would read as "connected" while every mail turn fails."""
    agent = _gmail(fleet["agent"])
    res = _mint(fleet, monkeypatch, {**_google_response(), "id_token": _id_token_for("jj@gmail.com")})
    assert res.status_code == 302
    assert res["Location"].endswith(
        "?google=wrong-account&account=jj%40gmail.com&expected=ace%40dimagi-ai.com#credentials")
    assert not AgentCredential.objects.filter(agent=agent, name="gog-token").exists()


@pytest.mark.django_db
def test_the_account_check_ignores_case(fleet, monkeypatch):
    _gmail(fleet["agent"], "ACE@dimagi-ai.com")
    assert _mint(fleet, monkeypatch, _google_response())["Location"].endswith("?google=ok#credentials")


@pytest.mark.django_db
def test_a_hosted_agent_with_no_google_account_may_use_any(fleet, monkeypatch):
    """A canopy-hosted address is not a Google account; with no `google_account` set,
    nothing names the account its Drive/Docs login must be, so any will do."""
    agent = _hosted(fleet["agent"])
    resp = {**_google_response(scope=["openid", "email", "https://www.googleapis.com/auth/drive"]),
            "id_token": _id_token_for("marshellis.agent@gmail.com")}
    assert _mint(fleet, monkeypatch, resp)["Location"].endswith("?google=ok#credentials")
    assert AgentCredential.objects.filter(agent=agent, name="gog-token").exists()


@pytest.mark.django_db
def test_a_hosted_agent_with_a_google_account_must_sign_in_as_it(fleet, monkeypatch):
    """`Agent.google_account` names the agent's Google login apart from its mailbox
    (family@marshellis.com on SES, signing in to Drive as a Gmail account): the mint is
    hinted at it and refuses any other account."""
    agent = _hosted(fleet["agent"])
    agent.google_account = "Marshellis.Agent@gmail.com"
    agent.save(update_fields=["google_account"])
    assert g.expected_account(agent) == "marshellis.agent@gmail.com"
    url = fleet["client"].get("/api/agents/ace/google/authorize").json()["url"]
    assert "login_hint=marshellis.agent%40gmail.com" in url and "gmail.modify" not in url

    res = _mint(fleet, monkeypatch, {**_google_response(), "id_token": _id_token_for("jj@gmail.com")})
    assert "google=wrong-account" in res["Location"] and "expected=marshellis.agent%40gmail.com" in res["Location"]
    assert not AgentCredential.objects.filter(agent=agent, name="gog-token").exists()
    resp = {**_google_response(), "id_token": _id_token_for("marshellis.agent@gmail.com")}
    assert _mint(fleet, monkeypatch, resp)["Location"].endswith("?google=ok#credentials")


@pytest.mark.django_db
def test_a_gmail_agent_with_a_separate_google_account_checks_that_account(fleet, monkeypatch):
    agent = _gmail(fleet["agent"])
    agent.google_account = "login@dimagi-ai.com"
    agent.save(update_fields=["google_account"])
    assert g.expected_account(agent) == "login@dimagi-ai.com"
    res = _mint(fleet, monkeypatch, _google_response())  # consents as ace@dimagi-ai.com
    assert "google=wrong-account" in res["Location"]


@pytest.mark.django_db
def test_a_hosted_agent_that_also_reads_a_gmail_address_keeps_gmail(fleet):
    """Ada's shape: a hosted primary address and a Gmail mailbox the runner still reads
    (an InboundMailbox) — the token must still carry Gmail."""
    from apps.inbound.models import InboundMailbox

    agent = _hosted(fleet["agent"])
    InboundMailbox.objects.create(address="ace@dimagi-ai.com", agent=agent)
    assert g.scopes_for(agent) == g.FLEET_SCOPES
