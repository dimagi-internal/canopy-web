"""Canopy Vault Phase 1b (canopy-web#1549), server half: the EC2 AWS IAM
identity proof (enrollment + resolve), read-anomaly alerts, and automatic
canopy-pat rotation.

The `X-Canopy-Aws-Proof` contract pinned here is the one canopy's CLI/runner
half builds against. STS is never called for real: every test patches the one
HTTP call (`aws_iam_proof._call_sts`'s `requests.post`) and signs its proofs
with botocore and throwaway credentials.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import time

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents import pat_rotation
from apps.agents.models import (
    Agent,
    AgentCredential,
    AgentCredentialAlert,
    AgentCredentialRead,
    AgentPatRotation,
)
from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

RESOLVE = "/api/agents/eva/credentials/resolve"
HTU = f"http://testserver{RESOLVE}"
ENROLL = "/api/runner-device-keys/"
ENROLL_HTU = f"http://testserver{ENROLL}"
ROLE = "arn:aws:iam::123456789012:role/canopy-runner"
ASSUMED = "arn:aws:sts::123456789012:assumed-role/canopy-runner/i-0abc123"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return raw


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


# ---- a fake STS -----------------------------------------------------------------------

class _Res:
    def __init__(self, status, text, ctype="text/xml"):
        self.status_code, self.text, self.headers = status, text, {"Content-Type": ctype}


def _sts_xml(arn: str) -> str:
    return (
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<GetCallerIdentityResult><Arn>{arn}</Arn><UserId>AROA:i-0abc123</UserId>"
        "<Account>123456789012</Account></GetCallerIdentityResult>"
        "<ResponseMetadata><RequestId>x</RequestId></ResponseMetadata></GetCallerIdentityResponse>")


@pytest.fixture
def sts(monkeypatch):
    """Patch the one outbound call. `sts.arn` is who 'signed'; `sts.calls` what
    canopy-web sent (the URL above all)."""
    import requests

    class Fake:
        arn = ASSUMED
        status = 200
        calls: list = []

        def post(self, url, headers=None, data=None, **kw):
            self.calls.append({"url": url, "headers": dict(headers or {}), "data": data, **kw})
            if self.status != 200:
                return _Res(self.status, "<ErrorResponse/>")
            return _Res(200, _sts_xml(self.arn))

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def _proof(htu=HTU, *, iat=None, url="https://sts.amazonaws.com/", signed_extra=True,
           mutate=None) -> str:
    """A botocore-signed, unsent sts:GetCallerIdentity, as the runner builds it."""
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    if signed_extra:
        headers["X-Canopy-Proof-Htu"] = htu
        headers["X-Canopy-Proof-Iat"] = str(int(iat if iat is not None else time.time()))
    req = AWSRequest(method="POST", url=url, data=BODY, headers=headers)
    SigV4Auth(Credentials("AKIAFAKE", "fake-secret", "fake-session-token"), "sts",
              "us-east-1").add_auth(req)
    obj = {"method": "POST", "url": url, "headers": dict(req.headers.items()), "body": BODY}
    if not signed_extra:  # present, but outside the signature
        obj["headers"]["X-Canopy-Proof-Htu"] = htu
        obj["headers"]["X-Canopy-Proof-Iat"] = str(int(time.time()))
    if mutate:
        mutate(obj)
    return base64.b64encode(json.dumps(obj).encode()).decode()


# ---- the fleet ---------------------------------------------------------------------

@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj,
                                 runtime_secrets=["canopy-pat", "gog-token"],
                                 credential_source=Agent.CANOPY_WEB)
    runner = Runner.objects.create(name="ec2-box", kind=Runner.CLOUD, owner=jj,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    client.force_login(jj)
    grant_step_up(client)
    client.put("/api/agents/eva/credentials", data={"values": {"canopy-pat": "pat-value"}},
               content_type="application/json")
    mail.outbox.clear()
    return {"client": client, "agent": agent, "runner": runner, "user": jj, "ws": ws,
            "pat": _pat(jj)}


def _enroll(fleet, *, role_arn=ROLE, proof=None, runner=None):
    return Client().post(
        ENROLL,
        data={"runner": str((runner or fleet["runner"]).pk), "kind": "aws-iam",
              "role_arn": role_arn, "proof": proof if proof is not None else _proof(ENROLL_HTU),
              "label": "ec2"},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")


def _active_role(fleet, sts):
    body = _enroll(fleet).json()
    assert fleet["client"].post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 200
    return body


def _resolve(fleet, proof=None, turn="turn-1", path=RESOLVE):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {fleet['pat']}"}
    if proof:
        headers["HTTP_X_CANOPY_AWS_PROOF"] = proof
    if turn:
        headers["HTTP_X_CANOPY_TURN_ID"] = turn
    return Client().get(path, **headers)


def _bind(fleet):
    Agent.objects.filter(pk=fleet["agent"].pk).update(require_device_binding=True)


# ---- ARN normalization ------------------------------------------------------------------

@pytest.mark.parametrize("arn,want", [
    (ASSUMED, ROLE),
    (ROLE, ROLE),
    ("arn:aws:iam::123456789012:role/service-role/runners/canopy-runner", ROLE),
])
def test_role_arns_normalize_to_the_path_free_role(arn, want):
    assert aws_iam_proof.canonical_role_arn(arn) == want


@pytest.mark.parametrize("arn", [
    "arn:aws:iam::123456789012:user/jj",
    "arn:aws-cn:sts::123456789012:assumed-role/r/s",
    "arn:aws:sts::123456789012:federated-user/jj",
    "arn:aws:sts::12345:assumed-role/r/s",
    "",
])
def test_non_role_arns_are_refused(arn):
    with pytest.raises(aws_iam_proof.AwsProofError):
        aws_iam_proof.canonical_role_arn(arn)


# ---- enrollment -----------------------------------------------------------------------

def test_enroll_verifies_the_proof_and_lands_pending(fleet, sts):
    res = _enroll(fleet, role_arn="arn:aws:iam::123456789012:role/svc/canopy-runner")
    assert res.status_code == 201, res.content
    body = res.json()
    assert set(body) == {"id", "jkt", "status", "approve_url"} and body["status"] == "pending"
    key = RunnerDeviceKey.objects.get()
    assert key.kind == "aws-iam" and key.role_arn == ROLE and key.public_jwk == {}
    assert body["jkt"] == key.jkt == hashlib.sha256(ROLE.encode()).hexdigest()
    # STS was called at the pinned endpoint, with the signed request intact.
    assert [c["url"] for c in sts.calls] == ["https://sts.amazonaws.com/"]
    assert sts.calls[0]["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert sts.calls[0]["data"] == BODY.encode()
    # Listing shows the role.
    rows = Client().get(f"{ENROLL}?runner={fleet['runner'].pk}",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}").json()
    assert rows[0]["kind"] == "aws-iam" and rows[0]["role_arn"] == ROLE


def test_enroll_is_idempotent(fleet, sts):
    first = _enroll(fleet).json()
    # A fresh proof (a byte-identical one would be a replay).
    again = _enroll(fleet, proof=_proof(ENROLL_HTU, iat=time.time() - 1))
    assert again.status_code == 200 and again.json()["id"] == first["id"]


def test_enroll_refuses_a_proof_signed_by_another_role(fleet, sts):
    sts.arn = "arn:aws:sts::123456789012:assumed-role/someone-else/i-1"
    res = _enroll(fleet)
    assert res.status_code == 422 and "someone-else" in res.json()["detail"]
    assert not RunnerDeviceKey.objects.exists()


def test_enroll_refuses_a_proof_for_the_resolve_url(fleet, sts):
    assert _enroll(fleet, proof=_proof(HTU)).status_code == 422
    assert sts.calls == []


def test_enroll_needs_a_proof_and_a_role(fleet, sts):
    assert _enroll(fleet, proof="").status_code == 422
    assert _enroll(fleet, role_arn="arn:aws:iam::123456789012:user/jj").status_code == 422


def test_an_aws_iam_key_is_approved_with_step_up_like_any_other(fleet, sts):
    body = _enroll(fleet).json()
    grant_step_up(fleet["client"], seconds=-1)
    res = fleet["client"].post(f"/api/runner-device-keys/{body['id']}/approve")
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    grant_step_up(fleet["client"])
    assert fleet["client"].post(f"/api/runner-device-keys/{body['id']}/approve").json()["status"] == "active"


# ---- the proof on resolve -------------------------------------------------------------

def test_a_valid_aws_proof_resolves_and_names_the_runner(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    res = _resolve(fleet, _proof())
    assert res.status_code == 200, res.content
    assert res.json()["values"]["canopy-pat"] == "pat-value"
    assert AgentCredentialRead.objects.get(name="canopy-pat").runner_id == fleet["runner"].pk
    assert RunnerDeviceKey.objects.get().last_used_at is not None
    assert all(c["url"] == "https://sts.amazonaws.com/" for c in sts.calls)


def test_the_runner_halfs_exact_shape_is_accepted(fleet, sts):
    """canopy-web#1551's runner (runner/ec2/cloud_runner.py::aws_proof): an explicit
    `Host` header, single-string values, compact JSON, standard base64."""
    _bind(fleet)
    _active_role(fleet, sts)
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
               "Host": "sts.amazonaws.com", "X-Canopy-Proof-Htu": HTU,
               "X-Canopy-Proof-Iat": str(int(time.time()) - 2)}
    req = AWSRequest(method="POST", url="https://sts.amazonaws.com/", data=BODY, headers=headers)
    SigV4Auth(Credentials("AKIAFAKE", "fake-secret", "tok"), "sts", "us-east-1").add_auth(req)
    signed = dict(req.headers.items())
    assert "host;" in signed["Authorization"] and "x-canopy-proof-htu" in signed["Authorization"]
    raw = {"method": "POST", "url": "https://sts.amazonaws.com/", "headers": signed, "body": BODY}
    proof = base64.b64encode(json.dumps(raw, separators=(",", ":")).encode()).decode("ascii")
    assert _resolve(fleet, proof).status_code == 200


def test_the_htu_may_carry_the_refs_query(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    res = _resolve(fleet, _proof(HTU + "?refs=canopy-pat"), path=RESOLVE + "?refs=canopy-pat")
    assert res.status_code == 200


def test_a_client_chosen_sts_url_is_refused_and_never_called(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    n = len(sts.calls)
    assert _resolve(fleet, _proof(url="https://evil.example/")).status_code == 401
    assert _resolve(fleet, _proof(url="https://sts.us-west-2.amazonaws.com/")).status_code == 401
    assert len(sts.calls) == n


def test_a_replayed_aws_proof_is_refused(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    proof = _proof()
    assert _resolve(fleet, proof).status_code == 200
    n = len(sts.calls)
    assert _resolve(fleet, proof).status_code == 401
    assert len(sts.calls) == n  # consumed before any STS round trip


def test_a_stale_aws_proof_is_refused_and_120s_is_allowed(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    assert _resolve(fleet, _proof(iat=time.time() - 200)).status_code == 401
    assert _resolve(fleet, _proof(iat=time.time() - 100)).status_code == 200


def test_htu_and_iat_must_be_inside_the_signature(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    assert _resolve(fleet, _proof(signed_extra=False)).status_code == 401


@pytest.mark.parametrize("htu", [
    "http://testserver/api/agents/other/credentials/resolve",
    "http://evil.example/api/agents/eva/credentials/resolve",
])
def test_an_aws_proof_for_another_url_is_refused(fleet, sts, htu):
    _bind(fleet)
    _active_role(fleet, sts)
    assert _resolve(fleet, _proof(htu)).status_code == 401


def test_an_aws_proof_for_another_action_is_refused(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)

    def other_action(obj):
        obj["body"] = "Action=AssumeRole&Version=2011-06-15&RoleArn=x"

    assert _resolve(fleet, _proof(mutate=other_action)).status_code == 401


def test_sts_refusing_the_signature_is_a_401(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    sts.status = 403
    assert _resolve(fleet, _proof()).status_code == 401


def test_a_role_that_is_not_an_active_key_for_this_agent_is_refused(fleet, sts):
    _bind(fleet)
    _enroll(fleet)  # pending only
    assert _resolve(fleet, _proof()).status_code == 401
    sts.arn = "arn:aws:sts::123456789012:assumed-role/other-role/i-9"
    n = len(sts.calls)
    assert _resolve(fleet, _proof(iat=time.time() - 1)).status_code == 401
    assert len(sts.calls) == n + 1  # refused for the role, not as a replay


def test_an_aws_key_on_a_runner_the_agent_does_not_route_to_is_refused(fleet, sts):
    _bind(fleet)
    _active_role(fleet, sts)
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    RunnerDeviceKey.objects.update(runner=Runner.objects.create(
        name="theirs", kind=Runner.CLOUD, owner=other, status=Runner.ONLINE, capabilities={}))
    assert _resolve(fleet, _proof()).status_code == 401


# ---- read-anomaly alerts ------------------------------------------------------------------

def _alerts(kind=None):
    qs = AgentCredentialAlert.objects.all()
    return list(qs.filter(kind=kind) if kind else qs)


def test_a_refused_device_proof_alerts_the_owner_once_an_hour(fleet, sts):
    _bind(fleet)
    assert _resolve(fleet).status_code == 401
    [alert] = _alerts("device-proof")
    assert alert.occurrences == 1 and alert.notified["emailed"] == ["jj@dimagi.com"]
    assert len(mail.outbox) == 1 and "Eva" in mail.outbox[0].subject
    assert "/w/connect/agents/eva/settings#credentials" in mail.outbox[0].body
    assert _resolve(fleet).status_code == 401
    alert.refresh_from_db()
    assert alert.occurrences == 2 and len(mail.outbox) == 1  # debounced
    # An hour on, a new alert (and a new email).
    AgentCredentialAlert.objects.update(created_at=timezone.now() - dt.timedelta(hours=1, minutes=1))
    _resolve(fleet)
    assert len(_alerts("device-proof")) == 2 and len(mail.outbox) == 2


def test_a_box_read_without_a_turn_id_alerts(fleet):
    assert _resolve(fleet, turn="").status_code == 200
    [alert] = _alerts("no-turn")
    assert alert.refs == ["canopy-pat"]
    assert _alerts() == [alert]


def test_a_read_inside_a_turn_raises_nothing(fleet):
    assert _resolve(fleet, turn="t-1").status_code == 200
    assert _alerts() == [] and mail.outbox == []


def test_an_admin_resolving_by_hand_without_a_turn_is_expected(fleet):
    """`via == "admin"`: an owner/admin who does not pair a box this agent
    routes to — a person resolving for their own Claude Code session."""
    RunnerAssignment.objects.all().delete()
    assert _resolve(fleet, turn="").status_code == 200
    assert _alerts("no-turn") == []


def test_asking_for_an_undeclared_ref_alerts(fleet):
    res = _resolve(fleet, path=RESOLVE + "?refs=canopy-pat,aws-root-key")
    assert res.status_code == 200 and "aws-root-key" not in res.json()["values"]
    [alert] = _alerts("undeclared-ref")
    assert alert.refs == ["aws-root-key"]


def test_an_unscoped_read_of_a_stored_orphan_alerts(fleet):
    AgentCredential.objects.create(agent=fleet["agent"], name="old-secret",
                                   value_enc=AgentCredential.seal(fleet["agent"], "old-secret", "x"))
    _resolve(fleet)
    assert _alerts("undeclared-ref")[0].refs == ["old-secret"]


def test_naming_a_resolve_extra_is_not_undeclared(fleet):
    _resolve(fleet, path=RESOLVE + "?refs=canopy-pat,github_token")
    assert _alerts("undeclared-ref") == []


def test_a_burst_of_reads_alerts_once(fleet, settings):
    settings.CANOPY_VAULT_READ_BURST_LIMIT = 3
    for _ in range(6):
        assert _resolve(fleet).status_code == 200
    [alert] = _alerts("burst")
    assert alert.occurrences == 3  # reads 4, 5, 6


def test_no_alerts_for_an_agent_on_1password(fleet):
    Agent.objects.filter(pk=fleet["agent"].pk).update(credential_source=Agent.ONEPASSWORD)
    _resolve(fleet, turn="", path=RESOLVE + "?refs=nope")
    assert _alerts() == []


def test_alerts_are_listed_for_admins_only(fleet):
    _resolve(fleet, turn="")
    rows = fleet["client"].get("/api/agents/eva/credential-alerts").json()
    assert [r["kind"] for r in rows] == ["no-turn"] and rows[0]["notified"] is True
    viewer = get_user_model().objects.create_user(username="v", email="v@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=viewer,
                                       role=WorkspaceMembership.VIEWER)
    c = Client()
    c.force_login(viewer)
    assert c.get("/api/agents/eva/credential-alerts").status_code == 403


def test_an_alert_failure_never_denies_a_runner(fleet, monkeypatch):
    from apps.agents import vault_alerts

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(vault_alerts, "raise_alert", boom)
    assert _resolve(fleet, turn="").status_code == 200


# ---- canopy-pat rotation --------------------------------------------------------------

@pytest.fixture
def agent_login(fleet):
    """Eva's own canopy login, holding a live PAT that is her stored canopy-pat."""
    login = get_user_model().objects.create_user(username="eva@dimagi-ai.com",
                                                 email="eva@dimagi-ai.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=login,
                                       role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=fleet["agent"].pk).update(user=login)
    raw, token = PersonalToken.create_for_user(user=login, label="eva agent", ttl_days=0)
    fleet["client"].put("/api/agents/eva/credentials", data={"values": {"canopy-pat": raw}},
                        content_type="application/json")
    fleet["agent"].refresh_from_db()
    return {"login": login, "raw": raw, "token": token}


def _stored_pat(agent) -> str:
    return AgentCredential.objects.get(agent=agent, name="canopy-pat").plaintext()


def test_turning_rotation_on_is_an_audited_admin_setting(fleet, agent_login):
    res = fleet["client"].patch("/api/agents/eva/pat-rotation",
                                data={"enabled": True, "period_days": 14},
                                content_type="application/json")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["enabled"] is True and body["period_days"] == 14
    assert body["enabled_by_email"] == "jj@dimagi.com" and body["next_due_at"]
    from apps.events.models import SettingChange

    assert SettingChange.objects.filter(operation="set_pat_rotation").exists()
    assert fleet["client"].get("/api/agents/eva/pat-rotation").json()["enabled"] is True


def test_rotation_needs_the_agents_own_login_and_a_stored_pat(fleet):
    res = fleet["client"].patch("/api/agents/eva/pat-rotation", data={"enabled": True},
                                content_type="application/json")
    assert res.status_code == 422 and "canopy login" in res.json()["detail"]


def test_a_login_that_signed_in_needs_a_workspace_owner(fleet, agent_login):
    get_user_model().objects.filter(pk=agent_login["login"].pk).update(last_login=timezone.now())
    admin = get_user_model().objects.create_user(username="a", email="a@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=admin,
                                       role=WorkspaceMembership.EDITOR)
    from apps.agents.models import AgentAdmin

    AgentAdmin.objects.create(agent=fleet["agent"], user=admin)
    c = Client()
    c.force_login(admin)
    res = c.patch("/api/agents/eva/pat-rotation", data={"enabled": True},
                  content_type="application/json")
    assert res.status_code == 403
    # The workspace owner may.
    assert fleet["client"].patch("/api/agents/eva/pat-rotation", data={"enabled": True},
                                 content_type="application/json").status_code == 200


def test_a_due_rotation_mints_writes_overlaps_then_revokes(fleet, agent_login):
    Agent.objects.filter(pk=fleet["agent"].pk).update(
        pat_rotation_enabled=True, pat_rotation_days=30, pat_rotation_enabled_by=fleet["user"],
        pat_rotated_at=timezone.now() - dt.timedelta(days=31))
    out = pat_rotation.tick()
    assert out == {"rotated": 1, "failed": 0, "revoked": 0}

    agent = Agent.objects.get(pk=fleet["agent"].pk)
    new_raw = _stored_pat(agent)
    assert new_raw != agent_login["raw"]
    new = PersonalToken.lookup(new_raw)
    assert new is not None and new.user_id == agent_login["login"].pk and new.expires_at is None
    # The old one still works, but only for the overlap.
    old = PersonalToken.objects.get(pk=agent_login["token"].pk)
    assert PersonalToken.lookup(agent_login["raw"]) is not None
    assert old.expires_at is not None
    assert dt.timedelta(hours=23) < old.expires_at - timezone.now() <= dt.timedelta(hours=24)
    row = AgentPatRotation.objects.get()
    assert row.status == "rotated" and row.old_token_id == old.pk and row.new_token_id == new.pk
    assert agent.pat_rotated_at is not None
    # Not due again yet.
    assert pat_rotation.tick()["rotated"] == 0
    # 24 h on: revoked.
    later = timezone.now() + dt.timedelta(hours=25)
    assert pat_rotation.tick(now=later)["revoked"] == 1
    assert PersonalToken.objects.get(pk=old.pk).revoked_at is not None
    assert AgentPatRotation.objects.get().old_revoked_at is not None
    # The new token authenticates.
    assert Client().get("/api/agents/eva/credentials/access",
                        HTTP_AUTHORIZATION=f"Bearer {new_raw}").status_code == 200


def test_rotation_never_revokes_a_token_that_is_not_the_agents(fleet, agent_login):
    """The stored canopy-pat is a PERSON's token: replace it, leave theirs alone."""
    fleet["client"].put("/api/agents/eva/credentials", data={"values": {"canopy-pat": fleet["pat"]}},
                        content_type="application/json")
    row = pat_rotation.rotate(fleet["agent"], trigger="manual", actor=fleet["user"])
    assert row.old_token_id is None and row.revoke_after is None
    assert PersonalToken.lookup(fleet["pat"]) is not None
    assert PersonalToken.lookup(fleet["pat"]).expires_at != timezone.now() + pat_rotation.OVERLAP


def test_a_refused_rotation_is_recorded_and_backs_off(fleet, agent_login):
    Agent.objects.filter(pk=fleet["agent"].pk).update(
        pat_rotation_enabled=True, pat_rotation_enabled_by=fleet["user"],
        pat_rotated_at=timezone.now() - dt.timedelta(days=31))
    AgentCredential.objects.filter(name="canopy-pat").delete()
    assert pat_rotation.tick()["failed"] == 1
    assert AgentPatRotation.objects.get().status == "failed"
    assert pat_rotation.tick()["failed"] == 0  # not retried every tick


def test_disabled_agents_are_never_rotated(fleet, agent_login):
    Agent.objects.filter(pk=fleet["agent"].pk).update(
        pat_rotated_at=timezone.now() - dt.timedelta(days=400))
    assert pat_rotation.tick()["rotated"] == 0


def test_rotate_now_needs_a_step_up_from_a_browser(fleet, agent_login):
    grant_step_up(fleet["client"], seconds=-1)
    res = fleet["client"].post("/api/agents/eva/pat-rotation/rotate")
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    grant_step_up(fleet["client"])
    res = fleet["client"].post("/api/agents/eva/pat-rotation/rotate")
    assert res.status_code == 200, res.content
    assert res.json()["history"][0]["status"] == "rotated"
    assert _stored_pat(fleet["agent"]) != agent_login["raw"]


def test_the_management_command_runs_a_tick(fleet, agent_login):
    from io import StringIO

    from django.core.management import call_command

    Agent.objects.filter(pk=fleet["agent"].pk).update(
        pat_rotation_enabled=True, pat_rotation_enabled_by=fleet["user"],
        pat_rotated_at=timezone.now() - dt.timedelta(days=31))
    out = StringIO()
    call_command("rotate_canopy_pats", "--dry-run", stdout=out)
    assert "due: eva" in out.getvalue()
    call_command("rotate_canopy_pats", stdout=out)
    assert "rotated 1" in out.getvalue()
