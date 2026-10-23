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
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import pat_rotation
from apps.agents.models import (
    Agent,
    AgentCredential,
    AgentCredentialAlert,
    AgentCredentialRead,
    AgentPatRotation,
    Vault,
    VaultAttachment,
    VaultSecret,
)
from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.aws_proof import ASSUMED, BODY, ENROLL, ENROLL_HTU, HTU, RESOLVE, ROLE, _active_role, _bind, _enroll, _proof

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh_cache")]


# ---- a fake STS -----------------------------------------------------------------------


# ---- the fleet ---------------------------------------------------------------------


def _resolve(fleet, proof=None, turn="turn-1", path=RESOLVE):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {fleet['pat']}"}
    if proof:
        headers["HTTP_X_CANOPY_AWS_PROOF"] = proof
    if turn:
        headers["HTTP_X_CANOPY_TURN_ID"] = turn
    return Client().get(path, **headers)


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
    assert set(body) == {"id", "jkt", "status", "approve_url", "ecdh_jkt"} and body["status"] == "pending"
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
    assert res.json()["values"]["CANOPY_WEB_PAT"] == "pat-value"
    assert AgentCredentialRead.objects.get(name="CANOPY_WEB_PAT").runner_id == fleet["runner"].pk
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
    res = _resolve(fleet, _proof(HTU + "?refs=CANOPY_WEB_PAT"), path=RESOLVE + "?refs=CANOPY_WEB_PAT")
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
    assert "/w/connect/agents/eva/settings/security#cred-device-keys" in mail.outbox[0].body
    assert "Review eva\u2019s approved machines" in mail.outbox[0].body
    assert _resolve(fleet).status_code == 401
    alert.refresh_from_db()
    assert alert.occurrences == 2 and len(mail.outbox) == 1  # debounced
    # An hour on, a new alert (and a new email).
    AgentCredentialAlert.objects.update(created_at=timezone.now() - dt.timedelta(hours=1, minutes=1))
    _resolve(fleet)
    assert len(_alerts("device-proof")) == 2 and len(mail.outbox) == 2


def _resolve_as_box(fleet, turn=""):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {fleet['pat']}",
               "HTTP_X_CANOPY_RUNNER": str(fleet["runner"].pk)}
    if turn:
        headers["HTTP_X_CANOPY_TURN_ID"] = turn
    return Client().get(RESOLVE, **headers)


def test_a_refused_device_proof_names_the_box_the_request_names(fleet, sts):
    _bind(fleet)
    assert _resolve_as_box(fleet, turn="turn-1").status_code == 401
    [alert] = _alerts("device-proof")
    assert alert.runner_id == fleet["runner"].pk
    assert f"on {fleet['runner'].name} refused" in alert.detail
    assert f"on {fleet['runner'].name} refused" in mail.outbox[0].body


def test_other_alerts_link_to_the_alert_list_on_the_security_tab(fleet):
    assert _resolve_as_box(fleet).status_code == 200  # a no-turn alert
    assert "/w/connect/agents/eva/settings/security#cred-alerts" in mail.outbox[0].body
    assert "See eva\u2019s vault alerts and credential read history" in mail.outbox[0].body


def test_a_box_read_without_a_turn_id_alerts(fleet):
    assert _resolve_as_box(fleet).status_code == 200
    [alert] = _alerts("no-turn")
    assert alert.refs == ["CANOPY_WEB_PAT"] and alert.runner_id == fleet["runner"].pk
    assert _alerts() == [alert]
    body = mail.outbox[0].body
    assert "stolen or misused runner token" in body and "revoke its token" in body


def test_a_proven_box_read_without_a_turn_id_alerts(fleet, sts):
    _active_role(fleet, sts)
    assert _resolve(fleet, proof=_proof(), turn="").status_code == 200
    assert len(_alerts("no-turn")) == 1


def test_a_boxs_own_credential_refresh_is_expected(fleet):
    """canopy-web#1563: cloud-ec2-2's runner-update timer refreshes every agent's
    credentials every 30 min, outside any turn, and says so."""
    res = Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}",
                       HTTP_X_CANOPY_RUNNER=str(fleet["runner"].pk),
                       HTTP_X_CANOPY_READ_PURPOSE="runner-refresh")
    assert res.status_code == 200
    assert _alerts("no-turn") == [] and mail.outbox == []
    assert AgentCredentialRead.objects.filter(agent=fleet["agent"]).exists()  # still logged


def test_a_verification_check_still_alerts_but_says_it_is_one(fleet):
    """`canopy agent prepare --check` labels its reads. The label is self-reported
    (a stolen token could send it), so the read still alerts — worded as the check."""
    res = Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}",
                       HTTP_X_CANOPY_RUNNER=str(fleet["runner"].pk),
                       HTTP_X_CANOPY_READ_PURPOSE="check: vault laptop check (by hal@box)")
    assert res.status_code == 200
    assert _alerts("no-turn") == []
    [alert] = _alerts("no-turn-check")
    assert "labelled as a verification check: vault laptop check (by hal@box)" in alert.detail
    body = mail.outbox[0].body
    assert "verification check" in body and "not proof" in body and "revoke its token" in body


def test_a_labelled_check_does_not_use_up_the_hours_real_alert(fleet):
    """Its own kind: a check first must not debounce an unlabelled read after it."""
    Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}",
                 HTTP_X_CANOPY_RUNNER=str(fleet["runner"].pk),
                 HTTP_X_CANOPY_READ_PURPOSE="check: anything")
    assert _resolve_as_box(fleet).status_code == 200
    assert len(_alerts("no-turn-check")) == 1 and len(_alerts("no-turn")) == 1
    assert len(mail.outbox) == 2


def test_a_refresh_purpose_without_a_named_box_is_not_an_exemption(fleet):
    """The purpose only explains a box's read; it never turns an unnamed read into one."""
    res = Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}",
                       HTTP_X_CANOPY_READ_PURPOSE="runner-refresh")
    assert res.status_code == 200
    assert AgentCredentialRead.objects.filter(agent=fleet["agent"]).latest("pk").via == "admin"


def test_an_owner_who_pairs_a_box_resolving_by_hand_is_expected(fleet):
    """canopy-web#1563: the owner pairs a runner the agent routes to, but this
    request names no box (no device proof, no X-Canopy-Runner) — it is their
    own session, so it is an admin read and raises no no-turn alert."""
    assert _resolve(fleet, turn="").status_code == 200
    assert _alerts("no-turn") == [] and mail.outbox == []
    assert AgentCredentialRead.objects.filter(agent=fleet["agent"]).latest("pk").via == "admin"


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
    res = _resolve(fleet, path=RESOLVE + "?refs=CANOPY_WEB_PAT,aws-root-key")
    assert res.status_code == 200 and "aws-root-key" not in res.json()["values"]
    [alert] = _alerts("undeclared-ref")
    assert alert.refs == ["aws-root-key"]


def test_an_unscoped_read_of_a_stored_orphan_alerts(fleet):
    AgentCredential.objects.create(agent=fleet["agent"], name="old-secret",
                                   value_enc=AgentCredential.seal(fleet["agent"], "old-secret", "x"))
    _resolve(fleet)
    assert _alerts("undeclared-ref")[0].refs == ["old-secret"]


def test_a_name_a_workspace_vault_supplies_is_not_undeclared(fleet):
    """The fleet's gog OAuth clients live in the workspace vault, not in each
    agent's runtime.yaml; asking for one is not an undeclared read."""
    v = Vault.objects.create(workspace=fleet["ws"], kind=Vault.WORKSPACE, name="Shared", kv=1)
    VaultAttachment.objects.create(vault=v, workspace=fleet["ws"])
    VaultSecret.objects.create(vault=v, name="gog-oauth-client-web", value_zk="zk1:x", kv=1)
    _resolve(fleet, path=RESOLVE + "?refs=CANOPY_WEB_PAT,gog-oauth-client-web,aws-root-key")
    [alert] = _alerts("undeclared-ref")
    assert alert.refs == ["aws-root-key"]


def test_naming_a_resolve_extra_is_not_undeclared(fleet):
    _resolve(fleet, path=RESOLVE + "?refs=CANOPY_WEB_PAT,github_token")
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
    _resolve_as_box(fleet)
    rows = fleet["client"].get("/api/agents/eva/credential-alerts").json()
    assert [r["kind"] for r in rows] == ["no-turn"] and rows[0]["notified"] is True
    viewer = get_user_model().objects.create_user(username="v", email="v@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=viewer,
                                       role=WorkspaceMembership.VIEWER)
    c = Client()
    c.force_login(viewer)
    assert c.get("/api/agents/eva/credential-alerts").status_code == 403


def test_an_alert_failure_never_denies_a_runner(fleet, monkeypatch):
    from apps.agents.credentials import vault_alerts

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
    fleet["client"].put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": raw}},
                        content_type="application/json")
    fleet["agent"].refresh_from_db()
    return {"login": login, "raw": raw, "token": token}


def _stored_pat(agent) -> str:
    return AgentCredential.objects.get(agent=agent, name="CANOPY_WEB_PAT").plaintext()


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
    fleet["client"].put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": fleet["pat"]}},
                        content_type="application/json")
    row = pat_rotation.rotate(fleet["agent"], trigger="manual", actor=fleet["user"])
    assert row.old_token_id is None and row.revoke_after is None
    assert PersonalToken.lookup(fleet["pat"]) is not None
    assert PersonalToken.lookup(fleet["pat"]).expires_at != timezone.now() + pat_rotation.OVERLAP


def test_a_refused_rotation_is_recorded_and_backs_off(fleet, agent_login):
    Agent.objects.filter(pk=fleet["agent"].pk).update(
        pat_rotation_enabled=True, pat_rotation_enabled_by=fleet["user"],
        pat_rotated_at=timezone.now() - dt.timedelta(days=31))
    AgentCredential.objects.filter(name="CANOPY_WEB_PAT").delete()
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
