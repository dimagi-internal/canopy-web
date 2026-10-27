"""Connecting an agent's Google account — finished on one of its runners, so canopy-web
never holds the token (apps/agents/credentials/google_mint.py).

canopy-web's part: which scopes and account, the consent link around the RUNNER's PKCE
challenge, relaying Google's single-use code to that runner exactly once, and storing
only the ciphertext the runner sends back. The exchange itself (code + verifier + the
client secret from the agent's vault) is `canopy vault google-mint`, tested in canopy.

The scope assertions are transcribed from the tokens that actually work: on 2026-09-06
op://Agent-Echo/gog-token and op://Agent-Eva/gog-token both carried exactly
LIVE_FLEET_SCOPES below.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import google_mint, google_oauth as g, zk_seal
from apps.agents.models import Agent, AgentCredential, AgentGoogleMint
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.testing import give_vault_key
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import ref

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
# ace/config/agent.json: what its mailbox must be able to do (gog's service names),
# keyed to the scope path that grants each.
ACE_DECLARED = {"gmail": "gmail", "calendar": "calendar", "drive": "drive", "docs": "documents",
                "slides": "presentations", "sheets": "spreadsheets", "forms": "forms"}
GMAIL = "https://www.googleapis.com/auth/gmail"
CALLBACK = "/api/oauth/google/callback"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


# --- what is asked for ------------------------------------------------------------

def test_the_request_covers_every_service_an_agent_declares():
    """The live tokens record what was MINTED, not what is needed; ace declares
    slides and calendar too, and a token without them fails its first slides call."""
    for service, path in ACE_DECLARED.items():
        assert any(s.startswith(f"https://www.googleapis.com/auth/{path}") for s in g.FLEET_SCOPES), service


def test_the_request_is_a_superset_of_what_the_live_fleet_already_has():
    """A re-mint must not take capability AWAY from a working mailbox."""
    assert set(LIVE_FLEET_SCOPES) <= set(g.FLEET_SCOPES)


def test_the_consent_link_forces_a_refresh_token_and_carries_the_runners_challenge():
    """Without prompt=consent a re-mint returns no refresh token; without the challenge
    the code would be redeemable by whoever holds it — canopy-web included."""
    url = g.authorize_url(client_id="cid", state="st", code_challenge=CHALLENGE)
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge"] == [CHALLENGE] and q["code_challenge_method"] == ["S256"]


def test_one_redirect_uri_serves_every_agent():
    assert g.callback_url().endswith("/api/oauth/google/callback")
    a = g.authorize_url(client_id="cid", state=g.sign_state(agent_slug="ace", user_pk=1), code_challenge=CHALLENGE)
    b = g.authorize_url(client_id="cid", state=g.sign_state(agent_slug="echo", user_pk=1), code_challenge=CHALLENGE)
    assert parse_qs(urlparse(a).query)["redirect_uri"] == parse_qs(urlparse(b).query)["redirect_uri"]


def test_state_round_trips_and_refuses_tampering():
    from django.core import signing

    state = g.sign_state(agent_slug="ace", user_pk=7, mint="m1")
    assert g.unsign_state(state) == {"agent": "ace", "user": "7", "mint": "m1"}
    with pytest.raises(signing.BadSignature):
        g.unsign_state(state[:-3] + "aaa")


def test_canopy_web_keeps_no_way_to_exchange_a_code():
    """The server-side exchange is gone: no token endpoint, no client secret read."""
    for name in ("exchange_code", "build_gog_token", "email_from_id_token", "google_client_credentials",
                 "TOKEN_ENDPOINT"):
        assert not hasattr(g, name), name


# --- the flow ---------------------------------------------------------------------

@pytest.fixture
def fleet(settings):
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {"APP": {"client_id": "cid.apps.googleusercontent.com", "secret": "GOCSPX-x"}}}
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, runtime_secrets=["gog-token"])
    key = give_vault_key(jj)
    runner = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE,
                                   capabilities={"google_mint": True})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    machine, _, ecdh = a_machine(runner, jj)
    client = Client()
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj, "ws": ws, "key": key, "runner": runner,
            "machine": machine, "ecdh": ecdh}


def _start(fleet, **body):
    return fleet["client"].post("/api/agents/ace/google/mint", body, content_type="application/json")


def _claim(fleet):
    r = fleet["client"].get(f"/api/harness/runners/{fleet['runner'].pk}/mint/claim")
    assert r.status_code == 200, r.content
    return r.json()["google"]


def _runner_post(fleet, mint_id, step, body):
    return fleet["client"].post(f"/api/harness/runners/{fleet['runner'].pk}/google-mint/{mint_id}/{step}",
                                body, content_type="application/json")


def _to_consent(fleet) -> tuple[str, str]:
    """Start a sign-in and let the runner post its challenge: (mint id, consent link)."""
    mint_id = _start(fleet).json()["id"]
    work = _claim(fleet)
    assert work == {**work, "id": mint_id, "agent": "ace", "status": "requested", "code": ""}
    assert _runner_post(fleet, mint_id, "challenge", {"code_challenge": CHALLENGE}).status_code == 204
    out = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()
    assert out["status"] == "awaiting"
    return mint_id, out["authorize_url"]


def _return(fleet, url, **params):
    state = parse_qs(urlparse(url).query)["state"][0]
    return fleet["client"].get(CALLBACK, {"state": state, **params})


def _sealed_token(fleet, token='{"refresh_token": "1//refresh"}'):
    agent = fleet["agent"]
    return zk_seal.seal_to(token, agent_id=str(agent.pk), name="gog-token", kv=agent.zk_kv,
                           recipients=zk_seal.recipients_for(agent))


@pytest.mark.django_db
def test_a_sign_in_runs_on_the_runner_and_only_ciphertext_comes_back(fleet):
    mint_id, url = _to_consent(fleet)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "login_hint=ace%40dimagi-ai.com" in url and f"code_challenge={CHALLENGE}" in url

    res = _return(fleet, url, code="4/code")
    assert res.status_code == 302
    assert res["Location"].endswith(f"/w/connect/agents/ace/settings?google=pending&mint={mint_id}#credentials")

    work = _claim(fleet)
    assert work["status"] == "completing" and work["code"] == "4/code"
    assert work["client_id"] == "cid.apps.googleusercontent.com" and work["client"] == "canopy-web"
    assert work["redirect_uri"].endswith(CALLBACK) and work["agent_id"] == str(fleet["agent"].pk)
    assert {r["kind"] for r in work["recipients"]} == {"device", "member"}
    assert "GOCSPX" not in str(work), "the client secret is the runner's, from the agent's vault"
    assert _claim(fleet) is None, "the code is handed out once"
    assert AgentGoogleMint.objects.get(pk=mint_id).code == "", "and not kept"

    value = _sealed_token(fleet)
    r = _runner_post(fleet, mint_id, "result", {"value_zk": value, "kv": 0, "account": "ace@dimagi-ai.com"})
    assert r.status_code == 204, r.content
    out = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()
    assert (out["status"], out["outcome"], out["account"]) == ("done", "ok", "ace@dimagi-ai.com")
    stored = AgentCredential.objects.get(agent=fleet["agent"], name="gog-token").value_zk
    assert stored == value
    assert ref.open_sealed_value(stored, str(fleet["agent"].pk), "gog-token", "member", str(fleet["user"].pk),
                                 fleet["key"]) == '{"refresh_token": "1//refresh"}'


@pytest.mark.django_db
def test_no_runner_online_says_so_and_starts_nothing(fleet):
    Runner.objects.filter(pk=fleet["runner"].pk).update(last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    r = _start(fleet)
    assert r.status_code == 409 and r.json()["error"] == "no_mint_runner"
    assert "No runner that may hold ace is online" in r.json()["detail"]
    assert not AgentGoogleMint.objects.exists()


@pytest.mark.django_db
def test_a_runner_that_cannot_finish_a_sign_in_is_not_chosen(fleet):
    Runner.objects.filter(pk=fleet["runner"].pk).update(capabilities={})
    assert _start(fleet).status_code == 409


@pytest.mark.django_db
def test_a_runner_whose_owner_may_not_hold_the_agent_is_not_chosen(fleet):
    stranger = get_user_model().objects.create_user(username="s", email="s@dimagi.com")
    Runner.objects.filter(pk=fleet["runner"].pk).update(owner=stranger)
    assert _start(fleet).status_code == 409


@pytest.mark.django_db
def test_a_token_in_the_clear_is_refused(fleet):
    mint_id, url = _to_consent(fleet)
    _return(fleet, url, code="4/code")
    _claim(fleet)
    r = _runner_post(fleet, mint_id, "result", {"value_zk": '{"refresh_token": "1//x"}', "account": "ace@dimagi-ai.com"})
    assert r.status_code == 422
    assert not AgentCredential.objects.filter(agent=fleet["agent"], name="gog-token").exists()


@pytest.mark.django_db
def test_a_token_sealed_to_a_stranger_is_refused(fleet):
    from cryptography.hazmat.primitives.asymmetric import ec

    mint_id, url = _to_consent(fleet)
    _return(fleet, url, code="4/code")
    _claim(fleet)
    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    value = zk_seal.seal_to("x", agent_id=str(fleet["agent"].pk), name="gog-token", kv=0,
                            recipients=[("device", ref.jkt(other), ref.jwk(other))])
    r = _runner_post(fleet, mint_id, "result", {"value_zk": value, "kv": 0, "account": "ace@dimagi-ai.com"})
    assert r.status_code == 422 and r.json()["error"] == "foreign_reader"


@pytest.mark.django_db
def test_a_consent_from_another_google_account_is_not_stored(fleet):
    """The runner reads a Gmail agent's mailbox by its address, so a token for any other
    account can never serve it — stored, it would read "connected" while mail fails."""
    Agent.objects.filter(pk=fleet["agent"].pk).update(email="ace@dimagi-ai.com")
    mint_id, url = _to_consent(fleet)
    _return(fleet, url, code="4/code")
    _claim(fleet)
    _runner_post(fleet, mint_id, "result", {"value_zk": _sealed_token(fleet), "kv": 0, "account": "jj@gmail.com"})
    out = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()
    assert (out["status"], out["outcome"], out["account"]) == ("failed", "wrong-account", "jj@gmail.com")
    assert out["expected_account"] == "ace@dimagi-ai.com"
    assert not AgentCredential.objects.filter(agent=fleet["agent"], name="gog-token").exists()


@pytest.mark.django_db
def test_the_runner_reports_why_an_exchange_failed(fleet):
    mint_id, url = _to_consent(fleet)
    _return(fleet, url, code="4/code")
    _claim(fleet)
    _runner_post(fleet, mint_id, "result", {"error": "no-refresh-token", "detail": "Google returned no refresh token"})
    out = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()
    assert (out["status"], out["outcome"]) == ("failed", "no-refresh-token")


@pytest.mark.django_db
def test_a_denied_consent_ends_the_sign_in(fleet):
    mint_id, url = _to_consent(fleet)
    res = _return(fleet, url, error="access_denied")
    assert res["Location"].endswith(f"?google=denied&mint={mint_id}#credentials")
    assert _claim(fleet) is None


@pytest.mark.django_db
def test_the_callback_refuses_a_state_signed_for_someone_else(fleet):
    other = get_user_model().objects.create_user(username="m", email="m@dimagi.com")
    state = g.sign_state(agent_slug="ace", user_pk=other.pk, mint="x")
    assert fleet["client"].get(CALLBACK, {"code": "c", "state": state}).status_code == 403


@pytest.mark.django_db
def test_the_callback_refuses_unsigned_state(fleet):
    assert fleet["client"].get(CALLBACK, {"code": "c", "state": "ace"}).status_code == 400


@pytest.mark.django_db
def test_a_new_sign_in_supersedes_an_unfinished_one(fleet):
    first = _start(fleet).json()["id"]
    second = _start(fleet).json()["id"]
    assert AgentGoogleMint.objects.get(pk=first).outcome == "superseded"
    assert _claim(fleet)["id"] == second


@pytest.mark.django_db
def test_an_old_sign_in_expires(fleet):
    mint_id, _ = _to_consent(fleet)
    AgentGoogleMint.objects.filter(pk=mint_id).update(
        updated_at=timezone.now() - dt.timedelta(seconds=google_mint.TTL_SECONDS + 5))
    out = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()
    assert (out["status"], out["outcome"]) == ("failed", "expired")


@pytest.mark.django_db
def test_only_the_runner_it_was_handed_to_may_answer(fleet):
    mint_id = _start(fleet).json()["id"]
    other = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=fleet["user"])
    r = fleet["client"].post(f"/api/harness/runners/{other.pk}/google-mint/{mint_id}/challenge",
                             {"code_challenge": CHALLENGE}, content_type="application/json")
    assert r.status_code == 409


@pytest.mark.django_db
def test_a_non_member_cannot_start_a_mint():
    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    ws = Workspace.objects.create(slug="private", display_name="P", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret", name="S", workspace=ws, runtime_secrets=["gog-token"])
    c = Client()
    c.force_login(get_user_model().objects.create_user(username="x", email="x@dimagi.com"))
    assert c.post("/api/agents/secret/google/mint", {}, content_type="application/json").status_code == 404


@pytest.mark.django_db
def test_the_return_trip_keeps_the_deployment_path_prefix(fleet, settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://labs.connect.dimagi.com/canopy"
    mint_id, url = _to_consent(fleet)
    res = _return(fleet, url, code="4/code")
    assert res["Location"] == ("https://labs.connect.dimagi.com/canopy/w/connect/agents/ace/settings"
                               f"?google=pending&mint={mint_id}#credentials")


# --- only what the agent needs, and from the right account ------------------------

def _hosted(agent, address="family@marshellis.com", status="verified"):
    from apps.mail.models import MailDomain, MailRoute

    local, _, host = address.partition("@")
    d = MailDomain.objects.create(workspace=agent.workspace, name=host, status=status)
    MailRoute.objects.create(domain=d, local_part=local, kind=MailRoute.AGENT, agent=agent)
    agent.email = address
    agent.save(update_fields=["email"])
    return agent


@pytest.mark.django_db
def test_a_domain_registered_but_not_yet_verified_is_still_gmail(fleet):
    agent = _hosted(fleet["agent"], "ace@dimagi-ai.com", status="pending")
    assert g.scopes_for(agent) == g.FLEET_SCOPES
    assert g.expected_account(agent) == "ace@dimagi-ai.com"


@pytest.mark.django_db
def test_a_canopy_hosted_agent_is_never_asked_for_gmail(fleet):
    agent = _hosted(fleet["agent"])
    assert not [s for s in g.scopes_for(agent) if s.startswith(GMAIL)]
    _, url = _to_consent(fleet)
    q = parse_qs(urlparse(url).query)
    assert GMAIL not in q["scope"][0]
    assert q["login_hint"] == ["ace@dimagi-ai.com"], "a hosted address is no Google account: the slug default"
    assert g.expected_account(agent) == ""


@pytest.mark.django_db
def test_a_hosted_agent_with_a_google_account_must_sign_in_as_it(fleet):
    agent = _hosted(fleet["agent"])
    agent.google_account = "Marshellis.Agent@gmail.com"
    agent.save(update_fields=["google_account"])
    assert g.expected_account(agent) == "marshellis.agent@gmail.com"
    _, url = _to_consent(fleet)
    assert "login_hint=marshellis.agent%40gmail.com" in url and "gmail.modify" not in url


@pytest.mark.django_db
def test_a_hosted_agent_that_also_reads_a_gmail_address_keeps_gmail(fleet):
    from apps.inbound.models import InboundMailbox

    agent = _hosted(fleet["agent"])
    InboundMailbox.objects.create(address="ace@dimagi-ai.com", agent=agent)
    assert g.scopes_for(agent) == g.FLEET_SCOPES


@pytest.mark.django_db
def test_a_login_hint_overrides_the_default(fleet):
    mint_id = _start(fleet, login_hint="x@y.com").json()["id"]
    _claim(fleet)
    _runner_post(fleet, mint_id, "challenge", {"code_challenge": CHALLENGE})
    url = fleet["client"].get(f"/api/agents/ace/google/mint/{mint_id}").json()["authorize_url"]
    assert "login_hint=x%40y.com" in url
