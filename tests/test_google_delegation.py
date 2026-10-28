"""A person connects THEIR OWN Google account to an agent — a credential they delegate
to it, minted on one of its runners (apps/agents/credentials/google_mint.py,
docs/architecture/vault.md § Connect your own Google account).

What is pinned: who may start one (the agent's admins only — a delegated entry is in force
only while its person is one), which runner may finish it (one with a machine that may
hold a credential of theirs: a cloud box, never another person's laptop), what the runner
is told (seal to those machines and the person, never under the vault key, under
`google-delegated-<account>`), what is stored (only such a seal, from the named account),
and that it then lists and withdraws like a GitHub token.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import accounts, google_mint, google_oauth as g, zk_seal
from apps.agents.models import Agent, AgentAdmin, AgentCredential, AgentGoogleMint
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.testing import give_vault_key
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import ref

CALLBACK = "/api/oauth/google/callback"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
GINA = "gina@gmail.com"
ENTRY = "google-delegated-gina@gmail.com"


@pytest.fixture
def home(settings):
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {"APP": {"client_id": "cid.apps.googleusercontent.com", "secret": "GOCSPX-x"}}}
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    gina = User.objects.create_user(username="gina", email=GINA)
    ws = Workspace.objects.create(slug="marshellis", display_name="Marshellis", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=gina, role=WorkspaceMembership.ADMIN)
    agent = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=jj)
    give_vault_key(jj)
    give_vault_key(gina)
    cloud = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                  last_heartbeat_at=timezone.now(), status=Runner.ONLINE,
                                  capabilities={"google_mint": True})
    RunnerAssignment.objects.create(agent=agent, runner=cloud, rank=0)
    machine, _, ecdh = a_machine(cloud, jj)
    client = Client()
    client.force_login(gina)
    box = Client()  # the runner's protocol, as its owner (as tests/test_google_mint.py does)
    box.force_login(jj)
    return {"client": client, "box": box, "agent": agent, "jj": jj, "gina": gina, "ws": ws, "cloud": cloud,
            "machine": machine, "ecdh": ecdh}


def _admin(home):
    AgentAdmin.objects.create(agent=home["agent"], user=home["gina"], granted_by=home["jj"])


def _start(home, **body):
    return home["client"].post("/api/agents/bailey/google/delegate", {"account": GINA, **body},
                               content_type="application/json")


def _claim(home):
    r = home["box"].get(f"/api/harness/runners/{home['cloud'].pk}/mint/claim")
    assert r.status_code == 200, r.content
    return r.json()["google"]


def _runner_post(home, mint_id, step, body):
    return home["box"].post(f"/api/harness/runners/{home['cloud'].pk}/google-mint/{mint_id}/{step}",
                            body, content_type="application/json")


def _to_code(home, **body) -> tuple[str, dict]:
    """Start, post the challenge, come back from Google: (mint id, the runner's work)."""
    r = _start(home, **body)
    assert r.status_code == 200, r.content
    mint_id = r.json()["id"]
    assert r.json()["delegated"] is True and r.json()["expected_account"] == GINA
    _claim(home)
    assert _runner_post(home, mint_id, "challenge", {"code_challenge": CHALLENGE}).status_code == 204
    url = home["client"].get(f"/api/agents/bailey/google/delegate/{mint_id}").json()["authorize_url"]
    q = parse_qs(urlparse(url).query)
    assert q["login_hint"] == [GINA]
    assert q["scope"][0].split() == list(g.delegated_scopes(body.get("services")))
    res = home["client"].get(CALLBACK, {"state": q["state"][0], "code": "4/code"})
    assert res["Location"].endswith(f"/w/marshellis/agents/bailey/connect/google?google=pending&mint={mint_id}")
    return mint_id, _claim(home)


def _seal_for(work, plaintext='{"refresh_token": "1//gina"}'):
    return zk_seal.seal_to(plaintext, agent_id=work["agent_id"], name=work["name"], kv=work["kv"],
                           recipients=[(r["kind"], r["target"], r["public_jwk"]) for r in work["recipients"]])


# --- who may ----------------------------------------------------------------------

@pytest.mark.django_db
def test_a_workspace_admin_who_is_not_an_admin_of_the_agent_is_refused(home):
    """A delegated entry is in force only while its person is an admin of the agent
    (`accounts.serves`): a non-admin is told so rather than handed a credential that
    would never be used. Being a WORKSPACE admin is not being the agent's admin."""
    r = _start(home)
    assert r.status_code == 403
    assert not AgentGoogleMint.objects.exists()


@pytest.mark.django_db
def test_an_agent_admin_may_start_and_only_gmail_is_asked_for_by_default(home):
    _admin(home)
    mint_id, work = _to_code(home)
    assert work["delegated"] is True and work["name"] == ENTRY
    assert work["expected_account"] == GINA
    mint = AgentGoogleMint.objects.get(pk=mint_id)
    assert mint.delegated_by == home["gina"] and mint.services == ["gmail"]
    scopes = " ".join(g.delegated_scopes(None))
    assert "drive" not in scopes and "gmail.modify" in scopes


@pytest.mark.django_db
def test_an_unknown_service_is_refused(home):
    _admin(home)
    r = _start(home, services=["gmail", "youtube"])
    assert r.status_code == 422 and r.json()["error"] == "bad_services"


@pytest.mark.django_db
def test_only_a_machine_that_may_hold_her_credential_may_finish_it(home):
    """The owner's laptop runs the agent, but its device key is in his home directory:
    sealed to it, he could open her inbox. With no cloud box online, nothing starts."""
    _admin(home)
    laptop = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=home["jj"], workspace=home["ws"],
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE,
                                   capabilities={"google_mint": True})
    RunnerAssignment.objects.create(agent=home["agent"], runner=laptop, rank=1)
    a_machine(laptop, home["jj"])
    Runner.objects.filter(pk=home["cloud"].pk).update(last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    r = _start(home)
    assert r.status_code == 409 and r.json()["error"] == "no_mint_runner"
    assert "never another person's laptop" in r.json()["detail"]


# --- what the runner is told, and what is stored -------------------------------------

@pytest.mark.django_db
def test_the_runner_seals_to_her_machines_and_her_key_and_it_lands_delegated(home):
    _admin(home)
    mint_id, work = _to_code(home)
    targets = {(r["kind"], r["target"]) for r in work["recipients"]}
    assert targets == {("device", home["machine"].ecdh_jkt), ("member", str(home["gina"].pk))}
    assert ("member", str(home["jj"].pk)) not in targets, "the vault's owner is not a reader of hers"
    r = _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"],
                                                "account": "Gina@gmail.com", "name": ENTRY})
    assert r.status_code == 204, r.content
    out = home["client"].get(f"/api/agents/bailey/google/delegate/{mint_id}").json()
    assert (out["status"], out["outcome"]) == ("done", "ok")
    entry = AgentCredential.objects.get(agent=home["agent"], name=ENTRY)
    assert (entry.account, entry.provider, entry.delegated_by) == ("delegated", "google", home["gina"])
    assert entry.identity == {"email": GINA}
    assert "https://www.googleapis.com/auth/gmail.modify" in entry.scopes
    assert ref.open_sealed_value(entry.value_zk, str(home["agent"].pk), ENTRY, "device", home["machine"].ecdh_jkt,
                                 home["ecdh"]) == '{"refresh_token": "1//gina"}'
    assert accounts.label(entry, home["agent"]).startswith("Google · delegated by")
    # It reaches the machine only in a runner resolve's `delegated` field.
    assert ENTRY in accounts.resolve_payload(home["agent"], home["machine"])
    assert ENTRY in accounts.delegated_names(home["agent"])


@pytest.mark.django_db
def test_a_value_under_the_vault_key_is_refused(home):
    _admin(home)
    mint_id, _ = _to_code(home)
    r = _runner_post(home, mint_id, "result", {"value_zk": "zk1:abc", "kv": 1, "account": GINA, "name": ENTRY})
    assert r.status_code == 422 and r.json()["error"] == "not_sealed"
    assert not AgentCredential.objects.filter(name=ENTRY).exists()


@pytest.mark.django_db
def test_a_seal_to_the_agents_other_readers_is_refused(home):
    """Sealed to the vault's members (as the agent's own token is), the owner could open it."""
    _admin(home)
    mint_id, work = _to_code(home)
    value = zk_seal.seal_to("x", agent_id=work["agent_id"], name=ENTRY, kv=work["kv"],
                            recipients=zk_seal.recipients_for(home["agent"]))
    r = _runner_post(home, mint_id, "result", {"value_zk": value, "kv": work["kv"], "account": GINA, "name": ENTRY})
    assert r.status_code == 422 and r.json()["error"] == "foreign_reader"


@pytest.mark.django_db
def test_an_older_cli_that_names_no_entry_stores_nothing(home):
    _admin(home)
    mint_id, work = _to_code(home)
    _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"], "account": GINA})
    out = home["client"].get(f"/api/agents/bailey/google/delegate/{mint_id}").json()
    assert (out["status"], out["outcome"]) == ("failed", "outdated-runner")
    assert not AgentCredential.objects.filter(name=ENTRY).exists()


@pytest.mark.django_db
def test_a_consent_from_another_account_stores_nothing(home):
    _admin(home)
    mint_id, work = _to_code(home)
    _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"],
                                            "account": "someone@gmail.com", "name": ENTRY})
    out = home["client"].get(f"/api/agents/bailey/google/delegate/{mint_id}").json()
    assert (out["status"], out["outcome"]) == ("failed", "wrong-account")
    assert not AgentCredential.objects.filter(name=ENTRY).exists()


@pytest.mark.django_db
def test_she_stops_being_an_admin_before_it_finishes(home):
    _admin(home)
    mint_id, work = _to_code(home)
    AgentAdmin.objects.filter(user=home["gina"]).delete()
    _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"],
                                            "account": GINA, "name": ENTRY})
    assert AgentGoogleMint.objects.get(pk=mint_id).outcome == "not-admin"
    assert not AgentCredential.objects.filter(name=ENTRY).exists()


@pytest.mark.django_db
def test_her_sign_in_and_the_agents_own_do_not_supersede_each_other(home):
    _admin(home)
    mine = _start(home).json()["id"]
    jj = home["box"]
    own = jj.post("/api/agents/bailey/google/mint", {}, content_type="application/json")
    assert own.status_code == 200, own.content
    assert AgentGoogleMint.objects.get(pk=mine).status == AgentGoogleMint.REQUESTED
    # Each reads only its own kind of sign-in.
    assert jj.get(f"/api/agents/bailey/google/mint/{mine}").status_code == 404
    assert home["client"].get(f"/api/agents/bailey/google/delegate/{own.json()['id']}").status_code == 404


@pytest.mark.django_db
def test_someone_elses_delegated_account_is_not_replaced(home):
    _admin(home)
    AgentCredential.objects.create(agent=home["agent"], name=ENTRY, value_zk="zks1:x", account="delegated",
                                   delegated_by=home["jj"], provider="google")
    r = _start(home)
    assert r.status_code == 409 and r.json()["error"] == "entry_taken"


# --- the settings list --------------------------------------------------------------

@pytest.mark.django_db
def test_it_lists_and_withdraws_like_a_github_token(home):
    _admin(home)
    mint_id, work = _to_code(home)
    _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"],
                                            "account": GINA, "name": ENTRY})
    [row] = home["client"].get("/api/me/delegated-credentials/").json()
    assert (row["agent"], row["name"], row["provider"]) == ("bailey", ENTRY, "google")
    assert row["identity"] == {"email": GINA} and row["machines"] == ["cloud-ec2-2"] and row["in_force"]
    assert "value_zk" not in row
    # The page-safe read: which account, nothing about the credential.
    [acct] = home["client"].get("/api/me/connected-accounts/").json()
    assert (acct["agent"], acct["provider"], acct["account"], acct["in_force"]) == ("bailey", "google", GINA, True)
    assert acct["session_until"] is None and acct["id"] == row["id"]
    r = home["client"].delete(f"/api/me/delegated-credentials/{row['id']}")
    assert r.status_code == 204
    assert not AgentCredential.objects.filter(name=ENTRY).exists()


def test_the_entry_name_is_the_account_lowercased():
    assert google_mint.delegated_entry_name(" Gina@Gmail.com ") == ENTRY


# --- a session: lent from a page for up to an hour ----------------------------------

def _session_to_code(home, minutes=60):
    r = _start(home, session_minutes=minutes)
    assert r.status_code == 200, r.content
    mint_id = r.json()["id"]
    assert r.json()["session_minutes"] == minutes
    _claim(home)
    assert _runner_post(home, mint_id, "challenge", {"code_challenge": CHALLENGE}).status_code == 204
    url = home["client"].get(f"/api/agents/bailey/google/delegate/{mint_id}").json()["authorize_url"]
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["online"], "a session asks Google for no refresh token"
    assert q["include_granted_scopes"] == ["false"]
    res = home["client"].get(CALLBACK, {"state": q["state"][0], "code": "4/code"})
    assert res.status_code == 200 and b"close this window" in res.content
    return mint_id, _claim(home)


@pytest.mark.django_db
def test_a_session_lasts_its_minutes_then_is_served_no_longer_and_gone(home):
    import datetime as dt

    from django.utils import timezone

    from apps.agents.credentials import accounts

    _admin(home)
    mint_id, work = _session_to_code(home, minutes=60)
    assert work["session"] is True and work["session_minutes"] == 60
    _runner_post(home, mint_id, "result", {"value_zk": _seal_for(work), "kv": work["kv"],
                                            "account": GINA, "name": ENTRY})
    [row] = home["client"].get("/api/me/connected-accounts/").json()
    assert row["account"] == GINA and row["in_force"] and row["session_until"]
    entry = AgentCredential.objects.get(name=ENTRY)
    assert accounts.serves(entry, home["agent"])
    later = timezone.now() + dt.timedelta(minutes=61)
    assert accounts.session_ended(entry, now=later)
    entry.identity = {**entry.identity, "session_until": (timezone.now() - dt.timedelta(seconds=1)).isoformat()}
    entry.save(update_fields=["identity"])
    assert not accounts.serves(entry, home["agent"])
    assert home["client"].get("/api/me/connected-accounts/").json() == []
    assert not AgentCredential.objects.filter(name=ENTRY).exists(), "an ended session leaves nothing behind"


@pytest.mark.django_db
def test_a_session_longer_than_an_hour_is_refused(home):
    _admin(home)
    assert _start(home, session_minutes=61).status_code == 422
