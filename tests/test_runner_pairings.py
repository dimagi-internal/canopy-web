"""A downloaded laptop runner pairs by browser: no PAT at any point.

The box makes its device key, opens a pairing with a proof of it, a person
approves it in the browser behind a passkey, the approval's one-time secret goes
back only through the box's own loopback, and the box claims with it — getting a
runner whose device key is already ACTIVE.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from canopy_sdk.jose import make_dpop_proof
from canopy_sdk.keys import public_jwk
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events.models import Activity
from apps.harness import api_runner_pairings, device_keys
from apps.harness.models import Runner, RunnerDeviceKey, RunnerPairing
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

PAIRINGS = "/api/harness/runner-pairings/"


@pytest.fixture(autouse=True)
def _fresh_cache(settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example.org"
    cache.clear()
    yield
    cache.clear()


def _person(username: str):
    return get_user_model().objects.create_user(username=username, email=f"{username}@dimagi.com")


@pytest.fixture
def org():
    jj = _person("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    browser = Client()
    browser.force_login(jj)
    grant_step_up(browser)
    return {"user": jj, "ws": ws, "other": other, "browser": browser}


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _open(key, *, signer=None, **extra):
    body = {"public_jwk": public_jwk(key), "kind": "secure-enclave", "hostname": "Jons-MacBook-Pro.local",
            "account": "jj", "platform": "macos", "has_emdash": False, "redirect_port": 53682, **extra}
    proof = make_dpop_proof(signer or key, "POST", f"http://testserver{PAIRINGS}")
    return Client().post(PAIRINGS, data=body, content_type="application/json", HTTP_DPOP=proof)


def _claim(key, pairing_id, secret=""):
    url = f"{PAIRINGS}{pairing_id}/claim"
    return Client().post(url, data={"secret": secret}, content_type="application/json",
                         HTTP_DPOP=make_dpop_proof(key, "POST", f"http://testserver{url}"))


def _approve(client, code, *, workspace="connect", name="jj-jons-macbook-pro"):
    return client.post(f"{PAIRINGS}{code}/approve", data={"workspace": workspace, "name": name},
                       content_type="application/json")


def _secret(res) -> tuple[str, str]:
    url = res.json()["redirect_url"]
    parts = urlsplit(url)
    assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port == 53682
    assert parts.path == "/paired"
    q = parse_qs(parts.query)
    return q["pairing"][0], q["secret"][0]


# ---- the whole flow -------------------------------------------------------------------

def test_open_approve_claim_makes_an_active_runner_owned_by_the_approver(org):
    key = _key()
    ecdh = _key()
    res = _open(key, ecdh_public_jwk=public_jwk(ecdh), ecdh_store="secure-enclave")
    assert res.status_code == 201, res.content
    body = res.json()
    code = body["user_code"]
    assert len(code) == 9 and code[4] == "-" and not set(code) & set("01OIL")
    assert body["verify_url"] == f"https://canopy.example.org/pair/{code}"
    assert body["interval"] == 3

    # The box polls before anyone approves.
    waiting = _claim(key, body["id"])
    assert waiting.status_code == 202 and waiting.json()["status"] == "waiting"

    seen = org["browser"].get(f"{PAIRINGS}{code.lower().replace('-', '')}")
    assert seen.status_code == 200, seen.content
    view = seen.json()
    assert view["status"] == "pending" and view["hostname"] == "Jons-MacBook-Pro.local"
    assert view["kind"] == "secure-enclave" and view["platform"] == "macos"
    assert view["suggested_name"] == "jj-jons-macbook-pro"
    # Only workspaces the caller may run work in.
    assert view["workspaces"] == [{"slug": "connect", "name": "Connect"}]

    approved = _approve(org["browser"], code)
    assert approved.status_code == 200, approved.content
    pairing_id, secret = _secret(approved)
    assert pairing_id == body["id"]
    pairing = RunnerPairing.objects.get(pk=body["id"])
    assert secret not in str(pairing.secret_hash) and pairing.secret_hash

    paired = _claim(key, body["id"], secret)
    assert paired.status_code == 201, paired.content
    out = paired.json()
    assert out["status"] == "paired" and out["name"] == "jj-jons-macbook-pro" and out["workspace"] == "connect"
    runner = Runner.objects.get(pk=out["runner_id"])
    assert runner.owner == org["user"] and runner.kind == Runner.EMDASH
    assert runner.engine == Runner.ENGINE_CLAUDE_CLI and runner.capabilities == {"sessions": True}
    assert runner.host == "Jons-MacBook-Pro.local"
    dk = RunnerDeviceKey.objects.get(pk=out["device_key_id"])
    assert dk.runner == runner and dk.status == RunnerDeviceKey.ACTIVE
    assert dk.jkt == device_keys.thumbprint(public_jwk(key)) and dk.kind == "secure-enclave"
    assert dk.approved_by == org["user"] and dk.ecdh_ready and dk.ecdh_store == "secure-enclave"
    assert RunnerPairing.objects.get(pk=body["id"]).status == "consumed"
    kinds = list(Activity.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == ["runner.pairing.approved", "runner.paired"]

    # Used: a second claim is refused, and the box signs in with its key instead.
    assert _claim(key, body["id"], secret).status_code == 401
    session = Client().post("/api/runner-sessions/", data={"runner": str(runner.pk)},
                            content_type="application/json",
                            HTTP_DPOP=make_dpop_proof(key, "POST", "http://testserver/api/runner-sessions/"))
    assert session.status_code == 201, session.content


def test_an_emdash_box_gets_the_emdash_engine(org):
    key = _key()
    body = _open(key, has_emdash=True).json()
    _, secret = _secret(_approve(org["browser"], body["user_code"]))
    out = _claim(key, body["id"], secret).json()
    assert Runner.objects.get(pk=out["runner_id"]).engine == Runner.ENGINE_EMDASH


# ---- the anonymous doors refuse uniformly ---------------------------------------------

def test_a_proof_by_another_key_is_refused(org):
    res = _open(_key(), signer=_key())
    assert res.status_code == 401
    assert not RunnerPairing.objects.exists()


def test_no_proof_or_a_replayed_proof_is_refused(org):
    key = _key()
    body = {"public_jwk": public_jwk(key), "kind": "software", "platform": "windows", "redirect_port": 50000}
    assert Client().post(PAIRINGS, data=body, content_type="application/json").status_code == 401
    proof = make_dpop_proof(key, "POST", f"http://testserver{PAIRINGS}")
    first = Client().post(PAIRINGS, data=body, content_type="application/json", HTTP_DPOP=proof)
    assert first.status_code == 201
    again = Client().post(PAIRINGS, data=body, content_type="application/json", HTTP_DPOP=proof)
    assert again.status_code == 401


@pytest.mark.parametrize("bad", [{"redirect_port": 80}, {"platform": "solaris"}, {"kind": "aws-iam"},
                                 {"hostname": "x" * 101}])
def test_a_malformed_request_is_refused(org, bad):
    assert _open(_key(), **bad).status_code == 422


def test_a_new_pairing_replaces_the_live_one_for_the_same_key(org):
    key = _key()
    first = _open(key).json()
    second = _open(key).json()
    assert first["user_code"] != second["user_code"]
    assert list(RunnerPairing.objects.values_list("pk", flat=True)) == [__import__("uuid").UUID(second["id"])]


def test_a_claim_by_another_key_or_with_a_wrong_secret_is_refused(org):
    key = _key()
    body = _open(key).json()
    _approve(org["browser"], body["user_code"])
    assert _claim(_key(), body["id"], "x").status_code == 401
    assert _claim(key, body["id"], "not-the-secret").status_code == 401
    assert _claim(key, body["id"]).status_code == 401
    assert not Runner.objects.exists()


def test_reapproving_issues_a_new_secret_and_the_old_one_stops_working(org):
    key = _key()
    body = _open(key).json()
    _, old = _secret(_approve(org["browser"], body["user_code"]))
    _, new = _secret(_approve(org["browser"], body["user_code"]))
    assert old != new
    assert _claim(key, body["id"], old).status_code == 401
    assert _claim(key, body["id"], new).status_code == 201


def test_an_expired_pairing_cannot_be_approved_or_claimed(org):
    key = _key()
    body = _open(key).json()
    _, secret = _secret(_approve(org["browser"], body["user_code"]))
    RunnerPairing.objects.filter(pk=body["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _claim(key, body["id"], secret).status_code == 401
    assert _approve(org["browser"], body["user_code"]).status_code == 409
    assert org["browser"].get(f"{PAIRINGS}{body['user_code']}").json()["status"] == "expired"


def test_approval_extends_the_pairing_for_a_slow_claim(org):
    body = _open(_key()).json()
    before = RunnerPairing.objects.get(pk=body["id"]).expires_at
    _approve(org["browser"], body["user_code"])
    assert RunnerPairing.objects.get(pk=body["id"]).expires_at - before == timedelta(minutes=10)


def test_a_declined_pairing_is_refused_to_the_box(org):
    key = _key()
    body = _open(key).json()
    res = org["browser"].post(f"{PAIRINGS}{body['user_code']}/decline")
    assert res.status_code == 200 and res.json() == {"status": "declined"}
    assert _claim(key, body["id"]).status_code == 401
    assert _approve(org["browser"], body["user_code"]).status_code == 409


def test_an_approver_who_lost_agent_work_pairs_nothing(org):
    key = _key()
    body = _open(key).json()
    _, secret = _secret(_approve(org["browser"], body["user_code"]))
    WorkspaceMembership.objects.filter(user=org["user"], workspace=org["ws"]).update(
        role=WorkspaceMembership.VIEWER)
    assert _claim(key, body["id"], secret).status_code == 401
    assert not Runner.objects.exists()


# ---- reattach ---------------------------------------------------------------------------

def test_a_key_that_is_already_an_active_runner_reattaches(org):
    key = _key()
    body = _open(key).json()
    _, secret = _secret(_approve(org["browser"], body["user_code"]))
    paired = _claim(key, body["id"], secret).json()
    again = _open(key)
    assert again.status_code == 200, again.content
    assert again.json() == {"status": "reattached", "runner_id": paired["runner_id"],
                            "name": "jj-jons-macbook-pro", "workspace": "connect",
                            "device_key_id": paired["device_key_id"]}
    assert RunnerPairing.objects.count() == 1


@pytest.mark.parametrize("state", ["revoked", "pending", "retired"])
def test_a_known_key_in_any_other_state_is_never_re_paired(org, state):
    key = _key()
    runner = Runner.objects.create(name="old", kind=Runner.EMDASH, owner=org["user"], workspace=org["ws"])
    jwk = device_keys.normalize_public_jwk(public_jwk(key))
    RunnerDeviceKey.objects.create(runner=runner, kind="software", public_jwk=jwk,
                                   jkt=device_keys.thumbprint(jwk),
                                   status="active" if state == "retired" else state)
    if state == "retired":
        Runner.objects.filter(pk=runner.pk).update(status=Runner.RETIRED)
    assert _open(key).status_code == 401
    assert not RunnerPairing.objects.exists()


# ---- who may approve --------------------------------------------------------------------

def test_approve_needs_a_fresh_passkey_step_up(org):
    body = _open(_key()).json()
    browser = Client()
    browser.force_login(org["user"])
    res = _approve(browser, body["user_code"])
    assert res.status_code == 403 and "step_up_required" in res.content.decode()
    assert RunnerPairing.objects.get(pk=body["id"]).approved_at is None


def test_a_token_cannot_approve(org):
    body = _open(_key()).json()
    raw, _ = PersonalToken.create_for_user(user=org["user"], label="t")
    res = _approve(Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), body["user_code"])
    assert res.status_code == 403
    assert RunnerPairing.objects.get(pk=body["id"]).approved_at is None


def test_an_agent_login_cannot_own_a_runner(org):
    from apps.agents.models import Agent

    ace = _person("ace")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=ace, role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="ace", name="ACE", workspace=org["ws"], user=ace)
    browser = Client()
    browser.force_login(ace)
    grant_step_up(browser)
    assert _approve(browser, _open(_key()).json()["user_code"]).status_code == 403


def test_approve_needs_agent_work_in_the_chosen_workspace(org):
    viewer = _person("vi")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=viewer, role=WorkspaceMembership.VIEWER)
    browser = Client()
    browser.force_login(viewer)
    grant_step_up(browser)
    code = _open(_key()).json()["user_code"]
    assert browser.get(f"{PAIRINGS}{code}").json()["workspaces"] == []
    assert _approve(browser, code).status_code == 403
    assert _approve(browser, code, workspace="dimagi").status_code == 404
    assert _approve(browser, code, workspace="nope").status_code == 404


@pytest.mark.parametrize("name", ["Jons Mac", "-bad", "x" * 64, ""])
def test_a_name_that_cannot_be_a_runner_name_is_refused(org, name):
    assert _approve(org["browser"], _open(_key()).json()["user_code"], name=name).status_code == 422


def test_a_name_you_already_use_is_refused(org):
    Runner.objects.create(name="jj-jons-macbook-pro", kind=Runner.EMDASH, owner=org["user"], workspace=org["ws"])
    res = _approve(org["browser"], _open(_key()).json()["user_code"])
    assert res.status_code == 409


def test_an_unknown_code_is_404_and_signed_out_is_refused(org, settings):
    assert org["browser"].get(f"{PAIRINGS}ABCD-EFGH").status_code == 404
    settings.REQUIRE_AUTH = True
    code = _open(_key()).json()["user_code"]  # the box's door stays open
    assert Client().get(f"{PAIRINGS}{code}").status_code == 401
    assert Client().post(f"{PAIRINGS}{code}/approve", data={"workspace": "connect", "name": "x"},
                         content_type="application/json").status_code == 401


@pytest.mark.parametrize("account,host,expected", [
    ("jj", "Jons-MacBook-Pro.local", "jj-jons-macbook-pro"),
    ("", "DESKTOP-ABC123", "desktop-abc123"),
    ("José", "1st_box", "jos-1st-box"),
    ("", "", "runner"),
    ("9", "99", "runner"),
])
def test_suggested_name_is_a_runner_name(account, host, expected):
    import re

    from apps.harness.schemas import RUNNER_NAME_PATTERN

    name = api_runner_pairings.suggested_name(account, host)
    assert name == expected and re.match(RUNNER_NAME_PATTERN, name)
