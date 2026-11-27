"""Sealed AWS roles: canopy-web carries a role's own AWS key, and the sessions made with it, only
as ciphertext it cannot open (`apps/agents/aws_sealed.py`).

Pinned, with real P-256 keys (the browser's and the machine's halves played here):
* a role's approver key is registered by a superuser in a browser after a step-up, sealed only to
  superusers with a vault key — never to a machine, never as plaintext;
* a standing agent's key is sealed only to the machines that run it, and only for a role it stands for;
* a sealed request needs the role's approver key; only an approver who HOLDS it may approve; approving
  takes the per-request passkey ceremony;
* the approver's browser gets the key sealed to them plus the agent's machines and their
  endorsements, and hands back a session sealed only to those machines, never outlasting the grant;
* the agent's machine gets ciphertext it opens with its own key; canopy-web's custodial route never
  vends a sealed grant; with no sealed source the agent is sent to the custodial route.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from django.utils import timezone

from apps.agents import api_aws, aws_sealed
from apps.agents.credentials import zk, zk_seal
from apps.agents.models import Agent, AgentAwsRoleRequest, AwsSealedKey
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.passkeys.models import UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace
from tests.fixtures.passkey_confirm import approve_approval
from tests.fixtures.vault_zk import ref
from tests.token_helpers import pat_client

pytestmark = pytest.mark.django_db

J = "application/json"
RW_ARN = "arn:aws:iam::858923557655:role/labs-jj-agent-readwrite"
READ_ARN = "arn:aws:iam::858923557655:role/labs-jj-observe-readonly"
KEY = json.dumps({"AccessKeyId": "AKIAEXAMPLEEXAMPLE12", "SecretAccessKey": "not-a-real-secret"})


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, ref.jwk(priv.public_key())


def _derive(priv):
    def derive(epk: dict) -> bytes:
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(ref.unb64u(epk["x"]), "big"),
                                            int.from_bytes(ref.unb64u(epk["y"]), "big"),
                                            ec.SECP256R1()).public_key()
        return priv.exchange(ec.ECDH(), pub)
    return derive


@pytest.fixture()
def roles(settings):
    settings.CANOPY_AWS_ROLES = {"labs-read": READ_ARN, "labs-readwrite": RW_ARN}
    settings.CANOPY_AWS_ROLE_AGENTS = {"labs-read": ["hal"], "labs-readwrite": []}


@pytest.fixture()
def w(roles, monkeypatch):
    class W:
        pass

    w = W()
    ws = a_workspace("dimagi")
    w.owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    w.owner.is_superuser = True
    w.owner.save()
    w.other_owner = a_member(ws, email="bo@dimagi.com", role=M.OWNER)   # may approve, holds no key
    w.editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    w.ace_login = a_member(ws, email="ace@dimagi-ai.com", role=M.VIEWER)
    w.hal_login = a_member(ws, email="hal@dimagi-ai.com", role=M.VIEWER)
    w.ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=w.editor, user=w.ace_login)
    w.hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=w.owner, user=w.hal_login)
    # jj's vault key (the approver's half) and one machine that runs each agent (the machine's half).
    w.jj_priv, jj_jwk = _keypair()
    UserVaultKeyset.objects.create(user=w.owner, seals=[], public_jwk=jj_jwk)
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD,
                                   owner=w.owner, workspace=ws)
    w.devices = {}
    for agent in (w.ace, w.hal):
        priv, jwk = _keypair()
        row = RunnerDeviceKey.objects.create(
            runner=runner, kind=RunnerDeviceKey.SOFTWARE, status=RunnerDeviceKey.ACTIVE,
            jkt=f"sig-{agent.slug}", public_jwk={}, ecdh_public_jwk=jwk, ecdh_jkt=device_keys.thumbprint(jwk),
            ecdh_approved_at=timezone.now())
        w.devices[agent.pk] = (row, priv)
    # Which machines run which agent is routing's answer; here, one each.
    monkeypatch.setattr(zk, "eligible_device_keys",
                        lambda agent, facts=None: [w.devices[agent.pk][0]] if agent.pk in w.devices else [])
    return w


def _browser(user, *, step_up=True) -> Client:
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


def _seal_to_jj(w, role="labs-readwrite"):
    return zk_seal.seal_to(KEY, agent_id=aws_sealed.key_binding(role), name=aws_sealed.KEY_NAME, kv=0,
                           recipients=[("member", str(w.owner.pk), UserVaultKeyset.objects.get(user=w.owner).public_jwk)])


def _seal_to_device(w, agent, plaintext, *, binding, name):
    row, _ = w.devices[agent.pk]
    return zk_seal.seal_to(plaintext, agent_id=binding, name=name, kv=0,
                           recipients=[("device", row.ecdh_jkt, row.ecdh_public_jwk)])


def _register(c, role, value, agent="", akid="AKIAEXAMPLEEXAMPLE12"):
    return c.put(f"/api/aws/sealed-keys/{role}", json.dumps({"access_key_id": akid, "value_zk": value, "agent": agent}),
                 content_type=J)


# --- registering a role's key ---------------------------------------------------------------


def test_a_superuser_registers_the_approvers_key_sealed_to_people_only(w):
    r = _register(_browser(w.owner), "labs-readwrite", _seal_to_jj(w))
    assert r.status_code == 200, r.content
    assert r.json()["holders"] == ["jj@dimagi.com"] and r.json()["agent"] == ""
    row = AwsSealedKey.objects.get(role="labs-readwrite", agent=None)
    assert row.value_zk.startswith("zks1:") and "not-a-real-secret" not in row.value_zk
    # jj's own key opens it, and nothing on the server could: it has no private key to try.
    opened = zk_seal.open_for(row.value_zk, agent_id="aws-role:labs-readwrite", name=aws_sealed.KEY_NAME, kv=0,
                              kind="member", target=str(w.owner.pk), derive=_derive(w.jj_priv))
    assert json.loads(opened)["SecretAccessKey"] == "not-a-real-secret"
    listed = {x["role"]: x for x in _browser(w.owner).get("/api/aws/sealed-keys").json()}
    assert listed["labs-readwrite"]["keys"][0]["access_key_id"] == "AKIAEXAMPLEEXAMPLE12"
    assert "value_zk" not in json.dumps(listed)


def test_registering_is_a_superusers_in_a_browser_after_a_step_up_and_never_plaintext(w):
    value = _seal_to_jj(w)
    assert _register(_browser(w.other_owner), "labs-readwrite", value).status_code == 403   # not a superuser
    assert _register(_browser(w.owner, step_up=False), "labs-readwrite", value).status_code == 403
    assert _register(_browser(w.owner), "labs-readwrite", KEY).status_code == 422          # plaintext
    assert _register(_browser(w.owner), "no-such-role", value).status_code == 404
    token = pat_client(w.owner)
    assert _register(token, "labs-readwrite", value).status_code == 403                    # a token: no browser
    assert not AwsSealedKey.objects.exists()


def test_the_approvers_key_is_never_sealed_to_a_machine_or_a_non_superuser(w):
    to_machine = _seal_to_device(w, w.ace, KEY, binding="aws-role:labs-readwrite", name=aws_sealed.KEY_NAME)
    assert _register(_browser(w.owner), "labs-readwrite", to_machine).status_code == 422
    _, bo_jwk = _keypair()
    UserVaultKeyset.objects.create(user=w.other_owner, seals=[], public_jwk=bo_jwk)
    to_bo = zk_seal.seal_to(KEY, agent_id="aws-role:labs-readwrite", name=aws_sealed.KEY_NAME, kv=0,
                            recipients=[("member", str(w.other_owner.pk), bo_jwk)])
    assert _register(_browser(w.owner), "labs-readwrite", to_bo).status_code == 422


def test_a_standing_key_is_sealed_only_to_its_agents_machines_and_only_for_a_role_it_stands_for(w):
    binding = aws_sealed.key_binding("labs-read", w.hal)
    mine = _seal_to_device(w, w.hal, KEY, binding=binding, name=aws_sealed.KEY_NAME)
    theirs = _seal_to_device(w, w.ace, KEY, binding=binding, name=aws_sealed.KEY_NAME)
    assert _register(_browser(w.owner), "labs-read", theirs, agent="hal").status_code == 422
    assert _register(_browser(w.owner), "labs-readwrite", mine, agent="hal").status_code == 422  # not standing
    r = _register(_browser(w.owner), "labs-read", mine, agent="hal")
    assert r.status_code == 200 and r.json()["machines"] == 1


# --- a standing agent's machine ----------------------------------------------------------------


def _sealed_creds(client, slug, role):
    return client.post(f"/api/agents/{slug}/aws-credentials/sealed", json.dumps({"role": role}), content_type=J)


def test_a_standing_agents_machine_gets_its_key_sealed_to_it_and_what_to_ask_sts(w):
    binding = aws_sealed.key_binding("labs-read", w.hal)
    _register(_browser(w.owner), "labs-read", _seal_to_device(w, w.hal, KEY, binding=binding, name=aws_sealed.KEY_NAME),
              agent="hal")
    body = _sealed_creds(pat_client(w.hal_login), "hal", "labs-read").json()
    assert body["kind"] == "key" and body["binding"] == binding
    assert body["sts"]["role_arn"] == READ_ARN and body["sts"]["source_identity"] == "canopy-agent-hal"
    row, priv = w.devices[w.hal.pk]
    opened = zk_seal.open_for(body["value_zk"], agent_id=binding, name=body["name"], kv=0, kind="device",
                              target=row.ecdh_jkt, derive=_derive(priv))
    assert json.loads(opened)["AccessKeyId"] == "AKIAEXAMPLEEXAMPLE12"
    # No sealed source for another role: the agent is sent to the custodial route.
    none = _sealed_creds(pat_client(w.hal_login), "hal", "labs-readwrite")
    assert none.status_code == 404 and none.json()["error"] == "no_sealed_source"
    # A browser never gets it.
    assert _sealed_creds(_browser(w.owner), "hal", "labs-read").status_code == 403


# --- a sealed grant -----------------------------------------------------------------------------


def _ask(w, delivery="sealed", minutes=60):
    return pat_client(w.ace_login).post(
        "/api/agents/ace/aws-role-requests",
        json.dumps({"role": "labs-readwrite", "reason": "resize the labs RDS", "minutes": minutes,
                    "delivery": delivery}), content_type=J)


def test_a_sealed_request_needs_the_roles_key_and_only_a_holder_may_approve_it(w):
    assert _ask(w).status_code == 422                       # no approver key registered yet
    _register(_browser(w.owner), "labs-readwrite", _seal_to_jj(w))
    r = _ask(w)
    assert r.status_code == 201, r.content
    card = r.json()
    assert card["delivery"] == "sealed"
    approval = AgentAwsRoleRequest.objects.get(pk=card["id"]).approval
    assert approve_approval(w.other_owner, approval.pk).status_code == 403     # an owner, but holds no key
    page = _browser(w.owner).get(f"/api/approvals/{approval.pk}").json()
    assert page["client_step"] == {"name": "aws.sealed_session", "agent": "ace", "request": card["id"]}
    assert any("sealed key" in line for line in page["plan"])


def _approved_sealed_grant(w, minutes=60):
    _register(_browser(w.owner), "labs-readwrite", _seal_to_jj(w))
    card = _ask(w, minutes=minutes).json()
    row = AgentAwsRoleRequest.objects.get(pk=card["id"])
    r = approve_approval(w.owner, row.approval_id)
    assert r.status_code == 200 and r.json()["status"] == "done", r.content
    row.refresh_from_db()
    return row


def test_the_approvers_browser_gets_the_key_sealed_to_them_and_the_agents_machines(w):
    _register(_browser(w.owner), "labs-readwrite", _seal_to_jj(w))
    gid = _ask(w).json()["id"]
    sealing = _browser(w.owner).get(f"/api/agents/ace/aws-role-requests/{gid}/sealing")
    assert sealing.status_code == 200, sealing.content
    body = sealing.json()
    assert body["key_binding"] == "aws-role:labs-readwrite" and body["session_binding"] == f"aws-grant:{gid}"
    assert body["sts"]["role_arn"] == RW_ARN and body["sts"]["source_identity"] == "canopy-agent-ace"
    assert [m["target"] for m in body["machines"]["recipients"]] == [w.devices[w.ace.pk][0].ecdh_jkt]
    assert body["machines"]["agent_id"] == str(w.ace.pk)
    # Someone who holds no key, or a token, gets nothing.
    assert _browser(w.other_owner).get(f"/api/agents/ace/aws-role-requests/{gid}/sealing").status_code == 404
    assert pat_client(w.owner).get(f"/api/agents/ace/aws-role-requests/{gid}/sealing").status_code == 403


def _session(expires: dt.datetime) -> str:
    return json.dumps({"AccessKeyId": "ASIASESSION", "SecretAccessKey": "s", "SessionToken": "t",
                       "Expiration": expires.isoformat()})


def _hand_over(c, w, row, value, expires):
    return c.put(f"/api/agents/ace/aws-role-requests/{row.pk}/sealed-session",
                 json.dumps({"value_zk": value, "expires_at": expires.isoformat()}), content_type=J)


def test_the_approver_hands_over_a_session_sealed_to_the_machines_and_the_machine_opens_it(w, monkeypatch):
    custodial = []
    monkeypatch.setattr(api_aws, "sts_client", lambda: custodial.append(1))
    row = _approved_sealed_grant(w)
    ends = timezone.now() + dt.timedelta(minutes=55)
    binding = aws_sealed.session_binding(row)
    # Before the session arrives, the machine is told to wait.
    pending = _sealed_creds(pat_client(w.ace_login), "ace", "labs-readwrite")
    assert pending.status_code == 409 and pending.json()["error"] == "session_pending"
    # Sealed to a machine that does not run the agent: refused.
    wrong = _seal_to_device(w, w.hal, _session(ends), binding=binding, name=aws_sealed.SESSION_NAME)
    assert _hand_over(_browser(w.owner), w, row, wrong, ends).status_code == 422
    # Longer than the grant: refused.
    late = row.expires_at + dt.timedelta(hours=2)
    good = _seal_to_device(w, w.ace, _session(ends), binding=binding, name=aws_sealed.SESSION_NAME)
    assert _hand_over(_browser(w.owner), w, row, good, late).status_code == 422
    # Only the approver hands it over.
    assert _hand_over(_browser(w.other_owner), w, row, good, ends).status_code == 404
    r = _hand_over(_browser(w.owner), w, row, good, ends)
    assert r.status_code == 200 and r.json()["sealed_session_expires_at"], r.content
    body = _sealed_creds(pat_client(w.ace_login), "ace", "labs-readwrite").json()
    assert body["kind"] == "session" and body["grant"] == row.pk
    dev, priv = w.devices[w.ace.pk]
    opened = zk_seal.open_for(body["value_zk"], agent_id=binding, name=aws_sealed.SESSION_NAME, kv=0,
                              kind="device", target=dev.ecdh_jkt, derive=_derive(priv))
    assert json.loads(opened)["AccessKeyId"] == "ASIASESSION"
    # canopy-web's own role never vends a sealed grant.
    refused = pat_client(w.ace_login).post("/api/agents/ace/aws-credentials", json.dumps({"role": "labs-readwrite"}),
                                           content_type=J)
    assert refused.status_code == 403 and refused.json()["error"] == "approval_required"
    assert custodial == []


def test_a_revoked_sealed_grant_hands_out_nothing(w):
    row = _approved_sealed_grant(w)
    ends = timezone.now() + dt.timedelta(minutes=55)
    value = _seal_to_device(w, w.ace, _session(ends), binding=aws_sealed.session_binding(row),
                            name=aws_sealed.SESSION_NAME)
    assert _hand_over(_browser(w.owner), w, row, value, ends).status_code == 200
    assert _browser(w.owner).delete(f"/api/agents/ace/aws-role-requests/{row.pk}").status_code == 200
    assert _sealed_creds(pat_client(w.ace_login), "ace", "labs-readwrite").status_code == 404


def test_a_custodial_request_is_unchanged(w, monkeypatch):
    r = _ask(w, delivery="custodial")
    assert r.status_code == 201 and r.json()["delivery"] == "custodial"
    page = _browser(w.owner).get(f"/api/approvals/{AgentAwsRoleRequest.objects.get(pk=r.json()['id']).approval_id}")
    assert page.json()["client_step"] is None
